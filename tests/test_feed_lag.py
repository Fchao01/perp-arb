"""Feed-lag telemetry: exchange timestamps parsed in any unit, lag samples
recorded per diff, and frames without a timestamp change nothing.

Run:  python3 -m pytest tests/test_feed_lag.py
"""
import asyncio
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from entropy_arb.book import OrderBook  # noqa: E402
from entropy_arb.engine import CSV_HEADER  # noqa: E402
from entropy_arb.feeds import FeedLag, LighterBookFeed, exchange_ts  # noqa: E402


def test_exchange_ts_units_and_locations():
    s = 1793203668.798
    for v in (s, s * 1e3, s * 1e6, s * 1e9):
        assert abs(exchange_ts({"timestamp": v}) - s) < 1e-3
    assert abs(exchange_ts({}, {"timestamp": int(s * 1000)}) - s) < 1e-3
    assert exchange_ts({"timestamp": True}) is None
    assert exchange_ts({"type": "update/order_book"}, {"nonce": 5}) is None


def test_feed_lag_stats():
    lag = FeedLag()
    assert lag.stats() is None
    for ms in range(1, 101):
        lag.add(1000.0 + ms / 1000.0, 1000.0)
    n, p50, p90 = lag.stats()
    assert n == 100 and abs(p50 - 51) < 1e-6 and abs(p90 - 91) < 1e-6
    lag.add(5.0, None)                     # no exchange time: ignored
    assert lag.stats()[0] == 100


class FakeWS:
    async def send(self, _):
        pass


def _diff(nonce, ts=None):
    msg = {"channel": "order_book:1", "type": "update/order_book",
           "order_book": {"begin_nonce": nonce, "nonce": nonce,
                          "bids": [{"price": "100.0", "size": "1"}],
                          "asks": [{"price": "100.2", "size": "1"}]}}
    if ts is not None:
        msg["timestamp"] = ts
    return msg


def test_book_feed_records_lag_only_with_timestamps():
    async def go():
        book = OrderBook()
        feed = LighterBookFeed("RH", "wss://x", 1, book, lambda: None)
        snap = {"channel": "order_book:1", "type": "subscribed/order_book",
                "order_book": {"nonce": 1, "bids": [], "asks": []}}
        await feed._handle_book(FakeWS(), snap, snapshot=True)
        feed._recv_ts = 1793203668.850
        await feed._handle_book(FakeWS(), _diff(2, ts=1793203668800), snapshot=False)
        assert abs(book.exch_ts - 1793203668.8) < 1e-6
        assert abs(feed.lag.stats()[1] - 50.0) < 1e-3
        await feed._handle_book(FakeWS(), _diff(3), snapshot=False)
        assert feed.lag.stats()[0] == 1          # untimed diff adds nothing
        assert book.best_bid() == 100.0
    asyncio.run(go())


def test_trades_csv_header_keeps_old_columns_first():
    # new columns are appended, so readers keyed by name keep working
    assert CSV_HEADER[-2:] == ["buy_feed_lag_ms", "sell_feed_lag_ms"]
    assert CSV_HEADER.index("residual_qty") == len(CSV_HEADER) - 3
