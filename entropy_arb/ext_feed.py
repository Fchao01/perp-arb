"""External reference price (Binance USD-M futures or OKX) for the engine.

BTC and ETH usually move on Binance first and on Lighter a few tens of ms
later. A premium that appears on Lighter right after Binance has moved is
mostly one Lighter venue not having caught up yet; with a 300 ms taker delay
those fills come back worse than planned. Data from Nov 10-11 (BTC 2,233 and
ETH 3,398 trades): with Binance flat (< 0.5 bps) over the 200 ms before the
decision, BTC captured -0.04 bps and ETH +0.17 bps; after a Binance move of
0.5 bps or more, -0.35 and -0.25..-0.66 bps.

This feed keeps the last few seconds of the external mid so the engine can
skip signals that follow an external move (execution: ext_filter).
"""
from __future__ import annotations

import asyncio
import bisect
import json
import logging
import time
from typing import List, Optional

try:
    from websockets.asyncio.client import connect as ws_connect
except ImportError:
    from websockets import connect as ws_connect  # type: ignore

log = logging.getLogger("ext_feed")

URLS = {
    "binance": "wss://fstream.binance.com/ws/{sym}@bookTicker",
    "okx": "wss://ws.okx.com:8443/ws/v5/public",
}
KEEP_SEC = 5.0


def venue_symbol(venue: str, sym: str) -> str:
    if venue == "binance":
        return sym.lower() + "usdt"
    if venue == "okx":
        return f"{sym.upper()}-USDT-SWAP"
    raise ValueError(venue)


def parse_mid(venue: str, msg: dict) -> Optional[float]:
    if venue == "binance":
        d = msg.get("data", msg)
        if d.get("e") != "bookTicker":
            return None
        b, a = float(d["b"]), float(d["a"])
    elif venue == "okx":
        data = msg.get("data")
        if not data or (msg.get("arg") or {}).get("channel") != "bbo-tbt":
            return None
        q = data[0]
        if not q.get("bids") or not q.get("asks"):
            return None
        b, a = float(q["bids"][0][0]), float(q["asks"][0][0])
    else:
        raise ValueError(venue)
    if b <= 0 or a <= 0:
        return None
    return (b + a) / 2


class ExtFeed:
    def __init__(self, venue: str, symbol: str, stale_sec: float = 2.0,
                 connect=ws_connect) -> None:
        self.venue, self.symbol = venue, symbol.upper()
        self.vsym = venue_symbol(venue, symbol)
        self.stale_sec = stale_sec
        self._connect = connect
        self.ts: List[float] = []
        self.mids: List[float] = []

    def add(self, ts: float, mid: float) -> None:
        self.ts.append(ts)
        self.mids.append(mid)
        if self.ts[0] < ts - KEEP_SEC * 2:            # trim in batches
            i = bisect.bisect_left(self.ts, ts - KEEP_SEC)
            del self.ts[:i], self.mids[:i]

    def move_bps(self, window_sec: float, now: Optional[float] = None) -> Optional[float]:
        """|mid(now) / mid(now - window) - 1| in bps, or None when the feed
        is stale or has no history that far back."""
        now = time.time() if now is None else now
        if not self.ts or now - self.ts[-1] > self.stale_sec:
            return None
        i = bisect.bisect_right(self.ts, now - window_sec) - 1
        if i < 0:
            return None
        return abs(self.mids[-1] / self.mids[i] - 1) * 1e4

    async def run(self, stop: asyncio.Event) -> None:
        url = URLS[self.venue].format(sym=self.vsym)
        sub = None
        if self.venue == "okx":
            sub = json.dumps({"op": "subscribe", "args": [
                {"channel": "bbo-tbt", "instId": self.vsym}]})
        backoff = 1.0
        while not stop.is_set():
            try:
                async with self._connect(url, open_timeout=10, ping_interval=20,
                                         ping_timeout=20) as ws:
                    if sub:
                        await ws.send(sub)
                    log.info("external feed %s %s connected", self.venue, self.vsym)
                    backoff = 1.0
                    async for raw in ws:
                        if raw == "pong":
                            continue
                        mid = parse_mid(self.venue, json.loads(raw))
                        if mid is not None:
                            self.add(time.time(), mid)
                        if stop.is_set():
                            break
            except asyncio.CancelledError:
                raise
            except Exception as e:
                log.warning("external feed %s error: %s — reconnect in %.0fs "
                            "(filter off while down)", self.venue, e, backoff)
            if stop.is_set():
                break
            try:
                await asyncio.wait_for(stop.wait(), timeout=backoff)
            except asyncio.TimeoutError:
                pass
            backoff = min(backoff * 2, 30.0)
