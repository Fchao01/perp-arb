#!/usr/bin/env python3
"""Record an external exchange's best bid/ask next to the bot, for lead-lag
analysis. Read-only: it never trades and needs no keys.

BTC usually moves on Binance first and on Lighter a few tens of ms later.
With a 300 ms taker delay that lag decides whether a premium seen on
Lighter is real or just one venue not having caught up yet. This tool
writes the external quotes to CSV so trades.csv can be checked against
them offline before any trading rule is built on it.

To keep files small it writes a row only when the mid moves by at least
--min-move-bps from the last written row, or after --max-gap-ms without a
row. One file per venue, symbol and UTC day.

    nohup python3 tools/ext_recorder.py --symbols BTC,ETH,SOL,HYPE \\
        > logs/ext_recorder.out 2>&1 < /dev/null &

Columns: recv_ts (local epoch seconds), exch_ms (exchange timestamp, ms),
bid, ask.

中文：记录外部交易所（默认币安合约）的最优买卖价，用来分析领先-滞后关系。
只读，不下单、不需要密钥。中间价变动超过 --min-move-bps 或间隔超过
--max-gap-ms 才写一行；按交易所、币种、UTC 日期分文件。
"""
from __future__ import annotations

import argparse
import asyncio
import json
import logging
import os
import time
from datetime import datetime, timezone
from typing import Dict, Optional, Tuple

log = logging.getLogger("ext_recorder")

VENUES = {
    "binance": "wss://fstream.binance.com/stream?streams=",
    "okx": "wss://ws.okx.com:8443/ws/v5/public",
}


def venue_symbol(venue: str, sym: str) -> str:
    sym = sym.upper()
    if venue == "binance":
        return sym.lower() + "usdt"
    if venue == "okx":
        return f"{sym}-USDT-SWAP"
    raise ValueError(venue)


def parse(venue: str, msg: dict) -> Optional[Tuple[str, int, float, float]]:
    """-> (venue symbol, exchange ms, bid, ask) or None for non-quote frames."""
    if venue == "binance":
        d = msg.get("data", msg)
        if d.get("e") != "bookTicker":
            return None
        return (d["s"].lower(), int(d.get("T") or d.get("E") or 0),
                float(d["b"]), float(d["a"]))
    if venue == "okx":
        arg = msg.get("arg") or {}
        data = msg.get("data")
        if not data or arg.get("channel") != "bbo-tbt":
            return None
        q = data[0]
        if not q.get("bids") or not q.get("asks"):
            return None
        return (arg["instId"], int(q.get("ts") or 0),
                float(q["bids"][0][0]), float(q["asks"][0][0]))
    raise ValueError(venue)


class Sampler:
    """Decides which quotes to write: a mid move of at least min_move_bps
    since the last written row, or max_gap_ms without a row."""

    def __init__(self, min_move_bps: float, max_gap_ms: float) -> None:
        self.min_move = min_move_bps / 1e4
        self.max_gap = max_gap_ms / 1000.0
        self.last: Dict[str, Tuple[float, float]] = {}   # sym -> (mid, ts)

    def take(self, sym: str, ts: float, bid: float, ask: float) -> bool:
        mid = (bid + ask) / 2
        prev = self.last.get(sym)
        if (prev is None or abs(mid / prev[0] - 1) >= self.min_move
                or ts - prev[1] >= self.max_gap):
            self.last[sym] = (mid, ts)
            return True
        return False


