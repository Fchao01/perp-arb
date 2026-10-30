"""Maker mode: quote math, guards, fills -> hedges, shutdown, reconcile.

Venues are in-memory fakes that record every order action, so these tests
exercise the real MakerEngine logic without any network or SDK.

Run:  python3 -m pytest tests/test_maker.py
"""
import asyncio
import csv
import math
import os
import sys
import tempfile
from datetime import datetime, timezone

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from entropy_arb.book import OrderBook  # noqa: E402
from entropy_arb.center import CenterTracker  # noqa: E402
from entropy_arb.config import ConfigError, load_config  # noqa: E402
from entropy_arb.maker import (Blackout, MakerEngine, WindowRange,  # noqa: E402
                               maker_edge_bps, quote_prices, round_to_tick)

NO_ENV = os.path.join(tempfile.gettempdir(), "entropy-arb-no-such.env")


# ------------------------------------------------------------------ fakes

class FakeFeed:
    def __init__(self):
        self.listeners = []
        self.is_ready = True


class FakeVenue:
    kind = "lighter"

    def __init__(self, key, name, price_decimals=1, size_decimals=5,
                 min_base=0.00007, min_quote=10.0, fee=0.0):
        self.key, self.name = key, name
        self.price_decimals, self.size_decimals = price_decimals, size_decimals
        self.min_base, self.min_quote = min_base, min_quote
        self.fee_bps, self.cap_usd, self.orders_per_min = fee, 1e9, 1000
        self.position = self.cash = self.volume_usd = 0.0
        self.equity = self.free = self.start_equity = None
        self.last_traded_ts = 0.0
        self.book = OrderBook()
        self.orders_feed = FakeFeed()
        self.calls = []
        self.fail = {}          # method -> error text (sticky)
        self.chain_position = None
        self._coi = 1000
        self.taker_fill_frac = 1.0

    def set_book(self, bid, ask, sz=5.0):
        self.book.apply_lighter({"bids": [{"price": str(bid), "size": str(sz)}],
                                 "asks": [{"price": str(ask), "size": str(sz)}]},
                                snapshot=True)

    def ready_to_trade(self):
        return self.orders_feed.is_ready

    def px_round(self, px, round_up):
        f = 10 ** self.price_decimals
        v = math.ceil(px * f - 1e-9) / f if round_up else math.floor(px * f + 1e-9) / f
        return round(v, 8)

    def next_coi(self):
        self._coi += 1
        return self._coi

    async def place_post_only(self, *, coi, is_buy, qty, px):
        self.calls.append(("place", coi, is_buy, qty, px))
        return self.fail.get("place")

    async def modify_resting(self, *, coi, qty, px):
        self.calls.append(("modify", coi, qty, px))
        return self.fail.get("modify")

    async def cancel_resting(self, coi):
        self.calls.append(("cancel", coi))
        return self.fail.get("cancel")

    async def cancel_all_resting(self):
        self.calls.append(("cancel_all",))
        return self.fail.get("cancel_all")

    async def schedule_cancel_all(self, at_ms):
        self.calls.append(("schedule", at_ms))
        return self.fail.get("schedule")

    async def send_taker(self, *, is_buy, qty, limit_px, reduce_only=False,
                         limit_ioc=False):
        self.calls.append(("taker", is_buy, qty, limit_px))
        if "taker" in self.fail:
            return {"status": "send-failed", "filled_base": 0.0,
                    "avg_px": None, "err": self.fail["taker"],
                    "unresolved": False}
        px = self.book.best_ask() if is_buy else self.book.best_bid()
        return {"status": "filled", "filled_base": qty * self.taker_fill_frac,
                "avg_px": px, "err": None, "unresolved": False}

    async def fetch_position(self):
        return self.position if self.chain_position is None else self.chain_position

    def of(self, kind):
        return [c for c in self.calls if c[0] == kind]


def make_cfg(maker="", execution=""):
    f = tempfile.NamedTemporaryFile("w", suffix=".yaml", delete=False)
    f.write(f"""
thresholds:
  midline_bps: auto
  upper_bps: 1.0
  lower_bps: 1.0
execution:
  net_tolerance_base: 0.0001
  hedge_slippage_bps: 5.0
{execution}
logging:
  status_interval_sec: 3600
maker:
  half_spread_bps: 3.0
  quote_usd: 50
  max_position_usd: 200
  requote_bps: 0.5
  fills_csv: {tempfile.mkdtemp()}/maker_fills.csv
{maker}
""")
    f.close()
    return load_config(f.name, NO_ENV, symbol="BTC", hedge_venue="lighter",
                       primary_venue="light-rh")


