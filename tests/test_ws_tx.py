"""Websocket order transport: reply parsing, the sender, and send_taker over
ws with a fake signer / nonce manager / account stream (no network).

Run:  python3 -m pytest tests/test_ws_tx.py
"""
import asyncio
import json
import os
import sys
import types

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from entropy_arb import ws_tx  # noqa: E402
from entropy_arb.config import (LIGHTER_PROFILES, ConfigError,  # noqa: E402
                                LighterCreds, VenueConf, load_config)
from entropy_arb.venue_lighter import LighterVenue  # noqa: E402


def run(coro):
    return asyncio.run(coro)


# ------------------------------------------------------------- parsing

def test_reply_error_variants():
    assert ws_tx.reply_error({"type": "jsonapi/sendtx", "data": {
        "id": "a", "code": 200, "tx_hash": "0x1"}}) is None
    assert ws_tx.reply_error({"code": 0, "tx_hash": "0x1"}) is None
    assert ws_tx.reply_error({"tx_hash": "0x1"}) is None
    assert "21104" in ws_tx.reply_error(
        {"error": {"code": 21104, "message": "invalid nonce"}, "id": "a"})
    assert ws_tx.reply_error({"data": {"code": 21104,
                                       "message": "invalid nonce"}}) \
        == "code=21104 msg=invalid nonce"
    assert ws_tx.reply_error({"error": "boom"}) == "boom"
    assert ws_tx.reply_error({"code": 429, "message": "x"}).startswith(
        "RATE_LIMITED: ")
    assert ws_tx.reply_error({"error": {"code": 23000,
                                        "message": "Too Many Requests"}}
                             ).startswith("RATE_LIMITED: ")


def test_reply_id_and_shape():
    assert ws_tx.reply_id({"id": "x"}) == "x"
    assert ws_tx.reply_id({"data": {"id": "y"}}) == "y"
    assert ws_tx.reply_id({"type": "jsonapi/sendtx"}) is None
    assert ws_tx.looks_like_tx_reply({"type": "jsonapi/sendtx"})
    assert ws_tx.looks_like_tx_reply({"code": 200, "tx_hash": "0x"})
    assert not ws_tx.looks_like_tx_reply({"type": "ping"})
    assert not ws_tx.looks_like_tx_reply(
        {"type": "update/order_book", "code": 0})


# --------------------------------------------------------------- sender

class FakeWs:
    """Async-iterable websocket: frames pushed to .inbox are received."""

    def __init__(self, fail_send=False):
        self.sent = []
        self.inbox: asyncio.Queue = asyncio.Queue()
        self.fail_send = fail_send
        self.on_send = None

    async def send(self, frame):
        if self.fail_send:
            raise ConnectionError("closed")
        self.sent.append(json.loads(frame))
        if self.on_send:
            self.on_send(json.loads(frame))

    def __aiter__(self):
        return self

    async def __anext__(self):
        item = await self.inbox.get()
        if item is None:
            raise StopAsyncIteration
        return item

    async def __aenter__(self):
        return self

    async def __aexit__(self, *a):
        return False


def connector(ws):
    def connect(url, **kw):
        return ws
    return connect


async def _start(sender, ws, stop):
    task = asyncio.create_task(sender.run(stop))
    await ws.inbox.put(json.dumps({"type": "connected"}))
    await asyncio.wait_for(sender.ready.wait(), 1)
    return task


def test_sender_matches_by_id_and_fifo():
    async def go():
        ws = FakeWs()
        s = ws_tx.WsTxSender("RH", "wss://x", connect=connector(ws))
        stop = asyncio.Event()
        task = await _start(s, ws, stop)
        f1 = await s.send(14, '{"a":1}')
        f2 = await s.send(14, '{"a":2}')
        assert ws.sent[0]["type"] == "jsonapi/sendtx"
        assert ws.sent[0]["data"]["tx_info"] == {"a": 1}
        id2 = ws.sent[1]["data"]["id"]
        # reply to the second by id, then an id-less reply goes to the first
        await ws.inbox.put(json.dumps({"id": id2, "code": 200}))
        await ws.inbox.put(json.dumps({"type": "ping"}))
        await ws.inbox.put(json.dumps({"type": "jsonapi/sendtx", "code": 200}))
        assert (await asyncio.wait_for(f2, 1))["id"] == id2
        assert (await asyncio.wait_for(f1, 1))["type"] == "jsonapi/sendtx"
        await asyncio.sleep(0)
        assert {"type": "pong"} in ws.sent
        stop.set()
        await ws.inbox.put(None)
        await asyncio.wait_for(task, 1)
    run(go())


