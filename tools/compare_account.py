#!/usr/bin/env python3
"""Compare this bot's executions with a reference account (default: RH 6223
and its mainnet twin 397711) on the same signals. Read-only: public trade
history plus your trades.csv; no keys are used.

    python3 tools/compare_account.py --symbol BTC --trades logs/btc/trades.csv \
        --env-file 18.env --hours 6

It reports, for both accounts:
  * send -> match: from the moment an order leaves the client to its first
    fill on the exchange (yours from trades.csv send times, the reference's
    from client order ids that are millisecond timestamps)
and, on signals both accounts traded (same direction, within --window-ms):
  * how much later you decided than the reference sent its order
  * which RH order reached the book first
  * RH fill price versus your planned price, for you and the reference
  * the premium each account actually captured (RH vs mainnet fills)

Your own fills are matched to trades.csv by client order id, so the latency
is the exchange's match time, not the time your account stream reported it.
"""
from __future__ import annotations

import argparse
import csv
import json
import os
import sys
import time
import urllib.parse
import urllib.request
from dataclasses import dataclass, field
from typing import Dict, Iterable, List, Optional

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))

from entropy_arb.config import LIGHTER_PROFILES  # noqa: E402

RH_API = LIGHTER_PROFILES["lighter-rh"].api_url
MAIN_API = LIGHTER_PROFILES["lighter"].api_url


# ------------------------------------------------------------------ data

@dataclass
class Order:
    """One account's fills of one order (grouped by client order id)."""
    cid: int
    side: str                 # "B" or "S" (the account's side)
    first_ms: int
    last_ms: int
    qty: float = 0.0
    notional: float = 0.0
    taker: bool = True
    fills: int = 0

    @property
    def vwap(self) -> float:
        return self.notional / self.qty if self.qty else float("nan")

    @property
    def usd(self) -> float:
        return self.notional


def normalize(trades: Iterable[dict], account: int) -> List[dict]:
    """Public trade rows -> this account's side of each fill."""
    out = []
    for t in trades:
        try:
            ask, bid = int(t.get("ask_account_id")), int(t.get("bid_account_id"))
        except (TypeError, ValueError):
            continue
        if account not in (ask, bid):
            continue
        is_bid = bid == account
        maker_ask = bool(t.get("is_maker_ask"))
        cid = t.get("bid_client_id") if is_bid else t.get("ask_client_id")
        try:
            out.append({
                "ts": int(t["timestamp"]), "px": float(t["price"]),
                "qty": float(t["size"]), "side": "B" if is_bid else "S",
                "cid": int(cid) if cid not in (None, "") else -1,
                # the ask is the maker when is_maker_ask, else the bid is
                "taker": maker_ask if is_bid else not maker_ask,
            })
        except (KeyError, TypeError, ValueError):
            continue
    return out


def group_orders(fills: Iterable[dict]) -> Dict[tuple, Order]:
    orders: Dict[tuple, Order] = {}
    for f in fills:
        k = (f["cid"], f["side"])
        o = orders.get(k)
        if o is None:
            o = orders[k] = Order(f["cid"], f["side"], f["ts"], f["ts"])
        o.first_ms, o.last_ms = min(o.first_ms, f["ts"]), max(o.last_ms, f["ts"])
        o.qty += f["qty"]
        o.notional += f["qty"] * f["px"]
        o.taker = o.taker and f["taker"]
        o.fills += 1
    return orders


def cid_is_ms_clock(o: Order, tol_ms: int = 10_000) -> bool:
    """Reference accounts that use a millisecond timestamp as client order
    id: then first_ms - cid is their send -> match latency."""
    return o.cid > 10 ** 12 and 0 <= o.first_ms - o.cid <= tol_ms


def pct(xs: List[float], q: float) -> Optional[float]:
    if not xs:
        return None
    xs = sorted(xs)
    return xs[min(len(xs) - 1, int(q * len(xs)))]


def fmt(x: Optional[float], d: int = 0, unit: str = "") -> str:
    return "—" if x is None else f"{x:.{d}f}{unit}"


# ------------------------------------------------------------ fetching

