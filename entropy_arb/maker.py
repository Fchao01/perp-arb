"""Maker mode: post-only quotes on the primary venue, hedged on the hedge venue.

Both sides are quoted on the primary (RH) around

    ref = hedge_mid * (1 + center_bps / 1e4)
    bid = ref * (1 - half_spread),  ask = ref * (1 + half_spread)

where center_bps is a time-weighted EMA of the mid-to-mid premium (the same
center tools/maker_shadow.py simulates). Quotes are post-only, so they never
take liquidity; a quote is modified only when its target moves by at least
requote_bps. Every fill is hedged at once with a taker order on the hedge
venue, so the position held is the basis, not the outright.

Quoting is pulled (both quotes cancelled) whenever the market is unsafe:

  * either book stale, a venue down / rate-limited / its order stream not ready
  * the hedge mid moved more than max_move_bps within move_window_sec, or more
    than max_long_move_bps within long_move_window_sec, or the primary mid
    moved more than max_primary_move_bps within move_window_sec
  * the hedge venue's spread is wider than max_hedge_spread_bps
  * the premium is more than max_basis_dev_bps away from the center
  * more than max_fills_per_min fills in the last minute (one-way flow)
  * a configured blackout window (e.g. around the US open / close)
  * a hedge that did not complete
The price / flow triggers keep quoting paused for pause_sec. A session
mark-to-market loss beyond max_loss_usd stops quoting for good.

Safety around the process itself: every resting order in the market is
cancelled on start and on shutdown, and (dead_man_sec > 0) an exchange-side
scheduled cancel-all is kept armed, so quotes do not outlive a crashed or
disconnected process.
"""
from __future__ import annotations

import asyncio
import csv
import logging
import math
import os
import time
from collections import deque
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Deque, Dict, Optional, Tuple

from .book import floor_step
from .center import CenterTracker
from .config import parse_window
from .engine import Engine

log = logging.getLogger("maker")

OPEN_STATUSES = {"in-progress", "pending", "open"}

FILL_HEADER = ["ts", "time_utc", "side", "coi", "qty", "px", "quote_px",
               "fair_mid", "center_bps", "premium_bps", "hedge_px",
               "hedge_status", "edge_bps", "edge_usd", "primary_position",
               "hedge_position"]


def _utc(ts: float) -> str:
    return datetime.fromtimestamp(ts, timezone.utc).strftime(
        "%Y-%m-%dT%H:%M:%S.%f")[:-3] + "Z"


# ---------------------------------------------------------------- helpers

class WindowRange:
    """Min and max of a value over a sliding time window (monotonic deques,
    O(1) amortized per sample)."""

    def __init__(self, window_sec: float) -> None:
        self.window = window_sec
        self._hi: Deque[Tuple[float, float]] = deque()
        self._lo: Deque[Tuple[float, float]] = deque()

    def add(self, t: float, x: float) -> None:
        while self._hi and self._hi[-1][1] <= x:
            self._hi.pop()
        self._hi.append((t, x))
        while self._lo and self._lo[-1][1] >= x:
            self._lo.pop()
        self._lo.append((t, x))
        self._expire(t)

    def _expire(self, now: float) -> None:
        cutoff = now - self.window
        while self._hi and self._hi[0][0] < cutoff:
            self._hi.popleft()
        while self._lo and self._lo[0][0] < cutoff:
            self._lo.popleft()

    def move_bps(self, now: float, current: float) -> float:
        """Largest distance (bps) between `current` and any value seen in
        the window."""
        self._expire(now)
        if not self._hi or current <= 0:
            return 0.0
        hi, lo = self._hi[0][1], self._lo[0][1]
        return max(hi / current - 1.0, 1.0 - lo / current) * 1e4


class Blackout:
    """Weekday time-of-day windows in a named time zone (DST-aware)."""

    def __init__(self, windows, tz_name: str) -> None:
        self.windows = [(w, *parse_window(w)) for w in windows]
        self.tz = timezone.utc
        if self.windows:
            try:
                from zoneinfo import ZoneInfo
                self.tz = ZoneInfo(tz_name)
            except Exception:
                log.warning("time zone %r unavailable — blackout windows are "
                            "read as UTC", tz_name)

    def active(self, ts: float) -> Optional[str]:
        if not self.windows:
            return None
        dt = datetime.fromtimestamp(ts, self.tz)
        if dt.weekday() >= 5:
            return None
        m = dt.hour * 60 + dt.minute
        for text, a, b in self.windows:
            if (a <= m < b) if a < b else (m >= a or m < b):
                return text
        return None


