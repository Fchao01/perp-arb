"""Engine signal math: midline band directions, inventory ladder, scan.

Run:  python3 -m pytest tests/  (or  python3 tests/test_engine.py)
"""
import asyncio
import os
import sys
import tempfile

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from entropy_arb.book import ArbPlan, OrderBook  # noqa: E402
from entropy_arb.config import load_config  # noqa: E402
from entropy_arb.engine import Engine  # noqa: E402

NO_ENV = os.path.join(tempfile.gettempdir(), "entropy-arb-no-such.env")


def make_cfg(midline=5.0, upper=4.0, lower=3.0, execution=""):
    f = tempfile.NamedTemporaryFile("w", suffix=".yaml", delete=False)
    f.write(f"""
thresholds:
  midline_bps: {midline}
  upper_bps: {upper}
  lower_bps: {lower}
execution:
  premium_persist_sec: 0.0
{execution}
""")
    f.close()
    return load_config(f.name, NO_ENV,
                       symbol="SNDK", hedge_venue="lighter-rh", primary_venue="entropy")


class StubVenue:
    def __init__(self, key, label, cap=10000.0, fee=0.0):
        self.key, self.name = key, label
        self.cap_usd, self.fee_bps = cap, fee
        self.size_decimals, self.min_base, self.min_quote = 4, 1e-4, 10.0
        self.position, self.cash = 0.0, 0.0
        self.orders_per_min = 30
        self.last_traded_ts = 0.0
        self.book = OrderBook()

    def ready_to_trade(self):
        return True

    def set_book(self, bid, ask, sz=50.0):
        self.book.apply_hl([[{"px": str(bid), "sz": str(sz)}],
                            [{"px": str(ask), "sz": str(sz)}]])


def make_engine(**thr):
    cfg = make_cfg(**thr)
    eng = Engine(cfg)
    eng.entropy = StubVenue("entropy", "ENTROPY")
    eng.hedge = StubVenue("hedge", "RH")
    eng.venues = {"entropy": eng.entropy, "hedge": eng.hedge}
    eng._step, eng._min_base, eng._min_notional = 1e-4, 1e-4, 10.0
    return eng


def approx(a, b, tol=1e-9):
    assert abs(a - b) <= tol, f"{a} != {b}"


def test_eff_threshold_directions():
    eng = make_engine(midline=5.0, upper=4.0, lower=3.0)
    e, h = eng.entropy, eng.hedge
    # sell entropy: hurdle = midline + upper = 9
    approx(eng._eff_threshold(buy=h, sell=e), 9.0)
    # buy entropy: hurdle = lower - midline = -2 (unwind side of a positive
    # midline is deliberately cheap — that's what completes the round trip)
    approx(eng._eff_threshold(buy=e, sell=h), -2.0)
    # round trip nets upper + lower regardless of midline sign
    for m in (-7.0, 0.0, 12.5):
        eng.cfg.midline_bps = m
        total = eng._eff_threshold(buy=h, sell=e) + eng._eff_threshold(buy=e, sell=h)
        approx(total, 7.0)


def test_inventory_ladder():
    eng = make_engine()
    eng.cfg.inventory_scale_bps, eng.cfg.inventory_floor_frac = 10.0, 0.5
    e, h = eng.entropy, eng.hedge
    e.set_book(99.9, 100.1)   # mid 100
    h.set_book(99.9, 100.1)
    approx(eng._inv_add_bps(e, h), 0.0)          # flat: dead zone
    e.position = 90.0                             # long $9k of $10k cap
    v = eng._inv_add_bps(e, h)                    # buying entropy adds long
    assert 7.5 < v < 8.5, v                       # u=0.9 -> ~+8
    approx(eng._inv_add_bps(h, e), 0.0)           # selling entropy reduces
    h.position = -90.0                            # hedge short $9k too
    v2 = eng._inv_add_bps(e, h)                   # both legs add -> max()
    assert abs(v2 - v) < 0.6, (v, v2)             # max, not sum


def run_scan(eng):
    async def go():
        # first pass arms the direction, second passes the persistence gate
        # (premium_persist_sec is 0 in the test config)
        eng._scan(__import__("time").time())
        return eng._scan(__import__("time").time())
    return asyncio.run(go())


def test_scan_fires_sell_entropy_above_band():
    eng = make_engine(midline=5.0, upper=4.0, lower=3.0)
    # entropy 15 bps rich vs hedge: above midline+upper=9 -> sell entropy
    eng.entropy.set_book(100.14, 100.16)
    eng.hedge.set_book(99.99, 100.01)
    best = run_scan(eng)
    assert best is not None
    buy, sell, plan = best
    assert sell.key == "entropy" and buy.key == "hedge"
    assert plan.exp_edge_usd > 0


