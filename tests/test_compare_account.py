"""tools/compare_account.py: maker/taker side detection, order grouping,
latency and shared-signal pairing, cursor paging — all offline.

Run:  python3 -m pytest tests/test_compare_account.py
"""
import importlib.util
import os
import sys
import tempfile
from pathlib import Path

SPEC = importlib.util.spec_from_file_location(
    "compare_account", Path(__file__).resolve().parents[1] / "tools/compare_account.py")
ca = importlib.util.module_from_spec(SPEC)
sys.modules["compare_account"] = ca      # dataclasses look the module up
SPEC.loader.exec_module(ca)


T0 = 1_793_000_000_000          # realistic epoch ms


def trade(ts, px, sz, bid, ask, maker_ask, bid_cid, ask_cid):
    return {"timestamp": ts, "price": str(px), "size": str(sz),
            "bid_account_id": bid, "ask_account_id": ask,
            "is_maker_ask": maker_ask, "bid_client_id": bid_cid,
            "ask_client_id": ask_cid}


def test_normalize_sides_and_taker_flag():
    rows = [trade(1000, 100.0, 1, 7, 9, True, 11, 22),     # 7 buys, taker
            trade(1001, 100.1, 1, 9, 7, True, 33, 44),     # 7 sells, maker
            trade(1002, 100.2, 1, 9, 7, False, 55, 66),    # 7 sells, taker
            trade(1003, 100.0, 1, 1, 2, True, 0, 0)]       # not ours
    n = ca.normalize(rows, 7)
    assert [(f["side"], f["taker"], f["cid"]) for f in n] == [
        ("B", True, 11), ("S", False, 44), ("S", True, 66)]


def test_group_orders_vwap_and_clock_ids():
    fills = [{"ts": T0 + 300, "px": 100.0, "qty": 1.0, "side": "B", "cid": T0, "taker": True},
             {"ts": T0 + 300, "px": 101.0, "qty": 3.0, "side": "B", "cid": T0, "taker": True},
             {"ts": T0 + 400, "px": 99.0, "qty": 1.0, "side": "S", "cid": 17, "taker": True}]
    g = ca.group_orders(fills)
    o = g[(T0, "B")]
    assert o.qty == 4.0 and abs(o.vwap - 100.75) < 1e-12 and o.fills == 2
    assert ca.cid_is_ms_clock(o) and not ca.cid_is_ms_clock(g[(17, "S")])


def _my_csv(rows):
    f = tempfile.NamedTemporaryFile("w", suffix=".csv", delete=False)
    cols = ["decision_ts", "buy_venue", "sell_venue", "buy_status", "sell_status",
            "buy_client_order_id", "sell_client_order_id", "buy_send_ts",
            "sell_send_ts", "buy_limit", "sell_limit", "buy_avg_px",
            "sell_avg_px", "midline_bps"]
    f.write(",".join(cols) + "\n")
    for r in rows:
        f.write(",".join(str(r.get(c, "")) for c in cols) + "\n")
    f.close()
    return f.name


def test_analyze_end_to_end():
    # one execution of ours: buy RH (cid 501) / sell LIGHTER (cid 502),
    # decided at t=10.000 s, both legs sent at 10.002 s
    path = _my_csv([{"decision_ts": T0 / 1000 + 10.000, "buy_venue": "RH",
                     "sell_venue": "LIGHTER", "buy_status": "filled",
                     "sell_status": "filled", "buy_client_order_id": 501,
                     "sell_client_order_id": 502, "buy_send_ts": T0 / 1000 + 10.002,
                     "sell_send_ts": T0 / 1000 + 10.002, "buy_limit": 100.0,
                     "sell_limit": 100.03, "buy_avg_px": 100.01,
                     "sell_avg_px": 100.02, "midline_bps": -1.0}])
    mine = ca.load_my_trades(path, 0)
    assert len(mine) == 1 and mine[0].rh_side == "B" and mine[0].rh_cid == 501
    my_rh = ca.group_orders(ca.normalize(
        [trade(T0 + 10_300, 100.01, 1, 5, 9, True, 501, 0)], 5))
    my_lt = ca.group_orders(ca.normalize(
        [trade(T0 + 10_340, 100.02, 1, 9, 6, False, 0, 502)], 6))
    # reference sent RH buy at 9.970 s (cid = ms clock), filled at 10.250;
    # its mainnet hedge sent at 9.972, filled at 10.270
    ref_rh = ca.group_orders(ca.normalize(
        [trade(T0 + 10_250, 100.0, 2, 77, 9, True, T0 + 9_970, 0)], 77))
    ref_lt = ca.group_orders(ca.normalize(
        [trade(T0 + 10_270, 100.03, 2, 9, 78, False, 0, T0 + 9_972)], 78))
    res = ca.analyze(mine, my_rh, my_lt, ref_rh, ref_lt, window_ms=1000)
    lat = res["latency"]
    assert lat["mine_rh"] == [298] and lat["mine_lt"] == [338]
    assert lat["ref_rh"] == [280] and lat["ref_lt"] == [298]
    (row,) = res["shared"]
    assert row["decide_after_ref_send_ms"] == 30
    assert row["send_after_ref_ms"] == 32
    assert row["ref_matched_first_ms"] == 50          # we filled 50 ms later
    assert abs(row["my_rh_slip"] - 1.0) < 1e-9        # paid 1 bps over plan
    assert abs(row["ref_rh_slip"] - 0.0) < 1e-9
    assert abs(row["ref_prem"] - (100.0 / 100.03 - 1) * 1e4) < 1e-9
    ca.report(res, len(mine), len(ref_rh))            # prints without error


