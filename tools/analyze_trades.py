#!/usr/bin/env python3
"""Summarize execution latency from the enriched trade CSV.

Usage:
    python3 tools/analyze_trades.py --csv logs/lit/trades.csv
    python3 tools/analyze_trades.py --csv logs/lit/trades.csv --target-ms 350
"""
from __future__ import annotations

import argparse
import csv
import math
import statistics
import sys


def percentile(values: list[float], q: float) -> float:
    if not values:
        return float("nan")
    values = sorted(values)
    k = (len(values) - 1) * q
    lo, hi = math.floor(k), math.ceil(k)
    if lo == hi:
        return values[lo]
    return values[lo] + (values[hi] - values[lo]) * (k - lo)


def metric(rows: list[dict[str, str]], field: str) -> list[float]:
    vals = []
    for row in rows:
        try:
            value = float(row.get(field, ""))
        except (TypeError, ValueError):
            continue
        if math.isfinite(value):
            vals.append(value)
    return vals


def show(label: str, values: list[float], target: float) -> None:
    if not values:
        print(f"{label}: no values")
        return
    under = sum(v <= target for v in values) / len(values) * 100.0
    print(f"{label}: n={len(values)} mean={statistics.fmean(values):.1f}ms "
          f"p50={percentile(values, 50):.1f}ms "
          f"p95={percentile(values, 95):.1f}ms "
          f"p99={percentile(values, 99):.1f}ms "
          f"<={target:.0f}ms={under:.1f}%")


def main() -> None:
    ap = argparse.ArgumentParser(description="summarize trade latency")
    ap.add_argument("--csv", default="logs/trades.csv")
    ap.add_argument("--target-ms", type=float, default=350.0)
    args = ap.parse_args()
    try:
        with open(args.csv, newline="") as fh:
            rows = list(csv.DictReader(fh))
    except FileNotFoundError:
        print(f"{args.csv} not found", file=sys.stderr)
        raise SystemExit(1)
    if not rows:
        print("no trade rows")
        return
    print(f"{args.csv}: {len(rows)} trade rows")
    for side in ("buy", "sell"):
        show(f"{side} submit", metric(rows, f"{side}_submit_latency_ms"),
             args.target_ms)
        show(f"{side} confirm", metric(rows, f"{side}_confirm_latency_ms"),
             args.target_ms)
        show(f"{side} total", metric(rows, f"{side}_total_latency_ms"),
             args.target_ms)
    show("pair total", metric(rows, "pair_total_latency_ms"), args.target_ms)
    print("Use submit latency to tune the client/order path; confirm latency "
          "also includes exchange settlement and network return time.")


if __name__ == "__main__":
    main()
