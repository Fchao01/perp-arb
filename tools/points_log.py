#!/usr/bin/env python3
"""Log the account's Lighter RH live points next to its positions and volume.

The points program does not publish its weights. This tool samples, once per
--interval-min, the account's live points total (authenticated read) together
with what the account held and traded, so a week or two of rows shows how
many points a dollar of position and a dollar of volume are worth.

Read-only: it never sends an order. It signs an auth token with the same
LIGHTER_RH_* API key the bot uses (read from --env-file) and calls
    GET /api/v1/livePoints/total      (points, needs the auth token)
    GET /api/v1/account               (positions, public)
    GET /api/v1/pnl                   (today's volume, public)
Run ONE copy per account (not one per market):

    nohup python3 tools/points_log.py --env-file 1.env \\
        > logs/points_log.out 2>&1 < /dev/null &

Columns: time_utc, ts, live_points, points_delta, gross_usd, long_usd,
short_usd, boost_side_usd (positions on --boost-side, the side the hedge
boost currently rewards on RH), volume_today_usd, positions.

中文：每隔 --interval-min 分钟记录一次账户的 RH 实时积分，以及当时的持仓和
当日成交额，用来反推"每 1 美元持仓、每 1 美元成交各值多少分"。只读，不下单；
用机器人同一把 LIGHTER_RH_* API key 签一个查询令牌。每个账户只跑一个。
"""
from __future__ import annotations

import argparse
import asyncio
import csv
import logging
import os
import sys
import time
from datetime import datetime, timezone
from typing import Optional

import aiohttp
from dotenv import load_dotenv

log = logging.getLogger("points_log")

RH_API = "https://api.rh.lighter.xyz"
RH_CHAIN_ID = 466324
HEADER = ["time_utc", "ts", "live_points", "points_delta", "gross_usd",
          "long_usd", "short_usd", "boost_side_usd", "volume_today_usd",
          "positions"]


def summarize_positions(account: dict, boost_side: str) -> dict:
    """Totals of an /account entry's open positions, in USD."""
    long_usd = short_usd = 0.0
    parts = []
    for p in account.get("positions") or []:
        try:
            value = float(p.get("position_value") or 0.0)
            size = float(p.get("position") or 0.0)
        except (TypeError, ValueError):
            continue
        if size == 0.0 or value == 0.0:
            continue
        sign = 1 if int(p.get("sign") or 1) >= 0 else -1
        if sign > 0:
            long_usd += value
        else:
            short_usd += value
        parts.append((value, f"{p.get('symbol')}{'+' if sign > 0 else '-'}"
                             f"{value:.0f}"))
    parts.sort(reverse=True)
    return {
        "gross_usd": long_usd + short_usd,
        "long_usd": long_usd,
        "short_usd": short_usd,
        "boost_side_usd": long_usd if boost_side == "long" else short_usd,
        "positions": " ".join(s for _, s in parts),
    }


def volume_today(pnl: dict) -> Optional[float]:
    """Volume of the newest daily bar of a /pnl reply."""
    bars = pnl.get("pnl") or []
    if not bars:
        return None
    try:
        return float(max(bars, key=lambda b: b.get("timestamp", 0))["volume"])
    except (KeyError, TypeError, ValueError):
        return None


def last_points(path: str) -> Optional[float]:
    """live_points of the last row already in the CSV (for points_delta)."""
    try:
        with open(path, newline="") as fh:
            rows = list(csv.DictReader(fh))
    except OSError:
        return None
    for row in reversed(rows):
        try:
            return float(row["live_points"])
        except (KeyError, TypeError, ValueError):
            continue
    return None


def append_row(path: str, row: dict) -> None:
    os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
    new = not os.path.exists(path) or os.path.getsize(path) == 0
    with open(path, "a", newline="") as fh:
        w = csv.DictWriter(fh, fieldnames=HEADER)
        if new:
            w.writeheader()
        w.writerow(row)


def make_signer(account_index: int, key_index: int, private_key: str):
    try:
        from lighter import SignerClient
    except ImportError as e:
        raise SystemExit("needs the Lighter SDK: pip install -r "
                         "requirements-live.txt") from e
    signer = SignerClient(url=RH_API, account_index=account_index,
                          api_private_keys={key_index: private_key},
                          chain_id=RH_CHAIN_ID)
    err = signer.check_client()
    if err is not None:
        raise SystemExit(f"API key check failed: {err}")
    return signer


async def _get(session, path: str, params: dict, headers=None) -> dict:
    async with session.get(RH_API + path, params=params, headers=headers,
                           timeout=aiohttp.ClientTimeout(total=15)) as r:
        body = await r.json(content_type=None)
        if r.status != 200:
            raise RuntimeError(f"{path} -> HTTP {r.status}: {str(body)[:200]}")
        return body


