#!/usr/bin/env python3
"""Shadow-simulate RH maker quotes hedged on the other venue (read-only).

The idea under test: instead of taking stale RH quotes (and losing the race
to makers who cancel faster), BE the RH maker. Quote both sides on the
primary (RH) around the hedge venue's mid plus the premium center, and when
an RH taker fills a quote, hedge it on the hedge venue with a taker order.

Nothing is sent. The tool watches both books and RH's public trade stream
and simulates the quotes with the exchange's latencies:

  * a new or modified quote goes live place_latency_ms after the decision,
    and the old one stays live until cancel_latency_ms after it — the window
    in which a faster taker can still pick it off;
  * an RH taker trade fills a live quote when it printed through the quote's
    price (a taker that paid p would have filled a better-priced quote
    first). Trades exactly at the quote price are marked "touch": they need
    queue priority and are reported separately;
  * the hedge is priced on the hedge venue's book hedge_delay_ms after the
    fill, walking depth for the full size;
  * every fill gets markouts: its edge against the hedge venue's fair price
    1 s, 5 s and 30 s later. Negative markouts mean the quotes are being
    picked off.

Several half-spreads run side by side in one process. Results go to
<dir>/maker_shadow_fills.csv and maker_shadow_stats.csv, and `report`
summarizes them. Run it on the server the bot runs on.

只读影子模拟：在 RH 按"对冲所中间价 + 溢价中心 ± 半价差"模拟挂双边单，
按交易所的挂单/撤单延迟维护报价，用 RH 公开成交判断是否会被吃到，再按
对冲所盘口模拟吃单对冲，并记录成交后 1s/5s/30s 的价格走势。不下任何单。

Usage:
    python3 tools/maker_shadow.py collect --symbol SNDK --config sndk.yaml
    python3 tools/maker_shadow.py collect --symbol SNDK --config sndk.yaml \\
        --half-spread-bps 2,3,4,6 --center-bps auto --size-usd 300
    python3 tools/maker_shadow.py report --dir logs/sndk
"""
from __future__ import annotations

import argparse
import asyncio
import csv
import json
import logging
import math
import os
import signal
import sys
import time
from collections import Counter, defaultdict, deque
from datetime import datetime, timezone
from typing import Callable, Dict, List, Optional

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))

from entropy_arb.book import walk_depth  # noqa: E402
from entropy_arb.center import CenterTracker  # noqa: E402,F401

log = logging.getLogger("maker_shadow")

MARKOUTS_S = (1, 5, 30)
FILL_HEADER = (["sim", "time_utc", "ts", "side", "qty", "px", "notional_usd",
                "fair_mid", "center_bps", "quote_age_ms", "touch",
                "taker_account", "hedge_px", "hedge_delay_ms", "prem_bps",
                "edge_bps", "edge_usd", "rh_position_after"]
               + [f"markout_{s}s_bps" for s in MARKOUTS_S])
STATS_HEADER = ["time_utc", "ts", "sim", "elapsed_s", "quoting_s", "actions",
                "skipped_budget", "fills", "unhedged", "edge_usd", "mtm_usd",
                "rh_position", "hedge_position"]


def _utc(ts: float) -> str:
    return datetime.fromtimestamp(ts, timezone.utc).strftime(
        "%Y-%m-%dT%H:%M:%S.%f")[:-3] + "Z"


def _f(v, d=4):
    return "" if v is None else f"{v:.{d}f}"


class Quote:
    __slots__ = ("side", "px", "qty", "live_from", "live_until", "placed_at")

    def __init__(self, side, px, qty, live_from, placed_at):
        self.side, self.px, self.qty = side, px, qty
        self.live_from, self.live_until = live_from, None
        self.placed_at = placed_at

    def live(self, t: float) -> bool:
        return (self.live_from <= t
                and (self.live_until is None or t < self.live_until)
                and self.qty > 1e-12)