def make_engine(maker="", center=-1.5, execution=""):
    eng = MakerEngine(make_cfg(maker, execution))
    eng.entropy = FakeVenue("entropy", "RH")
    eng.hedge = FakeVenue("hedge", "LIGHTER")
    eng.venues = {"entropy": eng.entropy, "hedge": eng.hedge}
    eng.mcenter = CenterTracker(center, tau_sec=900, warmup_sec=0)
    # RH ~1.5 bps under LIGHTER, both 0.1 wide around 80000
    eng.hedge.set_book(80000.0, 80000.1)
    eng.entropy.set_book(79988.0, 79988.1)
    eng.entropy.orders_feed.listeners.append(eng.on_order_update)
    return eng


async def settle():
    for _ in range(5):
        await asyncio.sleep(0)


def run(coro):
    return asyncio.run(coro)


# ---------------------------------------------------------- pure helpers

def test_round_to_tick_never_tightens():
    assert round_to_tick(100.04, 0.1, up=False) == 100.0
    assert round_to_tick(100.01, 0.1, up=True) == 100.1
    assert round_to_tick(100.1, 0.1, up=True) == 100.1   # exact stays


def test_quote_prices_around_ref_and_post_only_clamp():
    bid, ask = quote_prices(80000.0, 3.0, 79000.0, 81000.0, 0.1)
    assert bid == round_to_tick(80000 * (1 - 3e-4), 0.1, False) == 79976.0
    assert ask == round_to_tick(80000 * (1 + 3e-4), 0.1, True) == 80024.0
    # primary book would be crossed: clamp one tick inside it
    bid, ask = quote_prices(80000.0, 3.0, 79950.0, 79960.0, 0.1)
    assert bid == 79959.9 and ask == 80024.0
    bid, ask = quote_prices(80000.0, 3.0, 80040.0, 80050.0, 0.1)
    assert bid == 79976.0 and ask == 80040.1


def test_maker_edge_matches_shadow_definition():
    # bought at 99.97 on the primary, sold the hedge at 100.00, center -1 bps
    e = maker_edge_bps(+1, 99.97, 100.0, -1.0, 0.0, 0.0)
    assert abs(e - (-1.0 - (99.97 / 100.0 - 1) * 1e4)) < 1e-9   # +2 bps
    e = maker_edge_bps(-1, 100.03, 100.0, -1.0, 0.0, 0.0)
    assert abs(e - ((100.03 / 100.0 - 1) * 1e4 + 1.0)) < 1e-9   # +4 bps
    # fees reduce the edge on both sides
    assert maker_edge_bps(+1, 99.97, 100.0, -1.0, 0.4, 0.0) < 2.0
    assert maker_edge_bps(-1, 100.03, 100.0, -1.0, 0.0, 0.4) < 4.0


def test_window_range_move():
    w = WindowRange(10.0)
    for t, x in ((0, 100.0), (1, 100.05), (2, 99.98), (3, 100.0)):
        w.add(t, x)
    assert abs(w.move_bps(3, 100.0) - 5.0) < 1e-6        # max 100.05
    assert abs(w.move_bps(11.5, 100.0) - 2.0) < 1e-6     # 0..1 expired, 99.98 left
    assert w.move_bps(13.5, 100.0) == 0.0               # only 100.0 left
    w.add(20, 101.0)
    assert w.move_bps(20, 101.0) == 0.0


def test_blackout_weekdays_wrap_and_dst():
    bo = Blackout(["15:55-16:45", "23:50-00:10"], "America/New_York")
    ts = lambda s: datetime.fromisoformat(s).replace(tzinfo=timezone.utc).timestamp()  # noqa: E731
    # Thu 2026-10-29: EDT (UTC-4): 20:00Z = 16:00 ET
    assert bo.active(ts("2026-10-29T20:00:00")) == "15:55-16:45"
    assert bo.active(ts("2026-10-29T21:00:00")) is None
    # Tue 2026-11-03: EST (UTC-5): 21:00Z = 16:00 ET — DST handled
    assert bo.active(ts("2026-11-03T21:00:00")) == "15:55-16:45"
    assert bo.active(ts("2026-11-03T20:00:00")) is None
    # wrap past midnight, ET
    assert bo.active(ts("2026-10-29T03:55:00")) == "23:50-00:10"   # 23:55
    assert bo.active(ts("2026-10-29T04:05:00")) == "23:50-00:10"   # 00:05
    # weekend: Sat 2026-10-31 16:00 ET
    assert bo.active(ts("2026-10-31T20:00:00")) is None
    assert Blackout([], "America/New_York").active(0) is None


