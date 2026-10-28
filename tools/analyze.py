#!/usr/bin/env python3
"""Analyze recorded minute data and suggest config.yaml thresholds.

Reads the CSV written by the built-in recorder (logs/minutes.csv by default)
and prints:

  * the premium distribution (midline candidates),
  * how often each candidate upper/lower band would have fired,
  * a ready-to-paste `thresholds:` snippet.

With --midline auto the bands are measured against the same moving center
the engine uses with `midline_bps: auto` (an EMA of the minute premium,
replayed minute by minute) and the suggestion is one symmetric band.

分析机器人自动采集的分钟级盘口数据，输出溢价分布、各档阈值的触发频率，
以及可直接粘贴进 config.yaml 的 thresholds 建议值。加 --midline auto 时按
引擎自动中枢（分钟溢价的 EMA，逐分钟回放）计算，并给出对称的门槛。

Usage:
    python3 tools/analyze.py                    # logs/minutes.csv
    python3 tools/analyze.py --csv path.csv --hours 24 --min-samples 10
    python3 tools/analyze.py --csv logs/eth/minutes.csv --midline auto --min-band 1.0
"""
from __future__ import annotations

import argparse
import csv
import math
import os
import sys
import time

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))

from entropy_arb.center import CenterTracker  # noqa: E402

CANDIDATES = [1.0, 1.5, 2.0, 3.0, 4.0, 5.0, 6.0, 8.0, 10.0, 15.0, 20.0]
AUTO_CANDIDATES = [0.25, 0.5, 0.75, 1.0, 1.5, 2.0, 3.0, 4.0, 5.0]


def pctl(sorted_vals: list, q: float) -> float:
    """Linear-interpolated percentile of a pre-sorted list, q in [0, 100]."""
    if not sorted_vals:
        return float("nan")
    k = (len(sorted_vals) - 1) * q / 100.0
    lo = math.floor(k)
    hi = math.ceil(k)
    if lo == hi:
        return sorted_vals[int(k)]
    return sorted_vals[lo] * (hi - k) + sorted_vals[hi] * (k - lo)


def load_rows(path: str, hours: float, min_samples: int) -> list:
    cutoff = time.time() - hours * 3600 if hours > 0 else 0.0
    rows = []
    with open(path, newline="") as fh:
        for r in csv.DictReader(fh):
            try:
                if float(r["minute_ts"]) < cutoff:
                    continue
                if int(r["samples"]) < min_samples:
                    continue
                rows.append({
                    "ts": float(r["minute_ts"]),
                    "prem": float(r["premium_close_bps"]),
                    "prem_mean": float(r["premium_mean_bps"]),
                    "sell_max": float(r["sell_edge_max_bps"]),
                    "buy_max": float(r["buy_edge_max_bps"]),
                })
            except (KeyError, ValueError):
                continue
    rows.sort(key=lambda r: r["ts"])
    return rows


def auto_rooms(rows: list, window_min: float, warmup_min: float,
               fees: float) -> tuple:
    """Executable room beyond the auto midline each minute, as the engine
    would have seen it: the EMA center from the minutes before, never the
    minute itself. Minutes before the warmup are skipped."""
    window = window_min * 60.0
    c = CenterTracker(None, window, warmup_min * 60.0, reset_gap_sec=window)
    sell, buy, centers = [], [], []
    for r in rows:
        ts = r["ts"] + 60.0
        if c.ready(ts):
            m = c.value
            centers.append(m)
            sell.append(r["sell_max"] - m - fees)
            buy.append(r["buy_max"] + m - fees)
        c.update(ts, r["prem_mean"])
    return sell, buy, centers


def print_auto(rows: list, args) -> None:
    sell, buy, centers = auto_rooms(rows, args.window_min, args.warmup_min,
                                    args.fees_bps)
    if not centers:
        print("not enough history for the auto midline to warm up / "
              "数据不够自动中枢预热", file=sys.stderr)
        sys.exit(1)
    n = len(centers)
    cs = sorted(centers)
    print(f"\nauto midline (EMA {args.window_min:g} min) over {n} minutes / "
          f"自动中枢的变化范围: min {cs[0]:+.2f}  p50 {pctl(cs, 50):+.2f}  "
          f"max {cs[-1]:+.2f}")
    print(f"\nshare of minutes each symmetric band would have fired against "
          f"the moving midline, after {args.fees_bps:.1f} bps fees / "
          f"各档对称门槛触发的分钟占比:")
    print(f"  {'band bps':>9} | {'SELL entropy':>12} | {'BUY entropy':>12}")
    for h in AUTO_CANDIDATES:
        s = sum(1 for x in sell if x >= h) / n
        b = sum(1 for x in buy if x >= h) / n
        print(f"  {h:>9.2f} | {s:>12.1%} | {b:>12.1%}")

    # the widest band that still fires both ways in ~10% of minutes, but
    # never narrower than --min-band (the primary leg's fill slippage)
    room = min(pctl(sorted(sell), 90), pctl(sorted(buy), 90))
    band = max(math.floor(room * 4) / 4, args.min_band)
    print(f"""
suggested starting point (both directions fire in ~10% of minutes; never
below --min-band {args.min_band:g}) /
建议起点（两个方向约 10% 的分钟触发；不低于 --min-band {args.min_band:g}）:

thresholds:
  midline_bps: auto
  upper_bps: {band:g}
  lower_bps: {band:g}
  midline_window_min: {args.window_min:g}

The band must exceed the primary leg's fill slippage: run tools/leg_report.py
on this symbol's trades.csv and pass its primary p50 slip as --min-band. /
门槛必须大于主所腿的成交滑点：用 tools/leg_report.py 查看该品种主所腿滑点
中位数，作为 --min-band 传入。
""")


