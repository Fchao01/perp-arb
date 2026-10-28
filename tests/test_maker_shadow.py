"""Maker shadow simulator: quote placement, latency windows, fills, hedging.

Run:  python3 -m pytest tests/
"""
import importlib.util
import os
import sys
from pathlib import Path
from types import SimpleNamespace

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from entropy_arb.book import OrderBook  # noqa: E402

SPEC = importlib.util.spec_from_file_location(
    "maker_shadow", Path(__file__).resolve().parents[1] / "tools/maker_shadow.py")
ms = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(ms)


def book(bid, ask, sz=10.0):
    b = OrderBook()
    b.apply_hl([[{"px": str(bid), "sz": str(sz)}],
                [{"px": str(ask), "sz": str(sz)}]])
    return b


def make_sim(**kw):
    rows, jobs = [], []
    opts = dict(size_usd=100.0, requote_bps=1.0, place_latency=0.2,
                cancel_latency=0.2, hedge_delay=0.35, actions_per_min=100,
                max_position_usd=1e9, tick=0.01, size_step=1e-4,
                min_base=1e-4, maker_fee_bps=0.0, hedge_fee_bps=0.0)
    opts.update(kw)
    sim = ms.MakerSim("hs5", 5.0, emit_fill=rows.append,
                      schedule=lambda d, fn: jobs.append((d, fn)), **opts)
    return sim, rows, jobs


def run_jobs(jobs):
    for _, fn in sorted(jobs, key=lambda j: j[0]):
        fn()
    jobs.clear()


def test_quotes_around_fair_plus_center_and_post_only():
    sim, _, _ = make_sim()
    # fair 100, center -4 bps -> reference 99.96; +/- 5 bps
    sim.on_market(0.0, 100.0, -4.0, 99.0, 101.0)
    bid, ask = sim._current(1), sim._current(-1)
    assert bid.px == 99.91 and ask.px == 100.01   # 99.91002 down, 100.00998 up
    assert bid.live_from == 0.2                   # live after place latency
    # RH book tighter than our reference: never cross it
    sim2, _, _ = make_sim()
    sim2.on_market(0.0, 100.0, 0.0, 99.99, 100.00)
    assert sim2._current(1).px <= 99.99 and sim2._current(-1).px >= 100.00


def test_fill_needs_live_quote_and_prints_through():
    sim, rows, jobs = make_sim()
    sim.on_market(0.0, 100.0, 0.0, 99.0, 101.0)
    ask = sim._current(-1).px                     # 100.05
    hb = book(99.98, 100.00)
    # before the quote is live: no fill
    sim.on_trade(0.1, 0.0, True, 100.10, 1.0, 7, 100.0, 0.0, lambda: hb)
    assert sim.fills == 0
    # at the quote price: a queue-dependent "touch" fill
    sim.on_trade(0.3, 0.0, True, ask, 0.1, 7, 100.0, 0.0, lambda: hb)
    assert sim.fills == 1
    # a taker buy that printed above our ask fills the rest of it
    sim.on_trade(0.4, 0.0, True, 100.10, 5.0, 7, 100.0, 0.0, lambda: hb)
    assert sim.fills == 2 and sim._current(-1).qty == 0
    assert abs(sim.position + int(100.0 / ask / 1e-4) * 1e-4) < 1e-9
    run_jobs(jobs)
    assert len(rows) == 2
    r = rows[0]
    assert r["side"] == "sell" and r["touch"] == 1
    # sold 100.05 on RH, bought the hedge at 100.00 with a 0 bps center
    assert ask == 100.05
    assert abs(float(r["edge_bps"]) - (ask / 100.00 - 1) * 1e4) < 1e-3
    # both legs settled: cash + positions at mids
    assert sim.hedge_position == -sim.position
    assert sim.mtm(100.0, 100.0) > 0
    assert float(r["markout_1s_bps"]) > 0


