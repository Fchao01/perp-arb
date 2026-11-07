"""Weekday time-of-day windows in a named time zone (DST-aware).

Used by the maker's blackout_windows and the taker's pause_windows, both
written as "HH:MM-HH:MM" in, by default, New York time, so US market
events (data releases 08:30, the open 09:30, the close 16:00) stay at the
same local time across daylight-saving changes. Weekends are never inside
a window.
"""
from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import Optional

log = logging.getLogger("schedule")


def parse_window(text: str, key: str = "maker.blackout_windows"):
    """'HH:MM-HH:MM' -> (start_min, end_min); end < start wraps midnight."""
    from .config import ConfigError
    try:
        a, b = str(text).split("-")
        out = []
        for part in (a, b):
            h, m = part.strip().split(":")
            h, m = int(h), int(m)
            if not (0 <= h < 24 and 0 <= m < 60):
                raise ValueError
            out.append(h * 60 + m)
    except ValueError:
        raise ConfigError(f"{key} entry {text!r} must look like "
                          f"'15:55-16:45' / 格式应为 '15:55-16:45'")
    if out[0] == out[1]:
        raise ConfigError(f"{key} entry {text!r} is empty")
    return out[0], out[1]


class Blackout:
    """active(ts) -> the window text ts falls in (weekdays only), or None."""

    def __init__(self, windows, tz_name: str) -> None:
        self.windows = [(w, *parse_window(w)) for w in windows]
        self.tz = timezone.utc
        if self.windows:
            try:
                from zoneinfo import ZoneInfo
                self.tz = ZoneInfo(tz_name)
            except Exception:
                log.warning("time zone %r unavailable — windows are read as "
                            "UTC", tz_name)

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
