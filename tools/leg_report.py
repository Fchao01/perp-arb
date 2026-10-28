#!/usr/bin/env python3
"""Diagnose two-leg execution from the bot's trades.csv (read-only).

Every row of trades.csv is one execution. This tool sorts them by outcome
(both legs filled, primary missed while the hedge filled, hedge missed,
nothing filled, send failure), measures each filled leg against its planned
price (positive = filled worse than planned), and compares the planned edge
with the edge the fills actually captured.

It then splits the same numbers by planned premium, by which venue's book
updated last before the decision, by UTC hour and by day. An edge that shows
up right after one venue's update is usually the *other* venue's stale
quote; that venue's makers cancel it before a ~300 ms taker lands, which
shows up here as misses and bad fills on that leg.

With --hedges it also adds up the repair hedges (the orders the bot sends to
flatten a one-legged fill) in the same time window, so the total shows what
execution really earned.

只读工具：读机器人的 trades.csv，把每次执行分成"两腿都成交 / 主所漏单
（对冲所单边成交）/ 对冲所漏单 / 都没成交 / 发送失败"，计算每条腿实际成交
价相对计划价的偏差（正数=比计划差），对比计划价差和实际成交价差；再按计划
价差大小、决策前哪个交易所盘口最后更新、UTC 小时和日期拆分。加 --hedges
会把同一时间段的补单盈亏也算进去。

Usage:
    python3 tools/leg_report.py --csv logs/sndk/trades.csv
    python3 tools/leg_report.py --csv logs/sndk/trades.csv --hedges logs/sndk/hedges.csv
    python3 tools/leg_report.py --csv old.csv --csv new.csv   # merged, deduped by trade_id
"""
from __future__ import annotations

import argparse
import csv
import math
import statistics
import sys
from collections import Counter, defaultdict
from datetime import datetime, timezone
from typing import Callable, Dict, List, Optional

KINDS = ("both", "imbalanced", "primary_missed", "hedge_missed",
         "nothing", "send_failed")
KIND_LABEL = {
    "both": "both legs filled / 两腿都成交",
    "imbalanced": "both filled, sizes differ / 两腿成交量不等",
    "primary_missed": "primary missed, hedge filled / 主所漏单",
    "hedge_missed": "hedge missed, primary filled / 对冲所漏单",
    "nothing": "nothing filled / 都没成交",
    "send_failed": "send failed / 发送失败",
}
SEND_FAILED = "send-failed"
FIT_BPS = 0.5          # a fill within this of the plan counts as "at plan"


def _f(v) -> Optional[float]:
    try:
        x = float(v)
    except (TypeError, ValueError):
        return None
    return x if math.isfinite(x) else None


def load_rows(paths: List[str]) -> List[dict]:
    """Rows from one or more trades.csv files, deduped by trade_id."""
    rows: Dict[str, dict] = {}
    for path in paths:
        with open(path, newline="") as fh:
            for i, r in enumerate(csv.DictReader(fh)):
                rows[r.get("trade_id") or f"{path}:{i}"] = r
    return sorted(rows.values(), key=lambda r: _f(r.get("ts")) or 0.0)


def primary_side(row: dict) -> str:
    """'buy' or 'sell': which side of the row is the primary venue.

    The direction names the primary's side (sell_entropy / buy_entropy,
    sell_primary / buy_primary in older logs)."""
    return "sell" if row.get("direction", "").startswith("sell") else "buy"


def leg_slip_bps(row: dict, side: str) -> Optional[float]:
    """Fill vs planned price in bps; positive = filled worse than planned."""
    px, lim = _f(row.get(f"{side}_avg_px")), _f(row.get(f"{side}_limit"))
    if not px or not lim:
        return None
    s = (px / lim - 1.0) * 1e4
    return s if side == "buy" else -s


def classify(row: dict) -> str:
    p = primary_side(row)
    h = "buy" if p == "sell" else "sell"
    if SEND_FAILED in (row.get(f"{p}_status"), row.get(f"{h}_status")):
        return "send_failed"
    qty = _f(row.get("qty")) or 0.0
    pf, hf = _f(row.get(f"{p}_fill")) or 0.0, _f(row.get(f"{h}_fill")) or 0.0
    if pf <= 0 and hf <= 0:
        return "nothing"
    if pf <= 0:
        return "primary_missed"
    if hf <= 0:
        return "hedge_missed"
    tol = max(qty * 0.01, 1e-12)
    return "both" if abs(pf - hf) <= tol else "imbalanced"


def last_updated(row: dict) -> Optional[str]:
    """Which venue's book changed most recently before the decision."""
    p = primary_side(row)
    h = "buy" if p == "sell" else "sell"
    pa, ha = _f(row.get(f"{p}_book_age_ms")), _f(row.get(f"{h}_book_age_ms"))
    if pa is None or ha is None:
        return None
    return "hedge" if ha < pa else "primary"


