#!/usr/bin/env python3
"""Measure how long executable cross-venue edges live (read-only).

The bot fires the moment its books show an executable premium past the
config hurdle, but its orders match some hundreds of milliseconds later. If
the edge is already gone by then, every fill lands at the normal premium.
This tool watches the same two books the bot trades (same config, same
fees, same hurdle, same sizing) and records every *episode*: the stretch
during which an executable edge exists in one direction.

For each episode it writes when it started, how long it lasted, which
venue's update opened and closed it, its excess over the hurdle, and the
excess still left at fixed checkpoints (50 ms ... 2 s). The report turns
that into the question that matters: of the edges the bot would fire on,
how many are still there when its order lands — and does waiting for a
persistence gate first leave edges that survive?

It never loads credentials and never sends orders.

只读工具：用与机器人完全相同的配置、手续费、阈值和下单规模，记录每一次
"可成交价差越过阈值"的事件持续了多少毫秒、由哪个交易所的更新开启和结束、
在各个时间点还剩多少超额价差。report 子命令据此回答：机器人下单落地时价差
还在不在；加持续性闸门（premium_persist_sec）后剩下的价差能不能活到成交。

Usage:
    python3 tools/edge_lifetime.py collect --symbol SNDK --config sndk.yaml
    python3 tools/edge_lifetime.py collect --symbol SNDK --minutes 120
    python3 tools/edge_lifetime.py report --csv logs/sndk/edge_episodes.csv
    python3 tools/edge_lifetime.py report --csv ... --latency-ms 50,150,340
"""
from __future__ import annotations

import argparse
import asyncio
import csv
import functools
import logging
import os
import signal
import sys
import time
from collections import Counter
from datetime import datetime, timezone
from typing import Callable, Dict, List, Optional

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))

from entropy_arb.book import plan_arb  # noqa: E402

log = logging.getLogger("edge_lifetime")

CHECKPOINTS_MS = (50, 100, 200, 300, 500, 1000, 2000)
CENSORED = ("stale", "shutdown")   # ended for a reason other than the market
HEADER = (["start_utc", "start_ts", "direction", "duration_ms", "started_by",
           "ended_by", "hurdle_bps", "excess_start_bps", "excess_peak_bps",
           "mid_premium_bps", "buy_px", "sell_px", "plan_notional_usd",
           "crossable_notional_usd"]
          + [f"excess_at_{c}ms" for c in CHECKPOINTS_MS])


def _fmt(v, digits=3):
    return "" if v is None else f"{v:.{digits}f}"