class MakerSim:
    """One parameter set. side +1 = RH bid (we buy), -1 = RH ask (we sell)."""

    def __init__(self, name: str, half_spread_bps: float, *, size_usd: float,
                 requote_bps: float, place_latency: float,
                 cancel_latency: float, hedge_delay: float,
                 actions_per_min: int, max_position_usd: float,
                 tick: float, size_step: float, min_base: float,
                 maker_fee_bps: float, hedge_fee_bps: float,
                 emit_fill: Callable[[dict], None],
                 schedule: Callable[[float, Callable[[], None]], None]) -> None:
        self.name = name
        self.half = half_spread_bps
        self.size_usd = size_usd
        self.requote = requote_bps
        self.place_latency = place_latency
        self.cancel_latency = cancel_latency
        self.hedge_delay = hedge_delay
        self.actions_per_min = actions_per_min
        self.max_position_usd = max_position_usd
        self.tick = tick
        self.size_step = size_step
        self.min_base = min_base
        self.maker_fee = maker_fee_bps / 1e4
        self.hedge_fee = hedge_fee_bps / 1e4
        self.emit_fill = emit_fill
        self.schedule = schedule
        self.quotes: Dict[int, deque] = {1: deque(), -1: deque()}
        self.target: Dict[int, Optional[float]] = {1: None, -1: None}
        self.actions: deque = deque()
        self.n_actions = 0
        self.skipped_budget = 0
        self.position = 0.0          # RH base position
        self.hedge_position = 0.0
        self.cash = 0.0
        self.fills = 0
        self.unhedged = 0
        self.edge_usd = 0.0
        self.quoting_s = 0.0
        self._last_tick: Optional[float] = None

    # ------------------------------------------------------------- quoting

    def _budget_ok(self, now: float) -> bool:
        while self.actions and now - self.actions[0] > 60.0:
            self.actions.popleft()
        return len(self.actions) < self.actions_per_min

    def _round(self, px: float, side: int) -> float:
        n = px / self.tick
        # bids round down, asks round up: never quote tighter than intended
        n = math.floor(n + 1e-9) if side > 0 else math.ceil(n - 1e-9)
        return round(n * self.tick, 10)

    def _current(self, side: int) -> Optional[Quote]:
        q = self.quotes[side]
        return q[-1] if q and q[-1].live_until is None else None

    def _cancel(self, side: int, now: float) -> None:
        cur = self._current(side)
        if cur is not None:
            cur.live_until = max(now + self.cancel_latency, cur.live_from)
        self.target[side] = None

    def cancel_all(self, now: float) -> None:
        for side in (1, -1):
            if self._current(side) is not None:
                if self._budget_ok(now):
                    self.actions.append(now)
                    self.n_actions += 1
                self._cancel(side, now)

    def on_market(self, now: float, fair: Optional[float],
                  center_bps: Optional[float], rh_bid: Optional[float],
                  rh_ask: Optional[float]) -> None:
        """Re-evaluate both quotes on any book update."""
        if self._last_tick is not None and any(
                self._current(s) for s in (1, -1)):
            self.quoting_s += now - self._last_tick
        self._last_tick = now
        if fair is None or center_bps is None or rh_bid is None or rh_ask is None:
            self.cancel_all(now)
            return
        ref = fair * (1 + center_bps / 1e4)
        for side in (1, -1):
            adds = side * self.position >= 0
            if adds and abs(self.position) * ref >= self.max_position_usd:
                if self._current(side) is not None:
                    self.cancel_all_side(side, now)
                continue
            px = ref * (1 - side * self.half / 1e4)
            # post-only: never cross RH's own book
            if side > 0:
                px = min(px, rh_ask - self.tick)
            else:
                px = max(px, rh_bid + self.tick)
            px = self._round(px, side)
            tgt = self.target[side]
            cur = self._current(side)
            if cur is not None and tgt is not None and \
                    abs(px / tgt - 1) * 1e4 < self.requote:
                continue
            if not self._budget_ok(now):
                self.skipped_budget += 1
                continue
            qty = int(self.size_usd / px / self.size_step) * self.size_step
            if qty < self.min_base:
                continue
            self.actions.append(now)
            self.n_actions += 1           # modify = one tx
            if cur is not None:
                cur.live_until = max(now + self.cancel_latency, cur.live_from)
            self.quotes[side].append(
                Quote(side, px, qty, now + self.place_latency, now))
            self.target[side] = px
            self._prune(side, now)

    def cancel_all_side(self, side: int, now: float) -> None:
        if self._budget_ok(now):
            self.actions.append(now)
            self.n_actions += 1
        self._cancel(side, now)

    def _prune(self, side: int, now: float) -> None:
        q = self.quotes[side]
        while len(q) > 1 and q[0].live_until is not None \
                and q[0].live_until < now - 5.0:
            q.popleft()

    # --------------------------------------------------------------- fills

    def on_trade(self, t_eval: float, wall: float, taker_buys: bool,
                 px: float, size: float, taker_account, fair: Optional[float],
                 center_bps: Optional[float], hedge_book) -> None:
        """An RH taker trade. taker_buys hits our ask (side -1)."""
        side = -1 if taker_buys else 1
        remaining = size
        for q in self.quotes[side]:
            if remaining <= 1e-12:
                break
            if not q.live(t_eval):
                continue
            through = px > q.px if side < 0 else px < q.px
            touch = abs(px - q.px) < self.tick / 2
            if not (through or touch):
                continue
            take = min(remaining, q.qty)
            q.qty -= take
            remaining -= take
            if q.qty <= 1e-12 and q is self._current(side):
                self.target[side] = None   # filled: requote on next update
            self._fill(t_eval, wall, side, take, q, touch and not through,
                       taker_account, fair, center_bps, hedge_book)

    def mtm(self, rh_mid: Optional[float],
            hedge_mid: Optional[float]) -> Optional[float]:
        """Cash plus both positions at current mids (0 at start)."""
        if rh_mid is None or hedge_mid is None:
            return None
        return self.cash + self.position * rh_mid + self.hedge_position * hedge_mid

    def _fill(self, now, wall, side, qty, q, touch, taker_account, fair,
              center_bps, hedge_book) -> None:
        self.position += side * qty
        self.cash -= side * qty * q.px * (1 + side * self.maker_fee)
        self.fills += 1
        rec = {"sim": self.name, "time_utc": _utc(wall), "ts": f"{wall:.3f}",
               "side": "buy" if side > 0 else "sell", "qty": f"{qty:.6g}",
               "px": q.px, "notional_usd": f"{qty * q.px:.2f}",
               "fair_mid": _f(fair), "center_bps": _f(center_bps, 3),
               "quote_age_ms": f"{(now - q.placed_at) * 1000:.0f}",
               "touch": int(touch), "taker_account": taker_account,
               "rh_position_after": f"{self.position:.6g}"}
        pending = {"markouts": {}, "hedged": False}

        def hedge() -> None:
            hb = hedge_book()
            levels = None
            if hb is not None:
                levels = hb.sorted_asks() if side < 0 else hb.sorted_bids()
            if levels:
                _, ntl = walk_depth(levels, qty)
                hpx = ntl / qty
                # sold on RH -> buy the hedge; bought on RH -> sell it
                if side < 0:
                    rh_eff = q.px * (1 - self.maker_fee)
                    h_eff = hpx * (1 + self.hedge_fee)
                    self.cash -= qty * h_eff
                else:
                    rh_eff = q.px * (1 + self.maker_fee)
                    h_eff = hpx * (1 - self.hedge_fee)
                    self.cash += qty * h_eff
                self.hedge_position -= side * qty
                # The pair opens a basis position at prem; RH normally sits
                # `center` bps off the hedge, so the edge is the distance
                # from the center (realized when the basis round-trips).
                prem = (rh_eff / h_eff - 1) * 1e4
                c = center_bps or 0.0
                edge = (prem - c) if side < 0 else (c - prem)
                edge_usd = edge / 1e4 * qty * q.px
                self.edge_usd += edge_usd
                rec.update(hedge_px=f"{hpx:.6f}",
                           hedge_delay_ms=f"{self.hedge_delay * 1000:.0f}",
                           prem_bps=f"{prem:.3f}", edge_bps=f"{edge:.3f}",
                           edge_usd=f"{edge_usd:.6f}")
            else:
                self.unhedged += 1
            pending["hedged"] = True

        def markout(s: int) -> None:
            hb = hedge_book()
            m = hb.mid() if hb is not None else None
            if m is not None and center_bps is not None:
                ref = m * (1 + center_bps / 1e4)
                e = (q.px / ref - 1) * 1e4 if side < 0 else (ref / q.px - 1) * 1e4
                rec[f"markout_{s}s_bps"] = f"{e:.3f}"
            pending["markouts"][s] = True
            if len(pending["markouts"]) == len(MARKOUTS_S):
                self.emit_fill(rec)

        self.schedule(self.hedge_delay, hedge)
        for s in MARKOUTS_S:
            self.schedule(float(s), lambda s=s: markout(s))