def test_old_quote_stays_live_through_cancel_latency():
    sim, rows, jobs = make_sim()
    sim.on_market(0.0, 100.0, 0.0, 99.0, 101.0)
    old = sim._current(-1).px                     # 100.05
    sim.on_market(1.0, 100.5, 0.0, 99.0, 102.0)   # fair jumps: requote
    assert sim._current(-1).px > old
    hb = book(100.48, 100.52)
    # 100 ms after the requote the stale ask can still be picked off
    sim.on_trade(1.1, 0.0, True, 100.30, 1.0, 9, 100.5, 0.0, lambda: hb)
    assert sim.fills == 1
    run_jobs(jobs)
    assert float(rows[0]["edge_bps"]) < 0         # adverse: hedge costs more
    assert float(rows[0]["px"]) == old
    # after the cancel latency it is gone
    sim2, _, _ = make_sim()
    sim2.on_market(0.0, 100.0, 0.0, 99.0, 101.0)
    sim2.on_market(1.0, 100.5, 0.0, 99.0, 102.0)
    sim2.on_trade(1.25, 0.0, True, 100.30, 1.0, 9, 100.5, 0.0, lambda: hb)
    assert sim2.fills == 0


def test_action_budget_keeps_stale_quote():
    sim, _, _ = make_sim(actions_per_min=2)
    sim.on_market(0.0, 100.0, 0.0, 99.0, 101.0)   # 2 actions: bid + ask
    first = sim._current(-1).px
    sim.on_market(1.0, 100.5, 0.0, 99.0, 102.0)
    assert sim._current(-1).px == first and sim.skipped_budget == 2
    sim.on_market(61.0, 100.5, 0.0, 99.0, 102.0)  # window rolled over
    assert sim._current(-1).px != first


def test_position_cap_stops_adding_side():
    sim, _, _ = make_sim(max_position_usd=50.0)
    sim.position = -1.0                           # short $100 on RH
    sim.on_market(0.0, 100.0, 0.0, 99.0, 101.0)
    assert sim._current(-1) is None               # no more selling
    assert sim._current(1) is not None            # buying reduces


def test_center_tracker_auto_and_fixed():
    c = ms.CenterTracker(None, tau_sec=10.0, warmup_sec=5.0)
    c.update(0.0, -4.0)
    assert not c.ready(1.0)
    c.update(10.0, -2.0)                          # dt = tau: full step
    assert c.value == -2.0 and c.ready(10.0)
    f = ms.CenterTracker(-3.9, 10.0, 5.0)
    f.update(0.0, 5.0)
    assert f.value == -3.9 and f.ready(0.0)


def test_center_tracker_gap_resets_and_seed():
    c = ms.CenterTracker(None, tau_sec=100.0, warmup_sec=50.0,
                         reset_gap_sec=100.0)
    assert c.seed([(0.0, -2.0), (60.0, -2.0)]) == 2
    assert c.ready(60.0) and c.value == -2.0
    c.update(500.0, 3.0)                          # gap 440 s > 100 s
    assert c.value == 3.0 and not c.ready(500.0)  # restarted, warming
    assert c.ready(550.0)


def test_trade_feed_dedupes_and_maps_taker_side():
    got = []
    feed = ms.RHTradeFeed("RH", "", 32, lambda *a: got.append(a))
    t = {"trade_id": 1, "type": "trade", "price": "100", "size": "2",
         "is_maker_ask": True, "ask_account_id": 5, "bid_account_id": 6,
         "timestamp": 1000}
    feed._handle({"trades": [t, t]})
    assert len(got) == 1
    taker_buys, px, size, taker, _ = got[0]
    assert taker_buys and px == 100.0 and size == 2.0 and taker == 6


def test_report_runs(tmp_path, capsys):
    fills = ms.CsvOut(str(tmp_path / "maker_shadow_fills.csv"), ms.FILL_HEADER)
    stats = ms.CsvOut(str(tmp_path / "maker_shadow_stats.csv"), ms.STATS_HEADER)
    fills({"sim": "hs4", "time_utc": "2026-10-28T13:40:00.000Z", "ts": "1",
           "side": "sell", "qty": "0.1", "px": "100", "notional_usd": "10",
           "touch": 0, "taker_account": 3, "edge_usd": "0.004",
           "edge_bps": "4", "quote_age_ms": "900", "markout_1s_bps": "3",
           "markout_5s_bps": "-1", "markout_30s_bps": "2"})
    stats({"time_utc": "x", "ts": "1", "sim": "hs4", "elapsed_s": "3600",
           "quoting_s": "3000", "actions": 10, "skipped_budget": 0,
           "fills": 1, "edge_usd": "0.004", "mtm_usd": "0.003",
           "rh_position": "-0.1"})
    fills.close()
    stats.close()
    ms.report(SimpleNamespace(dir=str(tmp_path), no_touch=False))
    out = capsys.readouterr().out
    assert "hs4" in out and "+4.00 bps of volume" in out and "#3 1" in out
    assert "mark-to-market at session ends $+0.00" in out