class EpisodeTracker:
    """Tracks open edge episodes from book updates; writes closed ones.

    `venues` maps key -> object with .name, .book, .fee_bps; key "entropy"
    is the primary and "hedge" the other leg, as in the engine. `hurdles`
    maps direction key -> net hurdle in bps (on top of fees)."""

    def __init__(self, venues: Dict[str, object], hurdles: Dict[str, float], *,
                 staleness_sec: float, sizing: dict,
                 emit: Callable[[dict], None],
                 clock: Callable[[], float] = time.monotonic,
                 wall: Callable[[], float] = time.time) -> None:
        self.venues = venues
        self.hurdles = hurdles
        self.staleness_sec = staleness_sec
        self.sizing = sizing
        self.emit = emit
        self.clock = clock
        self.wall = wall
        self.open: Dict[str, dict] = {}
        self.closed = 0
        p, h = venues["entropy"], venues["hedge"]
        # direction key -> (buy venue, sell venue), same keys as the engine
        self.directions = {"sell_entropy": (h, p), "buy_entropy": (p, h)}

    def _excess(self, buy, sell, hurdle: float) -> Optional[float]:
        """Top-of-book executable premium net of fees, minus the hurdle."""
        if not (buy.book.is_fresh(self.staleness_sec)
                and sell.book.is_fresh(self.staleness_sec)):
            return None
        ask, bid = buy.book.best_ask(), sell.book.best_bid()
        if not ask or not bid:
            return None
        net = (bid * (1 - sell.fee_bps / 1e4)) / (ask * (1 + buy.fee_bps / 1e4))
        return (net - 1.0) * 1e4 - hurdle

    def on_update(self, key: str) -> None:
        now = self.clock()
        who = self.venues[key].name
        for dkey, (buy, sell) in self.directions.items():
            excess = self._excess(buy, sell, self.hurdles[dkey])
            ep = self.open.get(dkey)
            if excess is not None and excess > 0:
                if ep is None:
                    self.open[dkey] = self._start(dkey, buy, sell, now, who,
                                                  excess)
                else:
                    ep["peak"] = max(ep["peak"], excess)
                    t_ms = (now - ep["t0"]) * 1000.0
                    if t_ms <= CHECKPOINTS_MS[-1]:
                        ep["samples"].append((t_ms, excess))
            elif ep is not None:
                self._close(dkey, now, who if excess is not None else "stale")

    def _start(self, dkey, buy, sell, now, who, excess) -> dict:
        plan, _ = plan_arb(buy.book, sell.book,
                           threshold_bps=self.hurdles[dkey],
                           buy_fee_bps=buy.fee_bps, sell_fee_bps=sell.fee_bps,
                           **self.sizing)
        p, h = self.venues["entropy"].book.mid(), self.venues["hedge"].book.mid()
        return {"t0": now, "wall0": self.wall(), "direction": dkey,
                "started_by": who, "hurdle": self.hurdles[dkey],
                "excess0": excess, "peak": excess,
                "samples": [(0.0, excess)],
                "mid_prem": (p / h - 1.0) * 1e4 if p and h else None,
                "buy_px": buy.book.best_ask(), "sell_px": sell.book.best_bid(),
                "plan_ntl": plan.buy_notional if plan else 0.0,
                "cross_ntl": plan.q_max_notional if plan else 0.0}

    def _close(self, dkey: str, now: float, ended_by: str) -> None:
        ep = self.open.pop(dkey)
        dur = (now - ep["t0"]) * 1000.0
        row = {
            "start_utc": datetime.fromtimestamp(ep["wall0"], timezone.utc)
            .strftime("%Y-%m-%dT%H:%M:%S.%f")[:-3] + "Z",
            "start_ts": f"{ep['wall0']:.3f}", "direction": dkey,
            "duration_ms": f"{dur:.1f}", "started_by": ep["started_by"],
            "ended_by": ended_by, "hurdle_bps": _fmt(ep["hurdle"]),
            "excess_start_bps": _fmt(ep["excess0"]),
            "excess_peak_bps": _fmt(ep["peak"]),
            "mid_premium_bps": _fmt(ep["mid_prem"]),
            "buy_px": ep["buy_px"], "sell_px": ep["sell_px"],
            "plan_notional_usd": f"{ep['plan_ntl']:.2f}",
            "crossable_notional_usd": f"{ep['cross_ntl']:.2f}",
        }
        for c in CHECKPOINTS_MS:
            alive = dur >= c
            at = [x for t, x in ep["samples"] if t <= c]
            row[f"excess_at_{c}ms"] = _fmt(at[-1]) if alive and at else ""
        self.closed += 1
        self.emit(row)

    def close_all(self, ended_by: str = "shutdown") -> None:
        now = self.clock()
        for dkey in list(self.open):
            self._close(dkey, now, ended_by)


class CsvSink:
    def __init__(self, path: str) -> None:
        d = os.path.dirname(path)
        if d:
            os.makedirs(d, exist_ok=True)
        new = not os.path.exists(path) or os.path.getsize(path) == 0
        self.fh = open(path, "a", newline="")
        self.w = csv.DictWriter(self.fh, fieldnames=HEADER)
        if new:
            self.w.writeheader()
            self.fh.flush()
        self.path = path
        self.rows: List[dict] = []   # this session, for the progress line

    def __call__(self, row: dict) -> None:
        self.w.writerow(row)
        self.fh.flush()
        self.rows.append(row)

    def close(self) -> None:
        self.fh.close()


# ------------------------------------------------------------------ collect