def test_sender_fails_pending_on_close_and_refuses_when_down():
    async def go():
        ws = FakeWs()
        s = ws_tx.WsTxSender("RH", "wss://x", connect=connector(ws))
        stop = asyncio.Event()
        task = await _start(s, ws, stop)
        fut = await s.send(14, '{"a":1}')
        stop.set()
        await ws.inbox.put(None)            # server closes the socket
        with pytest.raises(ws_tx.WsNotReady):
            await asyncio.wait_for(fut, 1)
        await asyncio.wait_for(task, 1)
        assert not s.ready.is_set()
        with pytest.raises(ws_tx.WsNotReady):
            await s.send(14, '{"a":2}')
    run(go())


# ------------------------------------------------- send_taker over ws

class FakeNonces:
    def __init__(self):
        self.n = 10
        self.locks = {}
        self.refreshed = 0
        self.failed = 0

    def rotate_key(self):
        return 3

    def lock(self, k):
        return self.locks.setdefault(k, asyncio.Lock())

    async def async_next_nonce(self, k):
        self.n += 1
        return k, self.n

    def acknowledge_failure(self, k):
        self.failed += 1
        self.n -= 1

    async def async_hard_refresh_nonce(self, k):
        self.refreshed += 1
        self.n = 10


class FakeSigner:
    def __init__(self):
        self.nonce_manager = FakeNonces()
        self.signed = []
        self.http_sent = []
        self.create_order_calls = 0

    def sign_create_order(self, **kw):
        self.signed.append(kw)
        return 14, json.dumps({"Nonce": kw["nonce"], "coi":
                               kw["client_order_index"]}), "0xh", None

    async def send_tx(self, tx_type, tx_info):
        self.http_sent.append(json.loads(tx_info))
        return types.SimpleNamespace(code=200, message=None)

    async def create_order(self, **kw):
        self.create_order_calls += 1
        return None, types.SimpleNamespace(code=200), None


class FakeOrders:
    def __init__(self):
        self.futs = {}

    def watch(self, coi):
        f = asyncio.get_running_loop().create_future()
        self.futs[coi] = f
        return f

    def unwatch(self, coi):
        self.futs.pop(coi, None)

    def fill(self, coi, qty=0.001, px=100.0):
        self.futs[coi].set_result({"status": "filled", "filled_base": qty,
                                   "avg_px": px, "exchange_order_id": 7})


@pytest.fixture(autouse=True)
def fake_lighter(monkeypatch):
    mod = types.ModuleType("lighter")
    mod.SignerClient = types.SimpleNamespace(
        ORDER_TYPE_LIMIT=0, ORDER_TYPE_MARKET=1,
        ORDER_TIME_IN_FORCE_IMMEDIATE_OR_CANCEL=0, DEFAULT_IOC_EXPIRY=0)
    monkeypatch.setitem(sys.modules, "lighter", mod)


def make_venue(transport="ws"):
    vc = VenueConf(key="entropy", kind="lighter", label="RH", symbol="BTC",
                   fee_bps=0, cap_usd=1000, orders_per_min=60,
                   lighter_profile=LIGHTER_PROFILES["lighter-rh"],
                   lighter_creds=LighterCreds(1, 3, "k"))
    v = LighterVenue(vc, session=None, settle_timeout_sec=1.0,
                     order_transport=transport, ws_reply_timeout_sec=0.2)
    v.market_id, v.size_decimals, v.price_decimals = 1, 5, 1
    v.signer = FakeSigner()
    v.orders_feed = FakeOrders()
    return v


async def _ws_venue(reply=None, fail_send=False):
    v = make_venue()
    ws = FakeWs(fail_send=fail_send)
    v.tx_ws = ws_tx.WsTxSender("RH", "wss://x", connect=connector(ws))
    stop = asyncio.Event()
    task = await _start(v.tx_ws, ws, stop)

    def on_send(frame):
        coi = frame["data"]["tx_info"]["coi"]
        if reply is not None:
            ws.inbox.put_nowait(json.dumps(dict(reply, id=frame["data"]["id"])))
        if reply is None or ws_tx.reply_error(reply) is None:
            asyncio.get_running_loop().call_soon(v.orders_feed.fill, coi)
    ws.on_send = on_send
    return v, ws, stop, task