class Writer:
    """One CSV per venue, symbol and UTC day; flushed about once a second."""

    def __init__(self, out_dir: str, venue: str) -> None:
        self.out_dir, self.venue = out_dir, venue
        self.files: Dict[str, Tuple[str, object]] = {}
        self.last_flush = 0.0
        os.makedirs(out_dir, exist_ok=True)

    def _fh(self, sym: str, ts: float):
        day = datetime.fromtimestamp(ts, timezone.utc).strftime("%Y%m%d")
        cur = self.files.get(sym)
        if cur and cur[0] == day:
            return cur[1]
        if cur:
            cur[1].close()
        path = os.path.join(self.out_dir, f"{self.venue}_{sym}_{day}.csv")
        new = not os.path.exists(path)
        fh = open(path, "a", buffering=1 << 16)
        if new:
            fh.write("recv_ts,exch_ms,bid,ask\n")
        self.files[sym] = (day, fh)
        return fh

    def write(self, sym: str, ts: float, exch_ms: int, bid: float, ask: float):
        self._fh(sym, ts).write(f"{ts:.6f},{exch_ms},{bid!r},{ask!r}\n")
        if ts - self.last_flush >= 1.0:
            self.flush()
            self.last_flush = ts

    def flush(self) -> None:
        for _, fh in self.files.values():
            fh.flush()

    def close(self) -> None:
        for _, fh in self.files.values():
            fh.close()
        self.files.clear()


async def run(venue: str, symbols, writer: Writer, sampler: Sampler,
              stop: asyncio.Event, connect=None) -> None:
    if connect is None:
        try:
            from websockets.asyncio.client import connect
        except ImportError:
            from websockets import connect  # type: ignore
    names = {venue_symbol(venue, s): s.upper() for s in symbols}
    if venue == "binance":
        url = VENUES[venue] + "/".join(f"{n}@bookTicker" for n in names)
        sub = None
    else:
        url = VENUES[venue]
        sub = json.dumps({"op": "subscribe", "args": [
            {"channel": "bbo-tbt", "instId": n} for n in names]})
    backoff, rows, t_report = 1.0, 0, time.time()
    while not stop.is_set():
        try:
            async with connect(url, open_timeout=10, ping_interval=20,
                               ping_timeout=20, max_size=2 ** 22) as ws:
                if sub:
                    await ws.send(sub)
                log.info("connected to %s for %s", venue, ",".join(names.values()))
                backoff = 1.0
                async for raw in ws:
                    now = time.time()
                    if raw == "pong":
                        continue
                    q = parse(venue, json.loads(raw))
                    if q is None:
                        continue
                    vsym, exch_ms, bid, ask = q
                    sym = names.get(vsym, vsym.upper())
                    if bid > 0 and ask > 0 and sampler.take(sym, now, bid, ask):
                        writer.write(sym, now, exch_ms, bid, ask)
                        rows += 1
                    if now - t_report >= 600:
                        log.info("%d rows written in the last %.0f min",
                                 rows, (now - t_report) / 60)
                        rows, t_report = 0, now
                    if stop.is_set():
                        break
        except asyncio.CancelledError:
            raise
        except Exception as e:
            log.warning("%s stream error: %s — reconnect in %.0fs", venue, e, backoff)
        writer.flush()
        if stop.is_set():
            break
        try:
            await asyncio.wait_for(stop.wait(), timeout=backoff)
        except asyncio.TimeoutError:
            pass
        backoff = min(backoff * 2, 60.0)


def main() -> None:
    p = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    p.add_argument("--venue", choices=sorted(VENUES), default="binance")
    p.add_argument("--symbols", default="BTC",
                   help="comma separated, e.g. BTC,ETH,SOL,HYPE")
    p.add_argument("--out", default="logs/ext", help="output directory")
    p.add_argument("--min-move-bps", type=float, default=0.1)
    p.add_argument("--max-gap-ms", type=float, default=1000.0)
    a = p.parse_args()
    logging.basicConfig(level=logging.INFO,
                        format="%(asctime)s %(levelname)-7s %(name)s: %(message)s")
    symbols = [s.strip() for s in a.symbols.split(",") if s.strip()]
    writer = Writer(a.out, a.venue)
    stop = asyncio.Event()
    try:
        asyncio.run(run(a.venue, symbols, writer,
                        Sampler(a.min_move_bps, a.max_gap_ms), stop))
    except KeyboardInterrupt:
        pass
    finally:
        writer.close()


if __name__ == "__main__":
    main()