def test_fetch_trades_is_one_request(monkeypatch):
    # the endpoint ignores cursor and always returns the newest page
    calls = []

    def fake_get(url, params, retries=3):
        calls.append(params)
        return {"trades": [{"timestamp": 300}, {"timestamp": 90}],
                "next_cursor": "c1"}
    monkeypatch.setattr(ca, "_get", fake_get)
    rows = ca.fetch_trades("https://x", 1, 2, since_ms=100)
    assert len(calls) == 1 and "cursor" not in calls[0]
    assert [r["timestamp"] for r in rows] == [300]


def _t(tid, ts, ask=1, bid=2):
    return {"trade_id": tid, "timestamp": ts, "ask_account_id": ask,
            "bid_account_id": bid, "price": "100", "size": "1"}


def test_store_dedups_and_survives_restart(tmp_path):
    path = str(tmp_path / "sub" / "fills.jsonl")
    st = ca.TradeStore(path)
    assert st.add("rh", 6223, [_t(1, 100), _t(2, 200)]) == 2
    assert st.add("rh", 6223, [_t(2, 200), _t(3, 300)]) == 1
    # same trade seen from the other side's account is its own row
    assert st.add("rh", 9, [_t(3, 300)]) == 1
    with open(path, "a") as fh:
        fh.write('{"v":"rh","a":6223,"t":{"trade')   # torn line from a crash
    st2 = ca.TradeStore(path)
    assert st2.add("rh", 6223, [_t(1, 100), _t(4, 400)]) == 1
    assert [t["trade_id"] for t in st2.trades("rh", 6223, since_ms=150)] == [2, 3, 4]
    assert st2.trades("main", 6223) == []


def test_trade_key_without_id():
    a = {"timestamp": 1, "ask_account_id": 1, "bid_account_id": 2,
         "price": "1", "size": "2"}
    assert ca.trade_key(a) == ca.trade_key(dict(a))
    assert ca.trade_key(a) != ca.trade_key(dict(a, size="3"))


def test_collect_accumulates_and_flags_gaps(tmp_path):
    st = ca.TradeStore(str(tmp_path / "f.jsonl"))
    polls = iter([
        [_t(i, i) for i in range(100)],            # first poll: backlog, fine
        [_t(i, i) for i in range(50, 150)],        # 50 new
        [_t(i, i) for i in range(200, 300)],       # whole page new -> gap
    ])
    msgs = []

    def fetch(api, account, market, since_ms):
        if account == 6223:
            return next(polls)
        raise RuntimeError("boom")                 # other stream errors
    ca.collect(st, [("rh", "x", 6223, 1), ("main", "y", 397711, 2)],
               interval=5, fetch=fetch, iterations=3,
               sleep=lambda s: None, out=lambda m, **k: msgs.append(m))
    assert len(st.trades("rh", 6223)) == 250
    assert "may have missed" not in msgs[0] and "may have missed" not in msgs[1]
    assert "may have missed" in msgs[2]
    assert all("397711 error" in m for m in msgs)


def test_coverage_text():
    o = ca.Order(cid=1, side="B", first_ms=0, last_ms=0)
    assert ca.coverage_text({}, 0) == "no fills"
    assert ca.coverage_text({(1, "B"): o}, 2 * 3_600_000) == \
        "fills cover the last 2.0 h"