def test_send_taker_ws_fills():
    async def go():
        v, ws, stop, task = await _ws_venue(reply={"code": 200, "tx_hash": "0x"})
        out = await v.send_taker(is_buy=True, qty=0.001, limit_px=100.0)
        assert out["status"] == "filled" and out["filled_base"] == 0.001
        assert v.signer.signed[0]["nonce"] == 11
        assert v.signer.signed[0]["api_key_index"] == 3
        assert v.signer.create_order_calls == 0 and not v.signer.http_sent
        assert out["submit_latency_ms"] is not None and out["ack_ts"]
        assert len(v.ws_send_stats) == 1 and v.ws_send_stats[0][2] is not None
        stop.set(); await ws.inbox.put(None); await task
    run(go())


def test_send_taker_ws_rejection_fails_fast_and_resyncs_nonce():
    async def go():
        v, ws, stop, task = await _ws_venue(
            reply={"error": {"code": 21104, "message": "invalid nonce"}})
        out = await v.send_taker(is_buy=False, qty=0.001, limit_px=100.0)
        assert out["status"] == "send-failed" and "21104" in out["err"]
        assert v.signer.nonce_manager.refreshed == 1
        assert not v.orders_feed.futs                  # unwatched
        stop.set(); await ws.inbox.put(None); await task
    run(go())


def test_send_taker_ws_without_reply_waits_for_account_stream():
    async def go():
        v, ws, stop, task = await _ws_venue(reply=None)
        out = await v.send_taker(is_buy=True, qty=0.001, limit_px=100.0)
        assert out["status"] == "filled"
        assert v.ws_send_stats[0][2] is None
        stop.set(); await ws.inbox.put(None); await task
    run(go())


def test_send_taker_ws_write_failure_resends_same_tx_over_http():
    async def go():
        v, ws, stop, task = await _ws_venue(fail_send=True)
        fut_task = asyncio.create_task(
            v.send_taker(is_buy=True, qty=0.001, limit_px=100.0))
        await asyncio.sleep(0.05)
        coi = next(iter(v.orders_feed.futs))
        v.orders_feed.fill(coi)
        out = await fut_task
        assert out["status"] == "filled"
        assert v.signer.http_sent == [{"Nonce": 11, "coi": coi}]
        assert v.signer.nonce_manager.refreshed == 0
        stop.set(); await ws.inbox.put(None); await task
    run(go())


def test_send_taker_uses_http_when_ws_down_or_off():
    async def go():
        for v in (make_venue("http"), make_venue("ws")):
            if v.order_transport == "ws":   # sender exists but not connected
                v.tx_ws = ws_tx.WsTxSender("RH", "wss://x")
            t = asyncio.create_task(
                v.send_taker(is_buy=True, qty=0.001, limit_px=100.0))
            await asyncio.sleep(0.01)
            v.orders_feed.fill(next(iter(v.orders_feed.futs)))
            out = await t
            assert out["status"] == "filled"
            assert v.signer.create_order_calls == 1 and not v.signer.signed
    run(go())


def test_http_exception_keeps_no_ack():
    async def go():
        v = make_venue("http")

        async def boom(**kw):
            raise RuntimeError("down")
        v.signer.create_order = boom
        out = await v.send_taker(is_buy=True, qty=0.001, limit_px=100.0)
        assert out["status"] == "send-failed" and out["ack_ts"] is None
        assert out["submit_latency_ms"] is None
    run(go())


# --------------------------------------------------------------- config

def _cfg(text):
    import tempfile
    f = tempfile.NamedTemporaryFile("w", suffix=".yaml", delete=False)
    f.write("thresholds:\n  midline_bps: 5.0\n  upper_bps: 4.0\n"
            "  lower_bps: 3.0\n" + text)
    f.close()
    return load_config(f.name, "/nonexistent.env", symbol="BTC",
                       hedge_venue="lighter", primary_venue="lighter-rh")


def test_config_order_transport():
    assert _cfg("").order_transport == "http"
    c = _cfg("execution:\n  order_transport: WS\n  ws_reply_timeout_sec: 0.8\n")
    assert c.order_transport == "ws" and c.ws_reply_timeout_sec == 0.8
    with pytest.raises(ConfigError):
        _cfg("execution:\n  order_transport: grpc\n")
