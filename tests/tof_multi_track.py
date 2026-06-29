"""Cluster ToF hits into separate tracks; pick a primary aim target."""

from __future__ import annotations

import math
from collections import deque
from typing import Any


def _bearing_deg(x_mm: float, z_mm: float) -> float:
    return math.degrees(math.atan2(x_mm, z_mm))


def _cluster_hits(hits: list[dict[str, Any]], merge_radius_mm: float) -> list[list[dict[str, Any]]]:
    clusters: list[list[dict[str, Any]]] = []
    for hit in hits:
        placed = False
        for cluster in clusters:
            cx = sum(h["x_mm"] for h in cluster) / len(cluster)
            cz = sum(h["z_mm"] for h in cluster) / len(cluster)
            if math.hypot(hit["x_mm"] - cx, hit["z_mm"] - cz) <= merge_radius_mm:
                cluster.append(hit)
                placed = True
                break
        if not placed:
            clusters.append([hit])
    return clusters


def _weighted_centroid(cluster: list[dict[str, Any]]) -> tuple[float, float, int, float | None]:
    weights: list[float] = []
    for h in cluster:
        dist = max(int(h["dist_mm"]), 80)
        w = 1.0 / dist
        if h.get("motion") in ("approach", "drift_in"):
            w *= 1.8
        elif h.get("motion") in ("depart", "drift_out"):
            w *= 0.6
        weights.append(w)
    wsum = sum(weights) or 1.0
    x = sum(h["x_mm"] * w for h, w in zip(cluster, weights)) / wsum
    z = sum(h["z_mm"] * w for h, w in zip(cluster, weights)) / wsum
    dist = min(int(h["dist_mm"]) for h in cluster)
    vels = [h.get("vel_mm_s") for h in cluster if h.get("vel_mm_s") is not None]
    vel = sum(vels) / len(vels) if vels else None
    return x, z, dist, vel


def _classify_track(
    track: dict[str, Any],
    history: deque[tuple[int, int, float]],
) -> dict[str, Any]:
    spread = 0.0
    if len(history) >= 3:
        xs = [p[0] for p in history]
        zs = [p[1] for p in history]
        spread = float(max(max(xs) - min(xs), max(zs) - min(zs)))

    age = len(history)
    motion = track.get("motion", "still")
    vel = abs(track.get("vel_mm_s") or 0)
    sensor_count = int(track.get("sensor_count", 1))
    dist = int(track.get("dist_mm", 9999))

    if age < 5:
        kind, conf, reason = "uncertain", 0.5, "observing…"
    elif motion in ("approach", "depart") or vel > 40 or spread > 150:
        kind, conf, reason = "human", 0.9, "active movement"
    elif motion in ("drift_in", "drift_out") or vel > 12 or spread > 70:
        kind, conf, reason = "human", 0.75, "shifting / unstable"
    elif spread <= 70 and vel <= 12:
        if dist < 900:
            kind, conf, reason = "uncertain", 0.72, "close still return"
        else:
            kind, conf, reason = "obstacle", min(0.95, 0.6 + age * 0.02), "stable fixed return"
    else:
        kind, conf, reason = "uncertain", 0.55, "fluctuating"

    track["kind"] = kind
    track["confidence"] = round(conf, 2)
    track["spread_mm"] = round(spread)
    track["reason"] = reason
    return track


def _pick_primary(tracks: list[dict[str, Any]]) -> dict[str, Any] | None:
    if not tracks:
        return None

    def score(t: dict[str, Any]) -> float:
        kind = t.get("kind", "uncertain")
        kind_w = {"human": 300.0, "uncertain": 100.0, "obstacle": 20.0}.get(kind, 0.0)
        dist = float(t.get("dist_mm", 9999))
        vel = float(t.get("vel_mm_s") or 0.0)
        approach_w = max(0.0, -vel) * 0.5
        conf = float(t.get("confidence", 0.0)) * 50.0
        return kind_w + approach_w + conf - dist * 0.05

    return max(tracks, key=score)