def round_to_tick(px: float, tick: float, up: bool) -> float:
    n = px / tick
    n = math.ceil(n - 1e-9) if up else math.floor(n + 1e-9)
    return round(n * tick, 10)


def quote_prices(ref: float, half_spread_bps: float, primary_bid: float,
                 primary_ask: float, tick: float
                 ) -> Tuple[Optional[float], Optional[float]]:
    """Bid / ask around ref, never crossing the primary's book (post-only),
    bids rounded down and asks up so a quote is never tighter than asked."""
    h = half_spread_bps / 1e4
    bid = min(ref * (1.0 - h), primary_ask - tick)
    ask = max(ref * (1.0 + h), primary_bid + tick)
    bid = round_to_tick(bid, tick, up=False)
    ask = round_to_tick(ask, tick, up=True)
    if bid <= 0 or ask <= bid:
        return None, None
    return bid, ask


def maker_edge_bps(sign: int, px: float, hedge_px: float, center_bps: float,
                   maker_fee_bps: float, hedge_fee_bps: float) -> float:
    """Edge of one hedged fill against the center, after fees (the formula of
    tools/maker_shadow.py). sign +1: bought on the primary, sold the hedge."""
    mf, hf = maker_fee_bps / 1e4, hedge_fee_bps / 1e4
    if sign > 0:
        prem = (px * (1 + mf)) / (hedge_px * (1 - hf)) - 1.0
        return center_bps - prem * 1e4
    prem = (px * (1 - mf)) / (hedge_px * (1 + hf)) - 1.0
    return prem * 1e4 - center_bps


@dataclass
class OrderRec:
    coi: int
    sign: int            # +1 bid (we buy), -1 ask (we sell)
    px: float
    qty: float
    filled_base: float = 0.0
    filled_quote: float = 0.0
    terminal: bool = False
    created: float = 0.0


class QuoteSide:
    def __init__(self, sign: int) -> None:
        self.sign = sign
        self.coi: Optional[int] = None
        self.busy = False


# ----------------------------------------------------------------- engine