# ------------------------------------------------------------------ feeds

class RHTradeFeed:
    """Public trade stream for one zkLighter market."""

    def __init__(self, name, ws_url, market_id, on_trade) -> None:
        self.name, self.ws_url, self.market_id = name, ws_url, market_id
        self.on_trade = on_trade
        self.seen: deque = deque(maxlen=4096)
        self._seen_set = set()

    def _mark(self, tid) -> bool:
        """Remember a trade id; False if it was already seen."""
        if tid in self._seen_set:
            return False
        if len(self.seen) == self.seen.maxlen:
            self._seen_set.discard(self.seen[0])
        self.seen.append(tid)
        self._seen_set.add(tid)
        return True

    def _handle(self, msg: dict) -> None:
        for t in msg.get("trades") or []:
            if not self._mark(t.get("trade_id")):
                continue
            if t.get("type") != "trade":
                continue
            taker_buys = bool(t.get("is_maker_ask"))
            taker = t.get("bid_account_id") if taker_buys else t.get("ask_account_id")
            self.on_trade(taker_buys, float(t["price"]), float(t["size"]),
                          taker, float(t.get("timestamp") or 0) / 1000.0)

    async def run(self, stop: asyncio.Event) -> None:
        try:
            from websockets.asyncio.client import connect as ws_connect
        except ImportError:
            from websockets import connect as ws_connect  # type: ignore
        backoff = 1.0
        while not stop.is_set():
            try:
                async with ws_connect(self.ws_url, max_size=2**23,
                                      open_timeout=10, ping_interval=15,
                                      ping_timeout=15) as ws:
                    log.info("[%s] trade stream connected", self.name)
                    async for raw in ws:
                        backoff = 1.0
                        msg = json.loads(raw)
                        t = msg.get("type")
                        if t == "connected":
                            await ws.send(json.dumps({
                                "type": "subscribe",
                                "channel": f"trade/{self.market_id}"}))
                        elif t == "ping":
                            await ws.send(json.dumps({"type": "pong"}))
                        elif t == "subscribed/trade":
                            # history snapshot: mark as seen, never fill on it
                            for tr in msg.get("trades") or []:
                                self._mark(tr.get("trade_id"))
                        elif t == "update/trade":
                            self._handle(msg)
                        if stop.is_set():
                            break
            except asyncio.CancelledError:
                raise
            except Exception as e:
                log.warning("[%s] trade ws error: %s — reconnect in %.0fs",
                            self.name, e, backoff)
            if stop.is_set():
                break
            await asyncio.sleep(backoff)
            backoff = min(backoff * 2, 30.0)