class MultiTrackTracker:
    """Per-frame clustering with short-lived track IDs for viz + approach lock."""

    def __init__(self, *, merge_radius_mm: float = 800.0, window: int = 24) -> None:
        self.merge_radius_mm = merge_radius_mm
        self._window = window
        self._histories: dict[int, deque[tuple[int, int, float]]] = {}
        self._next_id = 1
        self._last_positions: dict[int, tuple[float, float]] = {}
        self._missed_frames: dict[int, int] = {}

    def reset(self) -> None:
        self._histories.clear()
        self._last_positions.clear()
        self._missed_frames.clear()
        self._next_id = 1

    def _fresh_id(self) -> int:
        used = set(self._histories) | set(self._last_positions)
        tid = 1
        while tid in used:
            tid += 1
        self._next_id = max(self._next_id, tid + 1)
        return tid

    def _assign_id(self, x_mm: float, z_mm: float) -> int:
        best_id: int | None = None
        match_radius = max(self.merge_radius_mm * 1.5, 900.0)
        best_d = match_radius
        for tid, (lx, lz) in self._last_positions.items():
            d = math.hypot(x_mm - lx, z_mm - lz)
            if d < best_d:
                best_d = d
                best_id = tid
        if best_id is not None:
            return best_id
        return self._fresh_id()

    def update(
        self,
        hits: list[dict[str, Any]],
        *,
        now: float,
    ) -> tuple[list[dict[str, Any]], list[dict[str, Any]], dict[str, Any] | None]:
        if not hits:
            stale = list(self._last_positions.keys())
            for tid in stale:
                self._missed_frames[tid] = self._missed_frames.get(tid, 0) + 1
                if self._missed_frames[tid] > 8:
                    self._histories.pop(tid, None)
                    self._last_positions.pop(tid, None)
                    self._missed_frames.pop(tid, None)
            return [], [], None

        clusters = _cluster_hits(hits, self.merge_radius_mm)
        tracks: list[dict[str, Any]] = []
        new_positions: dict[int, tuple[float, float]] = {}

        for cluster in clusters:
            x, z, dist, vel = _weighted_centroid(cluster)
            tid = self._assign_id(x, z)
            hist = self._histories.setdefault(tid, deque(maxlen=self._window))
            hist.append((round(x), round(z), now))
            new_positions[tid] = (x, z)

            motions = [h["motion"] for h in cluster]
            if any(m in ("approach", "drift_in") for m in motions):
                motion = "approach"
            elif any(m in ("depart", "drift_out") for m in motions):
                motion = "depart"
            else:
                motion = "still"

            track = {
                "id": tid,
                "x_mm": round(x),
                "z_mm": round(z),
                "dist_mm": dist,
                "bearing_deg": round(_bearing_deg(x, z), 1),
                "zones": sorted({h["zone"] for h in cluster}),
                "sensor_count": len(cluster),
                "motion": motion,
                "vel_mm_s": vel,
            }
            tracks.append(_classify_track(track, hist))

        # Handle persistence and missed frames
        for tid in list(self._last_positions.keys()):
            if tid not in new_positions:
                self._missed_frames[tid] = self._missed_frames.get(tid, 0) + 1
                if self._missed_frames[tid] > 8:  # drop after ~8 frames
                    self._histories.pop(tid, None)
                    self._last_positions.pop(tid, None)
                    self._missed_frames.pop(tid, None)
                else:
                    # Keep position alive
                    new_positions[tid] = self._last_positions[tid]
            else:
                self._missed_frames[tid] = 0

        self._last_positions = new_positions

        primary = _pick_primary(tracks)
        classified_hits: list[dict[str, Any]] = []
        for hit in hits:
            best: dict[str, Any] | None = None
            best_d = self.merge_radius_mm
            for track in tracks:
                d = math.hypot(hit["x_mm"] - track["x_mm"], hit["z_mm"] - track["z_mm"])
                if d < best_d:
                    best_d = d
                    best = track
            if best is None:
                classified_hits.append(hit)
                continue
            classified_hits.append(
                {
                    **hit,
                    "track_id": best["id"],
                    "kind": best["kind"],
                    "confidence": best["confidence"],
                    "reason": best["reason"],
                }
            )

        return classified_hits, tracks, primary