def premium_bucket(edges: List[float]) -> Callable[[dict], Optional[str]]:
    def key(row: dict) -> Optional[str]:
        p = _f(row.get("marginal_premium_bps"))
        if p is None:
            return None
        lo = None
        for e in edges:
            if p < e:
                return f"<{e:g}" if lo is None else f"{lo:g}-{e:g}"
            lo = e
        return f">={edges[-1]:g}"
    return key


def _q(vals: List[float], p: float) -> float:
    s = sorted(vals)
    return s[int(p * (len(s) - 1))]


def summarize(rows: List[dict]) -> dict:
    """Outcome counts, per-leg slippage vs plan and planned vs captured edge."""
    kinds = Counter(classify(r) for r in rows)
    out = {"n": len(rows), "kinds": kinds}
    filled = [r for r in rows if classify(r) == "both"]
    for leg in ("primary", "hedge"):
        slips = []
        for r in filled:
            p = primary_side(r)
            side = p if leg == "primary" else ("buy" if p == "sell" else "sell")
            s = leg_slip_bps(r, side)
            if s is not None:
                slips.append(s)
        out[f"{leg}_slip"] = slips
    exp_usd = sum(_f(r.get("exp_edge_usd")) or 0.0 for r in filled)
    fill_usd = sum(_f(r.get("fill_edge_usd")) or 0.0 for r in filled)
    notional = sum(_f(r.get("buy_notional")) or 0.0 for r in filled)
    out["planned_bps"] = exp_usd / notional * 1e4 if notional else None
    out["captured_bps"] = fill_usd / notional * 1e4 if notional else None
    out["planned_usd_all"] = sum(_f(r.get("exp_edge_usd")) or 0.0 for r in rows)
    out["fill_edge_usd_all"] = sum(_f(r.get("fill_edge_usd")) or 0.0 for r in rows)
    return out


def repairs(hedge_rows: List[dict], trade_ids: set, t0: float,
            t1: float) -> dict:
    """Repair hedges sent for these executions. A hedge row names the
    execution that left the residual in its trade_id; rows without one
    (reconcile) count when logged between t0 and t1 plus a minute."""
    sel = [r for r in hedge_rows
           if (r.get("trade_id") in trade_ids if r.get("trade_id")
               else t0 <= (_f(r.get("ts")) or 0.0) <= t1 + 60.0)]
    pnl = [_f(r.get("close_pnl_usd")) for r in sel]
    notional = sum((_f(r.get("filled_qty")) or 0.0) * (_f(r.get("avg_px")) or 0.0)
                   for r in sel)
    return {"n": len(sel), "status": Counter(r.get("status") or "?" for r in sel),
            "unlinked": sum(not r.get("trade_id") for r in sel),
            "notional": notional, "pnl": sum(p for p in pnl if p is not None)}


def load_hedges(paths: List[str]) -> List[dict]:
    """Rows from one or more hedges.csv files, deduped."""
    rows: Dict[tuple, dict] = {}
    for path in paths:
        with open(path, newline="") as fh:
            for r in csv.DictReader(fh):
                rows[(r.get("ts"), r.get("venue"), r.get("client_order_id"))] = r
    return list(rows.values())


def _pct(a: int, n: int) -> str:
    return f"{a / n:4.0%}" if n else "   -"


def print_breakdown(title: str, rows: List[dict],
                    key: Callable[[dict], Optional[str]],
                    order: Optional[List[str]] = None) -> None:
    groups: Dict[str, List[dict]] = defaultdict(list)
    for r in rows:
        k = key(r)
        if k is not None:
            groups[k].append(r)
    if not groups:
        return
    print(f"\n{title}")
    print(f"  {'bucket':14s} {'n':>5s} {'both':>5s} {'P-miss':>6s} {'H-miss':>6s}"
          f" {'P slip':>7s} {'H slip':>7s} {'plan':>6s} {'got':>6s}")
    keys = [k for k in (order or sorted(groups)) if k in groups]
    for k in keys:
        g = groups[k]
        s = summarize(g)
        n, kc = s["n"], s["kinds"]
        med = lambda v: f"{statistics.median(v):+7.2f}" if v else "      -"
        bps = lambda v: f"{v:+6.2f}" if v is not None else "     -"
        print(f"  {k:14s} {n:5d} {_pct(kc['both'], n):>5s} "
              f"{_pct(kc['primary_missed'], n):>6s} {_pct(kc['hedge_missed'], n):>6s}"
              f" {med(s['primary_slip'])} {med(s['hedge_slip'])}"
              f" {bps(s['planned_bps'])} {bps(s['captured_bps'])}")


