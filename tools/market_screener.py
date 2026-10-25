#!/usr/bin/env python3
"""Public RH/Lighter market research. Never reads credentials or sends orders.

python3 tools/market_screener.py list
python3 tools/market_screener.py collect --hours 72 --notional 300
python3 tools/market_screener.py analyze --input logs/screener/RUN_DIRECTORY

See docs/market_screener.zh-CN.md for assumptions and interpretation.
"""
from __future__ import annotations

import argparse
import asyncio
import csv
import gzip
import io
import json
import logging
import math
import random
import signal
import sys
import time
import uuid
from collections import Counter, deque
from datetime import datetime, timezone
from pathlib import Path

import aiohttp
try:
    from websockets.asyncio.client import connect
except ImportError:
    from websockets import connect

API = {"RH": "https://api.rh.lighter.xyz",
       "LIGHTER": "https://mainnet.zklighter.elliot.ai"}
WS = {venue: url.replace("https://", "wss://") + "/stream"
      for venue, url in API.items()}
DIRECTIONS = {"buy_rh": ("RH", "LIGHTER"),
              "sell_rh": ("LIGHTER", "RH")}
LOG = logging.getLogger("screener")
SCHEMA = 1


def quantile(values, fraction):
    if not values:
        return None
    values = sorted(values)
    index = (len(values) - 1) * fraction
    lo, hi = math.floor(index), math.ceil(index)
    return values[lo] + (values[hi] - values[lo]) * (index - lo)


class Reservoir:
    """Bounded, reproducible sample for approximate distribution summaries."""
    def __init__(self, capacity=10000):
        self.values = []
        self.n = 0
        self.capacity = capacity
        self.rng = random.Random(1729)

    def add(self, value):
        self.n += 1
        if len(self.values) < self.capacity:
            self.values.append(value)
        else:
            index = self.rng.randrange(self.n)
            if index < self.capacity:
                self.values[index] = value


def finite(value):
    number = float(value)
    if not math.isfinite(number):
        raise ValueError("non-finite market value")
    return number


def walk(levels, qty):
    """Quote the full base quantity, never silently quote a partial fill."""
    remaining, notional = qty, 0.0
    for price, size in levels:
        take = min(remaining, size)
        notional += take * price
        remaining -= take
        if remaining <= max(1e-12, qty * 1e-10):
            return notional
    return None


def quantity_for_budget(asks, budget, step):
    qty, remaining = 0.0, budget
    for price, size in asks:
        take = min(size, remaining / price)
        qty += take
        remaining -= take * price
        if remaining <= budget * 1e-10:
            break
    if remaining > budget * 1e-8:
        return None
    return round(math.floor(qty / step + 1e-9) * step, 12)


def quotation(books, market, direction, qty):
    buyer, seller = DIRECTIONS[direction]
    if not books.get(buyer) or not books.get(seller) or qty <= 0:
        return None
    buy = walk(books[buyer]["asks"], qty)
    sell = walk(books[seller]["bids"], qty)
    if buy is None or sell is None:
        return None
    if any(qty < market[v]["min_base"] - 1e-12 for v in (buyer, seller)):
        return None
    if (buy < market[buyer]["min_quote_usd"] - 1e-8
            or sell < market[seller]["min_quote_usd"] - 1e-8):
        return None
    edge = (sell * (1 - market[seller]["fee_bps"] / 1e4)
            - buy * (1 + market[buyer]["fee_bps"] / 1e4))
    return {"qty": qty, "buy_usd": buy, "sell_usd": sell,
            "net_usd": edge, "net_bps": edge / buy * 1e4}


def target_quote(books, market, direction, budget):
    buyer, _ = DIRECTIONS[direction]
    if not books.get(buyer):
        return None
    qty = quantity_for_budget(books[buyer]["asks"], budget, market["step"])
    return quotation(books, market, direction, qty) if qty else None


def premium(books):
    if not all(books.get(v) for v in API):
        return None
    mids = {v: (books[v]["bids"][0][0] + books[v]["asks"][0][0]) / 2
            for v in API}
    return (mids["RH"] / mids["LIGHTER"] - 1) * 1e4


