"""Leg report: outcome classification, slippage sign, repairs, merging.

Run:  python3 -m pytest tests/
"""
import csv
import importlib.util
from pathlib import Path

SPEC = importlib.util.spec_from_file_location(
    "leg_report", Path(__file__).resolve().parents[1] / "tools/leg_report.py")
lr = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(lr)


def row(direction="sell_entropy", qty=1.0, buy_fill=1.0, sell_fill=1.0,
        buy_status="filled", sell_status="filled", buy_limit=100.0,
        sell_limit=100.1, buy_px=100.0, sell_px=100.1, trade_id="t1", ts=1.0,
        **kw):
    r = dict(ts=str(ts), trade_id=trade_id, direction=direction, qty=str(qty),
             buy_venue="LIGHTER" if direction == "sell_entropy" else "RH",
             sell_venue="RH" if direction == "sell_entropy" else "LIGHTER",
             buy_fill=str(buy_fill), sell_fill=str(sell_fill),
             buy_status=buy_status, sell_status=sell_status,
             buy_limit=str(buy_limit), sell_limit=str(sell_limit),
             buy_avg_px=str(buy_px) if buy_fill else "",
             sell_avg_px=str(sell_px) if sell_fill else "",
             exp_edge_usd="0.1", fill_edge_usd="0.1", buy_notional="100",
             marginal_premium_bps="10")
    r.update(kw)
    return r


def test_primary_side_follows_direction():
    assert lr.primary_side(row(direction="sell_entropy")) == "sell"
    assert lr.primary_side(row(direction="buy_entropy")) == "buy"
    assert lr.primary_side(row(direction="buy_primary")) == "buy"


def test_classify_outcomes():
    # sell_entropy: the primary (RH) sells, the hedge buys
    assert lr.classify(row()) == "both"
    assert lr.classify(row(sell_fill=0, sell_status="canceled")) == "primary_missed"
    assert lr.classify(row(buy_fill=0, buy_status="canceled")) == "hedge_missed"
    assert lr.classify(row(buy_fill=0, sell_fill=0)) == "nothing"
    assert lr.classify(row(sell_fill=0.5)) == "imbalanced"
    assert lr.classify(row(buy_status="send-failed", buy_fill=0)) == "send_failed"
    # sequential mode: lead missed, follower not sent
    assert lr.classify(row(sell_fill=0, buy_fill=0, buy_status="not-sent")) == "nothing"


def test_slip_is_positive_when_worse_on_both_sides():
    # buying 1 bp above the limit and selling 1 bp below it are both worse
    r = row(buy_limit=100.0, buy_px=100.01, sell_limit=100.0, sell_px=99.99)
    assert abs(lr.leg_slip_bps(r, "buy") - 1.0) < 1e-6
    assert abs(lr.leg_slip_bps(r, "sell") - 1.0) < 1e-6
    better = row(sell_limit=100.0, sell_px=100.02)
    assert lr.leg_slip_bps(better, "sell") < 0
    assert lr.leg_slip_bps(row(sell_fill=0), "sell") is None


def test_summarize_splits_slip_by_leg():
    rows = [row(trade_id="a", sell_limit=100.0, sell_px=99.95,
                buy_limit=100.0, buy_px=100.0),
            row(trade_id="b", sell_fill=0, sell_status="canceled")]
    s = lr.summarize(rows)
    assert s["kinds"]["both"] == 1 and s["kinds"]["primary_missed"] == 1
    assert abs(s["primary_slip"][0] - 5.0) < 1e-6   # RH sold 5 bps low
    assert abs(s["hedge_slip"][0]) < 1e-9


def test_last_updated_compares_book_ages():
    # sell_entropy: primary = sell side, hedge = buy side
    r = row(sell_book_age_ms="200", buy_book_age_ms="5")
    assert lr.last_updated(r) == "hedge"
    r = row(sell_book_age_ms="5", buy_book_age_ms="200")
    assert lr.last_updated(r) == "primary"
    assert lr.last_updated(row()) is None


def test_premium_bucket_edges():
    key = lr.premium_bucket([2.0, 6.0])
    assert key({"marginal_premium_bps": "1"}) == "<2"
    assert key({"marginal_premium_bps": "3"}) == "2-6"
    assert key({"marginal_premium_bps": "6"}) == ">=6"
    assert key({"marginal_premium_bps": ""}) is None


def test_repairs_link_by_trade_id_or_time():
    hedges = [
        {"ts": "5", "trade_id": "t1", "status": "filled", "filled_qty": "1",
         "avg_px": "10", "close_pnl_usd": "-0.5"},
        {"ts": "5", "trade_id": "other", "status": "filled", "filled_qty": "1",
         "avg_px": "10", "close_pnl_usd": "-9"},
        {"ts": "30", "trade_id": "", "status": "filled", "filled_qty": "2",
         "avg_px": "10", "close_pnl_usd": "-1"},
        {"ts": "9999", "trade_id": "", "status": "send-failed",
         "filled_qty": "0", "avg_px": "", "close_pnl_usd": ""},
    ]
    rp = lr.repairs(hedges, {"t1"}, 0.0, 10.0)
    assert rp["n"] == 2 and rp["unlinked"] == 1
    assert abs(rp["pnl"] + 1.5) < 1e-9 and abs(rp["notional"] - 30.0) < 1e-9


def _write(path, rows):
    with open(path, "w", newline="") as fh:
        w = csv.DictWriter(fh, fieldnames=list(rows[0]))
        w.writeheader()
        w.writerows(rows)


def test_load_rows_merges_and_dedupes(tmp_path):
    a, b = tmp_path / "a.csv", tmp_path / "b.csv"
    _write(a, [row(trade_id="x", ts=2.0), row(trade_id="y", ts=1.0)])
    _write(b, [row(trade_id="y", ts=1.0), row(trade_id="z", ts=3.0)])
    rows = lr.load_rows([str(a), str(b)])
    assert [r["trade_id"] for r in rows] == ["y", "x", "z"]


def test_report_runs(tmp_path, capsys):
    rows = [row(trade_id=str(i), ts=1793000000.0 + i,
                sell_fill=0 if i % 3 == 0 else 1.0,
                sell_book_age_ms="100", buy_book_age_ms="3")
            for i in range(9)]
    lr.report(rows, [], [2.0, 6.0])
    out = capsys.readouterr().out
    assert "primary missed" in out and "By which book updated last" in out
    assert "lead_venue: primary" in out   # 33% primary misses -> hint
