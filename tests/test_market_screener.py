"""Read-only research: depth, delayed execution, frozen train/test and storage."""
import argparse
import asyncio
import gzip
import importlib.util
import json
import math
from pathlib import Path

import pytest

SPEC = importlib.util.spec_from_file_location("market_screener", Path(__file__).resolve().parents[1] / "tools/market_screener.py")
scan = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(scan)


def arguments(**overrides):
    values = dict(notional=300, train_hours=1, min_train_samples=2,
        min_test_hours=0, min_cycles=1, min_band_bps=2, reserve_bps=0,
        holding_cost_bps_hour=0, cycle_target_bps=2, max_hold_seconds=30,
        rh_quote_usd=1, lighter_quote_usd=1, rh_fee_bps=None,
        lighter_fee_bps=None, symbols="", input="")
    return argparse.Namespace(**{**values, **overrides})


def market():
    return {"step": .001, **{v: {"fee_bps": 0, "min_base": .001,
        "min_quote_usd": 1, "quote_usd": 1, "market_id": 1} for v in scan.API}}


def books(rh_bid=99.99, rh_ask=100, lighter_bid=100.1, lighter_ask=100.11):
    return {"RH": {"bids": [[rh_bid, 100]], "asks": [[rh_ask, 100]], "generation": 1},
            "LIGHTER": {"bids": [[lighter_bid, 100]], "asks": [[lighter_ask, 100]], "generation": 1}}


def probe(ts, entry=None, delayed=None, **extra):
    return {"symbol": "TEST", "ts": ts, "delay_ts": ts + .35,
        "elapsed_ms": 350, "timing_valid": True, "continuity_valid": True,
        "entry": entry or books(), "delayed": delayed or books(), **extra}


def test_full_depth_equal_quantity_and_fee_units():
    levels = [[100, 1], [101, 2]]
    assert scan.walk(levels, 3) == 302
    assert scan.walk(levels, 4) is None
    qty = scan.quantity_for_budget(levels, 200, .001)
    assert scan.walk(levels, qty) <= 200
    quote = scan.quotation(books(), market(), "buy_rh", 3)
    assert quote["buy_usd"] == 300
    assert quote["sell_usd"] == pytest.approx(300.3)
    spec = market()
    spec["RH"]["fee_bps"] = 20
    assert scan.quotation(books(), spec, "buy_rh", 3)["net_usd"] < 0
    shallow = books()
    shallow["LIGHTER"]["bids"] = [[101, .1]]
    assert scan.target_quote(shallow, market(), "buy_rh", 300) is None


def test_metadata_uses_percent_to_bps_and_rejects_known_mismatch():
    item = dict(symbol="TEST", status="active", market_type="perp",
        market_id=1, supported_size_decimals=3, supported_price_decimals=2,
        min_base_amount="0.001", min_quote_amount="10", taker_fee="0.0280",
        multiplier="1")
    matches, _ = scan.common_markets({"RH": [item], "LIGHTER": [item]}, arguments())
    assert matches["TEST"]["RH"]["fee_bps"] == pytest.approx(2.8)
    matches, excluded = scan.common_markets({"RH": [item], "LIGHTER": [{**item, "multiplier": "1000"}]}, arguments())
    assert not matches
    assert "multiplier" in excluded["TEST"]
    with pytest.raises(ValueError, match="duplicate"):
        scan.common_markets({"RH": [item, item], "LIGHTER": [item]}, arguments())


def message(nonce, begin=None, bids=None, asks=None):
    return {"order_book": {"nonce": nonce, "begin_nonce": begin,
        "bids": bids or [{"price": "99", "size": "5"}],
        "asks": asks or [{"price": "100", "size": "5"}]}}


def test_book_gaps_staleness_and_reconnect_invalidate_probes():
    book = scan.Book()
    assert book.update(message(10), True, now=1)
    initial = book.snapshot(1, 1000, 10, 1)
    assert book.update(message(11, 10), False, now=1.1)
    # Duplicate updates must not keep a dead book fresh.
    assert book.update(message(11, 10), False, now=5)
    assert book.snapshot(5, 1000, 10, 1) is None
    assert not book.update(message(14, 13), False, now=5)
    assert book.snapshot(5, 1000, 10, 1) is None
    assert book.update(message(15), True, now=6)
    assert initial["generation"] != book.snapshot(6, 1000, 10, 1)["generation"]
    assert not book.update(message(16, 15, bids=[{"price": "101", "size": "1"}]), False, now=6.1)


