"""Equivalence tests for the order-book fast path.

The cached best bid/ask must always equal max()/min() over the dicts, and
plan_arb (top-of-book check before the full sort) must return exactly what
the original sort-first implementation returned.

Run:  python3 -m pytest tests/test_book_fastpath.py
"""
import os
import random
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from entropy_arb.book import (ArbPlan, OrderBook, crossable_base,  # noqa: E402
                              floor_step, plan_arb, walk_depth)


# ---------------------------------------------------------------- reference

def ref_best(book):
    bb = max(book.bids) if book.bids else None
    ba = min(book.asks) if book.asks else None
    mid = (bb + ba) / 2.0 if (book.bids and book.asks) else None
    return bb, ba, mid


def ref_plan_arb(buy_book, sell_book, *, threshold_bps, buy_fee_bps,
                 sell_fee_bps, take_fraction, cap_notional, min_base,
                 min_notional, size_step):
    """The original (sort-first) plan_arb, verbatim."""
    asks = sorted(buy_book.asks.items())
    bids = sorted(sell_book.bids.items(), key=lambda kv: -kv[0])
    if not asks or not bids:
        return None, "empty_book"
    threshold = threshold_bps / 1e4
    buy_fee = buy_fee_bps / 1e4
    sell_fee = sell_fee_bps / 1e4
    top_premium_bps = (bids[0][0] / asks[0][0] - 1.0) * 1e4
    if bids[0][0] * (1.0 - sell_fee) < asks[0][0] * (1.0 + buy_fee) * (1.0 + threshold):
        return None, "no_edge"
    q_max, q_max_notional = crossable_base(asks, bids, threshold, buy_fee, sell_fee)
    if q_max <= 0:
        return None, "no_edge"
    target = min(q_max * take_fraction, cap_notional / asks[0][0])
    target = floor_step(target, size_step)
    if target < min_base:
        return None, "below_min_base"
    buy_limit, buy_notional = walk_depth(asks, target)
    sell_limit, sell_notional = walk_depth(bids, target)
    if buy_notional < min_notional or sell_notional < min_notional:
        return None, "below_min_notional"
    return ArbPlan(
        qty=target, buy_limit=buy_limit, sell_limit=sell_limit,
        buy_notional=buy_notional, sell_notional=sell_notional,
        q_max=q_max, q_max_notional=q_max_notional,
        top_premium_bps=top_premium_bps,
        marginal_premium_bps=(sell_limit / buy_limit - 1.0) * 1e4,
        buy_fee=buy_fee, sell_fee=sell_fee,
    ), "ok"


# ---------------------------------------------------------------- helpers

def lvl(px, sz):
    return {"price": f"{px:.1f}", "size": f"{sz:.5f}"}


def random_snapshot(rng, mid, n):
    bids = [lvl(mid - 0.1 * (i + 1), rng.uniform(0.0001, 2)) for i in range(n)]
    asks = [lvl(mid + 0.1 * (i + 1), rng.uniform(0.0001, 2)) for i in range(n)]
    return {"bids": bids, "asks": asks}


def random_diff(rng, book, mid):
    """Adds, size changes and removals, biased toward the top of book."""
    out = {"bids": [], "asks": []}
    for side, sign in (("bids", -1), ("asks", 1)):
        levels = book.bids if side == "bids" else book.asks
        for _ in range(rng.randint(0, 6)):
            r = rng.random()
            if r < 0.35 and levels:
                # remove the current best (or one near it)
                top = sorted(levels, reverse=(side == "bids"))[:3]
                out[side].append(lvl(rng.choice(top), 0))
            elif r < 0.5 and levels:
                out[side].append(lvl(rng.choice(list(levels)), 0))
            elif r < 0.6:
                # a price that is not in the book being removed
                out[side].append(lvl(mid + sign * rng.randint(1, 400) * 0.1, 0))
            else:
                px = mid + sign * rng.randint(-3, 60) * 0.1  # may improve top
                out[side].append(lvl(px, rng.uniform(0.0001, 2)))
    return out


# ---------------------------------------------------------------- tests