def _get(url: str, params: dict, retries: int = 3) -> dict:
    q = urllib.parse.urlencode(params)
    req = urllib.request.Request(f"{url}?{q}",
                                 headers={"User-Agent": "entropy-arb-compare"})
    for attempt in range(retries):
        try:
            with urllib.request.urlopen(req, timeout=15) as r:
                return json.loads(r.read().decode())
        except Exception as e:
            if attempt == retries - 1:
                raise RuntimeError(f"GET {url}?{q} failed: {e}") from e
            time.sleep(1.0 + attempt)
    return {}


def market_id(api: str, symbol: str) -> int:
    data = _get(api + "/api/v1/orderBooks", {})
    for ob in data.get("order_books") or []:
        if ob.get("symbol") == symbol and ob.get("market_type", "perp") == "perp":
            return int(ob["market_id"])
    raise RuntimeError(f"{symbol} not found on {api}")


def fetch_trades(api: str, account: int, market: int, since_ms: int,
                 max_pages: int = 300) -> List[dict]:
    rows: List[dict] = []
    cursor = None
    for _ in range(max_pages):
        params = {"sort_by": "timestamp", "sort_dir": "desc", "limit": 100,
                  "account_index": account, "market_id": market}
        if cursor:
            params["cursor"] = cursor
        data = _get(api + "/api/v1/trades", params)
        page = data.get("trades") or []
        rows += page
        cursor = data.get("next_cursor")
        if not page or not cursor:
            break
        if min(int(t.get("timestamp") or 0) for t in page) < since_ms:
            break
        time.sleep(0.2)
    return [t for t in rows if int(t.get("timestamp") or 0) >= since_ms]


# ------------------------------------------------------------- analysis

@dataclass
class MyTrade:
    decision_ms: int
    rh_side: str            # side on RH
    rh_cid: int
    lt_cid: int
    rh_send_ms: Optional[int]
    lt_send_ms: Optional[int]
    rh_plan: float
    lt_plan: float
    rh_fill: Optional[float]
    lt_fill: Optional[float]
    midline: Optional[float]
    extra: dict = field(default_factory=dict)


def _f(r: dict, k: str) -> Optional[float]:
    try:
        v = r.get(k)
        return float(v) if v not in (None, "") else None
    except ValueError:
        return None


def load_my_trades(path: str, since_s: float, primary: str = "RH") -> List[MyTrade]:
    out = []
    with open(path, newline="") as fh:
        for r in csv.DictReader(fh):
            d = _f(r, "decision_ts")
            if d is None or d < since_s:
                continue
            if r.get("buy_status") != "filled" or r.get("sell_status") != "filled":
                continue
            rh_sells = r.get("sell_venue") == primary
            rh, lt = ("sell", "buy") if rh_sells else ("buy", "sell")

            def ms(k):
                v = _f(r, k)
                return int(v * 1000) if v is not None else None

            def cid(k):
                try:
                    return int(r.get(k) or -1)
                except ValueError:
                    return -1
            out.append(MyTrade(
                decision_ms=int(d * 1000), rh_side="S" if rh_sells else "B",
                rh_cid=cid(f"{rh}_client_order_id"),
                lt_cid=cid(f"{lt}_client_order_id"),
                rh_send_ms=ms(f"{rh}_send_ts"), lt_send_ms=ms(f"{lt}_send_ts"),
                rh_plan=float(r[f"{rh}_limit"]), lt_plan=float(r[f"{lt}_limit"]),
                rh_fill=_f(r, f"{rh}_avg_px"), lt_fill=_f(r, f"{lt}_avg_px"),
                midline=_f(r, "midline_bps")))
    return out


def slip_bps(side: str, plan: float, fill: float) -> float:
    """+ = worse than plan (paid more on a buy, received less on a sell)."""
    return (fill / plan - 1) * 1e4 if side == "B" else (1 - fill / plan) * 1e4


