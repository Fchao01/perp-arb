"""Daily loss limit: the market's PnL since midnight (in a chosen time zone)
and whether it has hit the limit.

PnL is the change in cash + positions at mid, the same measure as the
account's equity, so it includes what the held (hedged) positions gain or
lose while the premium between the venues moves. Once the day's PnL is at
or below -limit, the day is "tripped" and stays tripped until midnight even
if the PnL comes back: no new arbitrage until the next day. The day's PnL
and the tripped flag are saved to a small JSON file, so a restart during
the day carries on from where it was instead of starting a fresh budget.

中文：每日亏损上限。按所选时区的 0 点起算当天盈亏（成交现金 + 持仓按中间价），
与账户权益口径一致。当天亏到上限后停止开新单直到第二天 0 点（盈亏回升也不恢复）；
当天盈亏写入 JSON 文件，重启后接着算，不会重新给一份额度。
"""
from __future__ import annotations

import json
import logging
import os
import time
from datetime import datetime
from typing import Optional

log = logging.getLogger("daily")


class DailyLoss:
    def __init__(self, limit_usd: float, tz_name: str, path: str) -> None:
        from zoneinfo import ZoneInfo
        self.limit = float(limit_usd)
        self.tz = ZoneInfo(tz_name)
        self.path = path
        self.day: Optional[str] = None
        self.carry = 0.0        # PnL earlier today, before this process
        self.base: Optional[float] = None   # book value when counting began
        self.pnl: Optional[float] = None
        self.tripped = False

    def day_key(self, ts: float) -> str:
        return datetime.fromtimestamp(ts, self.tz).strftime("%Y-%m-%d")

    def _load(self, day: str) -> None:
        try:
            with open(self.path) as fh:
                st = json.load(fh)
        except (OSError, ValueError):
            return
        if st.get("day") == day:
            self.carry = float(st.get("pnl") or 0.0)
            self.tripped = bool(st.get("tripped"))
            log.info("daily loss: carrying today's PnL %+.2f from %s%s",
                     self.carry, self.path,
                     " (limit already hit)" if self.tripped else "")

    def update(self, ts: float, value: float) -> float:
        """Feed the current book value (cash + positions at mid); returns
        today's PnL. The first call of a process, and the first call of a
        new day, start counting from the value given."""
        day = self.day_key(ts)
        if self.day is None:
            self.day = day
            self._load(day)
            self.base = value
        elif day != self.day:
            if self.tripped:
                log.info("daily loss: new day %s — trading resumes / "
                         "新的一天，恢复开新单", day)
            self.day, self.carry, self.base, self.tripped = day, 0.0, value, False
        self.pnl = self.carry + (value - self.base)
        if not self.tripped and self.pnl <= -self.limit:
            self.tripped = True
        return self.pnl

    def save(self) -> None:
        if self.day is None or self.pnl is None:
            return
        os.makedirs(os.path.dirname(self.path) or ".", exist_ok=True)
        tmp = self.path + ".tmp"
        try:
            with open(tmp, "w") as fh:
                json.dump({"day": self.day, "pnl": round(self.pnl, 6),
                           "tripped": self.tripped, "limit": self.limit,
                           "ts": time.time()}, fh)
            os.replace(tmp, self.path)
        except OSError as e:
            log.warning("daily loss: could not save %s: %s", self.path, e)