class CsvOut:
    def __init__(self, path: str, header: List[str]) -> None:
        d = os.path.dirname(path)
        if d:
            os.makedirs(d, exist_ok=True)
        new = not os.path.exists(path) or os.path.getsize(path) == 0
        self.fh = open(path, "a", newline="")
        self.w = csv.DictWriter(self.fh, fieldnames=header, extrasaction="ignore")
        if new:
            self.w.writeheader()
        self.fh.flush()

    def __call__(self, row: dict) -> None:
        self.w.writerow(row)
        self.fh.flush()

    def close(self) -> None:
        self.fh.close()


# ---------------------------------------------------------------- collect

async def collect(args) -> None:
    import aiohttp
    from entropy_arb.config import load_config
    from entropy_arb.venue_hl import HLVenue
    from entropy_arb.venue_lighter import LighterVenue

    cfg = load_config(args.config, args.env_file, symbol=args.symbol,
                      hedge_venue=args.hedge, primary_venue=args.primary)
    if cfg.entropy.kind != "lighter":
        raise RuntimeError("the maker side must be a Lighter venue (RH or "
                           "Lighter); got " + cfg.entropy.label)
    out_dir = args.dir or os.path.dirname(cfg.recorder_csv)
    fills_out = CsvOut(os.path.join(out_dir, "maker_shadow_fills.csv"),
                       FILL_HEADER)
    stats_out = CsvOut(os.path.join(out_dir, "maker_shadow_stats.csv"),
                       STATS_HEADER)
    session = aiohttp.ClientSession()
    stop = asyncio.Event()
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        loop.add_signal_handler(sig, stop.set)

    def make(vc):
        if vc.kind == "lighter":
            return LighterVenue(vc, session, cfg.settle_timeout_sec)
        return HLVenue(vc, cfg.hl_api_url, cfg.hl_ws_url, session,
                       cfg.settle_timeout_sec)

    rh, hv = make(cfg.entropy), make(cfg.hedge)
    tasks = []
    try:
        await asyncio.gather(rh.load_market(), hv.load_market())
        tick = 10 ** -rh.price_decimals
        step = 10 ** -rh.size_decimals
        center = CenterTracker(None if args.center_bps == "auto"
                               else float(args.center_bps),
                               args.center_tau_sec, args.center_warmup_sec)

        def schedule(delay, fn):
            loop.call_later(delay, fn)

        sims = [MakerSim(
            f"hs{h:g}", h, size_usd=args.size_usd,
            requote_bps=args.requote_bps,
            place_latency=args.place_latency_ms / 1000.0,
            cancel_latency=args.cancel_latency_ms / 1000.0,
            hedge_delay=args.hedge_delay_ms / 1000.0,
            actions_per_min=args.actions_per_min,
            max_position_usd=args.max_position_usd or cfg.entropy.cap_usd,
            tick=tick, size_step=step, min_base=rh.min_base,
            maker_fee_bps=args.maker_fee_bps, hedge_fee_bps=hv.fee_bps,
            emit_fill=fills_out, schedule=schedule)
            for h in args.half_spread_bps]

        def fair_and_center(now):
            if not (rh.book.is_fresh(cfg.staleness_sec)
                    and hv.book.is_fresh(cfg.staleness_sec)):
                return None, None
            pm, hm = rh.book.mid(), hv.book.mid()
            if not (pm and hm):
                return None, None
            center.update(now, (pm / hm - 1) * 1e4)
            return hm, (center.value if center.ready(now) else None)

        def on_book() -> None:
            now = time.monotonic()
            fair, c = fair_and_center(now)
            for s in sims:
                s.on_market(now, fair, c, rh.book.best_bid(), rh.book.best_ask())

        def on_trade(taker_buys, px, size, taker, _exch_ts) -> None:
            now = time.monotonic()
            fair = hv.book.mid()
            for s in sims:
                s.on_trade(now - args.feed_lag_ms / 1000.0, time.time(),
                           taker_buys, px, size, taker, fair, center.value,
                           lambda: hv.book if hv.book.is_fresh(
                               cfg.staleness_sec) else None)

        tasks += rh.start_tasks(stop, on_book, False)
        tasks += hv.start_tasks(stop, on_book, False)
        tasks.append(asyncio.create_task(RHTradeFeed(
            rh.name, rh.profile.ws_url, rh.market_id, on_trade).run(stop)))
        log.info("shadow quoting %s on %s, hedging on %s | half-spreads %s bps"
                 " | center %s | size $%g | requote %.2f bps | latency place "
                 "%gms cancel %gms hedge %gms | budget %d/min -> %s",
                 cfg.symbol, rh.name, hv.name, args.half_spread_bps,
                 args.center_bps, args.size_usd, args.requote_bps,
                 args.place_latency_ms, args.cancel_latency_ms,
                 args.hedge_delay_ms, args.actions_per_min, out_dir)
        t0 = time.monotonic()
        deadline = time.time() + args.minutes * 60 if args.minutes > 0 else None
        while not stop.is_set():
            try:
                await asyncio.wait_for(stop.wait(), timeout=args.status_sec)
            except asyncio.TimeoutError:
                pass
            now_w = time.time()
            for s in sims:
                stats_out({"time_utc": _utc(now_w), "ts": f"{now_w:.3f}",
                           "sim": s.name,
                           "elapsed_s": f"{time.monotonic() - t0:.1f}",
                           "quoting_s": f"{s.quoting_s:.1f}",
                           "actions": s.n_actions,
                           "skipped_budget": s.skipped_budget,
                           "fills": s.fills, "unhedged": s.unhedged,
                           "edge_usd": f"{s.edge_usd:.4f}",
                           "mtm_usd": _f(s.mtm(rh.book.mid(), hv.book.mid())),
                           "rh_position": f"{s.position:.6g}",
                           "hedge_position": f"{s.hedge_position:.6g}"})
            log.info("center %s bps | " + " | ".join(
                f"{s.name}: fills {s.fills} edge ${s.edge_usd:+.2f} "
                f"mtm ${(s.mtm(rh.book.mid(), hv.book.mid()) or 0):+.2f} "
                f"pos {s.position:+.4g}"
                for s in sims),
                f"{center.value:+.2f}" if center.value is not None else "—")
            if deadline and now_w >= deadline:
                stop.set()
        # let scheduled hedges / markouts finish
        await asyncio.sleep(max(MARKOUTS_S) + 1 if any(s.fills for s in sims)
                            else 0)
    finally:
        stop.set()
        for t in tasks:
            t.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        for v in (rh, hv):
            await v.close()
        await session.close()
        fills_out.close()
        stats_out.close()


