"""execution.signal_source and execution.hedge_leg_slippage_bps.

Run:  python3 -m pytest tests/test_hedge_guards.py
"""
import asyncio
import csv
import os
import sys
import tempfile

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from entropy_arb.book import ArbPlan  # noqa: E402
from entropy_arb.config import ConfigError  # noqa: E402
from entropy_arb.engine import Engine  # noqa: E402
from test_engine import OrderVenue, make_cfg  # noqa: E402
from test_persist_sizing import cfg_with, make, scan_at  # noqa: E402


# ------------------------------------------------------------ config

def test_config_defaults_and_validation():
    c = cfg_with()
    assert c.signal_source == "any" and c.hedge_leg_slippage_bps is None
    c = cfg_with(execution="  signal_source: HEDGE\n  hedge_leg_slippage_bps: 3")
    assert c.signal_source == "hedge" and c.hedge_leg_slippage_bps == 3.0
    for bad in ("  signal_source: rh", "  hedge_leg_slippage_bps: -1"):
        with pytest.raises(ConfigError):
            cfg_with(execution=bad)


# ------------------------------------------------------ signal source

def _touch(eng, which, t):
    """Mark which book updated last (the stub books share a wall clock)."""
    eng.entropy.book.last_update_ts = t if which == "primary" else t - 0.01
    eng.hedge.book.last_update_ts = t if which == "hedge" else t - 0.01


def test_any_fires_whoever_created_the_signal():
    eng = make()
    _touch(eng, "primary", 999.0)
    first, fired = scan_at(eng, 1000.0, 1000.1)
    assert first is None and fired is not None
    assert eng._fire_src == "primary"


def test_hedge_only_skips_primary_led_signals_for_their_whole_life():
    eng = make(execution="  premium_persist_sec: 0.0\n  signal_source: hedge")
    _touch(eng, "primary", 999.0)                 # RH moved: primary-led
    first, a = scan_at(eng, 1000.0, 1000.1)
    assert first is None and a is None
    _touch(eng, "hedge", 1000.15)                 # LIGHTER updates later on
    assert scan_at(eng, 1000.2) == [None]         # still the same signal
    assert eng.skipped_by_source == 1             # counted once
    # edge disappears, then a LIGHTER update creates a new one
    eng.hedge.set_book(100.05, 100.06)
    assert scan_at(eng, 1000.3) == [None]
    eng.hedge.set_book(99.99, 100.00, sz=50.0)
    _touch(eng, "hedge", 1000.35)
    first, fired = scan_at(eng, 1000.4, 1000.5)
    assert first is None and fired is not None and eng._fire_src == "hedge"


def test_primary_only_is_the_mirror():
    eng = make(execution="  premium_persist_sec: 0.0\n  signal_source: primary")
    _touch(eng, "hedge", 999.0)
    assert scan_at(eng, 1000.0, 1000.1) == [None, None]
    eng2 = make(execution="  premium_persist_sec: 0.0\n  signal_source: primary")
    _touch(eng2, "primary", 999.0)
    assert scan_at(eng2, 1000.0, 1000.1)[1] is not None


def test_tie_counts_as_primary():
    eng = make()
    eng.entropy.book.last_update_ts = eng.hedge.book.last_update_ts = 5.0
    assert eng._signal_source() == "primary"


# ------------------------------------------------- hedge leg slippage

def run_exec(execution, rh_fills, lt_fills, qty=1.0):
    """concurrent: sell RH (primary) / buy LIGHTER (hedge), 10 bps plan."""
    cfg = make_cfg(execution=execution)
    cfg.trades_csv = os.path.join(tempfile.mkdtemp(), "trades.csv")
    eng = Engine(cfg)
    rh = OrderVenue("entropy", "RH", rh_fills)
    lt = OrderVenue("hedge", "LIGHTER", lt_fills)
    eng.entropy, eng.hedge = rh, lt
    eng.venues = {"entropy": rh, "hedge": lt}
    eng._step = 1e-4
    eng._fire_src = "hedge"
    plan = ArbPlan(qty, 100.0, 100.1, 100.0 * qty, 100.1 * qty, qty,
                   100.0 * qty, 10.0, 10.0, 0.0, 0.0)
    asyncio.run(eng._execute(lt, rh, plan))
    return eng, rh, lt


def test_hedge_leg_gets_its_own_protection():
    eng, rh, lt = run_exec("  leg_slippage_bps: 10.0\n"
                           "  hedge_leg_slippage_bps: 3.0", [1.0], [1.0])
    assert lt.orders[0]["limit_px"] == round(100.0 * 1.0003, 2)   # buy LIGHTER
    assert rh.orders[0]["limit_px"] == round(100.1 * 0.999, 2)    # sell RH
    row = next(csv.DictReader(open(eng.cfg.trades_csv)))
    assert row["signal_source"] == "hedge"


def test_without_option_both_legs_share_leg_slippage():
    eng, rh, lt = run_exec("  leg_slippage_bps: 10.0", [1.0], [1.0])
    assert lt.orders[0]["limit_px"] == round(100.0 * 1.001, 2)


MISS = {"status": "canceled-too-much-slippage", "filled_base": 0.0,
        "avg_px": None, "err": None, "unresolved": False}


def test_planned_hedge_miss_does_not_halt():
    ex = ("  leg_slippage_bps: 10.0\n  hedge_leg_slippage_bps: 3.0\n"
          "  max_consecutive_errors: 2")
    eng, rh, lt = run_exec(ex, [1.0], [MISS])
    assert eng.consec_errors == 0 and not eng.halted
    eng.consec_errors = 1                    # an earlier real problem
    eng2, _, _ = run_exec(ex, [1.0], [MISS])
    assert not eng2.halted


def test_hedge_miss_still_counts_without_the_option():
    eng, rh, lt = run_exec("  leg_slippage_bps: 10.0", [1.0], [MISS])
    assert eng.consec_errors == 1


def test_primary_miss_still_counts_with_the_option():
    eng, rh, lt = run_exec("  leg_slippage_bps: 10.0\n"
                           "  hedge_leg_slippage_bps: 3.0", [MISS], [1.0])
    assert eng.consec_errors == 1