def test_scan_quiet_inside_band():
    eng = make_engine(midline=5.0, upper=4.0, lower=3.0)
    # entropy 5 bps rich = exactly on the midline: inside the band, no trade
    eng.entropy.set_book(100.04, 100.06)
    eng.hedge.set_book(99.99, 100.01)
    assert run_scan(eng) is None


def test_scan_fires_buy_entropy_below_band():
    eng = make_engine(midline=5.0, upper=4.0, lower=3.0)
    # entropy 5 bps CHEAP (premium -5): below midline-lower=+2 -> buy entropy
    eng.entropy.set_book(99.94, 99.96)
    eng.hedge.set_book(99.99, 100.01)
    best = run_scan(eng)
    assert best is not None
    buy, sell, plan = best
    assert buy.key == "entropy" and sell.key == "hedge"


def make_auto_engine(recorder_csv=None, warmup_min=10.0, window_min=60.0):
    cfg = make_cfg(midline="auto", upper=4.0, lower=3.0)
    cfg.midline_warmup_min, cfg.midline_window_min = warmup_min, window_min
    if recorder_csv:
        cfg.recorder_csv = recorder_csv
    eng = Engine(cfg)
    eng.entropy = StubVenue("entropy", "ENTROPY")
    eng.hedge = StubVenue("hedge", "RH")
    eng.venues = {"entropy": eng.entropy, "hedge": eng.hedge}
    eng._step, eng._min_base, eng._min_notional = 1e-4, 1e-4, 10.0
    return eng


def test_fixed_midline_property_reads_config():
    eng = make_engine(midline=5.0)
    assert eng.midline == 5.0
    eng.cfg.midline_bps = -2.0
    assert eng.midline == -2.0


def test_auto_midline_blocks_trading_until_warm():
    import time
    eng = make_auto_engine(warmup_min=10.0)
    eng.entropy.set_book(100.14, 100.16)   # far above any band
    eng.hedge.set_book(99.99, 100.01)
    assert eng.midline is None and run_scan(eng) is None
    now = time.time()
    eng.center.update(now - 700, 15.0)     # warm for 700 s > 600 s
    eng.center.update(now, 15.0)
    approx(eng.midline, 15.0)
    # premium ~+15 sits on the auto midline: inside the band, no trade
    assert run_scan(eng) is None


def test_auto_midline_band_follows_the_center():
    import time
    eng = make_auto_engine(warmup_min=0.0)
    eng.center.update(time.time(), -2.0)   # basis center at -2 bps
    approx(eng._eff_threshold(eng.hedge, eng.entropy), -2.0 + 4.0)
    approx(eng._eff_threshold(eng.entropy, eng.hedge), 3.0 + 2.0)
    # entropy ask 7 bps under hedge bid: clears lower - midline = 5 -> buy
    eng.entropy.set_book(99.90, 99.92)
    eng.hedge.set_book(99.99, 100.01)
    best = run_scan(eng)
    assert best is not None and best[0].key == "entropy"


def test_seed_center_uses_recent_minutes_only():
    import time
    now = int(time.time()) // 60 * 60
    f = tempfile.NamedTemporaryFile("w", suffix=".csv", delete=False)
    f.write("minute_ts,premium_mean_bps\n")
    f.write(f"{now - 6 * 3600},50.0\n")          # outside the window
    for i in range(30, 0, -1):
        f.write(f"{now - 60 * i},-2.0\n")
    f.write(f"{now - 120},bad\n")
    f.close()
    eng = make_auto_engine(recorder_csv=f.name, warmup_min=10.0)
    assert eng._seed_center() == 30
    approx(eng.center.value, -2.0)
    assert eng.midline is not None              # 30 seeded minutes > warmup
    missing = make_auto_engine(recorder_csv=f.name + ".nope")
    assert missing._seed_center() == 0 and missing.midline is None


def test_scan_respects_position_caps():
    eng = make_engine(midline=0.0, upper=1.0, lower=1.0)
    eng.entropy.set_book(100.14, 100.16)
    eng.hedge.set_book(99.99, 100.01)
    eng.entropy.position = -100.0   # entropy already short at its cap
    eng.entropy.cap_usd = 10000.0
    eng.hedge.position = 100.0
    eng.hedge.cap_usd = 10000.0
    assert run_scan(eng) is None