def main() -> None:
    p = argparse.ArgumentParser(description="suggest thresholds from recorded "
                                            "minute data")
    p.add_argument("--csv", default="logs/minutes.csv")
    p.add_argument("--hours", type=float, default=0.0,
                   help="only use the last N hours (0 = all data)")
    p.add_argument("--min-samples", type=int, default=10,
                   help="skip minutes with fewer fresh samples than this")
    p.add_argument("--fees-bps", type=float, default=0.0,
                   help="SUM of both venues' taker fees in bps (each crossing "
                        "pays both legs); recorded edges are pre-fee, so this "
                        "is subtracted before counting firings (default 0.0 — "
                        "pass ~1.0 with a tradexyz hedge)")
    p.add_argument("--midline", choices=("fixed", "auto"), default="fixed",
                   help="fixed: bands around the median premium (default); "
                        "auto: bands around the engine's moving EMA midline")
    p.add_argument("--window-min", type=float, default=60.0,
                   help="auto midline EMA time constant (minutes)")
    p.add_argument("--warmup-min", type=float, default=10.0,
                   help="auto midline history needed before counting")
    p.add_argument("--min-band", type=float, default=0.5,
                   help="auto: smallest band to suggest (bps), e.g. the "
                        "primary leg's median fill slippage")
    args = p.parse_args()

    try:
        rows = load_rows(args.csv, args.hours, args.min_samples)
    except FileNotFoundError:
        print(f"{args.csv} not found — run the bot (even --record-only) to "
              f"collect data first / 未找到数据文件，请先运行机器人采集数据",
              file=sys.stderr)
        sys.exit(1)
    if len(rows) < 30:
        print(f"only {len(rows)} usable minute(s) in {args.csv} — collect at "
              f"least a few hours before trusting the numbers / 数据太少，"
              f"建议至少采集数小时", file=sys.stderr)
        if not rows:
            sys.exit(1)

    span_h = (rows[-1]["ts"] - rows[0]["ts"]) / 3600.0 + 1 / 60.0
    prem = sorted(r["prem"] for r in rows)
    mean = sum(prem) / len(prem)
    var = sum((x - mean) ** 2 for x in prem) / len(prem)
    median = pctl(prem, 50)

    print(f"\n=== {args.csv}: {len(rows)} minutes over {span_h:.1f}h ===\n")
    print("premium of Entropy over hedge, minute close (bps) / "
          "Entropy 相对对冲腿的溢价:")
    print(f"  mean {mean:+.2f}   std {math.sqrt(var):.2f}   "
          f"median {median:+.2f}")
    print(f"  p5 {pctl(prem, 5):+.2f}   p25 {pctl(prem, 25):+.2f}   "
          f"p75 {pctl(prem, 75):+.2f}   p95 {pctl(prem, 95):+.2f}")

    if args.midline == "auto":
        print_auto(rows, args)
        return

    midline = round(median, 1) or 0.0   # normalize -0.0
    # room beyond the midline that was actually executable each minute, net
    # of taker fees (config thresholds are net-of-fee: the engine adds fees
    # on top, and recorded edges are pre-fee)
    fees = args.fees_bps
    sell_room = sorted((r["sell_max"] - midline - fees for r in rows),
                       reverse=True)
    buy_room = sorted((r["buy_max"] + midline - fees for r in rows),
                      reverse=True)

    print(f"\nwith midline_bps = {midline:+.1f} (median) and {fees:.1f} bps "
          f"round-trip taker fees, minutes each band would have fired / "
          f"各档净阈值触发的分钟数:")
    print(f"  {'band bps':>9} | {'SELL entropy':>17} | {'BUY entropy':>17}")
    print(f"  {'':>9} | {'minutes':>8} {'per day':>8} | "
          f"{'minutes':>8} {'per day':>8}")
    per_day = 24.0 / span_h if span_h > 0 else 0.0
    for t in CANDIDATES:
        s_hits = sum(1 for x in sell_room if x >= t)
        b_hits = sum(1 for x in buy_room if x >= t)
        print(f"  {t:>9.1f} | {s_hits:>8} {s_hits * per_day:>8.1f} | "
              f"{b_hits:>8} {b_hits * per_day:>8.1f}")

    # default suggestion: the band that fired in ~10% of minutes (p90 of the
    # fee-adjusted executable room), floored at 1 bps — tune from the table
    sug_upper = max(round(pctl(sorted(sell_room), 90) * 2) / 2, 1.0)
    sug_lower = max(round(pctl(sorted(buy_room), 90) * 2) / 2, 1.0)
    print(f"""
suggested starting point (fires ~10% of minutes, already net of the
{fees:.1f} bps fees passed via --fees-bps; a full round trip nets
>= upper+lower bps after fees) /
建议起点（约 10% 的分钟触发；已扣除 --fees-bps 传入的 {fees:.1f} bps 手续费，
一次完整往返扣费后净赚 >= upper+lower bps）:

thresholds:
  midline_bps: {midline}
  upper_bps: {sug_upper}
  lower_bps: {sug_lower}

Re-run with --hours to focus on recent regimes; premiums drift, so refresh
these numbers regularly. / 溢价中枢会漂移，请定期重新分析并更新配置。
""")


if __name__ == "__main__":
    main()