async def sample(session, signer, account_index: int, boost_side: str) -> dict:
    auth, err = signer.create_auth_token_with_expiry()
    if err is not None:
        raise RuntimeError(f"auth token: {err}")
    pts = await _get(session, "/api/v1/livePoints/total",
                     {"account_index": str(account_index)},
                     {"Authorization": auth})
    if "total_live_points" not in pts:
        raise RuntimeError(f"livePoints/total: {str(pts)[:200]}")
    acct = await _get(session, "/api/v1/account",
                      {"by": "index", "value": str(account_index)})
    now_ms = int(time.time() * 1000)
    pnl = await _get(session, "/api/v1/pnl",
                     {"by": "index", "value": str(account_index),
                      "resolution": "1d", "count_back": "2",
                      "start_timestamp": str(now_ms - 2 * 86400_000),
                      "end_timestamp": str(now_ms)})
    accounts = acct.get("accounts") or [{}]
    out = summarize_positions(accounts[0], boost_side)
    out["live_points"] = float(pts["total_live_points"])
    out["volume_today_usd"] = volume_today(pnl)
    return out


async def run(args) -> None:
    if not os.path.isfile(args.env_file):
        raise SystemExit(f"{args.env_file} not found in {os.getcwd()} — use "
                         f"an env file of a bot running on this server / "
                         f"本目录下没有 {args.env_file}，请用这台服务器上某个"
                         f"在跑的市场的 env 文件")
    load_dotenv(args.env_file)
    try:
        account_index = int(os.environ["LIGHTER_RH_ACCOUNT_INDEX"])
        key_index = int(os.environ["LIGHTER_RH_API_KEY_INDEX"])
        private_key = os.environ["LIGHTER_RH_API_PRIVATE_KEY"].strip()
    except (KeyError, ValueError):
        raise SystemExit(f"LIGHTER_RH_ACCOUNT_INDEX / LIGHTER_RH_API_KEY_INDEX "
                         f"/ LIGHTER_RH_API_PRIVATE_KEY not set in "
                         f"{args.env_file}")
    signer = make_signer(account_index, key_index, private_key)
    prev = last_points(args.csv)
    log.info("account %d -> %s every %g min", account_index, args.csv,
             args.interval_min)
    async with aiohttp.ClientSession() as session:
        while True:
            try:
                s = await sample(session, signer, account_index,
                                 args.boost_side)
                ts = time.time()
                pts = s["live_points"]
                vol = s["volume_today_usd"]
                append_row(args.csv, {
                    "time_utc": datetime.fromtimestamp(ts, timezone.utc)
                    .strftime("%Y-%m-%dT%H:%M:%SZ"),
                    "ts": f"{ts:.0f}",
                    "live_points": f"{pts:.4f}",
                    "points_delta": ("" if prev is None
                                     else f"{pts - prev:.4f}"),
                    "gross_usd": f"{s['gross_usd']:.0f}",
                    "long_usd": f"{s['long_usd']:.0f}",
                    "short_usd": f"{s['short_usd']:.0f}",
                    "boost_side_usd": f"{s['boost_side_usd']:.0f}",
                    "volume_today_usd": "" if vol is None else f"{vol:.0f}",
                    "positions": s["positions"],
                })
                log.info("points %.2f (%s) | positions $%.0f, %s side $%.0f"
                         " | volume today %s", pts,
                         "first row" if prev is None else f"{pts - prev:+.2f}",
                         s["gross_usd"], args.boost_side, s["boost_side_usd"],
                         "—" if vol is None else f"${vol:,.0f}")
                prev = pts
            except asyncio.CancelledError:
                raise
            except Exception as e:
                log.warning("sample failed, will retry next interval: %s", e)
            if args.once:
                return
            await asyncio.sleep(args.interval_min * 60.0)


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--env-file", default=".env",
                    help="file with the LIGHTER_RH_* API key")
    ap.add_argument("--csv", default="logs/points/points.csv")
    ap.add_argument("--interval-min", type=float, default=60.0)
    ap.add_argument("--boost-side", choices=("long", "short"), default="long",
                    help="RH side the hedge boost rewards this week "
                         "(only labels the boost_side_usd column)")
    ap.add_argument("--once", action="store_true",
                    help="take one sample and exit")
    args = ap.parse_args()
    logging.basicConfig(level=logging.INFO,
                        format="%(asctime)s %(levelname)-7s %(message)s")
    try:
        asyncio.run(run(args))
    except KeyboardInterrupt:
        sys.exit(0)


if __name__ == "__main__":
    main()
