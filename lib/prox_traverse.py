"""ToF zone-sequence tracker for walk-by traverse (L→C→R / R→C→L)."""

from __future__ import annotations

from collections import deque
from dataclasses import dataclass, field

from lib.prox_investigate import glance_emotion_for_zone


@dataclass
class ProxTraverseConfig:
    enabled: bool = True
    sequence_window_sec: float = 3.5
    allow_skip_center: bool = True
    skip_center_max_gap_sec: float = 1.2
    min_zones_in_sequence: int = 2
    max_duration_sec: float = 8.0
    idle_exit_sec: float = 1.5
    reject_all_zones_at_start: bool = True


@dataclass
class ProxTraverseSnapshot:
    prox_traverse_active: bool = False
    prox_traverse_dir: str = ""
    prox_traverse_zone: str = ""
    prox_traverse_since: float = 0.0
    prox_traverse_confidence: int = 0
    prox_traverse_emotion: str = ""


def _subsequence_match(seq: list[str], pattern: list[str]) -> bool:
    pi = 0
    for zone in seq:
        if zone == pattern[pi]:
            pi += 1
            if pi == len(pattern):
                return True
    return False


def _first_last_ts(events: list[tuple[str, float]], zone_a: str, zone_b: str) -> float | None:
    ts_a = ts_b = None
    for zone, ts in events:
        if zone == zone_a and ts_a is None:
            ts_a = ts
        if zone == zone_b and ts_a is not None:
            ts_b = ts
    if ts_a is None or ts_b is None:
        return None
    return ts_b - ts_a


def match_traverse_direction(
    events: list[tuple[str, float]],
    now: float,
    *,
    window_sec: float,
    allow_skip_center: bool,
    skip_center_max_gap_sec: float,
    min_zones: int,
) -> tuple[str, int] | None:
    """Return (direction, confidence) or None. direction is L2R or R2L."""
    recent = [(z, t) for z, t in events if now - t <= window_sec]
    if len(recent) < min_zones:
        return None
    zones = [z for z, _ in recent]

    if _subsequence_match(zones, ["L", "C", "R"]):
        return "L2R", 3
    if _subsequence_match(zones, ["R", "C", "L"]):
        return "R2L", 3

    if allow_skip_center and _subsequence_match(zones, ["L", "R"]):
        gap = _first_last_ts(recent, "L", "R")
        if gap is not None and gap <= skip_center_max_gap_sec:
            return "L2R", 2
    if allow_skip_center and _subsequence_match(zones, ["R", "L"]):
        gap = _first_last_ts(recent, "R", "L")
        if gap is not None and gap <= skip_center_max_gap_sec:
            return "R2L", 2

    return None


@dataclass
class ProxTraverseTracker:
    config: ProxTraverseConfig = field(default_factory=ProxTraverseConfig)
    _events: deque[tuple[str, float]] = field(default_factory=lambda: deque(maxlen=16))
    _prev_zones: tuple[bool, bool, bool] = (False, False, False)
    _active: bool = False
    _dir: str = ""
    _zone: str = ""
    _since: float = 0.0
    _confidence: int = 0
    _last_zone_ts: float = 0.0
    _all_clear_since: float | None = None

    def update(
        self,
        zones: dict[str, bool],
        now: float,
        *,
        face_detected: bool = False,
        body_detected: bool = False,
    ) -> ProxTraverseSnapshot:
        if not self.config.enabled:
            return self.snapshot()

        zl = bool(zones.get("L", False))
        zc = bool(zones.get("C", False))
        zr = bool(zones.get("R", False))
        current = (zl, zc, zr)
        labels = ("L", "C", "R")

        for i, label in enumerate(labels):
            if current[i] and not self._prev_zones[i]:
                if self.config.reject_all_zones_at_start and zl and zc and zr:
                    self._prev_zones = current
                    return self.snapshot()
                self._events.append((label, now))
                self._zone = label
                self._last_zone_ts = now
                if not self._active:
                    matched = match_traverse_direction(
                        list(self._events),
                        now,
                        window_sec=self.config.sequence_window_sec,
                        allow_skip_center=self.config.allow_skip_center,
                        skip_center_max_gap_sec=self.config.skip_center_max_gap_sec,
                        min_zones=self.config.min_zones_in_sequence,
                    )
                    if matched is not None:
                        self._active = True
                        self._dir, self._confidence = matched
                        self._since = now

        self._prev_zones = current
        self._prune_events(now)

        if zl or zc or zr:
            self._all_clear_since = None
        elif self._all_clear_since is None:
            self._all_clear_since = now

        if face_detected or body_detected:
            self._clear_active()
        elif self._active:
            if (now - self._since) >= self.config.max_duration_sec:
                self._clear_active()
            elif (
                self._all_clear_since is not None
                and (now - self._all_clear_since) >= self.config.idle_exit_sec
            ):
                self._clear_active()

        return self.snapshot()

    def _prune_events(self, now: float) -> None:
        cutoff = now - self.config.sequence_window_sec - 0.5
        while self._events and self._events[0][1] < cutoff:
            self._events.popleft()

    def _clear_active(self) -> None:
        self._active = False
        self._dir = ""
        self._zone = ""
        self._since = 0.0
        self._confidence = 0
        self._all_clear_since = None

    def snapshot(self) -> ProxTraverseSnapshot:
        emotion = ""
        if self._active and self._zone:
            emotion = glance_emotion_for_zone(self._zone)
        return ProxTraverseSnapshot(
            prox_traverse_active=self._active,
            prox_traverse_dir=self._dir,
            prox_traverse_zone=self._zone,
            prox_traverse_since=self._since,
            prox_traverse_confidence=self._confidence,
            prox_traverse_emotion=emotion,
        )
