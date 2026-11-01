"""Signal-age sizing (sizing.persist_tiers): parsing, tier lookup, and the
scan sizing orders by how long the edge has persisted.

Run:  python3 -m pytest tests/test_persist_sizing.py
"""
import asyncio
import os
import sys
import tempfile

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from entropy_arb.config import ConfigError, load_config  # noqa: E402
from entropy_arb.engine import Engine, tier_cap  # noqa: E402
from test_engine import StubVenue  # noqa: E402

NO_ENV = os.path.join(tempfile.gettempdir(), "entropy-arb-no-such.env")


def cfg_with(sizing="", execution="  premium_persist_sec: 0.0"):
    f = tempfile.NamedTemporaryFile("w", suffix=".yaml", delete=False)
    f.write(f"""
thresholds:
  midline_bps: 0.0
  upper_bps: 1.0
  lower_bps: 1.0
sizing:
  take_fraction: 1.0
  max_order_notional_usd: 50
{sizing}
execution:
{execution}
inventory:
  scale_bps: 0
""")
    f.close()
    return load_config(f.name, NO_ENV, symbol="BTC", hedge_venue="lighter",
                       primary_venue="light-rh")


TIERS = """  persist_tiers:
    - [0.0, 20]
    - [0.3, 100]
    - [1.0, 300]"""


def test_tier_cap_lookup():
    tiers = ((0.0, 20.0), (0.3, 100.0), (1.0, 300.0))
    assert tier_cap(tiers, 0.0) == 20.0
    assert tier_cap(tiers, 0.29) == 20.0
    assert tier_cap(tiers, 0.3) == 100.0
    assert tier_cap(tiers, 5.0) == 300.0
    assert tier_cap(((0.3, 100.0),), 0.1) is None


def test_config_parsing_and_validation():
    assert cfg_with(TIERS).persist_tiers == ((0.0, 20.0), (0.3, 100.0), (1.0, 300.0))
    assert cfg_with().persist_tiers == ()
    for bad in ("  persist_tiers: [[0.3, 100], [0.3, 200]]",   # not increasing
                "  persist_tiers: [[0.5, 100], [0.2, 200]]",
                "  persist_tiers: [[-1, 100]]",
                "  persist_tiers: [[0.0, 5]]",                  # below min notional
                "  persist_tiers: [0.3, 100]",                  # not pairs
                "  persist_tiers: [[0.3]]"):
        with pytest.raises(ConfigError):
            cfg_with(bad)


def make(sizing="", execution="  premium_persist_sec: 0.0"):
    eng = Engine(cfg_with(sizing, execution))
    eng.entropy = StubVenue("entropy", "RH")
    eng.hedge = StubVenue("hedge", "LIGHTER")
    eng.venues = {"entropy": eng.entropy, "hedge": eng.hedge}
    eng._step, eng._min_base, eng._min_notional = 1e-4, 1e-4, 10.0
    # RH 5 bps rich with deep books: sell RH / buy LIGHTER, $5k crossable
    eng.entropy.set_book(100.05, 100.07, sz=50.0)
    eng.hedge.set_book(99.99, 100.00, sz=50.0)
    return eng


def scan_at(eng, *ts):
    async def go():
        return [eng._scan(t) for t in ts]
    return asyncio.run(go())


def test_order_size_grows_with_signal_age():
    eng = make(TIERS)
    t0 = 1000.0
    first, young, mid, old = scan_at(eng, t0, t0 + 0.1, t0 + 0.5, t0 + 2.0)
    assert first is None                      # first sighting only arms
    sizes = [round(b[2].buy_notional) for b in (young, mid, old)]
    assert sizes == [20, 100, 300], sizes
    assert abs(eng._fire_age - 2.0) < 1e-9


def test_first_tier_age_gates_young_signals():
    eng = make("  persist_tiers: [[0.3, 100], [1.0, 300]]")
    t0 = 1000.0
    _, young, later = scan_at(eng, t0, t0 + 0.2, t0 + 0.4)
    assert young is None                      # 0.2 s < first tier 0.3 s
    assert round(later[2].buy_notional) == 100


def test_age_resets_when_the_edge_disappears():
    eng = make(TIERS)
    t0 = 1000.0
    scan_at(eng, t0, t0 + 2.0)                # aged to the top tier
    eng.entropy.set_book(99.99, 100.01)       # edge gone
    assert scan_at(eng, t0 + 2.1) == [None]
    eng.entropy.set_book(100.05, 100.07, sz=50.0)
    first, again = scan_at(eng, t0 + 2.2, t0 + 2.3)
    assert first is None and round(again[2].buy_notional) == 20


def test_without_tiers_behaviour_is_unchanged():
    eng = make()                              # max_order_notional 50, persist 0
    t0 = 1000.0
    first, a, b = scan_at(eng, t0, t0 + 0.1, t0 + 5.0)
    assert first is None
    assert round(a[2].buy_notional) == 50 and round(b[2].buy_notional) == 50
    eng2 = make(execution="  premium_persist_sec: 0.3")
    _, young, later = scan_at(eng2, t0, t0 + 0.2, t0 + 0.4)
    assert young is None and round(later[2].buy_notional) == 50


def test_position_headroom_still_caps_tiered_orders():
    eng = make(TIERS)
    eng.entropy.cap_usd = eng.hedge.cap_usd = 150.0
    t0 = 1000.0
    _, old = scan_at(eng, t0, t0 + 2.0)
    assert old[2].buy_notional <= 150.0 + 1e-6
