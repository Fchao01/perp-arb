"""Threshold analyzer: auto-midline rooms use only earlier minutes.

Run:  python3 -m pytest tests/
"""
import importlib.util
from pathlib import Path

SPEC = importlib.util.spec_from_file_location(
    "analyze", Path(__file__).resolve().parents[1] / "tools/analyze.py")
an = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(an)


def minute(i, prem, sell_max, buy_max):
    return {"ts": 60.0 * i, "prem": prem, "prem_mean": prem,
            "sell_max": sell_max, "buy_max": buy_max}


def test_auto_rooms_measure_against_prior_center():
    # premium steady at -2 for 20 minutes, then one minute that spikes
    rows = [minute(i, -2.0, -1.5, 2.5) for i in range(20)]
    rows.append(minute(20, 5.0, 6.0, -4.0))
    sell, buy, centers = an.auto_rooms(rows, window_min=60, warmup_min=10,
                                       fees=0.0)
    # minutes 0..9 are warmup (the center needs 10 min of history first)
    assert len(centers) == len(rows) - 10
    assert all(abs(c + 2.0) < 1e-9 for c in centers)
    # steady minutes: +0.5 room each way around the -2 center
    assert abs(sell[0] - 0.5) < 1e-9 and abs(buy[0] - 0.5) < 1e-9
    # the spike is judged against the center before it, not after
    assert abs(sell[-1] - 8.0) < 1e-9


def test_auto_rooms_subtract_fees():
    rows = [minute(i, 0.0, 1.0, 1.0) for i in range(15)]
    sell, buy, _ = an.auto_rooms(rows, 60, 0, fees=0.4)
    assert all(abs(x - 0.6) < 1e-9 for x in sell + buy)