def test_shadow_does_not_select_only_future_winners():
    shadow = scan.Shadow()
    # At t0 there is +10bps; t+350ms loses it. The model must still enter.
    delayed = books(rh_bid=100, rh_ask=100.1, lighter_bid=99.9, lighter_ask=100)
    shadow.update(probe(100, delayed=delayed), market(), {"buy_rh": 5, "sell_rh": 5}, arguments())
    assert shadow.position is not None
    assert shadow.position["qty"] == 3
    assert shadow.position["entry_usd"] < 0
    # Exit is decided at 140; execution is again at its later quote.
    shadow.update(probe(140, entry=delayed, delayed=delayed), market(), {"buy_rh": 5, "sell_rh": 5}, arguments())
    assert shadow.position is None
    assert shadow.cycles[0]["forced"]
    assert shadow.closed_usd < 0


def test_unknown_future_is_censored_not_fabricated_fill():
    shadow = scan.Shadow()
    row = probe(100, delayed={"RH": None, "LIGHTER": None}, timing_valid=False)
    shadow.update(row, market(), {"buy_rh": 5, "sell_rh": 5}, arguments())
    assert shadow.position is None
    assert shadow.censored == 1
    assert not scan.usable(probe(100, delay_ts=99))


def test_exit_decision_is_not_retroactively_cancelled_when_future_loses():
    shadow = scan.Shadow()
    shadow.update(probe(100), market(), {"buy_rh": 5, "sell_rh": 5}, arguments())
    close_entry = books(rh_bid=100.3, rh_ask=100.31, lighter_bid=100.15, lighter_ask=100.16)
    close_future = books(rh_bid=99.5, rh_ask=100, lighter_bid=100, lighter_ask=100.5)
    shadow.update(probe(102, entry=close_entry, delayed=close_future), market(),
                  {"buy_rh": 5, "sell_rh": 5}, arguments())
    assert shadow.position is None
    assert shadow.cycle_count == 1
    assert not shadow.cycles[0]["forced"]
    assert shadow.closed_usd == pytest.approx(-2.7)


def test_market_and_holding_costs_apply_to_both_crossings():
    shadow = scan.Shadow()
    args = arguments(reserve_bps=1, holding_cost_bps_hour=10)
    shadow.update(probe(100), market(), {"buy_rh": 5, "sell_rh": 5}, args)
    same = books()
    shadow.update(probe(3700, entry=same, delayed=same), market(),
                  {"buy_rh": 5, "sell_rh": 5}, args)
    entry_cost = 300 * 1 / 1e4
    close_cost = (100.11 * 3) * 1 / 1e4
    holding_cost = 300 * 10 / 1e4
    actual = .3 + 3 * (99.99 - 100.11) - entry_cost - close_cost - holding_cost
    assert shadow.closed_usd == pytest.approx(actual)


def test_live_committed_offsets_and_frozen_calibration(tmp_path):
    manifest = {"schema": 1, "run_id": "test", "start_ts": 0,
        "markets": {"TEST": market()}, "arguments": {"interval_ms": 1200000}}
    scan.save_json(tmp_path / "manifest.json", manifest)
    writer = scan.ProbeWriter(tmp_path)
    writer.write([probe(10), probe(20), probe(30)])
    # Training center must not change when a large outlier arrives in test.
    test = books(rh_bid=109, rh_ask=110, lighter_bid=100, lighter_ask=100.01)
    writer.write([probe(3601, entry=test, delayed=test), probe(3603, entry=test, delayed=test)])
    path = next(tmp_path.glob("probes-*.jsonl.gz"))
    size = path.stat().st_size
    scan.save_json(tmp_path / "live.json", {"committed": {path.name: size}})
    # Simulate a collector writing a new, unfinished gzip member.
    with path.open("ab") as file:
        file.write(b"\x1f\x8bunfinished")
    report = scan.analyze(arguments(input=str(tmp_path)))
    center = scan.premium(books())
    assert report["details"]["TEST"]["calibration"]["center_bps"] == pytest.approx(center)
    assert report["details"]["TEST"]["calibration"]["hurdles_bps"] is not None
    assert report["rows"][0]["test_probes"] == 2
    assert report["rows"][0]["test_center_bps"] > 800
    assert report["rows"][0]["funding_observed"] is False
    assert (tmp_path / "latest_analysis.json").exists()
    assert len(list(scan.observations(tmp_path, {path.name: size}))) == 5


def test_minimums_and_late_missing_depth_do_not_change_requested_quantity():
    entry = books()
    quote = scan.target_quote(entry, market(), "buy_rh", 300)
    delayed = books(rh_ask=200, lighter_bid=201, lighter_ask=202)
    future = scan.quotation(delayed, market(), "buy_rh", quote["qty"])
    assert future["qty"] == 3
    assert future["buy_usd"] == 600  # do not silently resize to $300 at t+350
    spec = market()
    spec["RH"]["min_base"] = 4
    assert scan.quotation(entry, spec, "buy_rh", 3) is None
