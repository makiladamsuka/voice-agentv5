"""ToF snapshot parsing and debounced presence helpers for v5."""

from __future__ import annotations

import re
import time
from dataclasses import dataclass

_TOF_RE = re.compile(r"^TOF L=(-?\d+) C=(-?\d+) R=(-?\d+) VALID=(\d)(\d)(\d)\s*$")
DEFAULT_MIN_VALID_MM = 30
DEFAULT_MAX_VALID_MM = 800


@dataclass(frozen=True)
class TofSnapshot:
    left_mm: int
    center_mm: int
    right_mm: int
    left_valid: bool
    center_valid: bool
    right_valid: bool
    timestamp: float

    @classmethod
    def empty(cls) -> "TofSnapshot":
        return cls(-1, -1, -1, False, False, False, 0.0)


@dataclass(frozen=True)
class TofPresence:
    left: bool
    center: bool
    right: bool
    any_present: bool
    count_present: int


def parse_tof_line(line: str) -> TofSnapshot | None:
    match = _TOF_RE.match(line.strip())
    if not match:
        return None
    return TofSnapshot(
        left_mm=int(match.group(1)),
        center_mm=int(match.group(2)),
        right_mm=int(match.group(3)),
        left_valid=match.group(4) == "1",
        center_valid=match.group(5) == "1",
        right_valid=match.group(6) == "1",
        timestamp=time.time(),
    )


def sanitize_tof_snapshot(
    snap: TofSnapshot,
    *,
    min_valid_mm: int = DEFAULT_MIN_VALID_MM,
    max_valid_mm: int = DEFAULT_MAX_VALID_MM,
) -> TofSnapshot:
    def clean(mm: int, valid: bool) -> tuple[int, bool]:
        if valid and min_valid_mm <= mm <= max_valid_mm:
            return mm, True
        return -1, False

    l_mm, l_v = clean(snap.left_mm, snap.left_valid)
    c_mm, c_v = clean(snap.center_mm, snap.center_valid)
    r_mm, r_v = clean(snap.right_mm, snap.right_valid)
    return TofSnapshot(l_mm, c_mm, r_mm, l_v, c_v, r_v, snap.timestamp)


def format_tof_channel(mm: int, valid: bool) -> str:
    if not valid or mm < 0:
        return "clear"
    return f"{mm} mm"


class TofSnapshotFilter:
    """Reject single-frame spikes and briefly hold last good per-channel readings."""

    def __init__(
        self,
        *,
        min_valid_mm: int = DEFAULT_MIN_VALID_MM,
        max_valid_mm: int = DEFAULT_MAX_VALID_MM,
        max_jump_mm: int = 100,
        hold_sec: float = 0.35,
    ):
        self.min_valid_mm = min_valid_mm
        self.max_valid_mm = max_valid_mm
        self.max_jump_mm = max_jump_mm
        self.hold_sec = hold_sec
        self._last = TofSnapshot.empty()

    def apply(self, snap: TofSnapshot) -> TofSnapshot:
        snap = sanitize_tof_snapshot(
            snap,
            min_valid_mm=self.min_valid_mm,
            max_valid_mm=self.max_valid_mm,
        )
        now = snap.timestamp if snap.timestamp > 0 else time.time()
        channels = (
            (snap.left_mm, snap.left_valid),
            (snap.center_mm, snap.center_valid),
            (snap.right_mm, snap.right_valid),
        )
        last_channels = (
            (self._last.left_mm, self._last.left_valid),
            (self._last.center_mm, self._last.center_valid),
            (self._last.right_mm, self._last.right_valid),
        )
        out: list[tuple[int, bool]] = []
        for (mm, valid), (last_mm, last_valid) in zip(channels, last_channels):
            if (
                valid
                and last_valid
                and last_mm >= 0
                and abs(mm - last_mm) > self.max_jump_mm
            ):
                valid = False
                mm = -1
            if (
                not valid
                and last_valid
                and last_mm >= 0
                and self._last.timestamp > 0
                and (now - self._last.timestamp) <= self.hold_sec
            ):
                mm = last_mm
                valid = True
            out.append((mm, valid))
        filtered = TofSnapshot(
            out[0][0],
            out[1][0],
            out[2][0],
            out[0][1],
            out[1][1],
            out[2][1],
            now,
        )
        self._last = filtered
        return filtered


class TofPresenceTracker:
    """Debounce ToF presence transitions with hysteresis thresholds."""

    def __init__(
        self,
        *,
        present_max_mm: float,
        absent_min_mm: float,
        debounce_present_sec: float,
        debounce_absent_sec: float,
    ):
        self.present_max_mm = present_max_mm
        self.absent_min_mm = absent_min_mm
        self.debounce_present_sec = debounce_present_sec
        self.debounce_absent_sec = debounce_absent_sec
        self._stable = [False, False, False]
        self._pending = [False, False, False]
        self._pending_since = [0.0, 0.0, 0.0]

    def _raw_present(self, mm: int, valid: bool) -> bool | None:
        if not valid or mm < 0:
            return False
        if mm <= self.present_max_mm:
            return True
        if mm >= self.absent_min_mm:
            return False
        return None

    def update(self, snap: TofSnapshot) -> TofPresence:
        now = time.time()
        readings = (
            (snap.left_mm, snap.left_valid),
            (snap.center_mm, snap.center_valid),
            (snap.right_mm, snap.right_valid),
        )
        for i, (mm, valid) in enumerate(readings):
            raw = self._raw_present(mm, valid)
            target = self._stable[i] if raw is None else raw
            if target != self._stable[i]:
                if target != self._pending[i]:
                    self._pending[i] = target
                    self._pending_since[i] = now
                debounce = self.debounce_present_sec if target else self.debounce_absent_sec
                if now - self._pending_since[i] >= debounce:
                    self._stable[i] = target
            else:
                self._pending[i] = target
                self._pending_since[i] = now

        count = sum(self._stable)
        return TofPresence(
            left=self._stable[0],
            center=self._stable[1],
            right=self._stable[2],
            any_present=count > 0,
            count_present=count,
        )