def analyze(mine: List[MyTrade], my_rh: Dict[tuple, Order],
            my_lt: Dict[tuple, Order], ref_rh: Dict[tuple, Order],
            ref_lt: Dict[tuple, Order], window_ms: int = 1000) -> dict:
    res: dict = {}
    # --- send -> match latency
    by_cid_rh = {o.cid: o for o in my_rh.values()}
    by_cid_lt = {o.cid: o for o in my_lt.values()}
    my_rh_lat = [by_cid_rh[t.rh_cid].first_ms - t.rh_send_ms for t in mine
                 if t.rh_cid in by_cid_rh and t.rh_send_ms]
    my_lt_lat = [by_cid_lt[t.lt_cid].first_ms - t.lt_send_ms for t in mine
                 if t.lt_cid in by_cid_lt and t.lt_send_ms]
    ref_rh_t = [o for o in ref_rh.values() if o.taker and cid_is_ms_clock(o)]
    ref_lt_t = [o for o in ref_lt.values() if o.taker and cid_is_ms_clock(o)]
    res["latency"] = {
        "mine_rh": my_rh_lat, "mine_lt": my_lt_lat,
        "ref_rh": [o.first_ms - o.cid for o in ref_rh_t],
        "ref_lt": [o.first_ms - o.cid for o in ref_lt_t]}
    res["matched_my_rh"] = len(my_rh_lat)
    # --- shared signals
    rows = []
    for t in mine:
        cands = [o for o in ref_rh_t if o.side == t.rh_side
                 and abs(o.cid - t.decision_ms) <= window_ms]
        if not cands:
            continue
        ref = min(cands, key=lambda o: abs(o.cid - t.decision_ms))
        row = {"decision_ms": t.decision_ms, "side": t.rh_side,
               "ref_usd": ref.usd,
               "decide_after_ref_send_ms": t.decision_ms - ref.cid,
               "send_after_ref_ms": (t.rh_send_ms - ref.cid
                                     if t.rh_send_ms else None)}
        mo = by_cid_rh.get(t.rh_cid)
        row["ref_matched_first_ms"] = (mo.first_ms - ref.first_ms
                                       if mo else None)
        if t.rh_fill:
            row["my_rh_slip"] = slip_bps(t.rh_side, t.rh_plan, t.rh_fill)
        row["ref_rh_slip"] = slip_bps(t.rh_side, t.rh_plan, ref.vwap)
        lt_side = "S" if t.rh_side == "B" else "B"
        hedges = [o for o in ref_lt_t if o.side == lt_side
                  and abs(o.cid - ref.cid) <= 50]
        if t.rh_fill and t.lt_fill:
            row["my_prem"] = (t.rh_fill / t.lt_fill - 1) * 1e4
        if hedges:
            h = min(hedges, key=lambda o: abs(o.cid - ref.cid))
            row["ref_prem"] = (ref.vwap / h.vwap - 1) * 1e4
        row["midline"] = t.midline
        rows.append(row)
    res["shared"] = rows
    return res


def report(res: dict, n_mine: int, n_ref_rh: int) -> None:
    lat = res["latency"]
    print(f"your filled executions in window: {n_mine} "
          f"(RH leg found in public trades: {res['matched_my_rh']})")
    print(f"reference RH taker orders with clock ids: {len(lat['ref_rh'])} "
          f"(of {n_ref_rh} orders)\n")
    print("send -> first fill on the exchange (ms)      p25    p50    p90   n")
    for k, name in (("mine_rh", "you        RH"), ("ref_rh", "reference  RH"),
                    ("mine_lt", "you        LIGHTER"),
                    ("ref_lt", "reference  LIGHTER")):
        xs = lat[k]
        print(f"  {name:<40} {fmt(pct(xs, .25)):>6} {fmt(pct(xs, .5)):>6} "
              f"{fmt(pct(xs, .9)):>6} {len(xs):>4}")
    rows = res["shared"]
    print(f"\nsignals traded by both (same direction, ids within window): "
          f"{len(rows)}")
    if not rows:
        return

    def col(k):
        return [r[k] for r in rows if r.get(k) is not None]
    d = col("decide_after_ref_send_ms")
    s = col("send_after_ref_ms")
    first = col("ref_matched_first_ms")
    print(f"  you decided after the reference sent:  p25 {fmt(pct(d, .25))}  "
          f"p50 {fmt(pct(d, .5))}  p90 {fmt(pct(d, .9))} ms  "
          f"(you first in {sum(1 for x in d if x < 0)}/{len(d)})")
    print(f"  your RH order sent after the reference's: p50 {fmt(pct(s, .5))} ms")
    if first:
        print(f"  your RH order matched after the reference's: p50 "
              f"{fmt(pct(first, .5))} ms (you matched first in "
              f"{sum(1 for x in first if x < 0)}/{len(first)})")
    for k, name in (("my_rh_slip", "you"), ("ref_rh_slip", "reference")):
        xs = col(k)
        if xs:
            print(f"  RH fill vs your plan, {name:<9} mean {sum(xs)/len(xs):+.2f} "
                  f"bps (+ = worse)")
    pairs = [(r["my_prem"], r["ref_prem"], r["side"], r["midline"]) for r in rows
             if "my_prem" in r and "ref_prem" in r]
    if pairs:
        sg = lambda side: -1 if side == "B" else 1  # noqa: E731
        mine = [sg(sd) * p for p, _, sd, _ in pairs]
        ref = [sg(sd) * q for _, q, sd, _ in pairs]
        print(f"  premium captured in the trade direction on {len(pairs)} "
              f"pairs: you {sum(mine)/len(mine):+.2f} bps, reference "
              f"{sum(ref)/len(ref):+.2f} bps")