async def collect(args) -> None:
    import aiohttp
    from entropy_arb.config import load_config
    from entropy_arb.venue_hl import HLVenue
    from entropy_arb.venue_lighter import LighterVenue

    cfg = load_config(args.config, args.env_file, symbol=args.symbol,
                      hedge_venue=args.hedge, primary_venue=args.primary)
    out = args.csv or os.path.join(os.path.dirname(cfg.recorder_csv),
                                   "edge_episodes.csv")
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

    venues = {"entropy": make(cfg.entropy), "hedge": make(cfg.hedge)}
    sink = CsvSink(out)
    tasks = []
    try:
        await asyncio.gather(*(v.load_market() for v in venues.values()))
        p, h = venues["entropy"], venues["hedge"]
        step = 10 ** -min(p.size_decimals, h.size_decimals)
        sizing = dict(take_fraction=cfg.take_fraction,
                      cap_notional=cfg.max_order_notional,
                      min_base=max(p.min_base, h.min_base, step),
                      min_notional=max(cfg.min_order_notional,
                                       p.min_quote, h.min_quote),
                      size_step=step)
        # inventory surcharge is position-dependent and left out: this is
        # the hurdle a flat bot would fire on
        hurdles = {"sell_entropy": cfg.midline_bps + cfg.upper_bps
                   + args.extra_bps,
                   "buy_entropy": cfg.lower_bps - cfg.midline_bps
                   + args.extra_bps}
        tracker = EpisodeTracker(venues, hurdles,
                                 staleness_sec=cfg.staleness_sec,
                                 sizing=sizing, emit=sink)
        log.info("watching %s: %s vs %s | hurdle sell_primary %+.2f bps, "
                 "buy_primary %+.2f bps (net of fees %.2f+%.2f) -> %s",
                 cfg.symbol, p.name, h.name, hurdles["sell_entropy"],
                 hurdles["buy_entropy"], p.fee_bps, h.fee_bps, out)
        for key, v in venues.items():
            tasks += v.start_tasks(stop,
                                   functools.partial(tracker.on_update, key),
                                   False)
        deadline = time.time() + args.minutes * 60 if args.minutes > 0 else None
        while not stop.is_set():
            try:
                await asyncio.wait_for(stop.wait(), timeout=args.status_sec)
            except asyncio.TimeoutError:
                pass
            _progress(sink.rows, args.latency_ms)
            if deadline and time.time() >= deadline:
                stop.set()
        tracker.close_all()
    finally:
        stop.set()
        for t in tasks:
            t.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        for v in venues.values():
            await v.close()
        await session.close()
        sink.close()
    log.info("stopped — %d episode(s) this session in %s", len(sink.rows), out)