def test_cached_best_matches_dicts_over_random_streams():
    rng = random.Random(7)
    for trial in range(40):
        book = OrderBook()
        mid = 80000.0 + rng.randint(-50, 50)
        book.apply_lighter(random_snapshot(rng, mid, rng.randint(0, 40)),
                           snapshot=True)
        assert (book.best_bid(), book.best_ask(), book.mid()) == ref_best(book)
        for step in range(400):
            if rng.random() < 0.01:
                book.apply_lighter(random_snapshot(rng, mid, rng.randint(0, 40)),
                                   snapshot=True)
            elif rng.random() < 0.01:
                book.clear()
            else:
                mid += rng.choice((-0.1, 0.0, 0.1))
                book.apply_lighter(random_diff(rng, book, mid), snapshot=False)
            # read in varying order / frequency, like the engine does
            if rng.random() < 0.7:
                assert (book.best_bid(), book.best_ask(), book.mid()) == \
                    ref_best(book), (trial, step)
        assert (book.best_bid(), book.best_ask(), book.mid()) == ref_best(book)


def test_empty_sides_and_hl_snapshots():
    book = OrderBook()
    assert book.best_bid() is None and book.best_ask() is None
    assert book.mid() is None
    book.apply_lighter({"bids": [lvl(100.0, 1)], "asks": []}, snapshot=True)
    assert book.best_bid() == 100.0 and book.best_ask() is None
    assert book.mid() is None
    book.apply_lighter({"bids": [lvl(100.0, 0)], "asks": [lvl(101.0, 1)]},
                       snapshot=False)
    assert book.best_bid() is None and book.best_ask() == 101.0
    book.apply_hl([[{"px": "99.5", "sz": "1"}, {"px": "99.7", "sz": "0"}],
                   [{"px": "100.5", "sz": "2"}, {"px": "100.2", "sz": "1"}]])
    assert (book.best_bid(), book.best_ask(), book.mid()) == ref_best(book)
    assert book.best_bid() == 99.5 and book.best_ask() == 100.2


def _random_book(rng, mid, n_bid, n_ask):
    b = OrderBook()
    b.apply_lighter({
        "bids": [lvl(mid - 0.1 * rng.randint(1, 300), rng.uniform(0.0001, 1))
                 for _ in range(n_bid)],
        "asks": [lvl(mid + 0.1 * rng.randint(1, 300), rng.uniform(0.0001, 1))
                 for _ in range(n_ask)]}, snapshot=True)
    return b


def test_plan_arb_identical_to_sort_first_reference():
    rng = random.Random(11)
    reasons = {}
    for _ in range(3000):
        mid_buy = 80000.0
        # sell venue mid shifted so edges are sometimes there, sometimes not
        mid_sell = mid_buy + rng.uniform(-3.0, 3.0)
        buy = _random_book(rng, mid_buy, rng.randint(0, 30), rng.randint(0, 30))
        sell = _random_book(rng, mid_sell, rng.randint(0, 30), rng.randint(0, 30))
        kw = dict(threshold_bps=rng.choice((-2.0, -0.5, 0.0, 0.3, 0.75, 2.0)),
                  buy_fee_bps=rng.choice((0.0, 0.0, 0.4, 2.8)),
                  sell_fee_bps=rng.choice((0.0, 0.0, 0.4, 2.8)),
                  take_fraction=rng.choice((0.25, 0.5, 1.0)),
                  cap_notional=rng.choice((10.0, 50.0, 500.0, 20000.0)),
                  min_base=rng.choice((0.0, 0.0002)),
                  min_notional=rng.choice((0.0, 10.0)),
                  size_step=1e-5)
        got = plan_arb(buy, sell, **kw)
        want = ref_plan_arb(buy, sell, **kw)
        assert got == want, kw
        reasons[want[1]] = reasons.get(want[1], 0) + 1
    # the random cases must exercise every branch, not just "no_edge"
    for r in ("empty_book", "no_edge", "ok", "below_min_base",
              "below_min_notional"):
        assert reasons.get(r, 0) > 0, reasons


if __name__ == "__main__":
    test_cached_best_matches_dicts_over_random_streams()
    test_empty_sides_and_hl_snapshots()
    test_plan_arb_identical_to_sort_first_reference()
    print("ok")