def write_csv(path: str, rows: List[dict]) -> None:
    if not rows:
        return
    keys = sorted({k for r in rows for k in r})
    with open(path, "w", newline="") as fh:
        w = csv.DictWriter(fh, fieldnames=keys)
        w.writeheader()
        w.writerows(rows)


def main() -> None:
    p = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    p.add_argument("--symbol", required=True)
    p.add_argument("--trades", required=True, help="your trades.csv")
    p.add_argument("--hours", type=float, default=6.0)
    p.add_argument("--env-file", default=None,
                   help="read your account indices from this .env")
    p.add_argument("--my-rh", type=int, default=None)
    p.add_argument("--my-main", type=int, default=None)
    p.add_argument("--ref-rh", type=int, default=6223)
    p.add_argument("--ref-main", type=int, default=397711)
    p.add_argument("--window-ms", type=int, default=1000)
    p.add_argument("--out", default=None, help="write shared signals CSV")
    a = p.parse_args()

    if a.env_file:
        from dotenv import dotenv_values
        env = dotenv_values(a.env_file)
        a.my_rh = a.my_rh or int(env.get("LIGHTER_RH_ACCOUNT_INDEX") or 0) or None
        a.my_main = a.my_main or int(env.get("LIGHTER_ACCOUNT_INDEX") or 0) or None
    if not (a.my_rh and a.my_main):
        p.error("give --my-rh and --my-main, or --env-file with "
                "LIGHTER_RH_ACCOUNT_INDEX / LIGHTER_ACCOUNT_INDEX")

    since_s = time.time() - a.hours * 3600
    since_ms = int(since_s * 1000)
    mine = load_my_trades(a.trades, since_s)
    rh_m, main_m = market_id(RH_API, a.symbol), market_id(MAIN_API, a.symbol)
    print(f"{a.symbol}: RH market {rh_m}, mainnet market {main_m} | you "
          f"{a.my_rh}/{a.my_main} vs reference {a.ref_rh}/{a.ref_main} | "
          f"last {a.hours:g} h\n")
    my_rh = group_orders(normalize(fetch_trades(RH_API, a.my_rh, rh_m, since_ms), a.my_rh))
    my_lt = group_orders(normalize(fetch_trades(MAIN_API, a.my_main, main_m, since_ms), a.my_main))
    ref_rh = group_orders(normalize(fetch_trades(RH_API, a.ref_rh, rh_m, since_ms), a.ref_rh))
    ref_lt = group_orders(normalize(fetch_trades(MAIN_API, a.ref_main, main_m, since_ms), a.ref_main))
    res = analyze(mine, my_rh, my_lt, ref_rh, ref_lt, a.window_ms)
    report(res, len(mine), len(ref_rh))
    if a.out:
        write_csv(a.out, res["shared"])
        print(f"\nshared signals written to {a.out}")


if __name__ == "__main__":
    main()