# ----------------------------------------------------------------- report

def _num(r, k):
    v = r.get(k)
    return float(v) if v not in (None, "") else None


def _pct(v, q):
    if not v:
        return float("nan")
    v = sorted(v)
    return v[min(len(v) - 1, int(round(q * (len(v) - 1))))]


def report(args) -> None:
    fpath = os.path.join(args.dir, "maker_shadow_fills.csv")
    spath = os.path.join(args.dir, "maker_shadow_stats.csv")
    fills = list(csv.DictReader(open(fpath))) if os.path.exists(fpath) else []
    stats = list(csv.DictReader(open(spath))) if os.path.exists(spath) else []
    if args.no_touch:
        fills = [f for f in fills if f.get("touch") != "1"]
    # observed hours per sim: sum of elapsed at each session's last stats row
    hours = defaultdict(float)
    mtm = defaultdict(list)       # sim -> MTM at the end of each session
    last = {}

    def end_session(key):
        el, row = last[key]
        hours[key] += el / 3600.0
        if row.get("mtm_usd"):
            mtm[key].append(float(row["mtm_usd"]))

    for r in stats:
        key = r["sim"]
        el = float(r["elapsed_s"])
        if key in last and el < last[key][0]:
            end_session(key)
        last[key] = (el, r)
    for key in last:
        end_session(key)
    sims = sorted({f["sim"] for f in fills} | set(last),
                  key=lambda s: float(s[2:]) if s[2:].replace(".", "").isdigit() else 0)
    print(f"fills file: {fpath}" + ("  (touch fills excluded)" if args.no_touch else ""))
    for name in sims:
        fs = [f for f in fills if f["sim"] == name]
        h = hours.get(name, 0.0)
        hedged = [f for f in fs if f.get("edge_usd")]
        ntl = sum(float(f["notional_usd"]) for f in hedged)
        pnl = sum(float(f["edge_usd"]) for f in hedged)
        st = last.get(name, (0, {}))[1]
        print(f"\n== {name} ({name[2:]} bps half-spread): {h:.2f} h observed, "
              f"{len(fs)} fills ({len(fs) / h if h else float('nan'):.1f}/h), "
              f"touch {sum(f.get('touch') == '1' for f in fs)}")
        if st:
            print(f"   quote actions {st['actions']} (requote attempts blocked "
                  f"by the budget {st['skipped_budget']}), time quoting "
                  f"{float(st['quoting_s']) / max(float(st['elapsed_s']), 1):.0%}"
                  f", unhedged fills {st.get('unhedged') or 0} (last session)")
        if mtm.get(name):
            print(f"   mark-to-market at session ends ${sum(mtm[name]):+.2f} "
                  f"(cash + both open positions at mids)")
        if not hedged:
            continue
        bps = [float(f["edge_bps"]) for f in hedged]
        print(f"   hedged volume ${ntl:,.0f}  edge vs center ${pnl:+.2f}  "
              f"= {pnl / ntl * 1e4:+.2f} bps of volume  "
              f"(per fill p10 {_pct(bps, .1):+.2f} p50 {_pct(bps, .5):+.2f} "
              f"p90 {_pct(bps, .9):+.2f})"
              + (f"  ≈ ${pnl / h:+.2f}/h" if h else ""))
        for side in ("buy", "sell"):
            sf = [f for f in hedged if f["side"] == side]
            if sf:
                print(f"   {side:4s} n={len(sf):4d}  edge mean "
                      f"{sum(float(f['edge_bps']) for f in sf) / len(sf):+.2f} bps")
        for s in MARKOUTS_S:
            m = [_num(f, f"markout_{s}s_bps") for f in fs]
            m = [x for x in m if x is not None]
            if m:
                print(f"   markout {s:2d}s  mean {sum(m) / len(m):+.2f} bps  "
                      f"negative {sum(x < 0 for x in m) / len(m):.0%}")
        ages = [float(f["quote_age_ms"]) for f in fs]
        print(f"   quote age at fill ms  p10 {_pct(ages, .1):.0f}  "
              f"p50 {_pct(ages, .5):.0f}  (young fills = picked off right "
              f"after a requote)")
        byh = defaultdict(lambda: [0, 0.0, 0.0])
        for f in hedged:
            hr = f["time_utc"][11:13]
            byh[hr][0] += 1
            byh[hr][1] += float(f["edge_usd"])
            byh[hr][2] += float(f["notional_usd"])
        print("   by UTC hour: " + "  ".join(
            f"{hr}h n{v[0]} {v[1] / v[2] * 1e4:+.1f}bps"
            for hr, v in sorted(byh.items())))
        takers = Counter(f["taker_account"] for f in fs)
        print("   top takers: " + ", ".join(
            f"#{a} {n}" for a, n in takers.most_common(5)))


