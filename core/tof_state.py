"""Shared ToF state, filtering, and snapshot for viz + start_robot dashboard."""

from __future__ import annotations

import math
import re
import threading
import time
from collections import deque
from pathlib import Path
from typing import Any

from lib.tof_filter import MAX_TRUST_MM, TofFilterBank
from lib.tof_multi_track import MultiTrackTracker, _bearing_deg

APP_DIR = Path(__file__).resolve().parent.parent
CONFIG_PATH = APP_DIR / "config.yaml"

MAX_MM = MAX_TRUST_MM
HISTORY_LEN = 150
FILTER_BANK = TofFilterBank(3)

_TOF_RE = re.compile(
    r"TOF\s+L=(-?\d+)\s+C=(-?\d+)\s+R=(-?\d+)"
    r"\s+VL=(-?\d+)\s+VC=(-?\d+)\s+VR=(-?\d+)"
)

LABELS = ("LEFT", "CENTER", "RIGHT")
ZONE_KEYS = ("L", "C", "R")
COLORS = ("#3b82f6", "#a855f7", "#22c55e")
SENSOR_ANGLES_DEG = (-45, 0, 45)
SENSOR_MOUNT_Z_MM = 275
SWAP_LEFT_RIGHT = True


def _load_prox_cfg() -> dict[str, Any]:
    try:
        import yaml

        if CONFIG_PATH.exists():
            with open(CONFIG_PATH, encoding="utf-8") as f:
                return (yaml.safe_load(f) or {}).get("proximity", {}) or {}
    except Exception:
        pass
    return {}


def _motion_class(vel: int | None) -> str:
    if vel is None:
        return "still"
    if vel < -45:
        return "approach"
    if vel > 45:
        return "depart"
    if vel < -15:
        return "drift_in"
    if vel > 15:
        return "drift_out"
    return "still"


def _compute_hits(
    mm: list[int],
    vel: list[int | None],
    open_flags: list[bool],
) -> tuple[list[dict[str, Any]], dict[str, Any] | None]:
    channels: list[tuple[str, int, int]] = [
        ("L", 0, -45),
        ("C", 1, 0),
        ("R", 2, 45),
    ]
    if SWAP_LEFT_RIGHT:
        channels = [
            ("R", 0, 45),
            ("C", 1, 0),
            ("L", 2, -45),
        ]

    hits: list[dict[str, Any]] = []
    for zone, idx, angle in channels:
        if mm[idx] < 0 or open_flags[idx]:
            continue
        rad = math.radians(angle)
        dist = mm[idx]
        x_mm = round(math.sin(rad) * dist)
        z_mm = round(SENSOR_MOUNT_Z_MM + math.cos(rad) * dist)
        hits.append(
            {
                "zone": zone,
                "label": LABELS[ZONE_KEYS.index(zone)],
                "angle_deg": angle,
                "dist_mm": dist,
                "x_mm": x_mm,
                "z_mm": z_mm,
                "vel_mm_s": vel[idx],
                "motion": _motion_class(vel[idx]),
            }
        )

    fused: dict[str, Any] | None = None
    if hits:
        motions = [h["motion"] for h in hits]
        if any(m in ("approach", "drift_in") for m in motions):
            motion = "approach"
        elif any(m in ("depart", "drift_out") for m in motions):
            motion = "depart"
        else:
            motion = "still"
        if len(hits) == 1:
            h = hits[0]
            fused = {
                "x_mm": h["x_mm"],
                "z_mm": h["z_mm"],
                "zones": [h["zone"]],
                "motion": motion,
                "bearing_deg": round(_bearing_deg(h["x_mm"], h["z_mm"]), 1),
            }
        else:
            xs = [h["x_mm"] for h in hits]
            zs = [h["z_mm"] for h in hits]
            fused = {
                "x_mm": round(sum(xs) / len(xs)),
                "z_mm": round(sum(zs) / len(zs)),
                "zones": [h["zone"] for h in hits],
                "motion": motion,
            }
    return hits, fused


