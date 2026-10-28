"""Edge lifetime tool: episode tracking from book updates and the report math.

Run:  python3 -m pytest tests/
"""
import csv
import importlib.util
import os
import sys
from pathlib import Path
from types import SimpleNamespace

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from entropy_arb.book import OrderBook  # noqa: E402

SPEC = importlib.util.spec_from_file_location(
    "edge_lifetime", Path(__file__).resolve().parents[1] / "tools/edge_lifetime.py")
el = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(el)


class Clock:
    def __init__(self):
        self.t = 1000.0

    def __call__(self):
        return self.t


def venue(name, bid, ask):
    v = SimpleNamespace(name=name, fee_bps=0.0, book=OrderBook())
    set_book(v, bid, ask)
    return v


def set_book(v, bid, ask, sz=10.0):
    v.book.apply_hl([[{"px": str(bid), "sz": str(sz)}],
                     [{"px": str(ask), "sz": str(sz)}]])


def make_tracker():
    clock, rows = Clock(), []
    rh, lt = venue("RH", 99.99, 100.01), venue("LIGHTER", 99.99, 100.01)
    tr = el.EpisodeTracker(
        {"entropy": rh, "hedge": lt},
        {"sell_entropy": 2.0, "buy_entropy": 2.0},
        staleness_sec=1e9,
        sizing=dict(take_fraction=0.5, cap_notional=1e9, min_base=1e-4,
                    min_notional=1.0, size_step=1e-4),
        emit=rows.append, clock=clock, wall=lambda: 1793000000.0)
    return tr, clock, rh, lt, rows


def test_episode_open_checkpoints_and_close():
    tr, clock, rh, lt, rows = make_tracker()
    tr.on_update("hedge")
    assert not tr.open                      # flat books: no edge
    set_book(rh, 100.10, 100.12)            # RH 9 bps rich vs LIGHTER ask
    tr.on_update("entropy")
    assert "sell_entropy" in tr.open
    clock.t += 0.120
    set_book(rh, 100.06, 100.08)            # still past hurdle, smaller
    tr.on_update("entropy")
    clock.t += 0.200                        # 320 ms in
    set_book(lt, 100.05, 100.07)            # LIGHTER catches up: edge gone
    tr.on_update("hedge")
    assert not tr.open and len(rows) == 1
    r = rows[0]
    assert r["direction"] == "sell_entropy"
    assert r["started_by"] == "RH" and r["ended_by"] == "LIGHTER"
    assert abs(float(r["duration_ms"]) - 320.0) < 1e-6
    excess0 = (100.10 / 100.01 - 1) * 1e4 - 2.0
    assert abs(float(r["excess_start_bps"]) - excess0) < 1e-3
    assert float(r["excess_at_100ms"]) == float(r["excess_start_bps"])
    assert float(r["excess_at_300ms"]) < float(r["excess_at_100ms"])
    assert r["excess_at_500ms"] == ""       # ended before 500 ms
    assert float(r["plan_notional_usd"]) > 0


def test_close_all_marks_censored():
    tr, clock, rh, lt, rows = make_tracker()
    set_book(lt, 100.10, 100.12)            # RH cheap: buy primary
    tr.on_update("hedge")
    assert "buy_entropy" in tr.open
    clock.t += 0.05
    tr.close_all()
    assert rows[0]["ended_by"] == "shutdown"


def write_eps(path, eps):
    with open(path, "w", newline="") as fh:
        w = csv.DictWriter(fh, fieldnames=el.HEADER)
        w.writeheader()
        for i, (dur, ended) in enumerate(eps):
            row = {k: "" for k in el.HEADER}
            row.update(start_ts=f"{1793000000 + i}", direction="sell_entropy",
                       duration_ms=str(dur), started_by="RH", ended_by=ended,
                       excess_start_bps="1.0", plan_notional_usd="100")
            w.writerow(row)


def test_survival_with_persistence_gate(tmp_path):
    path = tmp_path / "eps.csv"
    write_eps(path, [(30, "LIGHTER"), (120, "RH"), (450, "RH"),
                     (900, "LIGHTER"), (80, "shutdown")])
    eps = el.load_episodes(str(path))
    # no gate, 340 ms latency: 2 of 4 decided episodes survive (shutdown at
    # 80 ms cannot be decided and is left out)
    assert el.survival(eps, 0, 340) == (4, 2)
    # 100 ms gate: only episodes alive at 100 ms fire; 2 of 3 last to 440 ms
    assert el.survival(eps, 100, 340) == (3, 2)
    # 500 ms gate: only the 900 ms episode fires, and it lasts to 840 ms
    assert el.survival(eps, 500, 340) == (1, 1)


def test_report_runs(tmp_path, capsys):
    path = tmp_path / "eps.csv"
    write_eps(path, [(30, "LIGHTER"), (450, "RH")])
    el.report(SimpleNamespace(csv=str(path), hours=0.0, min_excess=0.0,
                              latency_ms=[50, 340], persist_ms="0,100"))
    out = capsys.readouterr().out
    assert "sell_primary: 2 episodes" in out
    assert "persistence gate" in out
