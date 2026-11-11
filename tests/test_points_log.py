"""tools/points_log.py: the pure helpers (no network, no keys).

Run:  python3 -m pytest tests/test_points_log.py
"""
import os
import sys
import tempfile

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "tools"))

import points_log  # noqa: E402

ACCOUNT = {"positions": [
    {"symbol": "BTC", "sign": -1, "position": "0.1", "position_value": "10873.2"},
    {"symbol": "ETH", "sign": 1, "position": "4.4", "position_value": "9958.0"},
    {"symbol": "SPY", "sign": 1, "position": "1.2", "position_value": "861.4"},
    {"symbol": "AMD", "sign": 1, "position": "0", "position_value": "0"},
]}


def test_summarize_positions():
    s = points_log.summarize_positions(ACCOUNT, "long")
    assert round(s["long_usd"], 1) == 10819.4
    assert round(s["short_usd"], 1) == 10873.2
    assert round(s["gross_usd"], 1) == 21692.6
    assert s["boost_side_usd"] == s["long_usd"]
    assert s["positions"] == "BTC-10873 ETH+9958 SPY+861"   # largest first
    assert points_log.summarize_positions(ACCOUNT, "short")["boost_side_usd"] \
        == s["short_usd"]
    assert points_log.summarize_positions({}, "long")["gross_usd"] == 0.0


def test_volume_today_takes_the_newest_bar():
    pnl = {"pnl": [{"timestamp": 200, "volume": 30.5},
                   {"timestamp": 100, "volume": 999.0}]}
    assert points_log.volume_today(pnl) == 30.5
    assert points_log.volume_today({"pnl": []}) is None
    assert points_log.volume_today({}) is None


def test_csv_append_and_last_points():
    path = os.path.join(tempfile.mkdtemp(), "sub", "points.csv")
    assert points_log.last_points(path) is None
    row = {k: "" for k in points_log.HEADER}
    points_log.append_row(path, dict(row, live_points="12.5"))
    points_log.append_row(path, dict(row, live_points="14.25"))
    assert points_log.last_points(path) == 14.25
    with open(path) as fh:
        lines = fh.read().strip().splitlines()
    assert lines[0].split(",") == points_log.HEADER and len(lines) == 3