def report(rows: List[dict], hedge_rows: Optional[List[dict]],
           premium_edges: List[float]) -> None:
    if not rows:
        print("no trade rows")
        return
    ts = [_f(r.get("ts")) or 0.0 for r in rows]
    utc = lambda t: datetime.fromtimestamp(t, timezone.utc)
    venues = Counter((r.get("buy_venue"), r.get("sell_venue")) for r in rows)
    print(f"{len(rows)} executions  {utc(min(ts)):%Y-%m-%d %H:%M} -> "
          f"{utc(max(ts)):%Y-%m-%d %H:%M} UTC  venues {dict(venues)}")

    s = summarize(rows)
    n, kc = s["n"], s["kinds"]
    print("\nOutcomes / 结果")
    for k in KINDS:
        if kc[k]:
            print(f"  {KIND_LABEL[k]:44s} {kc[k]:5d}  {kc[k] / n:5.1%}")

    print("\nStatus per leg / 每条腿的状态")
    for leg in ("primary", "hedge"):
        c = Counter()
        for r in rows:
            p = primary_side(r)
            side = p if leg == "primary" else ("buy" if p == "sell" else "sell")
            c[r.get(f"{side}_status") or "?"] += 1
        print(f"  {leg:8s} " + "  ".join(f"{k} {v}" for k, v in c.most_common()))

    print("\nFill vs planned price, both-filled rows (bps, + = worse) / "
          "成交价相对计划价")
    for leg in ("primary", "hedge"):
        v = s[f"{leg}_slip"]
        if not v:
            print(f"  {leg:8s} no fills")
            continue
        at = sum(x <= FIT_BPS for x in v) / len(v)
        print(f"  {leg:8s} n={len(v)}  p10 {_q(v, .1):+.2f}  p50 {_q(v, .5):+.2f}  "
              f"p90 {_q(v, .9):+.2f}  mean {statistics.fmean(v):+.2f}  "
              f"within {FIT_BPS:g} bps of plan {at:.0%}")

    print("\nEdge / 价差收益")
    if s["planned_bps"] is not None:
        print(f"  both-filled rows: planned {s['planned_bps']:+.2f} bps, "
              f"captured {s['captured_bps']:+.2f} bps")
    total = s["fill_edge_usd_all"]
    print(f"  all rows: planned ${s['planned_usd_all']:.2f}, "
          f"fill edge ${s['fill_edge_usd_all']:.2f}")
    if hedge_rows is not None:
        rp = repairs(hedge_rows, {r.get("trade_id") for r in rows},
                     min(ts), max(ts))
        total += rp["pnl"]
        print(f"  repair hedges: {rp['n']} orders, ${rp['notional']:,.0f} filled "
              f"notional, realized ${rp['pnl']:.2f}")
        print("    " + "  ".join(f"{k} {v}" for k, v in rp["status"].most_common())
              + (f"  ({rp['unlinked']} without trade_id, matched by time)"
                 if rp["unlinked"] else ""))
        print(f"  fill edge + repairs: ${total:.2f}")

    print_breakdown("By planned premium (bps) / 按计划价差", rows,
                    premium_bucket(premium_edges),
                    [premium_bucket(premium_edges)({"marginal_premium_bps": e - 1e-9})
                     for e in premium_edges]
                    + [f">={premium_edges[-1]:g}"])
    print_breakdown("By which book updated last / 决策前哪边盘口最后更新",
                    rows, last_updated, ["hedge", "primary"])
    print_breakdown("By UTC hour / 按小时", rows,
                    lambda r: f"{utc(_f(r.get('ts')) or 0):%H}h")
    print_breakdown("By UTC day / 按日期", rows,
                    lambda r: f"{utc(_f(r.get('ts')) or 0):%m-%d}")
    print("\nColumns: P = primary, H = hedge; slip is the median fill vs plan "
          "on both-filled rows; plan / got are planned and captured edge in bps.")

    pm = kc["primary_missed"] / n
    pslip = statistics.median(s["primary_slip"]) if s["primary_slip"] else 0.0
    if pm >= 0.2 or pslip >= 2.0:
        print(f"\nThe primary leg misses {pm:.0%} of the time and fills a median "
              f"{pslip:+.2f} bps off plan: its quotes are mostly gone before the "
              "order lands. Sequential mode (lead_venue: primary, "
              "lead_slippage_bps: 0) sends the hedge only for what fills at plan.\n"
              f"主所腿漏单 {pm:.0%}，成交价中位比计划差 {pslip:+.2f} bps，报价多半在"
              "订单到达前就撤了。可试 lead_venue: primary、lead_slippage_bps: 0，"
              "只对按计划价成交的部分下对冲单。")


def main() -> None:
    ap = argparse.ArgumentParser(description="diagnose two-leg execution")
    ap.add_argument("--csv", action="append", required=True,
                    help="trades.csv (repeatable; merged by trade_id)")
    ap.add_argument("--hedges", action="append",
                    help="hedges.csv for repair P&L (repeatable)")
    ap.add_argument("--premium-bins", default="2,4,6,10,20",
                    help="planned premium bucket edges in bps")
    args = ap.parse_args()
    try:
        rows = load_rows(args.csv)
        hedge_rows = load_hedges(args.hedges) if args.hedges else None
    except FileNotFoundError as exc:
        print(f"{exc.filename} not found", file=sys.stderr)
        raise SystemExit(1)
    edges = sorted(float(x) for x in args.premium_bins.split(",") if x.strip())
    report(rows, hedge_rows, edges)


if __name__ == "__main__":
    main()
