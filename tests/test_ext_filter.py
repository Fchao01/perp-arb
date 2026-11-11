"""ext_filter: skip signals that follow a move on an external exchange.

Run:  python3 -m pytest tests/test_ext_filter.py
"""
import asyncio
import json
import os
import sys

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from entropy_arb import ext_feed  # noqa: E402
from entropy_arb.config import ConfigError  # noqa: E402
from test_persist_sizing import cfg_with, make, scan_at  # noqa: E402

EXT = "ext_filter:\n  venue: binance\n  window_ms: 200\n  max_move_bps: 0.5\n"


def test_parse_and_symbols():
    bn = {"e": "bookTicker", "s": "BTCUSDT", "b": "100.0", "a": "100.2"}
    assert ext_feed.parse_mid("binance", bn) == pytest.approx(100.1)
    assert ext_feed.parse_mid("binance", {"data": bn}) == pytest.approx(100.1)
    assert ext_feed.parse_mid("binance", {"result": None}) is None
    okx = {"arg": {"channel": "bbo-tbt", "instId": "BTC-USDT-SWAP"},
           "data": [{"bids": [["99", "1"]], "asks": [["101", "1"]], "ts": "1"}]}
    assert ext_feed.parse_mid("okx", okx) == 100.0
    assert ext_feed.venue_symbol("binance", "ETH") == "ethusdt"


def test_move_window_and_staleness():
    f = ext_feed.ExtFeed("binance", "BTC", stale_sec=2.0)
    assert f.move_bps(0.2, now=10.0) is None            # no data
    f.add(9.0, 100.0)
    f.add(9.85, 100.0)
    f.add(9.95, 100.01)                                  # +1 bp
    assert f.move_bps(0.2, now=10.0) == pytest.approx(1.0, abs=1e-6)
    assert f.move_bps(0.05, now=10.0) == pytest.approx(0.0)
    assert f.move_bps(0.2, now=12.5) is None             # stale
    f.add(30.0, 100.0)                                   # trims old history
    assert f.ts[0] >= 25.0 - 1e-9 or len(f.ts) <= 2


def _cfg(extra):
    import tempfile
    from entropy_arb.config import load_config
    f = tempfile.NamedTemporaryFile("w", suffix=".yaml", delete=False)
    f.write("thresholds:\n  midline_bps: 0.0\n  upper_bps: 1.0\n  lower_bps: 1.0\n" + extra)
    f.close()
    return load_config(f.name, "/nonexistent.env", symbol="BTC",
                       hedge_venue="lighter", primary_venue="lighter-rh")


def test_config():
    assert _cfg("").ext_venue is None
    c = _cfg(EXT)
    assert (c.ext_venue, c.ext_symbol, c.ext_window_ms, c.ext_max_move_bps) == \
        ("binance", "BTC", 200.0, 0.5)
    assert _cfg("ext_filter:\n  symbol: eth\n").ext_symbol == "ETH"
    for bad in ("ext_filter:\n  venue: ftx\n", "ext_filter:\n  max_move_bps: 0\n"):
        with pytest.raises(ConfigError):
            _cfg(bad)


def _engine_with_feed(points):
    eng = make(execution="  premium_persist_sec: 0.0")
    eng.cfg.ext_venue, eng.cfg.ext_window_ms, eng.cfg.ext_max_move_bps = "binance", 200.0, 0.5
    eng.ext = ext_feed.ExtFeed("binance", "BTC", stale_sec=2.0)
    for t, m in points:
        eng.ext.add(t, m)
    return eng


def test_skips_after_external_move_then_fires_when_calm():
    eng = _engine_with_feed([(999.0, 100.0), (999.95, 100.02)])   # +2 bps
    assert scan_at(eng, 1000.0, 1000.1) == [None, None]
    assert eng.skipped_by_ext == 1                                # once per signal
    eng.ext.add(1000.15, 100.02)                                  # calm since
    first = scan_at(eng, 1000.4)
    assert first[0] is not None and eng._fire_ext == pytest.approx(0.0)


def test_feed_down_does_not_block():
    eng = _engine_with_feed([(990.0, 100.0)])                     # stale
    first, fired = scan_at(eng, 1000.0, 1000.1)
    assert fired is not None and eng._fire_ext is None


def test_no_filter_by_default():
    eng = make()
    assert eng.ext is None
    first, fired = scan_at(eng, 1000.0, 1000.1)
    assert fired is not None


class FakeWs:
    def __init__(self, frames, stop):
        self.frames, self.stop = list(frames), stop

    async def send(self, m):
        pass

    def __aiter__(self):
        return self

    async def __anext__(self):
        if not self.frames:
            self.stop.set()
            raise StopAsyncIteration
        return self.frames.pop(0)

    async def __aenter__(self):
        return self

    async def __aexit__(self, *a):
        return False


def test_run_collects_mids():
    async def go():
        stop = asyncio.Event()
        frames = [json.dumps({"e": "bookTicker", "s": "BTCUSDT", "b": "1", "a": "3"})] * 3
        urls = []
        f = ext_feed.ExtFeed("binance", "BTC",
                             connect=lambda url, **kw: urls.append(url) or FakeWs(frames, stop))
        await f.run(stop)
        assert urls[0].endswith("/ws/btcusdt@bookTicker") and f.mids == [2.0] * 3
    asyncio.run(go())