def test_config_maker_section_and_validation():
    cfg = make_cfg(maker='  blackout_windows: ["09:25-09:45", "15:55-16:45"]')
    assert cfg.maker.half_spread_bps == 3.0
    assert cfg.maker.blackout_windows == ("09:25-09:45", "15:55-16:45")
    assert cfg.maker.max_actions_per_min == 600     # default
    for bad in ("  half_spread_bps: 0", "  blackout_windows: [\"9-10\"]",
                "  blackout_windows: \"09:00-10:00\"", "  no_such_key: 1",
                "  max_position_usd: 10", "  dead_man_sec: 5"):
        with pytest.raises(ConfigError):
            make_cfg(maker=bad)


# --------------------------------------------------------------- quoting

def test_places_both_quotes_at_expected_prices():
    async def go():
        eng = make_engine()
        eng._tick()
        await settle()
        places = eng.entropy.of("place")
        assert len(places) == 2
        ref = 80000.05 * (1 - 1.5e-4)
        by_side = {p[2]: p for p in places}
        assert by_side[True][4] == round_to_tick(ref * (1 - 3e-4), 0.1, False)
        assert by_side[False][4] == round_to_tick(ref * (1 + 3e-4), 0.1, True)
        for p in places:
            assert abs(p[3] * p[4] - 50) < 1.0          # ~$50 each
        return eng
    run(go())


def test_requote_only_beyond_threshold_and_uses_modify():
    async def go():
        eng = make_engine()
        eng._tick()
        await settle()
        n = len(eng.entropy.calls)
        eng.hedge.set_book(80000.2, 80000.3)            # +0.25 bps
        eng._tick()
        await settle()
        assert len(eng.entropy.calls) == n               # below 0.5 bps
        eng.hedge.set_book(80008.0, 80008.1)            # +1 bps
        eng._tick()
        await settle()
        mods = eng.entropy.of("modify")
        assert len(mods) == 2 and not eng.entropy.of("cancel")
    run(go())


def test_inventory_cap_drops_the_adding_side():
    async def go():
        eng = make_engine()
        eng.entropy.position = 0.0025          # ~$200 long on RH
        eng.hedge.position = -0.0025
        eng._tick()
        await settle()
        places = eng.entropy.of("place")
        assert [p[2] for p in places] == [False]          # ask only
    run(go())


def test_fill_hedges_on_hedge_venue_and_logs_edge():
    async def go():
        eng = make_engine()
        eng._tick()
        await settle()
        bid = next(p for p in eng.entropy.of("place") if p[2])
        coi, qty, px = bid[1], bid[3], bid[4]
        half = round(qty / 2, 5)
        eng.on_order_update({"client_order_index": coi, "status": "open",
                             "filled_base_amount": str(half),
                             "filled_quote_amount": str(half * px)})
        assert abs(eng.entropy.position - half) < 1e-12
        await eng.hedge_net("fill")
        takers = eng.hedge.of("taker")
        assert len(takers) == 1 and takers[0][1] is False   # SELL hedge
        assert abs(takers[0][2] - half) < 1e-12
        assert abs(eng.entropy.position + eng.hedge.position) < 1e-12
        # second partial fill of the same order: only the delta is new
        eng.on_order_update({"client_order_index": coi, "status": "filled",
                             "filled_base_amount": str(qty),
                             "filled_quote_amount": str(qty * px)})
        await eng.hedge_net("fill")
        assert abs(eng.entropy.position - qty) < 1e-12
        assert abs(eng.entropy.position + eng.hedge.position) < 1e-12
        assert eng.sides[1].coi is None                       # terminal
        with open(eng.mk.fills_csv) as fh:
            rows = list(csv.DictReader(fh))
        assert len(rows) == 2 and all(r["hedge_px"] for r in rows)
        # edge: bought ~4.5 bps under ref... sold hedge at bid 80000.0
        want = maker_edge_bps(+1, px, 80000.0, -1.5, 0.0, 0.0)
        assert abs(float(rows[0]["edge_bps"]) - want) < 1e-3
    run(go())


