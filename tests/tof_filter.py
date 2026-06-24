"""Smooth ToF samples: trusted range, hold on dropout, averaged velocity."""

from __future__ import annotations

import time
from collections import deque
from dataclasses import dataclass

MIN_TRUST_MM = 80
MAX_TRUST_MM = 1800
HOLD_SEC = 0.4
AVG_WINDOW = 5
MAX_VEL_MM_S = 300
VEL_GAP_SEC = 0.35


def is_trusted_mm(mm: int) -> bool:
    return MIN_TRUST_MM <= mm <= MAX_TRUST_MM


@dataclass(frozen=True)
class FilteredSample:
    """display_mm: -1 = open / no target; velocity_mm_s None = not computed."""

    display_mm: int
    velocity_mm_s: int | None
    is_open: bool


class TofChannelFilter:
    """Per-sensor moving average + hold-through brief dropouts."""

    def __init__(
        self,
        *,
        min_trust_mm: int = MIN_TRUST_MM,
        max_trust_mm: int = MAX_TRUST_MM,
        hold_sec: float = HOLD_SEC,
        avg_window: int = AVG_WINDOW,
    ) -> None:
        self.min_trust_mm = min_trust_mm
        self.max_trust_mm = max_trust_mm
        self.hold_sec = hold_sec
        self._window: deque[int] = deque(maxlen=max(2, avg_window))
        self._last_avg: float | None = None
        self._last_avg_ts: float = 0.0
        self._last_good_ts: float = 0.0

    def _trusted(self, mm: int) -> bool:
        return self.min_trust_mm <= mm <= self.max_trust_mm

    def _average(self) -> float | None:
        if not self._window:
            return None
        return sum(self._window) / len(self._window)

    def update(self, raw_mm: int, *, now: float | None = None) -> FilteredSample:
        ts = time.time() if now is None else now

        if self._trusted(raw_mm):
            self._window.append(raw_mm)
            self._last_good_ts = ts
            avg = self._average()
            if avg is None:
                return FilteredSample(-1, None, True)

            vel: int | None = None
            if (
                self._last_avg is not None
                and self._last_avg_ts > 0
                and (ts - self._last_avg_ts) <= VEL_GAP_SEC
            ):
                dt = ts - self._last_avg_ts
                if dt > 0.01:
                    raw_vel = (avg - self._last_avg) / dt
                    vel = int(max(-MAX_VEL_MM_S, min(MAX_VEL_MM_S, raw_vel)))

            self._last_avg = avg
            self._last_avg_ts = ts
            return FilteredSample(int(round(avg)), vel, False)

        # Dropout or above trust ceiling — hold briefly, then show open
        if self._last_good_ts > 0 and (ts - self._last_good_ts) <= self.hold_sec:
            if self._last_avg is not None:
                return FilteredSample(int(round(self._last_avg)), None, False)

        self._window.clear()
        self._last_avg = None
        return FilteredSample(-1, None, True)


class TofFilterBank:
    """Three-channel filter for L/C/R."""

    def __init__(self, channels: int = 3) -> None:
        self._filters = [TofChannelFilter() for _ in range(channels)]

    def update_all(self, raw_mm: list[int], *, now: float | None = None) -> tuple[list[int], list[int | None], list[bool]]:
        ts = time.time() if now is None else now
        display: list[int] = []
        vel: list[int | None] = []
        open_flags: list[bool] = []
        for i, raw in enumerate(raw_mm):
            s = self._filters[i].update(raw, now=ts)
            display.append(s.display_mm)
            vel.append(s.velocity_mm_s)
            open_flags.append(s.is_open)
        return display, vel, open_flags