class MakerEngine(Engine):
    """Reuses the taker engine's venues, feeds, recorder, reconciliation and
    lifecycle; replaces the strategy (quoting instead of taking) and the
    hedge (always on the hedge venue)."""

    MAX_ORDER_RECS = 2000

    def __init__(self, cfg, record_only: bool = False) -> None:
        super().__init__(cfg, record_only=record_only)
        m = self.mk = cfg.maker
        self.mcenter = CenterTracker(None, tau_sec=m.center_window_sec,
                                     warmup_sec=m.center_warmup_sec,
                                     reset_gap_sec=m.center_window_sec)
        self.sides: Dict[int, QuoteSide] = {1: QuoteSide(1), -1: QuoteSide(-1)}
        self.orders: Dict[int, OrderRec] = {}
        self._actions: Deque[float] = deque()
        self.hedge_move = WindowRange(m.move_window_sec)
        self.hedge_move_long = WindowRange(m.long_move_window_sec)
        self.primary_move = WindowRange(m.move_window_sec)
        self.blackout = Blackout(m.blackout_windows, m.blackout_tz)
        self._fill_times: Deque[float] = deque()
        self._pause_until = 0.0
        self._pause_reason: Optional[str] = None
        self._block_reason: Optional[str] = None
        self._halt_reason: Optional[str] = None
        self._cancel_all_needed = False
        self._cancel_all_busy = False
        self._hedge_problem = False
        self._hedge_fail_streak = 0
        self._hedge_lock = asyncio.Lock()
        self._hedge_evt = asyncio.Event()
        self._unhedged: Deque[list] = deque()   # [row, qty still unhedged]
        self._rec_pending: Dict[str, tuple] = {}
        self._fetch_fails: Dict[str, int] = {}
        self.maker_fills = 0
        self.maker_volume = 0.0
        self.maker_edge_usd = 0.0
        self._fills_fh = None
        self._fills_w = None
        self._action_tasks: set = set()

    # ------------------------------------------------------------ helpers

    @property
    def primary(self):
        return self.entropy

    def _now(self) -> float:
        return time.time()

    def _budget_ok(self, now: float) -> bool:
        while self._actions and now - self._actions[0] > 60.0:
            self._actions.popleft()
        return len(self._actions) < self.mk.max_actions_per_min

    def _premium_mid_bps(self) -> Optional[float]:
        pm, hm = self.primary.book.mid(), self.hedge.book.mid()
        if not (pm and hm):
            return None
        return (pm / hm - 1.0) * 1e4

    def _pause(self, now: float, reason: str) -> str:
        self._pause_until = now + self.mk.pause_sec
        self._pause_reason = reason
        return f"paused {self.mk.pause_sec:g}s: {reason}"

    def _halt(self, reason: str) -> None:
        if self._halt_reason is None:
            self._halt_reason = reason
            log.critical("[maker] quoting STOPPED: %s / 已停止报价", reason)
        self._update_evt.set()

    # ------------------------------------------------------------- guards

    def _sample(self, now: float) -> None:
        hm, pm = self.hedge.book.mid(), self.primary.book.mid()
        if hm:
            self.hedge_move.add(now, hm)
            self.hedge_move_long.add(now, hm)
        if pm:
            self.primary_move.add(now, pm)

    def quote_block_reason(self, now: float) -> Optional[str]:
        """Why quoting is not allowed right now (None = allowed)."""
        m, cfg = self.mk, self.cfg
        p, h = self.primary, self.hedge
        if self._halt_reason:
            return f"stopped: {self._halt_reason}"
        if self._cancel_all_needed:
            return "cancel failed — cancelling all resting orders"
        for v in (p, h):
            if not v.book.is_fresh(cfg.staleness_sec):
                return f"{v.name} book stale"
            if not v.ready_to_trade():
                return f"{v.name} order stream not ready"
            if v.key in self._venue_down:
                return f"{v.name} unreachable"
            if self._venue_limited(v):
                return f"{v.name} rate limited"
        if self._hedge_problem:
            return "hedge not completed — waiting for reconcile"
        if now < self._pause_until:
            return f"paused {self.mk.pause_sec:g}s: {self._pause_reason}"
        if not self.mcenter.ready(now) or self.mcenter.value is None:
            return "center warming up"
        bo = self.blackout.active(now)
        if bo:
            return f"blackout window {bo} ({m.blackout_tz})"
        hm, pm = h.book.mid(), p.book.mid()
        if not (hm and pm):
            return "no mid"
        mv = self.hedge_move.move_bps(now, hm)
        if mv > m.max_move_bps:
            return self._pause(now, f"{h.name} moved {mv:.1f} bps in "
                                    f"{m.move_window_sec:g}s")
        mv = self.hedge_move_long.move_bps(now, hm)
        if mv > m.max_long_move_bps:
            return self._pause(now, f"{h.name} moved {mv:.1f} bps in "
                                    f"{m.long_move_window_sec:g}s")
        mv = self.primary_move.move_bps(now, pm)
        if mv > m.max_primary_move_bps:
            return self._pause(now, f"{p.name} moved {mv:.1f} bps in "
                                    f"{m.move_window_sec:g}s")
        hb, ha = h.book.best_bid(), h.book.best_ask()
        spread = (ha / hb - 1.0) * 1e4 if hb and ha else float("inf")
        if spread > m.max_hedge_spread_bps:
            return f"{h.name} spread {spread:.1f} bps"
        prem = self._premium_mid_bps()
        if prem is not None and abs(prem - self.mcenter.value) > m.max_basis_dev_bps:
            return self._pause(now, f"premium {prem:+.1f} bps is "
                                    f"{prem - self.mcenter.value:+.1f} bps "
                                    f"from center")
        while self._fill_times and now - self._fill_times[0] > 60.0:
            self._fill_times.popleft()
        if len(self._fill_times) > m.max_fills_per_min:
            return self._pause(now, f"{len(self._fill_times)} fills in 60s")
        pnl = self.session_pnl()
        if pnl is not None and pnl < -m.max_loss_usd:
            self._halt(f"session loss ${pnl:+.2f} beyond max_loss_usd "
                       f"${m.max_loss_usd:g}")
            return f"stopped: {self._halt_reason}"
        return None

    # ------------------------------------------------------------ quoting

    def desired_quotes(self, now: float) -> Dict[int, Optional[Tuple[float, float]]]:
        """Target (px, qty) per side, None where that side must not quote."""
        p, h, m = self.primary, self.hedge, self.mk
        out: Dict[int, Optional[Tuple[float, float]]] = {1: None, -1: None}
        hm = h.book.mid()
        pb, pa = p.book.best_bid(), p.book.best_ask()
        if not (hm and pb and pa) or self.mcenter.value is None:
            return out
        ref = hm * (1.0 + self.mcenter.value / 1e4)
        tick = 10 ** -p.price_decimals
        bid, ask = quote_prices(ref, m.half_spread_bps, pb, pa, tick)
        if bid is None:
            return out
        step = 10 ** -p.size_decimals
        min_ntl = max(self.cfg.min_order_notional, p.min_quote)
        for sign, px in ((1, bid), (-1, ask)):
            qty = floor_step(m.quote_usd / px, step)
            if qty < p.min_base or qty * px < min_ntl:
                continue
            adds = sign * p.position >= 0
            if adds and (abs(p.position) + qty) * ref > m.max_position_usd:
                continue
            out[sign] = (px, qty)
        return out

    def _tick(self) -> None:
        now = self._now()
        self._sample(now)
        if self._cancel_all_needed and not self._cancel_all_busy:
            self._spawn_cancel_all()
        reason = self.quote_block_reason(now)
        if reason:
            if reason != self._block_reason:
                log.warning("[GUARD] quotes pulled: %s / 撤单暂停", reason)
            self._block_reason = reason
            for side in self.sides.values():
                self._manage_side(side, None, now)
            return
        if self._block_reason:
            log.info("[GUARD] quoting resumed / 恢复报价")
            self._block_reason = None
        targets = self.desired_quotes(now)
        for sign, side in self.sides.items():
            self._manage_side(side, targets[sign], now)

    def _manage_side(self, side: QuoteSide, target, now: float) -> None:
        if side.busy:
            return
        rec = self.orders.get(side.coi) if side.coi is not None else None
        if rec is not None and rec.terminal:
            side.coi, rec = None, None
        if target is None:
            if rec is not None:
                self._spawn(side, self._cancel(side, rec))
            return
        px, qty = target
        if rec is None:
            if self._budget_ok(now):
                self._spawn(side, self._place(side, px, qty))
            return
        if abs(px / rec.px - 1.0) * 1e4 < self.mk.requote_bps:
            return
        if not self._budget_ok(now):
            return
        if rec.filled_base > 0:
            self._spawn(side, self._replace(side, rec, px, qty))
        else:
            self._spawn(side, self._modify(side, rec, px, qty))

    def _spawn(self, side: QuoteSide, coro) -> None:
        side.busy = True

        async def run():
            try:
                await coro
            except asyncio.CancelledError:
                raise
            except Exception:
                log.exception("[maker] quote action failed")
            finally:
                side.busy = False
                self._update_evt.set()

        t = asyncio.get_running_loop().create_task(run())
        self._action_tasks.add(t)
        t.add_done_callback(self._action_tasks.discard)

    def _note_error(self, what: str, err: str) -> None:
        log.warning("[maker] %s failed: %s", what, err)
        if err.startswith("RATE_LIMITED"):
            self._mark_limited(self.primary)

    async def _place(self, side: QuoteSide, px: float, qty: float) -> None:
        p = self.primary
        coi = p.next_coi()
        rec = OrderRec(coi, side.sign, px, qty, created=self._now())
        self.orders[coi] = rec
        side.coi = coi
        self._actions.append(self._now())
        self._record_send(p)
        err = await p.place_post_only(coi=coi, is_buy=side.sign > 0,
                                      qty=qty, px=px)
        if err:
            rec.terminal = True
            if side.coi == coi:
                side.coi = None
            self._note_error("place", err)
        self._prune_orders()

    async def _modify(self, side: QuoteSide, rec: OrderRec, px: float,
                      qty: float) -> None:
        self._actions.append(self._now())
        self._record_send(self.primary)
        err = await self.primary.modify_resting(coi=rec.coi, qty=qty, px=px)
        if err is None:
            rec.px, rec.qty = px, qty
            return
        self._note_error("modify", err)
        if not rec.terminal:
            await self._cancel(side, rec)
        elif side.coi == rec.coi:
            side.coi = None

    async def _replace(self, side: QuoteSide, rec: OrderRec, px: float,
                       qty: float) -> None:
        await self._cancel(side, rec)
        if side.coi is None and self._block_reason is None \
                and self._halt_reason is None:
            await self._place(side, px, qty)

    async def _cancel(self, side: QuoteSide, rec: OrderRec) -> None:
        p = self.primary
        for attempt in range(2):
            self._actions.append(self._now())
            self._record_send(p)
            err = await p.cancel_resting(rec.coi)
            if err is None or rec.terminal:
                break
            self._note_error("cancel", err)
            await asyncio.sleep(0.2)
        else:
            # the order may still rest on the book: block quoting until a
            # market-wide cancel goes through
            self._cancel_all_needed = True
        if side.coi == rec.coi:
            side.coi = None

    def _spawn_cancel_all(self) -> None:
        self._cancel_all_busy = True

        async def run():
            try:
                err = await self.primary.cancel_all_resting()
                if err is None:
                    self._cancel_all_needed = False
                    for side in self.sides.values():
                        side.coi = None
                    log.info("[maker] all resting orders cancelled")
                else:
                    self._note_error("cancel-all", err)
                    await asyncio.sleep(1.0)
            finally:
                self._cancel_all_busy = False
                self._update_evt.set()

        t = asyncio.get_running_loop().create_task(run())
        self._action_tasks.add(t)
        t.add_done_callback(self._action_tasks.discard)

    def _prune_orders(self) -> None:
        if len(self.orders) <= self.MAX_ORDER_RECS:
            return
        live = {s.coi for s in self.sides.values()}
        for coi in [c for c, r in self.orders.items()
                    if r.terminal and c not in live][: len(self.orders) // 2]:
            del self.orders[coi]

    # -------------------------------------------------------------- fills

    def on_order_update(self, o: dict) -> None:
        """Account-stream callback: every update of every order (open ones
        included), used to catch partial fills of resting quotes."""
        try:
            coi = int(o.get("client_order_index"))
        except (TypeError, ValueError):
            return
        rec = self.orders.get(coi)
        if rec is None:
            return
        fb = float(o.get("filled_base_amount") or 0.0)
        fq = float(o.get("filled_quote_amount") or 0.0)
        d = fb - rec.filled_base
        if d > 1e-12:
            dq = fq - rec.filled_quote
            px = dq / d if dq > 0 else rec.px
            rec.filled_base, rec.filled_quote = fb, max(fq, rec.filled_quote)
            self._on_fill(rec, d, px)
        status = str(o.get("status", ""))
        if status and status not in OPEN_STATUSES:
            rec.terminal = True
            side = self.sides[rec.sign]
            if side.coi == coi:
                side.coi = None
            if status != "filled" and status != "canceled":
                log.info("[maker] quote %d %s", coi, status)
        self._update_evt.set()

    def _on_fill(self, rec: OrderRec, qty: float, px: float) -> None:
        p, now = self.primary, self._now()
        fee = self.mk.maker_fee_bps / 1e4
        p.position += rec.sign * qty
        if rec.sign > 0:
            p.cash -= qty * px * (1 + fee)
        else:
            p.cash += qty * px * (1 - fee)
        p.volume_usd += qty * px
        p.last_traded_ts = now
        self._fill_times.append(now)
        self.maker_fills += 1
        self.maker_volume += qty * px
        fair = self.hedge.book.mid()
        row = {"ts": f"{now:.3f}", "time_utc": _utc(now),
               "side": "buy" if rec.sign > 0 else "sell", "coi": rec.coi,
               "qty": f"{qty:.8g}", "px": f"{px:.8g}",
               "quote_px": f"{rec.px:.8g}",
               "fair_mid": f"{fair:.8g}" if fair else "",
               "center_bps": (f"{self.mcenter.value:.3f}"
                              if self.mcenter.value is not None else ""),
               "_sign": rec.sign, "_px": px, "_center": self.mcenter.value}
        self._unhedged.append([row, qty])
        log.info("[FILL] %s %s %.6g @ %.6g (quote %.6g) | %s pos %+.6g",
                 p.name, row["side"], qty, px, rec.px, p.name, p.position)
        self._hedge_evt.set()

    # -------------------------------------------------------------- hedge

    async def _hedge_loop(self) -> None:
        while not self.stop.is_set():
            await self._hedge_evt.wait()
            self._hedge_evt.clear()
            try:
                await self.hedge_net("fill")
            except asyncio.CancelledError:
                raise
            except Exception:
                log.exception("[maker] hedge failed")
                self._hedge_problem = True
                self._reconcile_evt.set()

    async def hedge_net(self, reason: str) -> None:
        """Bring primary + hedge positions back to flat with taker orders on
        the hedge venue only."""
        cfg = self.cfg
        p, h = self.primary, self.hedge
        async with self._hedge_lock:
            for _ in range(3):
                net = p.position + h.position
                step = 10 ** -h.size_decimals
                qty = floor_step(abs(net), step)
                is_buy = net < 0
                ref = h.book.best_ask() if is_buy else h.book.best_bid()
                if qty <= 0 or ref is None:
                    break
                if qty < h.min_base or qty * ref < max(h.min_quote,
                                                       cfg.min_order_notional):
                    break   # dust below the venue minimum
                slip = cfg.hedge_slippage_bps / 1e4
                limit = (h.px_round(ref * (1 + slip), True) if is_buy
                         else h.px_round(ref * (1 - slip), False))
                async with self._vlock(h.key):
                    self._record_send(h)
                    info = await h.send_taker(is_buy=is_buy, qty=qty,
                                              limit_px=limit)
                self.hedges += 1
                fill = info.get("filled_base") or 0.0
                apx = info.get("avg_px")
                if fill > 0:
                    px = apx or limit
                    fee = h.fee_bps / 1e4
                    h.position += fill if is_buy else -fill
                    if is_buy:
                        h.cash -= fill * px * (1 + fee)
                    else:
                        h.cash += fill * px * (1 - fee)
                    h.volume_usd += fill * px
                    self._assign_hedge(fill, apx, info.get("status", ""))
                h.last_traded_ts = time.time()
                log.info("[HEDGE] %s %s %.6g/%.6g on %s @ %s (%s) | net %+.6g",
                         reason, "BUY" if is_buy else "SELL", fill, qty,
                         h.name, f"{apx:.6g}" if apx else "—",
                         info.get("status"), p.position + h.position)
                err = info.get("err")
                if err or info.get("unresolved") or fill < qty - step / 2:
                    if err and str(err).startswith("RATE_LIMITED"):
                        self._mark_limited(h)
                    self._hedge_problem = True
                    self._hedge_fail_streak += 1
                    log.error("[HEDGE] incomplete (%s) — quoting paused until "
                              "reconciled", err or info.get("status"))
                    self._reconcile_evt.set()
                    if self._hedge_fail_streak >= cfg.max_consecutive_errors:
                        self._halt(f"{self._hedge_fail_streak} hedges in a row "
                                   f"did not complete")
                    return
                self._hedge_fail_streak = 0
            net = p.position + h.position
            if abs(net) <= max(cfg.net_tolerance_base, 10 ** -h.size_decimals) \
                    or self._is_dust(net):
                if self._hedge_problem:
                    log.info("[HEDGE] net back to %+.6g — resuming", net)
                self._hedge_problem = False

    def _is_dust(self, net: float) -> bool:
        h = self.hedge
        ref = h.book.mid()
        return ref is not None and (abs(net) < h.min_base or abs(net) * ref
                                    < max(h.min_quote,
                                          self.cfg.min_order_notional))

    def _assign_hedge(self, fill: float, apx: Optional[float],
                      status: str) -> None:
        h = self.hedge
        left = fill
        while left > 1e-12 and self._unhedged:
            item = self._unhedged[0]
            row, need = item
            take = min(need, left)
            item[1] -= take
            left -= take
            if item[1] <= 1e-12:
                self._unhedged.popleft()
                if apx:
                    e = maker_edge_bps(row["_sign"], row["_px"], apx,
                                       row["_center"] or 0.0,
                                       self.mk.maker_fee_bps, h.fee_bps)
                    usd = e / 1e4 * float(row["qty"]) * row["_px"]
                    self.maker_edge_usd += usd
                    row.update(hedge_px=f"{apx:.8g}", edge_bps=f"{e:.3f}",
                               edge_usd=f"{usd:.6f}")
                self._write_fill(row, status)

    def _write_fill(self, row: dict, hedge_status: str) -> None:
        prem = self._premium_mid_bps()
        out = {k: v for k, v in row.items() if not k.startswith("_")}
        out.update(hedge_status=hedge_status,
                   premium_bps=f"{prem:.3f}" if prem is not None else "",
                   primary_position=f"{self.primary.position:.8g}",
                   hedge_position=f"{self.hedge.position:.8g}")
        try:
            if self._fills_w is None:
                path = self.mk.fills_csv
                d = os.path.dirname(path)
                if d:
                    os.makedirs(d, exist_ok=True)
                new = not os.path.exists(path) or os.path.getsize(path) == 0
                self._fills_fh = open(path, "a", newline="")
                self._fills_w = csv.DictWriter(self._fills_fh,
                                               fieldnames=FILL_HEADER,
                                               extrasaction="ignore")
                if new:
                    self._fills_w.writeheader()
            self._fills_w.writerow(out)
            self._fills_fh.flush()
        except Exception:
            log.exception("[maker] could not write %s", self.mk.fills_csv)

    # ------------------------------------------------ engine integration

    async def _maybe_hedge(self, source_trade_id: Optional[str] = None) -> None:
        await self.hedge_net("reconcile")

    async def _reconcile_positions(self, hedge: bool,
                                   strict: bool = False) -> None:
        """Startup: adopt the chain (base engine). Afterwards a discrepancy is
        adopted only when it is seen twice with no fill or hedge in between,
        because resting quotes can fill at any moment and the chain and the
        order stream are not read atomically."""
        if strict:
            return await super()._reconcile_positions(hedge=hedge,
                                                      strict=strict)
        for v in self.venues.values():
            try:
                if v is self.hedge:
                    async with self._vlock(v.key):
                        r = await v.fetch_position()
                else:
                    r = await v.fetch_position()
            except asyncio.CancelledError:
                raise
            except Exception as e:
                n = self._fetch_fails.get(v.key, 0) + 1
                self._fetch_fails[v.key] = n
                if n >= 3 and v.key not in self._venue_down:
                    self._venue_down[v.key] = time.time()
                    log.critical("[%s] API unreachable — quoting pulled",
                                 v.name)
                else:
                    log.warning("[%s] position fetch failed (%d): %r",
                                v.name, n, e)
                continue
            self._fetch_fails[v.key] = 0
            if self._venue_down.pop(v.key, None) is not None:
                log.warning("[%s] API recovered", v.name)
            delta = r - v.position
            marker = (self.maker_fills, self.hedges)
            if abs(delta) <= 1e-12:
                self._rec_pending.pop(v.key, None)
                continue
            prev = self._rec_pending.get(v.key)
            if prev is not None and abs(prev[0] - delta) <= 1e-12 \
                    and prev[1] == marker:
                log.warning("[%s] reconcile: chain %+.6g vs local %+.6g — "
                            "adopting chain", v.name, r, v.position)
                mid = v.book.mid()
                if mid is not None:
                    v.cash -= delta * mid
                v.position = r
                self._rec_pending.pop(v.key, None)
                if v is self.hedge:
                    # whatever is still waiting for a hedge price is now
                    # covered (or not) by the adopted position
                    while self._unhedged:
                        self._write_fill(self._unhedged.popleft()[0],
                                         "reconciled")
            else:
                self._rec_pending[v.key] = (delta, marker)
        if hedge:
            await self._maybe_hedge()

    def _seed_maker_center(self) -> int:
        path = self.cfg.recorder_csv
        now = time.time()
        window = self.mk.center_window_sec
        samples = []
        try:
            with open(path, newline="") as fh:
                for r in csv.DictReader(fh):
                    try:
                        ts = float(r["minute_ts"]) + 60.0
                        prem = float(r["premium_mean_bps"])
                    except (KeyError, TypeError, ValueError):
                        continue
                    if now - window <= ts <= now:
                        samples.append((ts, prem))
        except FileNotFoundError:
            return 0
        except Exception:
            log.exception("could not read %s to seed the center", path)
            return 0
        samples.sort()
        return self.mcenter.seed(samples)

    async def _center_sampler(self) -> None:
        while not self.stop.is_set():
            if (self.primary.book.is_fresh(self.cfg.staleness_sec)
                    and self.hedge.book.is_fresh(self.cfg.staleness_sec)):
                prem = self._premium_mid_bps()
                if prem is not None:
                    self.mcenter.update(time.time(), prem)
            await asyncio.sleep(1.0)

    async def _dead_man_loop(self) -> None:
        sec = self.mk.dead_man_sec
        if sec <= 0:
            return
        armed = None
        while not self.stop.is_set():
            err = await self.primary.schedule_cancel_all(
                int((time.time() + sec) * 1000))
            if err is None and armed is not True:
                log.info("[SAFETY] exchange dead man's switch armed: resting "
                         "orders are cancelled %gs after the bot stops "
                         "refreshing it / 交易所定时撤单已启用", sec)
            elif err is not None and armed is not False:
                log.warning("[SAFETY] dead man's switch not accepted: %s", err)
            armed = err is None
            try:
                await asyncio.wait_for(self.stop.wait(),
                                       timeout=max(5.0, sec / 4.0))
            except asyncio.TimeoutError:
                pass

    async def _shutdown_guard(self) -> None:
        """Registered as an in-flight execution so the base engine waits for
        it on shutdown: pull every quote, then flatten the net."""
        await self.stop.wait()
        self._halt_reason = self._halt_reason or "shutdown"
        # let quote actions already on the wire land, so the cancel-all
        # below is the last thing the primary sees
        if self._action_tasks:
            await asyncio.wait(set(self._action_tasks), timeout=2.0)
        await self._cancel_all_retry("shutdown")
        await asyncio.sleep(1.0)   # late fill updates
        try:
            await self.hedge_net("shutdown")
        except Exception:
            log.exception("[maker] shutdown hedge failed")
        while self._unhedged:
            self._write_fill(self._unhedged.popleft()[0], "unhedged")
        log.info("[maker] shutdown — %d fills, $%.0f volume, edge $%.4f, "
                 "net %+.6g", self.maker_fills, self.maker_volume,
                 self.maker_edge_usd,
                 self.primary.position + self.hedge.position)
        if self._fills_fh is not None:
            self._fills_fh.close()

    async def _cancel_all_retry(self, why: str) -> bool:
        for _ in range(3):
            err = await self.primary.cancel_all_resting()
            if err is None:
                log.info("[maker] %s: all resting orders cancelled", why)
                return True
            log.error("[maker] %s cancel-all failed: %s", why, err)
            await asyncio.sleep(0.5)
        return False

    async def _strategy_loop(self) -> None:
        p, h = self.primary, self.hedge
        if p.kind != "lighter" or h.kind != "lighter":
            log.critical("maker mode needs Lighter venues on both legs "
                         "(e.g. --primary light-rh --hedge lighter)")
            self.request_stop()
            return
        p.orders_feed.listeners.append(self.on_order_update)
        guard = asyncio.get_running_loop().create_task(self._shutdown_guard())
        self._exec_tasks.add(guard)
        guard.add_done_callback(self._exec_tasks.discard)
        seeded = self._seed_maker_center()
        m = self.mk
        log.info("[maker] quoting %s around %s mid + center (EMA %gs, %d "
                 "minute(s) seeded): ±%g bps, $%g per quote, cap $%g, "
                 "requote %g bps | guards: %g bps/%gs, %g bps/%gs, %s %g "
                 "bps/%gs, spread %g bps, basis %g bps, %d fills/min, pause "
                 "%gs, blackout %s %s, max loss $%g",
                 p.name, h.name, m.center_window_sec, seeded,
                 m.half_spread_bps, m.quote_usd, m.max_position_usd,
                 m.requote_bps, m.max_move_bps, m.move_window_sec,
                 m.max_long_move_bps, m.long_move_window_sec, p.name,
                 m.max_primary_move_bps, m.move_window_sec,
                 m.max_hedge_spread_bps, m.max_basis_dev_bps,
                 m.max_fills_per_min, m.pause_sec,
                 list(m.blackout_windows) or "none", m.blackout_tz,
                 m.max_loss_usd)
        children = [asyncio.create_task(self._hedge_loop(), name="maker-hedge"),
                    asyncio.create_task(self._center_sampler(),
                                        name="maker-center"),
                    asyncio.create_task(self._dead_man_loop(),
                                        name="maker-deadman")]
        try:
            # clear anything a previous run left resting, then flatten
            while not self.stop.is_set():
                if p.ready_to_trade():
                    err = await p.cancel_all_resting()
                    if err is None:
                        break
                    log.error("[maker] startup cancel-all failed: %s", err)
                await asyncio.sleep(1.0)
            await self.hedge_net("startup")
            while not self.stop.is_set():
                await self._update_evt.wait()
                self._update_evt.clear()
                if self.stop.is_set():
                    break
                try:
                    self._tick()
                except Exception:
                    log.exception("[maker] tick failed")
        finally:
            # never cancel the hedge loop mid-order: wake it so it sees the
            # stop and exits; the shutdown guard waits on its lock
            self._hedge_evt.set()
            for t in children[1:]:
                t.cancel()

    async def _status_loop(self) -> None:
        cfg = self.cfg
        while not self.stop.is_set():
            await asyncio.sleep(cfg.status_interval_sec)
            p, h = self.primary, self.hedge
            quotes = []
            for sign in (1, -1):
                s = self.sides[sign]
                r = self.orders.get(s.coi) if s.coi is not None else None
                quotes.append(f"{r.px:.6g}" if r and not r.terminal else "—")
            c = self.mcenter.value
            prem = self._premium_mid_bps()
            pnl = self.session_pnl()
            log.info("[maker] quotes %s bid %s / ask %s | %s mid %s | prem %s "
                     "center %s | pos %s %+.6g %s %+.6g net %+.6g | fills %d "
                     "vol $%.0f edge $%.4f | MTM %s | %s",
                     p.name, quotes[0], quotes[1], h.name,
                     f"{h.book.mid():.6g}" if h.book.mid() else "—",
                     f"{prem:+.2f}" if prem is not None else "—",
                     f"{c:+.2f}" if c is not None else "—",
                     p.name, p.position, h.name, h.position,
                     p.position + h.position, self.maker_fills,
                     self.maker_volume, self.maker_edge_usd,
                     f"${pnl:+.4f}" if pnl is not None else "—",
                     self._block_reason or "quoting")