def common_markets(metadata, args):
    """Match exact symbols and reject known contract-spec mismatches."""
    indexed = {}
    for venue, items in metadata.items():
        indexed[venue] = {}
        for item in items:
            if (item.get("status") != "active"
                    or item.get("market_type") != "perp"
                    or item.get("is_frozen")):
                continue
            symbol = item["symbol"]
            if symbol in indexed[venue]:
                raise ValueError(f"ambiguous duplicate perpetual symbol: {venue}/{symbol}")
            indexed[venue][symbol] = item
    markets, excluded = {}, {}
    selected = set(args.symbols.split(",")) if args.symbols else None
    for symbol in sorted(set(indexed["RH"]) & set(indexed["LIGHTER"])):
        if selected and symbol not in selected:
            continue
        a, b = indexed["RH"][symbol], indexed["LIGHTER"][symbol]
        reason = None
        for field in ("multiplier", "settlement_type", "settlement_cap",
                      "start_timestamp", "end_timestamp", "outcome"):
            if field in a and field in b and finite(a[field]) != finite(b[field]):
                reason = f"contract mismatch: {field}"
                break
        if reason:
            excluded[symbol] = reason
            continue
        entry = {"step": 10 ** -min(int(a["supported_size_decimals"]),
                                    int(b["supported_size_decimals"])),
                 "manual_contract_review_required": True}
        for venue, item in (("RH", a), ("LIGHTER", b)):
            conversion = args.rh_quote_usd if venue == "RH" else args.lighter_quote_usd
            override = args.rh_fee_bps if venue == "RH" else args.lighter_fee_bps
            entry[venue] = {
                "market_id": int(item["market_id"]),
                "min_base": finite(item["min_base_amount"]),
                "min_quote_usd": finite(item["min_quote_amount"]) * conversion,
                # orderBooks reports percentage, not decimal fractions.
                "fee_bps": (finite(item["taker_fee"]) * 100
                            if override is None else override),
                "quote_usd": conversion,
            }
        markets[symbol] = entry
    if selected and selected - set(markets):
        raise ValueError(f"requested markets unavailable or incompatible: {sorted(selected - set(markets))}")
    return markets, excluded


async def discover(args):
    async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=20)) as session:
        async def one(venue):
            # Public GET only. No account, signer, auth or transaction endpoints.
            async with session.get(API[venue] + "/api/v1/orderBooks",
                                   params={"filter": "perp"}) as response:
                response.raise_for_status()
                data = await response.json()
                if data.get("code") != 200:
                    raise ValueError(f"{venue}: metadata request rejected")
                return data["order_books"]
        rh, lighter = await asyncio.gather(one("RH"), one("LIGHTER"))
    metadata = {"RH": rh, "LIGHTER": lighter}
    markets, excluded = common_markets(metadata, args)
    if not markets:
        raise ValueError("no compatible active common perpetual markets")
    return markets, excluded, metadata


class Book:
    def __init__(self):
        self.bids, self.asks = {}, {}
        self.nonce = None
        self.ready = False
        self.received = 0.0
        self.server_ts = None
        self.generation = 0

    def invalidate(self):
        self.ready = False
        self.nonce = None
        self.bids.clear()
        self.asks.clear()
        self.generation += 1

    def update(self, msg, snapshot, now=None):
        data = msg["order_book"]
        end, begin = data.get("nonce"), data.get("begin_nonce")
        if not snapshot:
            if not self.ready:
                return False
            if end is not None and self.nonce is not None and end <= self.nonce:
                return True  # duplicate/out-of-order; never roll a book backward
            if begin is None or self.nonce is None or begin != self.nonce:
                self.invalidate()
                return False
        else:
            self.invalidate()
        try:
            if end is None:
                raise ValueError("missing nonce")
            for key, side in (("bids", self.bids), ("asks", self.asks)):
                for level in data.get(key) or []:
                    price, size = finite(level["price"]), finite(level["size"])
                    if price <= 0 or size < 0:
                        raise ValueError("invalid price/size")
                    if size == 0:
                        side.pop(price, None)
                    else:
                        side[price] = size
            if self.bids and self.asks and max(self.bids) >= min(self.asks):
                raise ValueError("crossed book")
            self.nonce = end
            self.ready = True
            self.received = time.monotonic() if now is None else now
            self.server_ts = msg.get("last_updated_at", data.get("last_updated_at"))
            return True
        except (ValueError, TypeError, KeyError):
            self.invalidate()
            return False

    def snapshot(self, now, max_age_ms, levels, conversion):
        if not self.ready or not self.bids or not self.asks:
            return None
        age = (now - self.received) * 1000
        if age < 0 or age > max_age_ms:
            return None
        return {"bids": [[p * conversion, q] for p, q in sorted(self.bids.items(), reverse=True)[:levels]],
                "asks": [[p * conversion, q] for p, q in sorted(self.asks.items())[:levels]],
                "age_ms": age, "nonce": self.nonce, "generation": self.generation,
                "server_ts_raw": self.server_ts}


