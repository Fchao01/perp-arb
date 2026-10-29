"""fastjson must decode exactly like the standard library, and main's event
loop runner must work with and without uvloop.

The orjson / uvloop specific checks are skipped when those optional
packages are not installed.

Run:  python3 -m pytest tests/test_fastjson.py
"""
import asyncio
import json
import os
import sys

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from entropy_arb import fastjson  # noqa: E402

# Frames shaped like what the bot receives (Lighter book diff, account
# order update, Hyperliquid l2Book), plus edge cases.
FRAMES = [
    '{"channel":"order_book:1","offset":12345,"order_book":{"code":0,'
    '"asks":[{"price":"80520.0","size":"0.00000"},{"price":"80521.3",'
    '"size":"0.12500"}],"bids":[{"price":"80519.9","size":"1.23456"}],'
    '"offset":12345,"nonce":3456789012,"begin_nonce":3456789011},'
    '"timestamp":1793203668798,"type":"update/order_book"}',
    '{"type":"update/account_orders","channel":"account_orders:1:763629",'
    '"orders":{"1":[{"order_index":844421191853436,"client_order_index":'
    '1793203041303,"status":"filled","filled_base_amount":"0.00062",'
    '"filled_quote_amount":"49.935296","is_ask":false,"price":"80520.0"}]}}',
    '{"channel":"l2Book","data":{"coin":"io:SNDK","time":1793203668798,'
    '"levels":[[{"px":"1355.9","sz":"1.5","n":2}],[{"px":"1356.1","sz":"0.4",'
    '"n":1}]]}}',
    '{"type":"ping"}',
    '{"type":"connected","session_id":"abc"}',
    '{"a":1.5e-7,"b":-0.0,"c":12345678901234567,"d":null,"e":true,'
    '"f":"\\u4e2d\\u6587","g":[],"h":{}}',
]


@pytest.mark.parametrize("raw", FRAMES)
def test_decodes_like_stdlib(raw):
    assert fastjson.loads(raw) == json.loads(raw)
    assert fastjson.loads(raw.encode()) == json.loads(raw)


def test_falls_back_where_orjson_is_stricter():
    # wider than 64 bits, and NaN: json accepts both, orjson rejects both
    for raw in ('{"x":123456789012345678901234567890}', '{"x":NaN}'):
        got, want = fastjson.loads(raw), json.loads(raw)
        if raw.endswith("NaN}"):
            assert got["x"] != got["x"] and want["x"] != want["x"]
        else:
            assert got == want


def test_invalid_json_still_raises_jsondecodeerror():
    with pytest.raises(json.JSONDecodeError):
        fastjson.loads("{not json")


def test_backend_reported():
    try:
        import orjson  # noqa: F401
        assert fastjson.BACKEND == "orjson"
    except ImportError:
        assert fastjson.BACKEND == "json"
        assert fastjson.loads is json.loads


# ------------------------------------------------------------ event loop

def _load_main():
    import importlib.util
    path = os.path.join(os.path.dirname(__file__), "..", "main.py")
    spec = importlib.util.spec_from_file_location("ea_main", path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


async def _which_loop():
    await asyncio.sleep(0)
    return type(asyncio.get_running_loop()).__module__


def test_run_event_loop_without_uvloop():
    main = _load_main()
    assert not main.run_event_loop(_which_loop(),
                                   use_uvloop=False).startswith("uvloop")


def test_run_event_loop_with_uvloop_if_installed():
    main = _load_main()
    module = main.run_event_loop(_which_loop(), use_uvloop=True)
    try:
        import uvloop  # noqa: F401
        assert module.startswith("uvloop")
    except ImportError:
        assert module.startswith("asyncio")
    # the default policy must not leak into later asyncio.run() calls
    asyncio.set_event_loop_policy(None)