class TofState:
    def __init__(self) -> None:
        self._lock = threading.Lock()
        self.connected = False
        self.port = ""
        self.error = ""
        self.sample_count = 0
        self.dropouts = [0, 0, 0]
        self.mm = [-1, -1, -1]
        self.vel: list[int | None] = [None, None, None]
        self.open = [True, True, True]
        self.history: list[deque[int]] = [
            deque(maxlen=HISTORY_LEN) for _ in range(3)
        ]
        self.boot: deque[str] = deque(maxlen=40)
        self.last_ts = 0.0
        prox_cfg = _load_prox_cfg()
        merge_mm = float(prox_cfg.get("tof_merge_radius_mm", 800.0))
        self._tracker = MultiTrackTracker(merge_radius_mm=merge_mm)
        self.body_yaw_deg = 0.0
        self.head_yaw_on_body_deg = 0.0
        self.base_yaw_sign = -1.0
        self.encoder_yaw_deg = 0.0
        self.front_offset_deg = 0.0
        self.imu_drift_correction_deg = 0.0
        self.imu_yaw_rel_deg = 0.0
        self.fusion_stationary = False
        self.from_home_enc_deg = 0.0
        self.from_home_imu_deg = 0.0
        self.imu_total_from_home_deg = 0.0
        self.pan_from_home_deg = 0.0
        self.pan_cmd_from_home_deg = 0.0
        self.pitch_from_home_deg = 0.0
        self.imu_pitch_deg = 0.0
        self.imu_pitch_from_home_deg = 0.0
        self.pan_mech_deg = 0.0
        self.tilt_mech_deg = 0.0
        self.head_pan = 0.0
        self.head_tilt = 0.0
        self.pan_yaw_sign = -1.0
        self.tilt_sign = 1.0
        self.imu_pitch_sign = -1.0
        self.home_locked = False
        self.base_busy = False
        self.stationary = False
        self.map_yaw_deg = 0.0
        self.viz_base_yaw_deg = 0.0
        self.disagreement_deg = 0.0
        self.encoder_count_delta = 0
        self.imu_online = False
        self.max_yaw_deg = 120.0
        self.base_rotating = False
        self.approach_phase = "idle"
        self.clear_wait_remaining_sec = 0.0
        try:
            import yaml

            if CONFIG_PATH.exists():
                with open(CONFIG_PATH, encoding="utf-8") as f:
                    base_cfg = (yaml.safe_load(f) or {}).get("base", {}) or {}
                    self.max_yaw_deg = float(base_cfg.get("max_yaw_deg", 120.0))
        except Exception:
            pass

    def update_sample(
        self,
        mm: list[int],
        vel: list[int | None],
        *,
        open_flags: list[bool] | None = None,
    ) -> None:
        with self._lock:
            if self.base_rotating:
                return
            self.sample_count += 1
            self.mm = mm
            self.vel = vel
            if open_flags is not None:
                self.open = open_flags
            self.last_ts = time.time()
            for i in range(3):
                if mm[i] < 0:
                    self.dropouts[i] += 1
                else:
                    self.history[i].append(mm[i])

    def set_connected(self, port: str) -> None:
        with self._lock:
            self.connected = True
            self.port = port
            self.error = ""

    def set_error(self, msg: str) -> None:
        with self._lock:
            self.connected = False
            self.error = msg

    def add_boot(self, line: str) -> None:
        with self._lock:
            self.boot.append(line)

    def set_base_rotating(self, rotating: bool) -> None:
        with self._lock:
            self.base_rotating = bool(rotating)
            if rotating:
                self._tracker.reset()
                FILTER_BANK.reset()

    def reset_tracks(self) -> None:
        with self._lock:
            self._tracker.reset()
            self.last_ts = 0.0

    def update_pose(
        self,
        *,
        body_yaw_deg: float | None = None,
        head_yaw_on_body_deg: float | None = None,
        base_yaw_sign: float | None = None,
        encoder_yaw_deg: float | None = None,
        front_offset_deg: float | None = None,
        imu_drift_correction_deg: float | None = None,
        imu_yaw_rel_deg: float | None = None,
        fusion_stationary: bool | None = None,
        from_home_enc_deg: float | None = None,
        from_home_imu_deg: float | None = None,
        map_yaw_deg: float | None = None,
        viz_base_yaw_deg: float | None = None,
        disagreement_deg: float | None = None,
        encoder_count_delta: int | None = None,
        imu_online: bool | None = None,
        max_yaw_deg: float | None = None,
        imu_total_from_home_deg: float | None = None,
        pan_from_home_deg: float | None = None,
        pan_cmd_from_home_deg: float | None = None,
        pitch_from_home_deg: float | None = None,
        imu_pitch_deg: float | None = None,
        imu_pitch_from_home_deg: float | None = None,
        pan_mech_deg: float | None = None,
        tilt_mech_deg: float | None = None,
        head_pan: float | None = None,
        head_tilt: float | None = None,
        pan_yaw_sign: float | None = None,
        tilt_sign: float | None = None,
        imu_pitch_sign: float | None = None,
        home_locked: bool | None = None,
        base_busy: bool | None = None,
        stationary: bool | None = None,
        approach_phase: str | None = None,
        clear_wait_remaining_sec: float | None = None,
    ) -> None:
        with self._lock:
            if body_yaw_deg is not None:
                self.body_yaw_deg = float(body_yaw_deg)
            if head_yaw_on_body_deg is not None:
                self.head_yaw_on_body_deg = float(head_yaw_on_body_deg)
            if base_yaw_sign is not None:
                self.base_yaw_sign = float(base_yaw_sign)
            if encoder_yaw_deg is not None:
                self.encoder_yaw_deg = float(encoder_yaw_deg)
            if front_offset_deg is not None:
                self.front_offset_deg = float(front_offset_deg)
            if imu_drift_correction_deg is not None:
                self.imu_drift_correction_deg = float(imu_drift_correction_deg)
            if imu_yaw_rel_deg is not None:
                self.imu_yaw_rel_deg = float(imu_yaw_rel_deg)
            if fusion_stationary is not None:
                self.fusion_stationary = bool(fusion_stationary)
            if from_home_enc_deg is not None:
                self.from_home_enc_deg = float(from_home_enc_deg)
            if from_home_imu_deg is not None:
                self.from_home_imu_deg = float(from_home_imu_deg)
            if map_yaw_deg is not None:
                self.map_yaw_deg = float(map_yaw_deg)
            if viz_base_yaw_deg is not None:
                self.viz_base_yaw_deg = float(viz_base_yaw_deg)
            if disagreement_deg is not None:
                self.disagreement_deg = float(disagreement_deg)
            if encoder_count_delta is not None:
                self.encoder_count_delta = int(encoder_count_delta)
            if imu_online is not None:
                self.imu_online = bool(imu_online)
            if max_yaw_deg is not None:
                self.max_yaw_deg = float(max_yaw_deg)
            if imu_total_from_home_deg is not None:
                self.imu_total_from_home_deg = float(imu_total_from_home_deg)
            if pan_from_home_deg is not None:
                self.pan_from_home_deg = float(pan_from_home_deg)
            if pan_cmd_from_home_deg is not None:
                self.pan_cmd_from_home_deg = float(pan_cmd_from_home_deg)
            if pitch_from_home_deg is not None:
                self.pitch_from_home_deg = float(pitch_from_home_deg)
            if imu_pitch_deg is not None:
                self.imu_pitch_deg = float(imu_pitch_deg)
            if imu_pitch_from_home_deg is not None:
                self.imu_pitch_from_home_deg = float(imu_pitch_from_home_deg)
            if pan_mech_deg is not None:
                self.pan_mech_deg = float(pan_mech_deg)
            if tilt_mech_deg is not None:
                self.tilt_mech_deg = float(tilt_mech_deg)
            if head_pan is not None:
                self.head_pan = float(head_pan)
            if head_tilt is not None:
                self.head_tilt = float(head_tilt)
            if pan_yaw_sign is not None:
                self.pan_yaw_sign = float(pan_yaw_sign)
            if tilt_sign is not None:
                self.tilt_sign = float(tilt_sign)
            if imu_pitch_sign is not None:
                self.imu_pitch_sign = float(imu_pitch_sign)
            if home_locked is not None:
                self.home_locked = bool(home_locked)
            if base_busy is not None:
                self.base_busy = bool(base_busy)
            if stationary is not None:
                self.stationary = bool(stationary)
            if approach_phase is not None:
                self.approach_phase = str(approach_phase)
            if clear_wait_remaining_sec is not None:
                self.clear_wait_remaining_sec = float(clear_wait_remaining_sec)

    def snapshot(self) -> dict[str, Any]:
        with self._lock:
            ok = sum(1 for d in self.mm if d >= 0)
            mm = list(self.mm)
            vel = [v if v is None else int(v) for v in self.vel]
            open_flags = list(self.open)
            hits, fused_raw = _compute_hits(mm, vel, open_flags)
            hits, tracks, primary = self._tracker.update(hits, now=self.last_ts)
            fused = primary if primary else fused_raw
            tof_bearing_deg: float | None = None
            if primary:
                tof_bearing_deg = float(primary.get("bearing_deg", 0.0))
            elif fused:
                tof_bearing_deg = round(
                    _bearing_deg(float(fused["x_mm"]), float(fused["z_mm"])),
                    1,
                )
            aim_error_deg: float | None = None
            if primary:
                aim_error_deg = float(primary.get("bearing_deg", 0.0))
            return {
                "connected": self.connected,
                "port": self.port,
                "error": self.error,
                "sample_count": self.sample_count,
                "ok_count": ok,
                "dropouts": list(self.dropouts),
                "mm": mm,
                "vel": vel,
                "open": open_flags,
                "history": [list(h) for h in self.history],
                "boot": list(self.boot),
                "last_ts": self.last_ts,
                "max_mm": MAX_MM,
                "labels": list(LABELS),
                "colors": list(COLORS),
                "sensor_angles_deg": list(SENSOR_ANGLES_DEG),
                "sensor_mount_z_mm": SENSOR_MOUNT_Z_MM,
                "swap_left_right": SWAP_LEFT_RIGHT,
                "hits": hits,
                "tracks": tracks,
                "primary_target": primary,
                "fused": fused,
                "body_yaw_deg": self.body_yaw_deg,
                "head_yaw_on_body_deg": self.head_yaw_on_body_deg,
                "base_yaw_sign": self.base_yaw_sign,
                "encoder_yaw_deg": self.encoder_yaw_deg,
                "front_offset_deg": self.front_offset_deg,
                "from_home_enc_deg": self.from_home_enc_deg,
                "from_home_imu_deg": self.from_home_imu_deg,
                "imu_total_from_home_deg": self.imu_total_from_home_deg,
                "pan_from_home_deg": self.pan_from_home_deg,
                "pan_cmd_from_home_deg": self.pan_cmd_from_home_deg,
                "pitch_from_home_deg": self.pitch_from_home_deg,
                "imu_pitch_deg": self.imu_pitch_deg,
                "imu_pitch_from_home_deg": self.imu_pitch_from_home_deg,
                "pan_mech_deg": self.pan_mech_deg,
                "tilt_mech_deg": self.tilt_mech_deg,
                "head_pan": self.head_pan,
                "head_tilt": self.head_tilt,
                "pan_yaw_sign": self.pan_yaw_sign,
                "tilt_sign": self.tilt_sign,
                "imu_pitch_sign": self.imu_pitch_sign,
                "home_locked": self.home_locked,
                "base_busy": self.base_busy,
                "stationary": self.stationary,
                "map_yaw_deg": self.map_yaw_deg,
                "viz_base_yaw_deg": self.viz_base_yaw_deg,
                "disagreement_deg": self.disagreement_deg,
                "encoder_count_delta": self.encoder_count_delta,
                "imu_online": self.imu_online,
                "base_rotating": self.base_rotating,
                "approach_phase": self.approach_phase,
                "clear_wait_remaining_sec": self.clear_wait_remaining_sec,
                "max_yaw_deg": self.max_yaw_deg,
                "imu_drift_correction_deg": self.imu_drift_correction_deg,
                "imu_yaw_rel_deg": self.imu_yaw_rel_deg,
                "fusion_stationary": self.fusion_stationary,
                "aim_error_deg": aim_error_deg,
                "tof_bearing_deg": tof_bearing_deg,
            }


STATE = TofState()