# ------------------------------------------------------------------- main

def _floats(s: str) -> List[float]:
    return [float(x) for x in s.split(",") if x.strip()]


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    sub = ap.add_subparsers(dest="cmd", required=True)
    c = sub.add_parser("collect", help="shadow-quote and record fills")
    c.add_argument("--symbol", required=True)
    c.add_argument("--primary", default=None,
                   help="maker venue (default PRIMARY_VENUE / light-rh)")
    c.add_argument("--hedge", default="lighter", help="hedge venue")
    c.add_argument("--config", default="config.yaml")
    c.add_argument("--env-file", default=".env",
                   help="only PRIMARY_VENUE is read; no credentials are used")
    c.add_argument("--dir", default=None,
                   help="output directory (default: the recorder's)")
    c.add_argument("--half-spread-bps", type=_floats, default=[2, 3, 4, 6],
                   help="quote distance from the reference on each side; "
                        "comma list, one simulation each")
    c.add_argument("--center-bps", default="auto",
                   help="premium center in bps, or 'auto' (EMA of the "
                        "mid premium)")
    c.add_argument("--center-tau-sec", type=float, default=900.0)
    c.add_argument("--center-warmup-sec", type=float, default=60.0)
    c.add_argument("--size-usd", type=float, default=300.0)
    c.add_argument("--requote-bps", type=float, default=1.0,
                   help="move the quote when its target moves this much")
    c.add_argument("--place-latency-ms", type=float, default=200.0)
    c.add_argument("--cancel-latency-ms", type=float, default=200.0)
    c.add_argument("--hedge-delay-ms", type=float, default=350.0,
                   help="fill notification + hedge taker latency")
    c.add_argument("--feed-lag-ms", type=float, default=50.0,
                   help="judge fills against quotes this much before the "
                        "trade was received")
    c.add_argument("--actions-per-min", type=int, default=40,
                   help="quote place/modify/cancel budget per minute")
    c.add_argument("--max-position-usd", type=float, default=0.0,
                   help="RH position cap (default: the config's primary cap)")
    c.add_argument("--maker-fee-bps", type=float, default=0.0)
    c.add_argument("--minutes", type=float, default=0.0,
                   help="stop after this long (0 = until Ctrl-C)")
    c.add_argument("--status-sec", type=float, default=60.0)
    r = sub.add_parser("report", help="summarize recorded shadow fills")
    r.add_argument("--dir", required=True,
                   help="directory with maker_shadow_*.csv")
    r.add_argument("--no-touch", action="store_true",
                   help="exclude fills that needed queue priority")
    args = ap.parse_args()
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s.%(msecs)03d %(levelname)-7s %(name)s: %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S")
    logging.getLogger("websockets").setLevel(logging.WARNING)
    if args.cmd == "collect":
        try:
            asyncio.run(collect(args))
        except Exception as e:
            print(f"error: {e!r}", file=sys.stderr)
            sys.exit(1)
    else:
        report(args)


if __name__ == "__main__":
    main()
