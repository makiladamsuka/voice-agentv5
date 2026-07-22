"""Short-lived yaw-only memory for recently detected people."""

from __future__ import annotations

import time
from dataclasses import dataclass


def _clamp(value: float, lo: float, hi: float) -> float:
    return max(lo, min(hi, value))


def wrap_degrees(value: float) -> float:
    """Normalize an angle to [-180, 180)."""
    wrapped = (value + 180.0) % 360.0 - 180.0
    return wrapped


def angular_error_deg(target: float, current: float) -> float:
    """Signed shortest angular error from current to target."""
    return wrap_degrees(target - current)


def angular_distance_deg(a: float, b: float) -> float:
    return abs(angular_error_deg(a, b))


@dataclass
class PersonMemoryItem:
    id: int
    world_yaw_deg: float
    last_seen_ts: float
    first_seen_ts: float
    kind: str = "face"
    source: str = "camera"  # "camera" | "prox_verify"
    confidence: float = 0.5
    seen_count: int = 1

    def age_sec(self, now: float | None = None) -> float:
        now = time.time() if now is None else now
        return max(0.0, now - self.last_seen_ts)

    def ttl_sec(self, default_timeout: float, *, prox_verify_timeout: float) -> float:
        if self.source == "prox_verify":
            return prox_verify_timeout
        return default_timeout

    def to_dict(
        self,
        now: float | None = None,
        *,
        timeout_sec: float = 20.0,
        prox_verify_timeout_sec: float = 5.0,
    ) -> dict[str, float | int | str]:
        now = time.time() if now is None else now
        age = self.age_sec(now)
        ttl = self.ttl_sec(timeout_sec, prox_verify_timeout=prox_verify_timeout_sec)
        freshness = _clamp(1.0 - (age / max(ttl, 0.001)), 0.0, 1.0)
        return {
            "id": self.id,
            "world_yaw_deg": self.world_yaw_deg,
            "age_sec": age,
            "kind": self.kind,
            "source": self.source,
            "confidence": self.confidence,
            "seen_count": self.seen_count,
            "freshness": freshness,
        }


