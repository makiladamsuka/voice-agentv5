"""Short-lived yaw-only memory for ToF motion ghosts (unverified approach cues)."""

from __future__ import annotations

import time
from dataclasses import dataclass

from lib.person_memory import angular_distance_deg, wrap_degrees


def _clamp(value: float, lo: float, hi: float) -> float:
    return max(lo, min(hi, value))


def zone_to_yaw_offset(zone: str, zone_yaw_deg: float) -> float:
    if zone == "L":
        return -abs(zone_yaw_deg)
    if zone == "R":
        return abs(zone_yaw_deg)
    return 0.0


@dataclass
class MotionMemoryItem:
    id: int
    world_yaw_deg: float
    zone: str
    distance_mm: int
    created_ts: float
    investigated_ts: float = 0.0
    fade_start_ts: float = 0.0
    verified: bool = False

    def age_sec(self, now: float | None = None) -> float:
        now = time.time() if now is None else now
        return max(0.0, now - self.created_ts)

    def freshness(self, now: float | None = None, *, fade_sec: float = 5.0) -> float:
        now = time.time() if now is None else now
        if self.verified:
            return 0.0
        if self.fade_start_ts <= 0.0:
            return 1.0
        elapsed = now - self.fade_start_ts
        return _clamp(1.0 - (elapsed / max(fade_sec, 0.001)), 0.0, 1.0)

    def to_dict(self, now: float | None = None, *, fade_sec: float = 5.0) -> dict:
        now = time.time() if now is None else now
        return {
            "id": self.id,
            "world_yaw_deg": self.world_yaw_deg,
            "zone": self.zone,
            "distance_mm": self.distance_mm,
            "age_sec": self.age_sec(now),
            "freshness": self.freshness(now, fade_sec=fade_sec),
            "verified": self.verified,
            "fading": self.fade_start_ts > 0.0 and not self.verified,
        }


class MotionMemory:
    """Floor-map of unverified ToF approach bearings."""

    def __init__(
        self,
        *,
        fade_sec: float = 5.0,
        merge_angle_deg: float = 15.0,
        max_items: int = 8,
        zone_yaw_deg: float = 35.0,
    ) -> None:
        self.fade_sec = max(0.5, fade_sec)
        self.merge_angle_deg = max(1.0, merge_angle_deg)
        self.max_items = max(1, max_items)
        self.zone_yaw_deg = zone_yaw_deg
        self._items: list[MotionMemoryItem] = []
        self._next_id = 1

    def observe_from_prox(
        self,
        *,
        zone: str,
        base_world_yaw_deg: float,
        distance_mm: int = 0,
        now: float | None = None,
    ) -> MotionMemoryItem:
        now = time.time() if now is None else now
        self.prune(now)
        offset = zone_to_yaw_offset(zone, self.zone_yaw_deg)
        world_yaw = wrap_degrees(base_world_yaw_deg + offset)
        item = self._nearest(world_yaw)
        if item is None or angular_distance_deg(item.world_yaw_deg, world_yaw) > self.merge_angle_deg:
            item = MotionMemoryItem(
                id=self._next_id,
                world_yaw_deg=world_yaw,
                zone=zone,
                distance_mm=int(distance_mm),
                created_ts=now,
            )
            self._next_id += 1
            self._items.append(item)
        else:
            item.zone = zone
            item.distance_mm = int(distance_mm)
            item.created_ts = now
            item.investigated_ts = 0.0
            item.fade_start_ts = 0.0
            item.verified = False
        self._trim_oldest()
        return item

    def mark_investigated(self, item_id: int, now: float | None = None) -> None:
        now = time.time() if now is None else now
        for item in self._items:
            if item.id == item_id:
                item.investigated_ts = now
                return

    def start_fade(self, item_id: int | None = None, now: float | None = None) -> None:
        now = time.time() if now is None else now
        for item in self._items:
            if item.verified:
                continue
            if item_id is not None and item.id != item_id:
                continue
            if item.fade_start_ts <= 0.0:
                item.fade_start_ts = now

    def mark_verified(self, world_yaw_deg: float, now: float | None = None) -> None:
        now = time.time() if now is None else now
        item = self._nearest(world_yaw_deg)
        if item is not None and angular_distance_deg(item.world_yaw_deg, world_yaw_deg) <= self.merge_angle_deg:
            item.verified = True
            item.fade_start_ts = 0.0

    def prune(self, now: float | None = None) -> None:
        now = time.time() if now is None else now
        kept: list[MotionMemoryItem] = []
        for item in self._items:
            if item.verified:
                continue
            if item.fade_start_ts > 0.0 and item.freshness(now, fade_sec=self.fade_sec) <= 0.0:
                continue
            kept.append(item)
        self._items = kept

    def active(self, now: float | None = None) -> list[MotionMemoryItem]:
        now = time.time() if now is None else now
        self.prune(now)
        return sorted(self._items, key=lambda i: i.created_ts, reverse=True)

    def snapshots(self, now: float | None = None) -> list[dict]:
        now = time.time() if now is None else now
        return [item.to_dict(now, fade_sec=self.fade_sec) for item in self.active(now)]

    def latest_for_zone(self, zone: str, now: float | None = None) -> MotionMemoryItem | None:
        now = time.time() if now is None else now
        matches = [i for i in self.active(now) if i.zone == zone and not i.verified]
        if not matches:
            return None
        return max(matches, key=lambda i: i.created_ts)

    def _nearest(self, world_yaw_deg: float) -> MotionMemoryItem | None:
        if not self._items:
            return None
        return min(self._items, key=lambda i: angular_distance_deg(i.world_yaw_deg, world_yaw_deg))

    def _trim_oldest(self) -> None:
        if len(self._items) <= self.max_items:
            return
        self._items = sorted(self._items, key=lambda i: i.created_ts, reverse=True)[: self.max_items]