async def stream(venue, symbols, markets, books, stop):
    ids = {markets[s][venue]["market_id"]: s for s in symbols}
    backoff = 1
    while not stop.is_set():
        try:
            async with connect(WS[venue], max_size=2**24, open_timeout=15,
                               ping_interval=15, ping_timeout=15) as socket:
                for symbol in symbols:
                    books[venue][symbol].invalidate()
                for market_id in ids:
                    await socket.send(json.dumps({"type": "subscribe", "channel": f"order_book/{market_id}"}))
                    await asyncio.sleep(0.05)
                LOG.info("%s public websocket subscribed: %d markets", venue, len(ids))
                async for raw in socket:
                    msg = json.loads(raw)
                    if msg.get("type") == "ping":
                        await socket.send(json.dumps({"type": "pong"}))
                        continue
                    kind = msg.get("type")
                    if kind not in ("subscribed/order_book", "update/order_book"):
                        if kind == "error":
                            raise ValueError(f"public subscription error: {str(msg)[:300]}")
                        continue
                    channel = str(msg.get("channel", "")).replace(":", "/")
                    try:
                        symbol = ids[int(channel.rsplit("/", 1)[1])]
                    except (KeyError, ValueError, IndexError):
                        continue
                    if kind == "update/order_book" and not books[venue][symbol].ready:
                        continue  # already awaiting a snapshot; don't flood resubscriptions
                    valid = books[venue][symbol].update(msg, kind.startswith("subscribed"))
                    if not valid:
                        LOG.warning("%s/%s invalid or gapped book; resubscribing", venue, symbol)
                        channel = f"order_book/{markets[symbol][venue]['market_id']}"
                        await socket.send(json.dumps({"type": "unsubscribe", "channel": channel}))
                        await socket.send(json.dumps({"type": "subscribe", "channel": channel}))
                    backoff = 1
                    if stop.is_set():
                        break
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            LOG.warning("%s public feed disconnected: %r; retry %ss", venue, exc, backoff)
        finally:
            for symbol in symbols:
                books[venue][symbol].invalidate()
        try:
            await asyncio.wait_for(stop.wait(), timeout=backoff)
        except asyncio.TimeoutError:
            pass
        backoff = min(backoff * 2, 30)


class ProbeWriter:
    def __init__(self, directory):
        self.directory = directory
        self.file = None
        self.day = None

    def write(self, rows):
        grouped = {}
        for row in rows:
            day = datetime.fromtimestamp(row["ts"], timezone.utc).strftime("%Y%m%d")
            grouped.setdefault(day, []).append(row)
        # Each batch is a complete gzip member. Readers use the committed byte
        # offsets in live.json, never an in-progress compression stream.
        for day, batch in grouped.items():
            with gzip.open(self.directory / f"probes-{day}.jsonl.gz", "at", encoding="utf-8") as file:
                for row in batch:
                    file.write(json.dumps(row, separators=(",", ":"), allow_nan=False) + "\n")

    def close(self):
        if self.file:
            self.file.close()
        self.file = None


def save_json(path, data):
    temporary = path.with_name(path.name + "." + uuid.uuid4().hex[:8] + ".tmp")
    temporary.write_text(json.dumps(data, ensure_ascii=False, allow_nan=False), encoding="utf-8")
    temporary.replace(path)


async def collect(args):
    markets, excluded, metadata = await discover(args)
    run_id = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ") + "-" + uuid.uuid4().hex[:8]
    directory = Path(args.output) / run_id
    directory.mkdir(parents=True, exist_ok=False)
    manifest = {"schema": SCHEMA, "run_id": run_id, "start_ts": time.time(),
                "arguments": vars(args), "markets": markets, "excluded": excluded,
                "raw_metadata": metadata, "funding_observed": False,
                "limitations": ["No orders or accounts are accessed.",
                    "Metadata matching does not verify oracle/contract equivalence.",
                    "Quote conversions are constant user assumptions.",
                    "Book age is local receipt age, not exchange/network latency.",
                    "Samples are periodic probes, not a complete tick archive.",
                    "Market universe and fee metadata are fixed at startup."]}
    (directory / "manifest.json").write_text(json.dumps(manifest, indent=2), encoding="utf-8")
    books = {v: {s: Book() for s in markets} for v in API}
    stop = asyncio.Event()
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        try:
            loop.add_signal_handler(sig, stop.set)
        except (NotImplementedError, RuntimeError):
            pass
    tasks = []
    symbols = list(markets)
    for venue in API:
        for offset in range(0, len(symbols), args.markets_per_connection):
            tasks.append(asyncio.create_task(stream(venue,
                symbols[offset:offset + args.markets_per_connection], markets, books, stop)))
    writer = ProbeWriter(directory)
    deadline = time.monotonic() + args.hours * 3600 if args.hours else math.inf
    counts = Counter()
    analysis_task = None
    next_analysis = time.monotonic() + args.analysis_every_minutes * 60
    save_json(directory / "live.json", {"run_id": run_id, "state": "connecting",
              "updated_ts": time.time(), "markets": [], "committed": {}})
    LOG.info("READ ONLY: %d markets; output=%s; no keys required", len(markets), directory)

    def capture(symbol):
        now = time.monotonic()
        return {v: books[v][symbol].snapshot(now, args.max_book_age_ms,
                    args.depth_levels, markets[symbol][v]["quote_usd"]) for v in API}

    async def probe(symbol):
        started = time.monotonic()
        row = {"symbol": symbol, "ts": time.time(), "entry": capture(symbol)}
        await asyncio.sleep(max(0, started + args.latency_ms / 1000 - time.monotonic()))
        row["delayed"] = capture(symbol)
        row["elapsed_ms"] = (time.monotonic() - started) * 1000
        row["delay_ts"] = time.time()
        row["timing_valid"] = row["elapsed_ms"] <= args.latency_ms + args.max_lag_ms
        row["continuity_valid"] = all(
            row["entry"].get(v) and row["delayed"].get(v)
            and row["entry"][v]["generation"] == row["delayed"][v]["generation"]
            for v in API)
        return row

    try:
        last_log = 0
        while not stop.is_set() and time.monotonic() < deadline:
            batch_start = time.monotonic()
            rows = await asyncio.gather(*(probe(s) for s in symbols))
            rows.sort(key=lambda row: row["ts"])
            # Compression/file I/O must not block the websocket event loop.
            await loop.run_in_executor(None, writer.write, rows)
            counts["probes"] += len(rows)
            counts["usable"] += sum(r["timing_valid"] and r["continuity_valid"] for r in rows)
            live_markets = []
            for row in rows:
                symbol = row["symbol"]
                live_markets.append({"symbol": symbol, "premium_bps": premium(row["entry"]),
                    "usable": bool(row["timing_valid"] and row["continuity_valid"]),
                    "elapsed_ms": row["elapsed_ms"],
                    "quotes": {direction: target_quote(row["entry"], markets[symbol], direction, args.notional)
                               for direction in DIRECTIONS}})
            save_json(directory / "live.json", {"run_id": run_id, "state": "collecting",
                "updated_ts": time.time(), "counts": dict(counts), "markets": live_markets,
                "committed": {p.name: p.stat().st_size for p in directory.glob("probes-*.jsonl.gz")}})
            if (args.analysis_every_minutes > 0 and time.monotonic() >= next_analysis
                    and (analysis_task is None or analysis_task.done())):
                if analysis_task is not None:
                    analysis_task.result()
                analysis_task = asyncio.ensure_future(loop.run_in_executor(
                    None, analyze, argparse.Namespace(**{**vars(args), "input": str(directory)})))
                next_analysis = time.monotonic() + args.analysis_every_minutes * 60
            if time.monotonic() - last_log >= 60:
                LOG.info("probes=%d usable=%d directory=%s", counts["probes"], counts["usable"], directory)
                last_log = time.monotonic()
            remaining = min(deadline, batch_start + args.interval_ms / 1000) - time.monotonic()
            if remaining > 0:
                try:
                    await asyncio.wait_for(stop.wait(), timeout=remaining)
                except asyncio.TimeoutError:
                    pass
    finally:
        stop.set()
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        writer.close()
        if analysis_task is not None:
            await analysis_task
        manifest["end_ts"] = time.time()
        manifest["counts"] = dict(counts)
        save_json(directory / "manifest.json", manifest)
        live = json.loads((directory / "live.json").read_text(encoding="utf-8"))
        live.update(state="stopped", updated_ts=time.time())
        save_json(directory / "live.json", live)
        if counts["probes"]:
            await loop.run_in_executor(None, analyze, argparse.Namespace(**{**vars(args), "input": str(directory)}))
        LOG.info("collection finished: %s", directory)


