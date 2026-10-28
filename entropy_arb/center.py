"""Premium center: a fixed number, or a time-weighted EMA of the mid premium.

The engine's midline and the maker shadow's quote center both use it. With
`fixed` set the value never moves; with `fixed=None` every update pulls the
value toward the observed premium by dt / tau, so tau is the time constant
(roughly the averaging window).
"""
from __future__ import annotations

from typing import Iterable, Optional, Tuple


class CenterTracker:
    """Premium center in bps: fixed, or a time-weighted EMA of the mid-to-mid
    premium (primary over hedge) when `fixed` is None.

    reset_gap_sec: a gap between updates longer than this restarts the EMA
    and its warmup from the next sample (stale history is not a center)."""

    def __init__(self, fixed: Optional[float], tau_sec: float,
                 warmup_sec: float,
                 reset_gap_sec: Optional[float] = None) -> None:
        self.fixed = fixed
        self.tau = max(tau_sec, 1.0)
        self.warmup = warmup_sec
        self.reset_gap = reset_gap_sec
        self.value: Optional[float] = fixed
        self._t0: Optional[float] = None
        self._last: Optional[float] = None

    def update(self, now: float, premium_bps: float) -> None:
        if self.fixed is not None:
            return
        gap = None if self._last is None else now - self._last
        if gap is None or (self.reset_gap is not None and gap > self.reset_gap):
            self.value, self._t0 = premium_bps, now
        else:
            a = min(max(gap, 0.0) / self.tau, 1.0)
            self.value += a * (premium_bps - self.value)
        self._last = now

    def seed(self, samples: Iterable[Tuple[float, float]]) -> int:
        """Replay (ts, premium_bps) history, oldest first, so a restart
        resumes the center instead of warming up from scratch. Returns the
        number of samples used."""
        n = 0
        for ts, p in samples:
            self.update(ts, p)
            n += 1
        return n

    def ready(self, now: float) -> bool:
        if self.fixed is not None:
            return True
        return self._t0 is not None and now - self._t0 >= self.warmup