def test_fills_from_unknown_orders_are_ignored():
    async def go():
        eng = make_engine()
        eng.on_order_update({"client_order_index": 999999, "status": "filled",
                             "filled_base_amount": "1", "filled_quote_amount": "80000"})
        assert eng.entropy.position == 0.0
    run(go())


# ---------------------------------------------------------------- guards

def _quoting(eng):
    return {s for s, side in eng.sides.items()
            if side.coi is not None and not eng.orders[side.coi].terminal}


def test_fast_move_pulls_quotes_and_pauses():
    async def go():
        eng = make_engine()
        t = [1000.0]
        eng._now = lambda: t[0]
        eng._tick()
        await settle()
        assert _quoting(eng) == {1, -1}
        t[0] += 2
        eng.hedge.set_book(80100.0, 80100.1)            # +12.5 bps in 2 s
        eng.entropy.set_book(80088.0, 80088.1)
        eng._tick()
        await settle()
        assert len(eng.entropy.of("cancel")) == 2 and _quoting(eng) == set()
        assert "moved" in eng._block_reason
        t[0] += 30                                       # still paused
        eng._tick()
        await settle()
        assert _quoting(eng) == set()
        t[0] += 200                                      # pause over, windows clear
        eng._tick()
        await settle()
        assert _quoting(eng) == {1, -1}
    run(go())


def test_wide_hedge_spread_and_basis_jump_block():
    async def go():
        eng = make_engine()
        eng.hedge.set_book(79980.0, 80020.0)             # 5 bps wide
        eng.hedge.book.apply_lighter({"bids": [], "asks": []}, False)
        eng.hedge.set_book(79970.0, 80030.0)             # 7.5 bps
        assert "spread" in eng.quote_block_reason(eng._now())
        eng2 = make_engine()
        eng2.entropy.set_book(79850.0, 79850.1)          # -18.8 bps vs -1.5
        assert "from center" in eng2.quote_block_reason(eng2._now())
    run(go())


def test_blackout_and_fill_burst_and_loss_limit():
    async def go():
        eng = make_engine(maker='  blackout_windows: ["00:00-23:59"]\n'
                                '  blackout_tz: "UTC"')
        eng._now = lambda: datetime(2026, 10, 29, 12, tzinfo=timezone.utc).timestamp()
        assert "blackout" in eng.quote_block_reason(eng._now())

        eng = make_engine(maker="  max_fills_per_min: 3")
        now = eng._now()
        eng._fill_times.extend([now - 1] * 4)
        assert "fills in 60s" in eng.quote_block_reason(now)

        eng = make_engine(maker="  max_loss_usd: 1")
        assert eng.quote_block_reason(eng._now()) is None   # baseline
        eng.entropy.cash -= 5.0
        assert "loss" in eng.quote_block_reason(eng._now())
        assert eng._halt_reason                               # for good
        eng.entropy.cash += 10.0
        assert eng.quote_block_reason(eng._now()).startswith("stopped")
    run(go())


def test_stale_book_and_order_stream_block():
    async def go():
        eng = make_engine()
        eng.hedge.orders_feed.is_ready = False
        assert "order stream" in eng.quote_block_reason(eng._now())
        eng = make_engine()
        eng.entropy.book.alive_ts -= 3600
        assert "stale" in eng.quote_block_reason(eng._now())
    run(go())


def test_failed_hedge_blocks_until_net_is_flat():
    async def go():
        eng = make_engine()
        eng._tick()
        await settle()
        bid = next(p for p in eng.entropy.of("place") if p[2])
        eng.hedge.fail["taker"] = "boom"
        eng.on_order_update({"client_order_index": bid[1], "status": "filled",
                             "filled_base_amount": str(bid[3]),
                             "filled_quote_amount": str(bid[3] * bid[4])})
        await eng.hedge_net("fill")
        assert eng._hedge_problem
        assert "hedge" in eng.quote_block_reason(eng._now())
        del eng.hedge.fail["taker"]
        await eng.hedge_net("reconcile")
        assert not eng._hedge_problem
        assert eng.quote_block_reason(eng._now()) is None
    run(go())