class LimitedReader(io.RawIOBase):
    def __init__(self, source, limit):
        self.source, self.remaining = source, limit

    def read(self, size=-1):
        size = self.remaining if size < 0 else min(self.remaining, size)
        value = self.source.read(size)
        self.remaining -= len(value)
        return value

    def readable(self):
        return True


def observations(directory, committed):
    for name, size in sorted(committed.items()):
        if Path(name).name != name or not name.startswith("probes-") or not name.endswith(".jsonl.gz"):
            raise ValueError("invalid probe file name")
        with open(directory / name, "rb") as raw:
            with gzip.GzipFile(fileobj=LimitedReader(raw, size)) as data:
                for line in data:
                    yield json.loads(line)


def usable(row):
    return (row["timing_valid"] and row["continuity_valid"]
            and row["delay_ts"] >= row["ts"]
            and abs((row["delay_ts"] - row["ts"]) * 1000 - row["elapsed_ms"]) < 100)


def adjusted(quote, reserve_bps):
    return quote["net_usd"] - quote["buy_usd"] * reserve_bps / 1e4


class Shadow:
    """One hypothetical position per symbol; decisions never use future books.

    Both legs are assumed to fill fully at the delayed visible VWAP. There is
    no queue/partial-fill/impact prediction and no claim of actual fill rate.
    """
    def __init__(self, on_cycle=None):
        self.position = None
        self.closed_usd = 0.0
        self.mark = None
        self.peak = 0.0
        self.drawdown = 0.0
        self.censored = 0
        self.cycles = deque(maxlen=2000)
        self.cycle_count = self.wins = self.forced_count = 0
        self.holds, self.returns = Reservoir(), Reservoir()
        self.positive_hours = Counter()
        self.on_cycle = on_cycle
        self.last_available = -math.inf

    def update(self, row, market, hurdles, args):
        if row["ts"] < self.last_available:
            self.censored += 1
            return
        entry = {d: target_quote(row["entry"], market, d, args.notional) for d in DIRECTIONS}
        if self.position is None:
            choices = [(q["net_bps"] - args.reserve_bps - hurdles[d], d)
                       for d, q in entry.items() if q is not None
                       and q["net_bps"] - args.reserve_bps >= hurdles[d]]
            if not choices:
                return
            # Select at t0, then execute at t0+delay even if the edge vanished.
            _, direction = max(choices)
            delayed = quotation(row["delayed"], market, direction, entry[direction]["qty"]) if usable(row) else None
            if delayed is None:
                self.censored += 1
                return
            self.position = {"direction": direction, "qty": delayed["qty"],
                "opened_ts": row["delay_ts"], "entry_usd": adjusted(delayed, args.reserve_bps),
                "notional": delayed["buy_usd"]}
            self.last_available = row["delay_ts"]
        else:
            position = self.position
            reverse = "sell_rh" if position["direction"] == "buy_rh" else "buy_rh"
            close_now = quotation(row["entry"], market, reverse, position["qty"])
            age = row["ts"] - position["opened_ts"]
            holding = position["notional"] * args.holding_cost_bps_hour / 1e4 * max(age, 0) / 3600
            projected = (position["entry_usd"] + adjusted(close_now, args.reserve_bps) - holding
                         if close_now is not None else None)
            forced = age >= args.max_hold_seconds
            should_close = forced or (projected is not None and projected >= position["notional"] * args.cycle_target_bps / 1e4)
            if should_close:
                delayed = quotation(row["delayed"], market, reverse, position["qty"]) if usable(row) else None
                if delayed is None:
                    self.censored += 1
                else:
                    age = row["delay_ts"] - position["opened_ts"]
                    holding = position["notional"] * args.holding_cost_bps_hour / 1e4 * age / 3600
                    result = position["entry_usd"] + adjusted(delayed, args.reserve_bps) - holding
                    self.closed_usd += result
                    cycle = {"opened_ts": position["opened_ts"], "closed_ts": row["delay_ts"],
                        "direction": position["direction"], "qty": position["qty"], "hold_seconds": age,
                        "net_usd": result, "net_bps": result / position["notional"] * 1e4,
                        "forced": forced}
                    self.cycles.append(cycle)
                    self.cycle_count += 1
                    self.wins += result > 0
                    self.forced_count += forced
                    self.holds.add(age)
                    self.returns.add(cycle["net_bps"])
                    self.positive_hours[int(row["delay_ts"] // 3600)] += max(0, result)
                    if self.on_cycle:
                        self.on_cycle(cycle)
                    self.position = None
                    self.mark = None
                    self.last_available = row["delay_ts"]
        # Sampled liquidation mark, including the other side of the spread.
        if self.position is not None:
            position = self.position
            reverse = "sell_rh" if position["direction"] == "buy_rh" else "buy_rh"
            quote = quotation(row["delayed"], market, reverse, position["qty"]) if usable(row) else None
            self.mark = None
            if quote is not None:
                age = max(0, row["delay_ts"] - position["opened_ts"])
                holding = position["notional"] * args.holding_cost_bps_hour / 1e4 * age / 3600
                self.mark = position["entry_usd"] + adjusted(quote, args.reserve_bps) - holding
        equity = self.closed_usd + self.mark if self.mark is not None else self.closed_usd if self.position is None else None
        if equity is not None:
            self.peak = max(self.peak, equity)
            self.drawdown = min(self.drawdown, equity - self.peak)


def analyze(args):
    directory = Path(args.input).resolve()
    manifest = json.loads((directory / "manifest.json").read_text(encoding="utf-8"))
    if manifest.get("schema") != SCHEMA:
        raise ValueError("unsupported collection schema")
    live_path = directory / "live.json"
    if live_path.exists():
        committed = json.loads(live_path.read_text(encoding="utf-8"))["committed"]
    else:
        committed = {p.name: p.stat().st_size for p in directory.glob("probes-*.jsonl.gz")}
    if not committed:
        raise ValueError("no committed probes yet")
    markets = manifest["markets"]
    cutoff = manifest["start_ts"] + args.train_hours * 3600
    training = {s: {"premium": Reservoir(), "buy_rh": Reservoir(), "sell_rh": Reservoir()}
                for s in markets}
    last_ts = manifest["start_ts"]
    for row in observations(directory, committed):
        last_ts = max(last_ts, row["ts"])
        if row["ts"] >= cutoff or row["symbol"] not in markets:
            continue
        prem = premium(row["entry"])
        if prem is not None:
            training[row["symbol"]]["premium"].add(prem)
        for direction in DIRECTIONS:
            quote = target_quote(row["entry"], markets[row["symbol"]], direction, args.notional)
            if quote:
                training[row["symbol"]][direction].add(quote["net_bps"] - args.reserve_bps)
    calibration, stats = {}, {}
    output = directory / ("analysis-" + datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ") + "-" + uuid.uuid4().hex[:6])
    output.mkdir()
    cycle_file = open(output / "cycles.csv", "w", newline="", encoding="utf-8-sig")
    cycle_writer = csv.DictWriter(cycle_file, fieldnames=["symbol", "opened_ts", "closed_ts", "direction", "qty", "hold_seconds", "net_usd", "net_bps", "forced"])
    cycle_writer.writeheader()
    for symbol, values in training.items():
        center = quantile(values["premium"].values, .5)
        train_expected = args.train_hours * 3600 / (manifest["arguments"]["interval_ms"] / 1000)
        train_coverage = min(values[d].n for d in DIRECTIONS) / train_expected
        valid = (center is not None and train_coverage >= .8
                 and all(values[d].n >= args.min_train_samples for d in DIRECTIONS))
        hurdles = None
        if valid:
            hurdles = {d: max(quantile(values[d].values, .9),
                        (center if d == "sell_rh" else -center) + args.min_band_bps)
                       for d in DIRECTIONS}
        calibration[symbol] = {"center_bps": center, "hurdles_bps": hurdles,
            "training_depth_coverage": min(train_coverage, 1),
            "training_samples": {d: values[d].n for d in DIRECTIONS}}
        stats[symbol] = {"probes": 0, "depth": 0, "delay_usable": 0,
            "signals": Counter(), "outcomes": Counter(), "survived": Counter(),
            "delayed_edge": {d: Reservoir() for d in DIRECTIONS},
            "elapsed": Reservoir(), "premium": Reservoir(),
            "shadow": Shadow(on_cycle=lambda cycle, symbol=symbol: cycle_writer.writerow({"symbol": symbol, **cycle})),
            "first": None, "last": None, "gaps": 0, "series": {}}
    interval = manifest["arguments"]["interval_ms"] / 1000
    for row in observations(directory, committed):
        symbol = row["symbol"]
        if row["ts"] < cutoff or symbol not in markets:
            continue
        stat, market = stats[symbol], markets[symbol]
        if stat["last"] is not None:
            if row["ts"] <= stat["last"]:
                raise ValueError(f"non-monotonic or duplicate probe: {symbol}")
            stat["gaps"] += row["ts"] - stat["last"] > interval * 2.5
        stat["first"] = row["ts"] if stat["first"] is None else stat["first"]
        stat["last"] = row["ts"]
        stat["probes"] += 1
        stat["elapsed"].add(row["elapsed_ms"])
        prem = premium(row["entry"])
        if prem is not None:
            stat["premium"].add(prem)
        quotes = {d: target_quote(row["entry"], market, d, args.notional) for d in DIRECTIONS}
        stat["depth"] += all(quotes.values())
        stat["delay_usable"] += bool(usable(row))
        bucket = int(row["ts"] // 600) * 600
        sample = stat["series"].setdefault(bucket, {"ts": bucket, "n": 0, "sum": 0, "buy_sum": 0, "sell_sum": 0, "depth_n": 0})
        if prem is not None:
            sample["n"] += 1
            sample["sum"] += prem
        if all(quotes.values()):
            sample["depth_n"] += 1
            sample["buy_sum"] += quotes["buy_rh"]["net_bps"] - args.reserve_bps
            sample["sell_sum"] += quotes["sell_rh"]["net_bps"] - args.reserve_bps
        hurdles = calibration[symbol]["hurdles_bps"]
        if hurdles is None:
            continue
        for direction, quote in quotes.items():
            if quote is None or quote["net_bps"] - args.reserve_bps < hurdles[direction]:
                continue
            stat["signals"][direction] += 1
            delayed = quotation(row["delayed"], market, direction, quote["qty"]) if usable(row) else None
            if delayed:
                stat["outcomes"][direction] += 1
                edge = delayed["net_bps"] - args.reserve_bps
                stat["delayed_edge"][direction].add(edge)
                stat["survived"][direction] += edge >= hurdles[direction]
        stat["shadow"].update(row, market, hurdles, args)
    rows, details = [], {}
    for symbol, stat in stats.items():
        model = stat["shadow"]
        cycles = model.cycles
        test_hours = ((stat["last"] - stat["first"]) / 3600 if stat["probes"] > 1 else 0)
        coverage = stat["depth"] / stat["probes"] if stat["probes"] else 0
        net = model.closed_usd + model.mark if model.mark is not None else model.closed_usd if model.position is None else None
        hourly = model.positive_hours
        positive = sum(hourly.values())
        status = ("training" if last_ts < cutoff else "insufficient_training" if calibration[symbol]["hurdles_bps"] is None
                  else "insufficient_test" if test_hours < args.min_test_hours or model.cycle_count < args.min_cycles
                  else "low_coverage" if coverage < .9 or stat["delay_usable"] / stat["probes"] < .9 or stat["gaps"]
                  else "incomplete_shadow" if model.censored else "unmarked_inventory" if net is None
                  else "positive_shadow_sample" if net > 0 else "negative_shadow_sample")
        result = {"symbol": symbol, "status": status, "test_hours": test_hours,
            "test_probes": stat["probes"], "depth_coverage": coverage,
            "delay_coverage": stat["delay_usable"] / stat["probes"] if stat["probes"] else 0,
            "probe_elapsed_p95_ms": quantile(stat["elapsed"].values, .95),
            "train_center_bps": calibration[symbol]["center_bps"],
            "test_center_bps": quantile(stat["premium"].values, .5),
            "shadow_cycles": model.cycle_count, "shadow_closed_usd": model.closed_usd,
            "shadow_open_liquidation_usd": model.mark,
            "shadow_net_including_open_usd": net, "open_inventory": model.position is not None,
            "shadow_win_rate": model.wins / model.cycle_count if model.cycle_count else None,
            "cycle_p10_bps": quantile(model.returns.values, .1),
            "hold_p50_seconds": quantile(model.holds.values, .5),
            "hold_p95_seconds": quantile(model.holds.values, .95),
            "forced_exits": model.forced_count, "censored_decisions": model.censored,
            "sampled_max_drawdown_usd": model.drawdown,
            "best_hour_positive_profit_share": max(hourly.values()) / positive if positive else None,
            "observation_gaps": stat["gaps"], "manual_contract_review_required": True,
            "funding_observed": False}
        for direction in DIRECTIONS:
            count, known = stat["signals"][direction], stat["outcomes"][direction]
            result.update({direction + "_signals": count,
                direction + "_outcomes": known,
                direction + "_survival_lower_bound": stat["survived"][direction] / count if count else None,
                direction + "_survival_known": stat["survived"][direction] / known if known else None,
                direction + "_delayed_edge_p10_bps": quantile(stat["delayed_edge"][direction].values, .1)})
        rows.append(result)
        details[symbol] = {"calibration": calibration[symbol], "cycles": list(cycles),
            "open_position": model.position, "series": [
                {"ts": s["ts"], "premium_bps": s["sum"] / s["n"] if s["n"] else None,
                 "buy_bps": s["buy_sum"] / s["depth_n"] if s["depth_n"] else None,
                 "sell_bps": s["sell_sum"] / s["depth_n"] if s["depth_n"] else None}
                for s in stat["series"].values()]}
    rows.sort(key=lambda r: (r["status"] == "positive_shadow_sample",
                             r["shadow_net_including_open_usd"] if r["shadow_net_including_open_usd"] is not None else -math.inf), reverse=True)
    cycle_file.close()
    report = {"schema": SCHEMA, "run_id": manifest["run_id"], "generated_ts": time.time(),
        "train_end_ts": cutoff, "data_end_ts": last_ts, "arguments": vars(args),
        "rows": rows, "details": details, "model": "delayed_full_fill_shadow",
        "limitations": ["Shadow results are not actual PnL or fill probabilities.",
            "Both legs are assumed fully executable at visible delayed depth; no queue/partial fill prediction.",
            "Unobserved funding, oracle, collateral and settlement risks require manual review.",
            "Reserve per crossing and hourly holding cost are explicit assumptions, not measured costs.",
            "Distribution percentiles use deterministic reservoirs of at most 10000 observations per metric.",
            "Delayed survival is conditional on the frozen historical signal, not an actual fill rate.",
            "Test windows and sampled marks may miss intraprobe price extremes."]}
    save_json(output / "report.json", report)
    with open(output / "ranking.csv", "w", newline="", encoding="utf-8-sig") as file:
        writer = csv.DictWriter(file, fieldnames=list(rows[0]))
        writer.writeheader()
        writer.writerows(rows)
    # A single atomic pointer exposes only fully completed reports to the UI.
    save_json(directory / "latest_analysis.json", {"directory": output.name})
    LOG.info("analysis saved: %s (%d markets)", output, len(rows))
    return report


async def serve(args):
    from aiohttp import web
    root = Path(args.input).resolve()
    assets = Path(__file__).resolve().parent / "screener_web"
    cache = {"name": None, "report": None}

    def run_directory():
        if (root / "manifest.json").exists():
            return root
        available = sorted(p for p in root.glob("*/manifest.json"))
        return available[-1].parent if available else None

    def load_report(directory):
        pointer = directory / "latest_analysis.json"
        if not pointer.exists():
            return None
        name = json.loads(pointer.read_text(encoding="utf-8"))["directory"]
        if Path(name).name != name or not name.startswith("analysis-"):
            raise ValueError("invalid analysis pointer")
        path = directory / name / "report.json"
        if str(path) != cache["name"]:
            cache.update(name=str(path), report=json.loads(path.read_text(encoding="utf-8")))
        return cache["report"]

    async def status(request):
        directory = run_directory()
        if directory is None:
            return web.json_response({"live": None, "manifest": None, "report": None})
        manifest = json.loads((directory / "manifest.json").read_text(encoding="utf-8"))
        live_path = directory / "live.json"
        live = json.loads(live_path.read_text(encoding="utf-8")) if live_path.exists() else None
        report = load_report(directory)
        return web.json_response({"live": live,
            "manifest": {k: manifest.get(k) for k in ("run_id", "start_ts", "end_ts", "arguments", "excluded")},
            "report": {k: v for k, v in report.items() if k != "details"} if report else None})

    async def detail(request):
        directory = run_directory()
        report = load_report(directory) if directory else None
        symbol = request.query.get("symbol", "")
        if report is None or symbol not in report["details"]:
            raise web.HTTPNotFound(text="No completed analysis for this symbol")
        return web.json_response(report["details"][symbol])

    async def index(request):
        return web.FileResponse(assets / "index.html")

    app = web.Application()
    app.router.add_get("/", index)
    app.router.add_get("/api/status", status)
    app.router.add_get("/api/detail", detail)
    app.router.add_static("/assets/", assets, show_index=False)
    runner = web.AppRunner(app)
    await runner.setup()
    try:
        await web.TCPSite(runner, args.host, args.port).start()
        LOG.info("READ ONLY dashboard: http://%s:%d", args.host, args.port)
        await asyncio.Event().wait()
    finally:
        await runner.cleanup()


def add_analysis_options(parser):
    parser.add_argument("--notional", type=float, default=300, help="target buy notional per crossing, USD equivalent")
    parser.add_argument("--train-hours", type=float, default=24, help="fit only the first N hours; never reuse test data")
    parser.add_argument("--min-train-samples", type=int, default=100)
    parser.add_argument("--min-test-hours", type=float, default=24)
    parser.add_argument("--min-cycles", type=int, default=5)
    parser.add_argument("--min-band-bps", type=float, default=2)
    parser.add_argument("--reserve-bps", type=float, default=2, help="assumed extra cost per crossing, total for both legs")
    parser.add_argument("--holding-cost-bps-hour", type=float, default=0,
                        help="assumed combined holding cost; funding is NOT observed")
    parser.add_argument("--cycle-target-bps", type=float, default=2,
                        help="minimum projected full round-trip profit for exit")
    parser.add_argument("--max-hold-seconds", type=float, default=3600,
                        help="attempt shadow liquidation on next valid probe after this time")


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = parser.add_subparsers(dest="command", required=True)
    listing = sub.add_parser("list", help="discover public common perpetual markets")
    collection = sub.add_parser("collect", help="collect public delayed depth probes")
    for command in (listing, collection):
        command.add_argument("--symbols", default="", help="optional exact comma-separated symbols; empty = all common")
        command.add_argument("--rh-fee-bps", type=float, default=None)
        command.add_argument("--lighter-fee-bps", type=float, default=None)
        command.add_argument("--rh-quote-usd", type=float, default=1)
        command.add_argument("--lighter-quote-usd", type=float, default=1)
    collection.add_argument("--hours", type=float, default=72, help="0 = until Ctrl-C")
    collection.add_argument("--output", default="logs/screener")
    collection.add_argument("--interval-ms", type=float, default=5000, help="probe initiation interval, not feed update rate")
    collection.add_argument("--latency-ms", type=float, default=350)
    collection.add_argument("--max-lag-ms", type=float, default=100, help="censor probes with excessive scheduler lag")
    collection.add_argument("--max-book-age-ms", type=float, default=1000)
    collection.add_argument("--depth-levels", type=int, default=10)
    collection.add_argument("--markets-per-connection", type=int, default=25)
    collection.add_argument("--analysis-every-minutes", type=float, default=60, help="0 = final analysis only")
    add_analysis_options(collection)
    analysis = sub.add_parser("analyze", help="offline train/test shadow research; no network access")
    analysis.add_argument("--input", required=True)
    add_analysis_options(analysis)
    serving = sub.add_parser("serve", help="local read-only dashboard, no trading controls")
    serving.add_argument("--input", default="logs/screener", help="run folder or parent (auto-select newest run)")
    serving.add_argument("--host", default="127.0.0.1")
    serving.add_argument("--port", type=int, default=8765)
    args = parser.parse_args()
    for name, value in vars(args).items():
        if isinstance(value, (float, int)) and (not math.isfinite(value) or value < 0):
            parser.error(f"--{name.replace('_', '-')} must be finite and non-negative")
    for name in ("notional", "train_hours", "max_hold_seconds", "depth_levels", "markets_per_connection", "rh_quote_usd", "lighter_quote_usd", "port", "min_train_samples", "min_cycles"):
        if hasattr(args, name) and getattr(args, name) == 0:
            parser.error(f"--{name.replace('_', '-')} must be positive")
    if args.command == "collect" and args.interval_ms <= args.latency_ms + args.max_lag_ms:
        parser.error("--interval-ms must exceed latency-ms + max-lag-ms")
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    try:
        if args.command == "list":
            markets, excluded, _ = asyncio.run(discover(args))
            for symbol, market in markets.items():
                print(f"{symbol:16s} RH={market['RH']['market_id']:3d} LIGHTER={market['LIGHTER']['market_id']:3d} fee_bps={market['RH']['fee_bps']:g}+{market['LIGHTER']['fee_bps']:g} manual_spec_review=required")
            print(f"\n{len(markets)} common active markets; {len(excluded)} known spec mismatches excluded")
        elif args.command == "collect":
            asyncio.run(collect(args))
        elif args.command == "analyze":
            report = analyze(args)
            print(f"{report['run_id']}: {len(report['rows'])} markets; results are shadow estimates, not actual PnL")
        else:
            asyncio.run(serve(args))
    except KeyboardInterrupt:
        pass
    except (ValueError, OSError, aiohttp.ClientError) as exc:
        LOG.error("%s", exc)
        raise SystemExit(1)


if __name__ == "__main__":
    main()
