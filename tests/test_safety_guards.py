"""execution.pause_windows, no new arbitrage while a hedgeable imbalance is
open, and repair hedges that widen their protection / back off / alert.

Run:  python3 -m pytest tests/test_safety_guards.py
"""
import asyncio
import os
import sys
import tempfile
from datetime import datetime
from zoneinfo import ZoneInfo

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import entropy_arb.engine as engine_mod  # noqa: E402
from entropy_arb.config import ConfigError  # noqa: E402
from entropy_arb.schedule import Blackout  # noqa: E402
from test_engine import OrderVenue  # noqa: E402
from test_persist_sizing import cfg_with, make, scan_at  # noqa: E402

NY = ZoneInfo("America/New_York")


def ny_ts(y, mo, d, h, mi):
    return datetime(y, mo, d, h, mi, tzinfo=NY).timestamp()


FRI_0935 = ny_ts(2026, 11, 6, 9, 35)      # a Friday, inside 09:25-09:50
FRI_1100 = ny_ts(2026, 11, 6, 11, 0)
SAT_0935 = ny_ts(2026, 11, 7, 9, 35)


# --------------------------------------------------------- pause windows

def test_pause_config_parsing():
    c = cfg_with(execution='  pause_windows: ["08:25-08:45", "09:25-09:50"]')
    assert c.pause_windows == ("08:25-08:45", "09:25-09:50")
    assert c.pause_tz == "America/New_York"
    for bad in ('  pause_windows: ["9:25"]', '  pause_windows: ["25:00-26:00"]',
                '  pause_windows: ["09:25-09:50"]\n  pause_tz: Mars/Base'):
        with pytest.raises(ConfigError):
            cfg_with(execution=bad)


def test_blackout_is_dst_aware_and_weekday_only():
    b = Blackout(["09:25-09:50"], "America/New_York")
    assert b.active(FRI_0935) == "09:25-09:50"
    assert b.active(FRI_1100) is None
    assert b.active(SAT_0935) is None
    # same 09:35 New York time in summer (EDT) is also inside
    assert b.active(ny_ts(2026, 7, 7, 9, 35)) == "09:25-09:50"


def test_no_new_arbitrage_inside_a_pause_window(monkeypatch):
    eng = make(execution='  premium_persist_sec: 0.0\n'
                         '  pause_windows: ["09:25-09:50"]')
    monkeypatch.setattr(engine_mod.time, "time", lambda: FRI_0935)
    assert scan_at(eng, 1000.0, 1000.1, 1000.2) == [None, None, None]
    assert eng._paused_in == "09:25-09:50"
    assert all(v is None for v in eng._armed.values())   # nothing aged
    monkeypatch.setattr(engine_mod.time, "time", lambda: FRI_1100)
    first, fired = scan_at(eng, 1000.3, 1000.4)
    assert first is None and fired is not None            # starts fresh
    assert eng._paused_in is None


# -------------------------------------------------- open imbalance gate

def test_open_imbalance_blocks_new_arbitrage():
    eng = make()
    eng.entropy.position, eng.hedge.position = 1.0, 0.0   # $100 unhedged
    assert scan_at(eng, 1000.0, 1000.1) == [None, None]
    assert eng._imbalance_blocked
    eng.hedge.position = -1.0                             # repaired
    first, fired = scan_at(eng, 1000.2, 1000.3)
    assert first is None and fired is not None
    assert not eng._imbalance_blocked


def test_untradeable_residual_does_not_block():
    eng = make()
    eng.entropy.position, eng.hedge.position = 0.05, 0.0  # $5 < $10 minimum
    first, fired = scan_at(eng, 1000.0, 1000.1)
    assert fired is not None


# ------------------------------------------------ repair hedge escalation

def make_hedger(fills, hedge_slip=5.0, max_slip=40.0):
    cfg = cfg_with(execution=f"  premium_persist_sec: 0.0\n"
                             f"  hedge_slippage_bps: {hedge_slip}\n"
                             f"  hedge_slippage_max_bps: {max_slip}")
    cfg.hedges_csv = os.path.join(tempfile.mkdtemp(), "hedges.csv")
    eng = engine_mod.Engine(cfg)
    rh = OrderVenue("entropy", "RH", [])
    lt = OrderVenue("hedge", "LIGHTER", fills)
    for v in (rh, lt):
        v.set_book(99.99, 100.01)
    eng.entropy, eng.hedge = rh, lt
    eng.venues = {"entropy": rh, "hedge": lt}
    eng._step, eng._min_base, eng._min_notional = 1e-4, 1e-4, 10.0
    rh.position, lt.position = -1.0, 2.0                  # net +1 on LIGHTER
    return eng, lt


MISS = {"status": "canceled-too-much-slippage", "filled_base": 0.0,
        "avg_px": None, "err": None, "unresolved": False}


def test_hedge_protection_widens_then_caps():
    eng, lt = make_hedger([MISS] * 5)
    limits = []
    for _ in range(5):
        eng._hedge_next_ts = 0.0                          # skip the back-off
        asyncio.run(eng._maybe_hedge())
        limits.append(lt.orders[-1]["limit_px"])
    # sell protection 5, 10, 20, 40, 40 bps below the 99.99 bid
    want = [round(99.99 * (1 - b / 1e4), 2) for b in (5, 10, 20, 40, 40)]
    assert limits == want, limits
    assert eng._hedge_streak == 5


def test_hedge_backs_off_and_alerts_once():
    eng, lt = make_hedger([MISS] * 20)
    for _ in range(3):
        asyncio.run(eng._maybe_hedge())
    assert len(lt.orders) == 3 and eng._hedge_next_ts > 0
    asyncio.run(eng._maybe_hedge())                       # inside back-off
    assert len(lt.orders) == 3
    for _ in range(10):
        eng._hedge_next_ts = 0.0
        asyncio.run(eng._maybe_hedge())
    assert eng._hedge_alerted
    assert eng._hedge_next_ts - engine_mod.time.time() <= 60.0 + 1e-6


def test_successful_hedge_resets_the_streak():
    eng, lt = make_hedger([MISS, MISS, 1.0])
    asyncio.run(eng._maybe_hedge())
    asyncio.run(eng._maybe_hedge())
    assert eng._hedge_streak == 2
    eng._hedge_next_ts = 0.0
    asyncio.run(eng._maybe_hedge())
    assert eng._hedge_streak == 0 and lt.position == 1.0
    asyncio.run(eng._maybe_hedge())                       # flat now
    assert len(lt.orders) == 3


def test_max_slippage_must_cover_base():
    with pytest.raises(ConfigError):
        cfg_with(execution="  hedge_slippage_bps: 30\n  hedge_slippage_max_bps: 10")
    assert cfg_with(execution="  hedge_slippage_bps: 50").hedge_slippage_max_bps == 50


# ------------------------------------------------------ miss cooldown

def test_cooldown_blocks_new_arbitrage_then_expires(monkeypatch):
    eng = make()
    eng._cooldown_until = engine_mod.time.time() + 600
    assert scan_at(eng, 1000.0, 1000.1) == [None, None]
    eng._cooldown_until = 0.0
    first, fired = scan_at(eng, 1000.2, 1000.3)
    assert first is None and fired is not None


def test_cooldown_config():
    c = cfg_with()
    assert c.miss_cooldown_after == 3 and c.miss_cooldown_sec == 600.0
    c = cfg_with(execution="  miss_cooldown_after: 5\n  miss_cooldown_sec: 120")
    assert c.miss_cooldown_after == 5 and c.miss_cooldown_sec == 120.0