def test_repeated_hedge_failures_halt():
    async def go():
        eng = make_engine(execution="  max_consecutive_errors: 2")
        eng.entropy.position = 0.001
        eng.hedge.fail["taker"] = "boom"
        await eng.hedge_net("x")
        await eng.hedge_net("x")
        assert eng._halt_reason and "hedges in a row" in eng._halt_reason
    run(go())


def test_cancel_failure_escalates_to_cancel_all():
    async def go():
        eng = make_engine()
        eng._tick()
        await settle()
        eng.entropy.fail["cancel"] = "nope"
        eng._pause(eng._now(), "test")
        eng._tick()
        await asyncio.sleep(0.5)
        assert eng._cancel_all_needed
        eng._tick()
        await settle()
        assert eng.entropy.of("cancel_all")
        assert not eng._cancel_all_needed
        assert "cancel failed" not in (eng.quote_block_reason(eng._now()) or "")
    run(go())


def test_modify_error_cancels_the_order():
    async def go():
        eng = make_engine()
        eng._tick()
        await settle()
        eng.entropy.fail["modify"] = "gone"
        eng.hedge.set_book(80008.0, 80008.1)
        eng._tick()
        await settle()
        assert len(eng.entropy.of("cancel")) == 2
    run(go())


# ------------------------------------------------- shutdown and reconcile

def test_shutdown_cancels_everything_and_flattens():
    async def go():
        eng = make_engine()
        guard = asyncio.get_running_loop().create_task(eng._shutdown_guard())
        eng.entropy.position = 0.0006         # an unhedged fill
        eng.request_stop()
        await asyncio.wait_for(guard, timeout=5)
        assert eng.entropy.of("cancel_all")
        assert eng.hedge.of("taker")
        assert abs(eng.entropy.position + eng.hedge.position) < 1e-12
        assert eng.quote_block_reason(eng._now()).startswith("stopped")
    run(go())


def test_reconcile_adopts_only_a_stable_discrepancy():
    async def go():
        eng = make_engine()
        eng.entropy.chain_position = 0.001
        await eng._reconcile_positions(hedge=False)
        assert eng.entropy.position == 0.0               # first sighting
        eng.maker_fills += 1                             # a fill in between
        await eng._reconcile_positions(hedge=False)
        assert eng.entropy.position == 0.0
        await eng._reconcile_positions(hedge=False)      # stable twice
        assert eng.entropy.position == 0.001
        await eng._reconcile_positions(hedge=True)       # then hedged
        assert abs(eng.entropy.position + eng.hedge.position) < 1e-12
    run(go())


def test_dead_man_switch_refreshes():
    async def go():
        eng = make_engine(maker="  dead_man_sec: 20")
        t = asyncio.get_running_loop().create_task(eng._dead_man_loop())
        await settle()
        eng.request_stop()
        await asyncio.wait_for(t, timeout=2)
        sched = eng.entropy.of("schedule")
        assert sched and sched[0][1] > 0
    run(go())


def test_full_lifecycle_through_strategy_loop():
    """start -> cancel leftovers -> quote -> fill -> hedge loop -> stop ->
    cancel-all + flat, all through the real loops."""
    async def go():
        eng = make_engine(maker="  dead_man_sec: 0")
        eng.entropy.orders_feed.listeners.clear()   # the loop registers it
        loop_task = asyncio.get_running_loop().create_task(eng._strategy_loop())
        await settle()
        assert eng.entropy.calls[0] == ("cancel_all",)          # leftovers
        eng._update_evt.set()
        await settle()
        bid = next(p for p in eng.entropy.of("place") if p[2])
        eng.entropy.orders_feed.listeners[0](
            {"client_order_index": bid[1], "status": "filled",
             "filled_base_amount": str(bid[3]),
             "filled_quote_amount": str(bid[3] * bid[4])})
        for _ in range(20):
            await asyncio.sleep(0)
        assert eng.hedge.of("taker"), "hedge loop did not hedge the fill"
        assert abs(eng.entropy.position + eng.hedge.position) < 1e-12
        eng.request_stop()
        await asyncio.wait_for(loop_task, timeout=5)
        assert eng._exec_tasks, "shutdown guard must be awaited by the engine"
        await asyncio.wait_for(asyncio.gather(*eng._exec_tasks), timeout=5)
        assert eng.entropy.calls[-1] == ("cancel_all",)
        assert not [c for c in eng.entropy.calls[eng.entropy.calls.index(("cancel_all",), 1):]
                    if c[0] == "place"], "no quote after the shutdown cancel-all"
    run(go())
