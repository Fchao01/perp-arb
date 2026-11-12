"""execution.daily_loss_limit_usd: stop new arbitrage for the rest of the
day once the day's PnL (cash + positions at mid) is down to the limit.

Run:  python3 -m pytest tests/test_daily_loss.py
"""
import json
import os
import sys
import tempfile
from datetime import datetime
from zoneinfo import ZoneInfo

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from entropy_arb.config import ConfigError, load_config  # noqa: E402
from entropy_arb.daily import DailyLoss  # noqa: E402
from entropy_arb.engine import Engine  # noqa: E402
from test_engine import StubVenue  # noqa: E402
from test_persist_sizing import scan_at  # noqa: E402

NO_ENV = os.path.join(tempfile.gettempdir(), "entropy-arb-no-such.env")
SH = ZoneInfo("Asia/Shanghai")


def ts(text):
    """'2026-11-12 09:00' Beijing time -> epoch."""
    return datetime.strptime(text, "%Y-%m-%d %H:%M").replace(tzinfo=SH).timestamp()


def state_path():
    return os.path.join(tempfile.mkdtemp(), "btc", "daily_pnl.json")


def test_counts_from_the_first_value_and_trips_and_stays_tripped():
    d = DailyLoss(3.0, "Asia/Shanghai", state_path())
    assert d.update(ts("2026-11-12 09:00"), 100.0) == 0.0
    assert d.update(ts("2026-11-12 10:00"), 98.5) == -1.5 and not d.tripped
    assert d.update(ts("2026-11-12 11:00"), 97.0) == -3.0 and d.tripped
    assert d.update(ts("2026-11-12 12:00"), 99.0) == -1.0 and d.tripped  # sticky


def test_new_day_resets_at_midnight_in_the_time_zone():
    d = DailyLoss(3.0, "Asia/Shanghai", state_path())
    d.update(ts("2026-11-12 22:00"), 100.0)
    d.update(ts("2026-11-12 23:59"), 90.0)
    assert d.tripped
    assert d.update(ts("2026-11-13 00:01"), 90.0) == 0.0 and not d.tripped
    assert d.update(ts("2026-11-13 01:00"), 89.0) == -1.0


def test_restart_carries_the_day_and_a_new_day_does_not():
    path = state_path()
    d = DailyLoss(3.0, "Asia/Shanghai", path)
    d.update(ts("2026-11-12 09:00"), 500.0)
    d.update(ts("2026-11-12 10:00"), 498.0)
    d.save()
    assert json.load(open(path))["pnl"] == -2.0
    # a new process: value scale differs (cash restarts at 0), PnL carries
    d2 = DailyLoss(3.0, "Asia/Shanghai", path)
    assert d2.update(ts("2026-11-12 11:00"), 7.0) == -2.0
    assert d2.update(ts("2026-11-12 11:30"), 6.0) == -3.0 and d2.tripped
    d2.save()
    d3 = DailyLoss(3.0, "Asia/Shanghai", path)
    d3.update(ts("2026-11-12 12:00"), 0.0)
    assert d3.tripped                                  # still stopped today
    d4 = DailyLoss(3.0, "Asia/Shanghai", path)
    assert d4.update(ts("2026-11-13 09:00"), 0.0) == 0.0 and not d4.tripped


def cfg_with(execution, tmpdir):
    f = tempfile.NamedTemporaryFile("w", suffix=".yaml", delete=False)
    f.write(f"""
thresholds:
  midline_bps: 0.0
  upper_bps: 1.0
  lower_bps: 1.0
sizing:
  take_fraction: 1.0
  max_order_notional_usd: 50
execution:
  premium_persist_sec: 0.0
{execution}
inventory:
  scale_bps: 0
logging:
  daily_state_json: {tmpdir}/{{symbol}}/daily_pnl.json
""")
    f.close()
    return load_config(f.name, NO_ENV, symbol="BTC", hedge_venue="lighter",
                       primary_venue="light-rh")


def test_config():
    tmp = tempfile.mkdtemp()
    c = cfg_with("", tmp)
    assert c.daily_loss_limit_usd == 0.0 and c.daily_reset_tz == "Asia/Shanghai"
    assert c.daily_state_json == f"{tmp}/btc/daily_pnl.json"
    c = cfg_with("  daily_loss_limit_usd: 3\n  daily_reset_tz: UTC", tmp)
    assert c.daily_loss_limit_usd == 3.0 and c.daily_reset_tz == "UTC"
    for bad in ("  daily_loss_limit_usd: -1",
                "  daily_loss_limit_usd: 3\n  daily_reset_tz: Mars/Base"):
        with pytest.raises(ConfigError):
            cfg_with(bad, tmp)


def make(execution):
    eng = Engine(cfg_with(execution, tempfile.mkdtemp()))
    eng.entropy = StubVenue("entropy", "RH")
    eng.hedge = StubVenue("hedge", "LIGHTER")
    eng.venues = {"entropy": eng.entropy, "hedge": eng.hedge}
    eng._step, eng._min_base, eng._min_notional = 1e-4, 1e-4, 10.0
    eng.entropy.set_book(99.99, 100.00)       # RH cheap: buy RH
    eng.hedge.set_book(100.10, 100.12)
    return eng


def test_engine_stops_new_arbitrage_once_the_limit_is_hit():
    eng = make("  daily_loss_limit_usd: 3")
    first, fired = scan_at(eng, 1000.0, 1000.1)
    assert fired is not None                   # trading normally
    assert eng.daily.pnl == 0.0
    eng.entropy.cash -= 3.5                    # a day's costs: -3.5 USD
    assert scan_at(eng, 1001.0, 1001.1) == [None, None]
    assert eng.daily.tripped and eng.daily.pnl == pytest.approx(-3.5)
    assert "STOPPED" in eng._daily_status()
    eng.entropy.cash += 10.0                   # PnL recovers: still stopped
    assert scan_at(eng, 1002.0, 1002.1) == [None, None]


def test_off_by_default():
    eng = make("")
    assert eng.daily is None and eng._daily_status() == ""
    eng.entropy.cash -= 1000.0
    first, fired = scan_at(eng, 1000.0, 1000.1)
    assert fired is not None