class PersonMemory:
    """Maintains a short-lived floor-map of people by world yaw."""

    def __init__(
        self,
        *,
        timeout_sec: float = 20.0,
        merge_angle_deg: float = 12.0,
        camera_hfov_deg: float = 62.0,
        max_items: int = 6,
        prox_verify_timeout_sec: float = 5.0,
    ) -> None:
        self.timeout_sec = max(1.0, timeout_sec)
        self.prox_verify_timeout_sec = max(0.5, prox_verify_timeout_sec)
        self.merge_angle_deg = max(1.0, merge_angle_deg)
        self.camera_hfov_deg = max(1.0, camera_hfov_deg)
        self.max_items = max(1, max_items)
        self._items: list[PersonMemoryItem] = []
        self._next_id = 1

    def detection_to_yaw(
        self,
        *,
        norm_x: float,
        base_world_yaw_deg: float,
        pan_mech_deg: float,
    ) -> float:
        camera_yaw_offset = _clamp(norm_x, -1.0, 1.0) * (self.camera_hfov_deg * 0.5)
        return wrap_degrees(base_world_yaw_deg + pan_mech_deg + camera_yaw_offset)

    def detection_to_bearing(
        self,
        *,
        norm_x: float,
        norm_y: float = 0.0,
        base_world_yaw_deg: float,
        pan_mech_deg: float,
        tilt_mech_deg: float = 0.0,
    ) -> tuple[float, float]:
        """Compatibility helper: yaw-only memory always returns pitch 0."""
        return (
            self.detection_to_yaw(
                norm_x=norm_x,
                base_world_yaw_deg=base_world_yaw_deg,
                pan_mech_deg=pan_mech_deg,
            ),
            0.0,
        )

    def observe(
        self,
        *,
        norm_x: float,
        norm_y: float = 0.0,
        base_world_yaw_deg: float,
        pan_mech_deg: float,
        tilt_mech_deg: float = 0.0,
        kind: str,
        confidence: float = 1.0,
        source: str = "camera",
        now: float | None = None,
    ) -> PersonMemoryItem:
        now = time.time() if now is None else now
        self.prune(now)
        world_yaw = self.detection_to_yaw(
            norm_x=norm_x,
            base_world_yaw_deg=base_world_yaw_deg,
            pan_mech_deg=pan_mech_deg,
        )
        item = self._nearest(world_yaw)
        if item is None or angular_distance_deg(item.world_yaw_deg, world_yaw) > self.merge_angle_deg:
            item = PersonMemoryItem(
                id=self._next_id,
                world_yaw_deg=world_yaw,
                last_seen_ts=now,
                first_seen_ts=now,
                kind=kind,
                source=source,
                confidence=_clamp(confidence, 0.0, 1.0),
            )
            self._next_id += 1
            self._items.append(item)
        else:
            # Exponential average the bearing without crossing wrap discontinuities.
            alpha = 0.45 if kind == "face" else 0.30
            yaw_delta = angular_error_deg(world_yaw, item.world_yaw_deg)
            item.world_yaw_deg = wrap_degrees(item.world_yaw_deg + yaw_delta * alpha)
            item.last_seen_ts = now
            item.seen_count += 1
            item.kind = "face" if kind == "face" or item.kind == "face" else kind
            item.source = source if source == "prox_verify" else item.source
            item.confidence = _clamp(max(item.confidence * 0.85, confidence), 0.0, 1.0)
        self._trim_oldest()
        return item

    def observe_at_yaw(
        self,
        *,
        world_yaw_deg: float,
        kind: str,
        source: str = "prox_verify",
        confidence: float = 0.85,
        now: float | None = None,
    ) -> PersonMemoryItem:
        now = time.time() if now is None else now
        self.prune(now)
        world_yaw = wrap_degrees(world_yaw_deg)
        item = self._nearest(world_yaw)
        if item is None or angular_distance_deg(item.world_yaw_deg, world_yaw) > self.merge_angle_deg:
            item = PersonMemoryItem(
                id=self._next_id,
                world_yaw_deg=world_yaw,
                last_seen_ts=now,
                first_seen_ts=now,
                kind=kind,
                source=source,
                confidence=_clamp(confidence, 0.0, 1.0),
            )
            self._next_id += 1
            self._items.append(item)
        else:
            item.last_seen_ts = now
            item.seen_count += 1
            item.kind = "face" if kind == "face" or item.kind == "face" else kind
            item.source = "prox_verify"
            item.confidence = _clamp(max(item.confidence, confidence), 0.0, 1.0)
        self._trim_oldest()
        return item

    def prune(self, now: float | None = None) -> None:
        now = time.time() if now is None else now
        self._items = [
            item for item in self._items
            if item.age_sec(now) <= item.ttl_sec(
                self.timeout_sec, prox_verify_timeout=self.prox_verify_timeout_sec
            )
        ]

    def active(self, now: float | None = None) -> list[PersonMemoryItem]:
        now = time.time() if now is None else now
        self.prune(now)
        return sorted(self._items, key=lambda item: (item.confidence, -item.age_sec(now)), reverse=True)

    def best_for_current_view(
        self,
        *,
        current_world_yaw_deg: float,
        now: float | None = None,
    ) -> PersonMemoryItem | None:
        now = time.time() if now is None else now
        items = self.active(now)
        if not items:
            return None
        return min(
            items,
            key=lambda item: (
                item.age_sec(now) / self.timeout_sec,
                angular_distance_deg(item.world_yaw_deg, current_world_yaw_deg),
            ),
        )

    def best_for_reacquire(
        self,
        *,
        current_world_yaw_deg: float,
        now: float | None = None,
        kind: str = "face",
    ) -> PersonMemoryItem | None:
        """Best remembered person to reacquire after normal search times out."""
        now = time.time() if now is None else now
        items = [item for item in self.active(now) if item.kind == kind]
        if not items:
            return None
        return min(
            items,
            key=lambda item: (
                item.age_sec(now) / self.timeout_sec,
                angular_distance_deg(item.world_yaw_deg, current_world_yaw_deg),
            ),
        )

    def best_prox_verified(
        self,
        *,
        current_world_yaw_deg: float,
        now: float | None = None,
        max_age_sec: float | None = None,
    ) -> PersonMemoryItem | None:
        now = time.time() if now is None else now
        age_limit = max_age_sec if max_age_sec is not None else self.prox_verify_timeout_sec
        items = [
            item for item in self.active(now)
            if item.source == "prox_verify" and item.age_sec(now) <= age_limit
        ]
        if not items:
            return None
        return min(
            items,
            key=lambda item: angular_distance_deg(item.world_yaw_deg, current_world_yaw_deg),
        )

    def snapshots(self, now: float | None = None) -> list[dict[str, float | int | str]]:
        now = time.time() if now is None else now
        return [
            item.to_dict(
                now,
                timeout_sec=self.timeout_sec,
                prox_verify_timeout_sec=self.prox_verify_timeout_sec,
            )
            for item in self.active(now)
        ]

    def _nearest(self, world_yaw_deg: float) -> PersonMemoryItem | None:
        if not self._items:
            return None
        return min(self._items, key=lambda item: angular_distance_deg(item.world_yaw_deg, world_yaw_deg))

    def _trim_oldest(self) -> None:
        if len(self._items) <= self.max_items:
            return
        self._items = sorted(self._items, key=lambda item: item.last_seen_ts, reverse=True)[: self.max_items]