def _progress(rows: List[dict], latency_ms: List[float]) -> None:
    done = [r for r in rows if r["ended_by"] not in CENSORED]
    if not done:
        log.info("no completed episodes yet")
        return
    durs = sorted(float(r["duration_ms"]) for r in done)
    alive = " ".join(f">={l:g}ms {sum(d >= l for d in durs) / len(durs):.0%}"
                     for l in latency_ms)
    log.info("%d episodes | median %.0f ms | alive %s", len(done),
             durs[len(durs) // 2], alive)


# ------------------------------------------------------------------- report

def load_episodes(path: str, hours: float = 0.0,
                  min_excess: float = 0.0) -> List[dict]:
    cutoff = time.time() - hours * 3600 if hours > 0 else 0.0
    out = []
    with open(path, newline="") as fh:
        for r in csv.DictReader(fh):
            try:
                ep = {"ts": float(r["start_ts"]), "direction": r["direction"],
                      "dur": float(r["duration_ms"]),
                      "started_by": r["started_by"], "ended_by": r["ended_by"],
                      "excess0": float(r["excess_start_bps"]),
                      "ntl": float(r["plan_notional_usd"] or 0.0),
                      "at": {c: (float(r[f"excess_at_{c}ms"])
                                 if r.get(f"excess_at_{c}ms") else None)
                             for c in CHECKPOINTS_MS}}
            except (KeyError, ValueError):
                continue
            if ep["ts"] >= cutoff and ep["excess0"] >= min_excess:
                out.append(ep)
    return out


def survival(eps: List[dict], persist_ms: float, latency_ms: float):
    """Of episodes still alive after persist_ms (when a persistence gate
    would fire), the share still alive at persist_ms + latency_ms (when the
    order lands). Censored episodes too short to decide are left out.
    Returns (fired, survived)."""
    fired = survived = 0
    end = persist_ms + latency_ms
    for e in eps:
        if e["dur"] < persist_ms:
            continue                     # gate never fires
        if e["ended_by"] in CENSORED and e["dur"] < end:
            continue                     # unknown: feed loss / shutdown
        fired += 1
        survived += e["dur"] >= end
    return fired, survived


def _pct(vals: List[float], q: float) -> float:
    if not vals:
        return float("nan")
    v = sorted(vals)
    return v[min(len(v) - 1, int(round(q * (len(v) - 1))))]


def report(args) -> None:
    eps = load_episodes(args.csv, args.hours, args.min_excess)
    if not eps:
        print("no episodes in range")
        return
    span_h = max((max(e["ts"] for e in eps) - min(e["ts"] for e in eps))
                 / 3600.0, 1e-9)
    persists = [float(x) for x in args.persist_ms.split(",")]
    print(f"{len(eps)} episodes over {span_h:.2f} h "
          f"(min excess {args.min_excess:g} bps)")
    for dkey in ("sell_entropy", "buy_entropy"):
        de = [e for e in eps if e["direction"] == dkey]
        label = "sell_primary" if dkey == "sell_entropy" else "buy_primary"
        if not de:
            print(f"\n== {label}: none")
            continue
        durs = [e["dur"] for e in de if e["ended_by"] not in CENSORED]
        print(f"\n== {label}: {len(de)} episodes ({len(de) / span_h:.1f}/h), "
              f"plan notional median ${_pct([e['ntl'] for e in de], .5):,.0f}")
        print("  duration ms  p25 %.0f  p50 %.0f  p75 %.0f  p90 %.0f  max %.0f"
              % tuple(_pct(durs, q) for q in (.25, .5, .75, .9, 1.0)))
        print("  excess at start bps  p50 %.2f  p90 %.2f"
              % (_pct([e["excess0"] for e in de], .5),
                 _pct([e["excess0"] for e in de], .9)))
        print("  checkpoint   alive   mean excess left (bps)")
        for c in CHECKPOINTS_MS:
            left = [e["at"][c] for e in de if e["at"][c] is not None]
            known = [e for e in de if not (e["ended_by"] in CENSORED
                                           and e["dur"] < c)]
            share = len(left) / len(known) if known else float("nan")
            print(f"  {c:6d} ms  {share:6.1%}   "
                  + (f"{sum(left) / len(left):+.2f}" if left else "—"))
        print("  started by " + ", ".join(
            f"{k} {v}" for k, v in Counter(e["started_by"] for e in de)
            .most_common()) + " | ended by " + ", ".join(
            f"{k} {v}" for k, v in Counter(e["ended_by"] for e in de)
            .most_common()))
        print("  persistence gate -> edge still there when the order lands")
        print("  " + "persist ms".rjust(10) + "".join(
            f"   lat {l:g}ms".rjust(20) for l in args.latency_ms))
        for p in persists:
            cells = []
            for l in args.latency_ms:
                fired, ok = survival(de, p, l)
                cells.append((f"{ok}/{fired} {ok / fired:.0%}" if fired
                              else "—").rjust(20))
            print("  " + f"{p:g}".rjust(10) + "".join(cells))


# --------------------------------------------------------------------- main

def _latencies(s: str) -> List[float]:
    return [float(x) for x in s.split(",") if x.strip()]


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    sub = ap.add_subparsers(dest="cmd", required=True)
    c = sub.add_parser("collect", help="watch both books and record episodes")
    c.add_argument("--symbol", required=True)
    c.add_argument("--primary", default=None)
    c.add_argument("--hedge", default="lighter")
    c.add_argument("--config", default="config.yaml")
    c.add_argument("--env-file", default=".env",
                   help="only PRIMARY_VENUE is read; no credentials are used")
    c.add_argument("--csv", default=None,
                   help="output (default: next to recorder.csv, "
                        "edge_episodes.csv)")
    c.add_argument("--extra-bps", type=float, default=0.0,
                   help="add to both hurdles, e.g. to probe a wider band")
    c.add_argument("--minutes", type=float, default=0.0,
                   help="stop after this long (0 = until Ctrl-C)")
    c.add_argument("--status-sec", type=float, default=60.0)
    c.add_argument("--latency-ms", type=_latencies, default=[50, 150, 340])
    r = sub.add_parser("report", help="summarize a recorded episodes CSV")
    r.add_argument("--csv", required=True)
    r.add_argument("--hours", type=float, default=0.0,
                   help="only the most recent N hours (0 = all)")
    r.add_argument("--min-excess", type=float, default=0.0,
                   help="only episodes starting at least this far past the "
                        "hurdle (bps)")
    r.add_argument("--latency-ms", type=_latencies, default=[50, 150, 340],
                   help="order latencies to evaluate, comma separated")
    r.add_argument("--persist-ms", default="0,100,200,300,500,1000",
                   help="persistence gates to evaluate, comma separated")
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
