"""tools/ext_recorder.py: frame parsing, sampling, daily files, and the
stream loop with a fake websocket (no network).

Run:  python3 -m pytest tests/test_ext_recorder.py
"""
import asyncio
import importlib.util
import json
import os
import sys

_P = os.path.join(os.path.dirname(__file__), "..", "tools", "ext_recorder.py")
_spec = importlib.util.spec_from_file_location("ext_recorder", _P)
er = importlib.util.module_from_spec(_spec)
sys.modules["ext_recorder"] = er
_spec.loader.exec_module(er)

BN = {"stream": "btcusdt@bookTicker", "data": {
    "e": "bookTicker", "u": 1, "s": "BTCUSDT", "b": "76100.10", "B": "3.2",
    "a": "76100.20", "A": "1.1", "T": 1794200000123, "E": 1794200000125}}
OKX = {"arg": {"channel": "bbo-tbt", "instId": "BTC-USDT-SWAP"}, "data": [
    {"asks": [["76100.2", "5", "0", "1"]], "bids": [["76100.1", "3", "0", "2"]],
     "ts": "1794200000123"}]}


def test_parse_frames():
    assert er.parse("binance", BN) == ("btcusdt", 1794200000123, 76100.10, 76100.20)
    assert er.parse("binance", BN["data"])[0] == "btcusdt"      # single stream
    assert er.parse("binance", {"result": None, "id": 1}) is None
    assert er.parse("okx", OKX) == ("BTC-USDT-SWAP", 1794200000123, 76100.1, 76100.2)
    assert er.parse("okx", {"event": "subscribe", "arg": OKX["arg"]}) is None
    assert er.venue_symbol("binance", "hype") == "hypeusdt"
    assert er.venue_symbol("okx", "sol") == "SOL-USDT-SWAP"


def test_sampler_moves_and_heartbeat():
    s = er.Sampler(min_move_bps=1.0, max_gap_ms=1000)
    assert s.take("BTC", 0.0, 100.0, 100.0)            # first row
    assert not s.take("BTC", 0.1, 100.005, 100.005)     # 0.5 bps
    assert s.take("BTC", 0.2, 100.011, 100.011)         # 1.1 bps
    assert not s.take("BTC", 0.5, 100.011, 100.011)
    assert s.take("BTC", 1.3, 100.011, 100.011)         # heartbeat
    assert s.take("ETH", 1.3, 5.0, 5.0)                 # per symbol


def test_writer_rotates_daily(tmp_path):
    w = er.Writer(str(tmp_path), "binance")
    t1 = 1794182399.5                                     # 23:59:59.5 UTC
    w.write("BTC", t1, 1, 1.0, 2.0)
    w.write("BTC", t1 + 1, 2, 1.5, 2.5)
    w.close()
    files = sorted(os.listdir(tmp_path))
    assert len(files) == 2 and all(f.startswith("binance_BTC_") for f in files)
    lines = open(tmp_path / files[0]).read().splitlines()
    assert lines[0] == "recv_ts,exch_ms,bid,ask" and lines[1].endswith(",1,1.0,2.0")


class FakeWs:
    def __init__(self, frames, stop):
        self.frames, self.stop, self.sent = list(frames), stop, []

    async def send(self, m):
        self.sent.append(m)

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


def test_run_writes_sampled_rows(tmp_path):
    async def go():
        stop = asyncio.Event()
        f2 = json.loads(json.dumps(BN)); f2["data"]["b"] = "76100.11"   # tiny move
        f3 = json.loads(json.dumps(BN)); f3["data"]["b"] = "76120.00"   # 2.6 bps
        ws = FakeWs([json.dumps(BN), json.dumps(f2), json.dumps(f3)], stop)
        urls = []

        def connect(url, **kw):
            urls.append(url)
            return ws
        w = er.Writer(str(tmp_path), "binance")
        await er.run("binance", ["BTC", "ETH"], w, er.Sampler(1.0, 60000),
                     stop, connect=connect)
        w.close()
        assert urls[0].endswith("btcusdt@bookTicker/ethusdt@bookTicker")
        f = [x for x in os.listdir(tmp_path)][0]
        assert f.startswith("binance_BTC_")
        assert len(open(tmp_path / f).read().splitlines()) == 3   # header + 2
    asyncio.run(go())


def test_okx_subscribes(tmp_path):
    async def go():
        stop = asyncio.Event()
        ws = FakeWs([json.dumps(OKX)], stop)
        w = er.Writer(str(tmp_path), "okx")
        await er.run("okx", ["BTC"], w, er.Sampler(1.0, 60000), stop,
                     connect=lambda url, **kw: ws)
        w.close()
        assert json.loads(ws.sent[0])["args"][0] == {"channel": "bbo-tbt",
                                                      "instId": "BTC-USDT-SWAP"}
        assert os.listdir(tmp_path) == ["okx_BTC_" + __import__("datetime").datetime.now(
            __import__("datetime").timezone.utc).strftime("%Y%m%d") + ".csv"]
    asyncio.run(go())
