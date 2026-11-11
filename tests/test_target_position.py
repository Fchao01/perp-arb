"""inventory.target_primary_usd / target_band_usd: hold a position on the
primary and turn over around it instead of around flat.

Run:  python3 -m pytest tests/test_target_position.py
"""
import os
import sys
import tempfile

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from entropy_arb.config import ConfigError, load_config  # noqa: E402
from entropy_arb.engine import Engine  # noqa: E402
from test_engine import StubVenue, approx  # noqa: E402
from test_persist_sizing import scan_at  # noqa: E402

NO_ENV = os.path.join(tempfile.gettempdir(), "entropy-arb-no-such.env")


def cfg_with(inventory="", cap=10000):
    f = tempfile.NamedTemporaryFile("w", suffix=".yaml", delete=False)
    f.write(f"""
thresholds:
  midline_bps: 0.0
  upper_bps: 1.0
  lower_bps: 1.0
primary:
  max_position_usd: {cap}
hedge:
  max_position_usd: {cap}
sizing:
  take_fraction: 1.0
  max_order_notional_usd: 1000
execution:
  premium_persist_sec: 0.0
inventory:
  scale_bps: 0
{inventory}
""")
    f.close()
    return load_config(f.name, NO_ENV, symbol="BTC", hedge_venue="lighter",
                       primary_venue="light-rh")


def make(inventory="", cap=10000):
    eng = Engine(cfg_with(inventory, cap))
    eng.entropy = StubVenue("entropy", "RH", cap=cap)
    eng.hedge = StubVenue("hedge", "LIGHTER", cap=cap)
    eng.venues = {"entropy": eng.entropy, "hedge": eng.hedge}
    eng._step, eng._min_base, eng._min_notional = 1e-4, 1e-4, 10.0
    eng.entropy.set_book(99.99, 100.01)
    eng.hedge.set_book(99.99, 100.01)
    return eng


def hold(eng, usd):
    """Hedged position: primary +usd, hedge venue -usd (price 100)."""
    eng.entropy.position, eng.hedge.position = usd / 100.0, -usd / 100.0


TARGET = "  target_primary_usd: 8000\n  target_band_usd: 3000"


def test_config_defaults_and_validation():
    c = cfg_with()
    assert c.inventory_target_usd == 0.0 and c.inventory_target_band_usd == 0.0
    c = cfg_with(TARGET)
    assert c.inventory_target_usd == 8000.0
    assert c.inventory_target_band_usd == 3000.0
    assert cfg_with("  target_primary_usd: -8000").inventory_target_usd == -8000.0
    for bad in ("  target_band_usd: -1",
                "  target_primary_usd: 12000",      # beyond the $10,000 cap
                "  target_primary_usd: -12000"):
        with pytest.raises(ConfigError):
            cfg_with(bad)
    # cap 0 is the close-only setting; a leftover target must not stop the bot
    assert cfg_with(TARGET, cap=0).inventory_target_usd == 8000.0


def test_no_target_is_the_old_behavior():
    eng = make()
    e, h = eng.entropy, eng.hedge
    assert eng._pos_range(e) == (0.0, -10000.0, 10000.0)
    assert eng._pos_range(h) == (0.0, -10000.0, 10000.0)
    hold(eng, 4000)
    approx(eng._headroom(e, h, 100.0), 6000.0)     # buy primary: 10k - 4k
    approx(eng._headroom(h, e, 100.0), 14000.0)    # sell primary: 10k + 4k


def test_range_is_mirrored_on_the_hedge_venue_and_capped():
    eng = make(TARGET)
    assert eng._pos_range(eng.entropy) == (8000.0, 5000.0, 10000.0)
    assert eng._pos_range(eng.hedge) == (-8000.0, -10000.0, -5000.0)
    eng = make("  target_primary_usd: -8000\n  target_band_usd: 3000")
    assert eng._pos_range(eng.entropy) == (-8000.0, -10000.0, -5000.0)
    assert eng._pos_range(eng.hedge) == (8000.0, 5000.0, 10000.0)
    eng = make("  target_primary_usd: 8000")       # no band: cap only
    assert eng._pos_range(eng.entropy) == (8000.0, -10000.0, 10000.0)


def test_headroom_around_the_target():
    eng = make(TARGET)
    e, h = eng.entropy, eng.hedge
    hold(eng, 8000)                                # at the target
    approx(eng._headroom(e, h, 100.0), 2000.0)     # up to the $10k cap
    approx(eng._headroom(h, e, 100.0), 3000.0)     # down to target - band
    hold(eng, 5000)                                # at the lower edge
    approx(eng._headroom(h, e, 100.0), 0.0)        # no more selling
    approx(eng._headroom(e, h, 100.0), 5000.0)


def test_wrong_side_position_only_trades_toward_the_target():
    eng = make(TARGET)
    e, h = eng.entropy, eng.hedge
    hold(eng, -10000)                              # short, target is long
    assert eng._headroom(h, e, 100.0) < 0          # selling primary: blocked
    approx(eng._headroom(e, h, 100.0), 20000.0)    # buying: all the way over
    # a sell-primary signal is skipped, a buy-primary signal fires
    e.set_book(100.10, 100.12)
    h.set_book(99.99, 100.00)
    assert scan_at(eng, 1000.0, 1000.1) == [None, None]
    e.set_book(99.99, 100.00)
    h.set_book(100.10, 100.12)
    first, fired = scan_at(eng, 1001.0, 1001.1)
    assert first is None and fired is not None
    buy, sell, plan = fired
    assert buy is e and sell is h


def test_ladder_is_measured_from_the_target():
    eng = make("  target_primary_usd: 8000\n  target_band_usd: 2000")
    eng.cfg.inventory_scale_bps, eng.cfg.inventory_floor_frac = 10.0, 0.0
    e, h = eng.entropy, eng.hedge
    hold(eng, 8000)
    approx(eng._inv_add_bps(e, h), 0.0)            # at the target: nothing
    approx(eng._inv_add_bps(h, e), 0.0)
    hold(eng, 9000)                                # halfway to the upper edge
    v = eng._inv_add_bps(e, h)                     # buying moves further away
    assert 4.9 < v < 5.1, v
    approx(eng._inv_add_bps(h, e), 0.0)            # selling returns to target
    hold(eng, 7000)                                # halfway to the lower edge
    v = eng._inv_add_bps(h, e)
    assert 4.9 < v < 5.1, v
    approx(eng._inv_add_bps(e, h), 0.0)
    hold(eng, -10000)                              # far on the wrong side
    approx(eng._inv_add_bps(e, h), 0.0)            # buying back: no surcharge
    approx(eng._inv_add_bps(h, e), 10.0)           # adding to it: full


def test_cap_zero_still_only_closes():
    eng = make(TARGET, cap=0)
    e, h = eng.entropy, eng.hedge
    assert eng._pos_range(e) == (0.0, 0.0, 0.0)
    hold(eng, 3000)
    approx(eng._headroom(h, e, 100.0), 3000.0)     # can sell down to flat
    assert eng._headroom(e, h, 100.0) < 0          # cannot add