class OrderVenue(StubVenue):
    """StubVenue that records orders and fills the given quantity."""

    def __init__(self, key, label, fills):
        super().__init__(key, label)
        self.fills = list(fills)   # one result per order: qty, or a dict
        self.orders = []
        self.volume_usd = 0.0

    def px_round(self, px, round_up):
        return round(px, 2)

    async def send_taker(self, **kw):
        self.orders.append(kw)
        r = self.fills.pop(0)
        if isinstance(r, dict):
            return r
        return {"status": "filled" if r >= kw["qty"] else "canceled",
                "filled_base": r, "avg_px": kw["limit_px"] if r else None,
                "err": None, "unresolved": False}


def run_execute(lead, rh_fills, lighter_fills, qty=1.0):
    """sell RH (primary) / buy LIGHTER (hedge) at a 10 bps planned edge."""
    cfg = make_cfg(execution=f"""  lead_venue: {lead}
  lead_slippage_bps: 0.0
  leg_slippage_bps: 10.0""")
    cfg.trades_csv = os.path.join(tempfile.mkdtemp(), "trades.csv")
    eng = Engine(cfg)
    rh = OrderVenue("entropy", "RH", rh_fills)
    lt = OrderVenue("hedge", "LIGHTER", lighter_fills)
    eng.entropy, eng.hedge = rh, lt
    eng.venues = {"entropy": rh, "hedge": lt}
    eng._step = 1e-4
    plan = ArbPlan(qty, 100.0, 100.1, 100.0 * qty, 100.1 * qty, qty,
                   100.0 * qty, 10.0, 10.0, 0.0, 0.0)
    unresolved = asyncio.run(eng._execute(lt, rh, plan))
    return eng, rh, lt, unresolved


def test_sequential_lead_fills_then_follower_takes_lead_fill():
    eng, rh, lt, unresolved = run_execute("primary", [1.0], [1.0])
    assert not unresolved
    lead = rh.orders[0]
    # lead: tight IOC limit at the planned price, no slippage allowance
    assert lead["limit_ioc"] is True and lead["limit_px"] == 100.1
    assert lead["is_buy"] is False and lead["qty"] == 1.0
    # follower: same size, normal leg slippage protection
    assert lt.orders[0]["qty"] == 1.0 and lt.orders[0]["limit_ioc"] is False
    assert lt.orders[0]["limit_px"] == round(100.0 * 1.001, 2)
    assert eng.trades == 1 and eng.consec_errors == 0
    assert rh.position == -1.0 and lt.position == 1.0
    assert eng.recent_trades[-1]["ok"]


def test_sequential_lead_miss_sends_nothing_else():
    eng, rh, lt, _ = run_execute("primary", [0.0], [])
    assert len(rh.orders) == 1 and lt.orders == []
    assert eng.trades == 0 and eng.total_exp_edge == 0.0
    assert eng.consec_errors == 0           # a miss is not an execution fault
    assert rh.position == lt.position == 0.0
    assert eng.recent_trades[-1]["miss"]
    assert lt.last_traded_ts == 0.0         # follower never touched


def test_sequential_partial_lead_sizes_follower():
    eng, rh, lt, _ = run_execute("primary", [0.4], [0.4])
    assert lt.orders[0]["qty"] == 0.4
    assert eng.trades == 1
    # expected edge counted only for the matched size
    assert abs(eng.total_exp_edge - 0.1 * 0.4) < 1e-9


def test_sequential_unresolved_lead_skips_follower():
    timeout = {"status": "timeout", "filled_base": 0.0, "avg_px": None,
               "err": None, "unresolved": True}
    eng, rh, lt, unresolved = run_execute("primary", [timeout], [])
    assert unresolved and lt.orders == []
    assert eng.consec_errors == 1


def test_one_leg_fill_counts_as_execution_problem():
    eng, rh, lt, _ = run_execute("primary", [1.0], [0.0])
    assert eng.consec_errors == 1
    assert not eng.recent_trades[-1]["ok"]
    assert eng.trades == 0 and eng.total_exp_edge == 0.0
    # concurrent mode: same rule, both legs sent at once
    eng, rh, lt, _ = run_execute("none", [0.0], [1.0])
    assert len(rh.orders) == len(lt.orders) == 1
    assert rh.orders[0]["limit_ioc"] is False
    assert eng.consec_errors == 1


if __name__ == "__main__":
    for name, fn in sorted(globals().items()):
        if name.startswith("test_"):
            fn()
            print(f"{name:40s} OK")
