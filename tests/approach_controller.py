"""ToF base turns: IMU closed-loop aim at person + return HOME when clear."""

from __future__ import annotations

import math
import threading
import time
from pathlib import Path
from typing import TYPE_CHECKING, Callable

try:
    import yaml
except ImportError:
    yaml = None

from lib.base_home_drive import drive_base_to_imu_zero
from lib.head_mech import signed_pan_mech_deg
from lib.yaw_home_tracker import YawHomeTracker
from base_spin_motion import expected_encoder_delta, write_base_step_spin

try:
    from core.tof_state import STATE as TOF_STATE
except ImportError:
    TOF_STATE = None

if TYPE_CHECKING:
    from hardware.arduino_servo import ArduinoServoLink

APP_DIR = Path(__file__).resolve().parents[1]
DEFAULT_CONFIG_PATH = APP_DIR / "config.yaml"


def _load_yaml(path: Path) -> dict:
    if yaml is None or not path.exists():
        return {}
    with open(path, encoding="utf-8") as f:
        return yaml.safe_load(f) or {}


def _cfg(data, *keys, default=None):
    cur = data
    for key in keys:
        if not isinstance(cur, dict) or key not in cur:
            return default
        cur = cur[key]
    return cur


def start_imu(imu_cfg: dict):
    if not imu_cfg.get("enabled", True):
        return None
    try:
        from imu_sensor import ImuReader, startup_level_calibrate
    except ImportError:
        print("[Approach] WARNING: imu_sensor not available — encoder-only mode.")
        return None

    _axis = imu_cfg.get("axis_remap")
    axis_remap = tuple(int(v) for v in _axis) if _axis else (-3, 2, -1)
    reader = ImuReader(
        bus=int(imu_cfg.get("i2c_bus", 1)),
        address=int(imu_cfg.get("address", 0x69)),
        sample_hz=float(imu_cfg.get("sample_hz", 100.0)),
        roll_pitch_alpha=float(imu_cfg.get("roll_pitch_alpha", 0.02)),
        axis_remap=axis_remap,
        roll_offset_deg=float(imu_cfg.get("roll_offset_deg", 0.0)),
        pitch_offset_deg=float(imu_cfg.get("pitch_offset_deg", 0.0)),
        yaw_sign=float(imu_cfg.get("yaw_sign", 1.0)),
    )
    reader.start()
    if imu_cfg.get("auto_level_on_start", True):
        settle = float(imu_cfg.get("auto_level_sec", 2.0))
        print(f"[Approach] IMU level calibration ({settle:.1f}s) — hold still…")
        startup_level_calibrate(
            reader,
            duration_sec=settle,
            warmup_sec=float(imu_cfg.get("auto_level_warmup_sec", 0.3)),
            max_gyro_dps=float(imu_cfg.get("auto_level_gyro_max_dps", 8.0)),
            min_samples=int(imu_cfg.get("auto_level_min_samples", 40)),
        )
    time.sleep(0.15)
    return reader


def read_imu(reader, yaw_sign: float) -> tuple[float, float, bool]:
    if reader is None:
        return 0.0, 0.0, False
    sample = reader.latest()
    if sample is None:
        return 0.0, 0.0, False
    imu_yaw = reader.filter.yaw_integral_deg() * yaw_sign
    gyro = max(abs(sample.gyro_x_dps), abs(sample.gyro_y_dps), abs(sample.gyro_z_dps))
    return imu_yaw, gyro, True


def query_enc(link: ArduinoServoLink, fallback: float) -> tuple[float, int, bool, float]:
    try:
        st = link.query_status()
        if st is not None:
            cpd = float(st.counts_per_degree)
            return float(st.degrees), int(st.encoder_count), bool(st.busy), cpd
    except Exception:
        pass
    return fallback, 0, False, 31.1667


def lock_home(
    tracker: YawHomeTracker,
    link: ArduinoServoLink,
    reader,
    yaw_sign: float,
    servo_cfg: dict,
    pan: float,
    *,
    zero_encoder: bool = False,
) -> None:
    if zero_encoder:
        link.write_base_stop()
        link.zero_base()
        time.sleep(0.2)
    enc, count, _, cpd = query_enc(link, 0.0)
    tracker.counts_per_degree = cpd
    imu_yaw, _, imu_ok = read_imu(reader, yaw_sign)
    pan_mech = signed_pan_mech_deg(pan, servo_cfg)
    tracker.lock_home(
        encoder_deg=enc,
        encoder_count=count,
        imu_yaw_deg=imu_yaw,
        pan_mech_deg=pan_mech,
    )
    print(
        f"[Approach] HOME locked  enc={enc:+.1f}°  counts={count}  "
        f"imu={imu_yaw:+.1f}°  pan_mech={pan_mech:+.1f}°"
        + ("" if imu_ok else "  (IMU off)")
    )


class ApproachController:
    """Aim base at ToF bearing (encoder spin by bearing) and return HOME when clear."""

    def __init__(
        self,
        link: ArduinoServoLink,
        tracker: YawHomeTracker,
        reader,
        *,
        yaw_sign: float,
        base_cfg: dict,
        servo_cfg: dict,
        prox_cfg: dict,
        base_yaw_sign: float = -1.0,
        running: Callable[[], bool] | None = None,
    ) -> None:
        self._link = link
        self._tracker = tracker
        self._reader = reader
        self._yaw_sign = float(yaw_sign)
        self._base_cfg = base_cfg
        self._servo_cfg = servo_cfg
        self._base_yaw_sign = float(base_yaw_sign)
        self._running = running or (lambda: True)

        self.pan_center = float(servo_cfg.get("pan_center", 100.0))
        self.tilt_center = float(servo_cfg.get("tilt_center", 110.0))
        self.max_yaw_deg = float(base_cfg.get("max_yaw_deg", 120.0))
        self.base_sign = float(base_cfg.get("sign", -1.0))
        self.encoder_sign = float(base_cfg.get("encoder_sign", -1.0))
        self.spin_tolerance_deg = float(base_cfg.get("spin_stop_tolerance_deg", 1.5))
        self.spin_timeout_sec = float(base_cfg.get("spin_timeout_sec", 12.0))
        self.spin_stall_sec = float(base_cfg.get("spin_stall_sec", 0.35))
        self.spin_positive_uses_left = bool(base_cfg.get("spin_positive_uses_left", False))

        self.prox_enabled = bool(prox_cfg.get("enabled", True))
        self.prox_lockout_sec = float(prox_cfg.get("post_turn_lockout_sec", 2.0))
        self.prox_min_confidence = int(prox_cfg.get("min_confidence", 3))
        self.prox_max_turns = int(prox_cfg.get("max_turns_per_window", 2))
        self.prox_window_sec = float(prox_cfg.get("turn_window_sec", 30.0))
        self.prox_turn_step = float(prox_cfg.get("turn_step_deg", 35.0))
        self.prox_cooldown_sec = float(prox_cfg.get("cooldown_sec", 5.0))
        self.prox_post_motion_blanking_sec = float(
            prox_cfg.get("post_motion_blanking_sec", 0.5)
        )
        self.tof_align_blanking_sec = float(prox_cfg.get("tof_align_blanking_sec", 0.15))
        self._prox_swap_lr = bool(prox_cfg.get("swap_left_right", False))

        self.tof_turn_enabled = bool(prox_cfg.get("tof_turn_enabled", True))
        self.tof_min_bearing_deg = float(prox_cfg.get("tof_min_bearing_deg", 3.0))
        self.tof_min_dist_mm = int(prox_cfg.get("tof_min_dist_mm", 80))
        self.tof_max_dist_mm = int(prox_cfg.get("tof_max_dist_mm", 2000))
        self.tof_confirm_ticks = int(prox_cfg.get("tof_confirm_ticks", 2))
        self.tof_aim_tolerance_deg = float(prox_cfg.get("tof_aim_tolerance_deg", 5.0))
        self.tof_bearing_undershoot_deg = float(
            prox_cfg.get("tof_bearing_undershoot_deg", 3.5)
        )
        self.tof_undershoot_close_mm = int(prox_cfg.get("tof_undershoot_close_mm", 450))
        self.tof_undershoot_far_mm = int(prox_cfg.get("tof_undershoot_far_mm", 1500))
        self.tof_aim_obstacle_close_mm = int(
            prox_cfg.get("tof_aim_obstacle_close_mm", 900)
        )
        self.tof_bearing_flip_reject_deg = float(
            prox_cfg.get("tof_bearing_flip_reject_deg", 20.0)
        )
        self.tof_align_cooldown_sec = float(prox_cfg.get("tof_align_cooldown_sec", 0.25))
        self.tof_target_lock_sec = float(prox_cfg.get("tof_target_lock_sec", 2.5))
        self.tof_stale_sec = float(prox_cfg.get("tof_stale_sec", 0.55))
        self.tof_spin_settle_sec = float(prox_cfg.get("tof_spin_settle_sec", 0.65))
        self.tof_gyro_settle_dps = float(prox_cfg.get("tof_gyro_settle_dps", 12.0))
        self.clear_return_enabled = bool(prox_cfg.get("clear_return_enabled", True))
        self.clear_return_sec = float(prox_cfg.get("clear_return_sec", 2.5))
        self.clear_return_tolerance_deg = float(
            prox_cfg.get("clear_return_tolerance_deg", 6.0)
        )

        self._prox_turn_timestamps: list[float] = []
        self._last_base_motion_done_ts = 0.0
        self._last_prox_reaction_approach_ts = 0.0
        self._post_turn_lockout_until = 0.0
        self._last_prox_ts = 0.0
        self._locked_track_id: int | None = None
        self._lock_track_until = 0.0
        self._locked_x_mm = 0.0
        self._locked_z_mm = 0.0
        self._bearing_stable_ticks = 0
        self._last_target_bearing = 0.0
        self._last_committed_bearing: float | None = None
        self._clear_since: float | None = None
        self._maneuvering = False
        self._tof_ignore_until = 0.0
        self._last_base_busy = False
        self._prox_zone = ""
        self._prox_confidence = 0
        self._prox_approach_ts = 0.0
        self._prox_active = False
        self.loop_hz = 50.0
        self._tracker_lock = threading.Lock()
        self._cached_enc_deg = 0.0
        self._cached_enc_count = 0
        self._cached_busy = False
        self._cached_cpd = 31.1667

        self._hold_head_home()

    def _fetch_enc(self) -> tuple[float, int, bool, float]:
        enc, count, busy, cpd = query_enc(self._link, 0.0)
        self._cached_enc_deg = enc
        self._cached_enc_count = count
        self._cached_busy = busy
        self._cached_cpd = max(cpd, 0.05)
        return enc, count, busy, self._cached_cpd

    def _publish_viz_from_cache(self) -> None:
        imu_yaw, gyro, imu_ok = read_imu(self._reader, self._yaw_sign)
        pan_mech = signed_pan_mech_deg(self.pan_center, self._servo_cfg)
        with self._tracker_lock:
            self._tracker.counts_per_degree = self._cached_cpd
            sample = self._tracker.update(
                encoder_deg=self._cached_enc_deg,
                encoder_count=self._cached_enc_count,
                imu_yaw_deg=imu_yaw,
                pan_mech_deg=pan_mech,
                gyro_dps=gyro,
                base_busy=self._maneuvering or self._cached_busy,
            )
        if sample is not None:
            self._publish_pose(sample, imu_online=imu_ok and self._reader is not None)

    def publish_viz_pose(self) -> None:
        """IMU-only viz refresh — never queries serial (avoids fighting base commands)."""
        self._publish_viz_from_cache()

    def accept_tof_samples(self) -> bool:
        """False while base rotates or briefly after — ToF bearings are invalid then."""
        if self._maneuvering:
            return False
        if time.time() < self._tof_ignore_until:
            return False
        if TOF_STATE is not None and TOF_STATE.base_rotating:
            return False

        is_busy = self._cached_busy

        if is_busy:
            if not self._last_base_busy:
                if TOF_STATE is not None:
                    TOF_STATE.set_base_rotating(True)
            self._last_base_busy = True
            return False

        if self._last_base_busy:
            self._last_base_busy = False
            self._tof_ignore_until = time.time() + self.tof_spin_settle_sec
            self._reset_tof_after_motion()
            if TOF_STATE is not None:
                TOF_STATE.set_base_rotating(False)
            return False

        if self._reader is not None:
            _, gyro, ok = read_imu(self._reader, self._yaw_sign)
            if ok and gyro > self.tof_gyro_settle_dps:
                return False

        return True

    def handle_tof_line(self, line: str) -> None:
        from core.tof_state import FILTER_BANK, STATE, _TOF_RE

        if not self.accept_tof_samples():
            return
        m = _TOF_RE.search(line)
        if not m:
            return
        raw = [int(m.group(i)) for i in range(1, 4)]
        mm, vel, open_flags = FILTER_BANK.update_all(raw)
        STATE.update_sample(mm, vel, open_flags=open_flags)

    def _reset_tof_after_motion(self) -> None:
        """Drop stale hits/tracks so post-spin bearings are not body-fixed ghosts."""
        if TOF_STATE is None:
            return
        try:
            from core.tof_state import FILTER_BANK
        except ImportError:
            FILTER_BANK = None  # type: ignore[misc, assignment]
        TOF_STATE.reset_tracks()
        if FILTER_BANK is not None:
            FILTER_BANK.reset()

    def _begin_base_motion(self) -> None:
        self._maneuvering = True
        self._last_base_busy = True
        self._bearing_stable_ticks = 0
        self._last_target_bearing = 0.0
        self._locked_track_id = None
        self._lock_track_until = 0.0
        if TOF_STATE is not None:
            TOF_STATE.set_base_rotating(True)

    def _end_base_motion(self) -> None:
        self._maneuvering = False
        self._tof_ignore_until = time.time() + self.tof_spin_settle_sec
        self._bearing_stable_ticks = 0
        self._last_base_busy = False
        if self._link is not None and self._link.connected:
            try:
                st = self._link.query_status()
                if st is not None:
                    self._last_base_busy = bool(st.busy)
            except Exception:
                pass
        self._reset_tof_after_motion()
        if TOF_STATE is not None:
            TOF_STATE.set_base_rotating(False)

    def handle_prox_line(self, line: str) -> None:
        from hardware.arduino_servo import (
            _PROX_CLEAR_RE,
            _PROX_DEPART_RE,
            _PROX_EVENT_RE,
            _ZONE_RE,
        )

        if self._maneuvering:
            return

        m = _PROX_EVENT_RE.match(line)
        if m:
            zone = m.group(1)
            if self._prox_swap_lr and zone in ("L", "R"):
                zone = "R" if zone == "L" else "L"
            self._prox_zone = zone
            self._prox_confidence = int(m.group(4))
            self._prox_approach_ts = time.time()
            self._prox_active = True
            return

        if _PROX_DEPART_RE.match(line):
            return

        if _PROX_CLEAR_RE.match(line):
            self._prox_active = False
            self._prox_zone = ""
            self._prox_confidence = 0
            return

        m = _ZONE_RE.match(line)
        if m:
            zl = m.group(1) == "1"
            zc = m.group(2) == "1"
            zr = m.group(3) == "1"
            if self._prox_swap_lr:
                zl, zr = zr, zl
            if zl or zc or zr:
                self._prox_active = True

    def _hold_head_home(self) -> None:
        if self._link is not None and self._link.connected:
            self._link.write_angles(self.pan_center, self.tilt_center)

    def _query_imu_home(self) -> tuple[float, float, bool, float]:
        enc, count, busy, _cpd = self._fetch_enc()
        imu_yaw, gyro, imu_ok = read_imu(self._reader, self._yaw_sign)
        pan_mech = signed_pan_mech_deg(self.pan_center, self._servo_cfg)
        with self._tracker_lock:
            self._tracker.counts_per_degree = self._cached_cpd
            sample = self._tracker.update(
                encoder_deg=enc,
                encoder_count=count,
                imu_yaw_deg=imu_yaw,
                pan_mech_deg=pan_mech,
                gyro_dps=gyro,
                base_busy=True,
            )
        imu_home = sample.from_home_imu_deg if sample is not None else 0.0
        if sample is not None:
            self._publish_pose(sample, imu_online=imu_ok and self._reader is not None)
        return imu_home, enc, busy, gyro

    def _refresh_tracker(self) -> tuple[float, float]:
        enc, count, busy, cpd = self._fetch_enc()
        imu_yaw, gyro, imu_ok = read_imu(self._reader, self._yaw_sign)
        pan_mech = signed_pan_mech_deg(self.pan_center, self._servo_cfg)
        with self._tracker_lock:
            self._tracker.counts_per_degree = cpd
            sample = self._tracker.update(
                encoder_deg=enc,
                encoder_count=count,
                imu_yaw_deg=imu_yaw,
                pan_mech_deg=pan_mech,
                gyro_dps=gyro,
                base_busy=self._maneuvering or busy,
            )
            if (
                sample is not None
                and sample.stationary
                and not self._maneuvering
                and abs(sample.disagreement_deg) > 0.5
            ):
                self._tracker.force_snap_imu_to_encoder(
                    encoder_deg=enc,
                    imu_yaw_deg=imu_yaw,
                    pan_mech_deg=pan_mech,
                )
                sample = self._tracker.update(
                    encoder_deg=enc,
                    encoder_count=count,
                    imu_yaw_deg=imu_yaw,
                    pan_mech_deg=pan_mech,
                    gyro_dps=gyro,
                    base_busy=False,
                )
        self._last_base_busy = bool(busy)
        if sample is None:
            return 0.0, 0.0
        self._publish_pose(sample, imu_online=imu_ok and self._reader is not None)
        return sample.from_home_imu_deg, sample.from_home_enc_deg

    def _resync_after_maneuver(self):
        enc, count, busy, cpd = self._fetch_enc()
        imu_yaw, gyro, imu_ok = read_imu(self._reader, self._yaw_sign)
        pan_mech = signed_pan_mech_deg(self.pan_center, self._servo_cfg)
        with self._tracker_lock:
            self._tracker.counts_per_degree = cpd
            self._tracker.force_snap_imu_to_encoder(
                encoder_deg=enc,
                imu_yaw_deg=imu_yaw,
                pan_mech_deg=pan_mech,
            )
            sample = self._tracker.update(
                encoder_deg=enc,
                encoder_count=count,
                imu_yaw_deg=imu_yaw,
                pan_mech_deg=pan_mech,
                gyro_dps=gyro,
                base_busy=busy,
            )
        if sample is not None:
            self._publish_pose(sample, imu_online=imu_ok and self._reader is not None)
        return sample

    def _publish_pose(self, sample, *, imu_online: bool) -> None:
        if TOF_STATE is None:
            return
        TOF_STATE.update_pose(
            base_yaw_sign=self._base_yaw_sign,
            body_yaw_deg=sample.from_home_imu_deg,
            head_yaw_on_body_deg=sample.pan_mech_deg,
            encoder_yaw_deg=sample.from_home_enc_deg,
            front_offset_deg=sample.from_home_enc_deg,
            from_home_enc_deg=sample.from_home_enc_deg,
            from_home_imu_deg=sample.from_home_imu_deg,
            map_yaw_deg=sample.from_home_imu_deg,
            disagreement_deg=sample.disagreement_deg,
            encoder_count_delta=sample.encoder_count_delta,
            imu_online=imu_online,
            max_yaw_deg=self.max_yaw_deg,
            imu_drift_correction_deg=sample.imu_correction_deg,
            imu_yaw_rel_deg=sample.from_home_imu_deg,
            fusion_stationary=sample.stationary,
        )

    def _drain_serial_events(self) -> None:
        if self._link is not None:
            self._link._poll_prox_lines()

    def _poll_during_spin(self) -> None:
        """Drain serial + push live IMU pose to viz while base rotates."""
        self._drain_serial_events()
        self._fetch_enc()
        self._publish_viz_from_cache()

    def _live_tof_snapshot(self) -> dict:
        if TOF_STATE is None or not self.accept_tof_samples():
            return {}
        snap = TOF_STATE.snapshot()
        age = time.time() - float(snap.get("last_ts", 0.0) or 0.0)
        if age > self.tof_stale_sec:
            return {}
        return snap

    def _pick_target_track(
        self, snap: dict, now: float, *, for_aim: bool = False
    ) -> dict | None:
        tracks = snap.get("tracks") or []
        if for_aim:
            tracks = [
                t
                for t in tracks
                if self._aimable_kind(
                    t.get("kind", ""), dist_mm=int(t.get("dist_mm", 0))
                )
            ]
        if not tracks:
            primary = snap.get("primary_target")
            if for_aim and isinstance(primary, dict):
                dist = int(primary.get("dist_mm", 0))
                if not self._aimable_kind(primary.get("kind", ""), dist_mm=dist):
                    return None
            return primary if isinstance(primary, dict) else None

        if self._locked_track_id is not None and now < self._lock_track_until:
            for track in tracks:
                if track.get("id") == self._locked_track_id:
                    return track
            return min(
                tracks,
                key=lambda t: math.hypot(
                    float(t["x_mm"]) - self._locked_x_mm,
                    float(t["z_mm"]) - self._locked_z_mm,
                ),
            )

        primary = snap.get("primary_target")
        chosen: dict | None = None
        if isinstance(primary, dict):
            dist = int(primary.get("dist_mm", 0))
            if not for_aim or self._aimable_kind(primary.get("kind", ""), dist_mm=dist):
                chosen = primary
        if chosen is None and tracks:
            chosen = max(
                tracks,
                key=lambda t: (
                    {"human": 3, "uncertain": 2, "obstacle": 1}.get(t.get("kind", ""), 0),
                    float(t.get("confidence", 0)),
                ),
            )
        if chosen is None:
            return None

        tid = chosen.get("id")
        if tid is not None:
            new_id = int(tid)
            if self._locked_track_id != new_id:
                self._last_committed_bearing = None
            self._locked_track_id = new_id
            self._locked_x_mm = float(chosen.get("x_mm", 0.0))
            self._locked_z_mm = float(chosen.get("z_mm", 0.0))
            self._lock_track_until = now + self.tof_target_lock_sec
        return chosen

    def _target_bearing(self, target: dict) -> float:
        if "bearing_deg" in target:
            return float(target["bearing_deg"])
        return math.degrees(
            math.atan2(float(target["x_mm"]), float(target["z_mm"]))
        )

    def _undershoot_scale(self, dist_mm: int) -> float:
        """0 at close range (spot-on aim), 1 at far range (full undershoot)."""
        close = self.tof_undershoot_close_mm
        far = max(self.tof_undershoot_far_mm, close + 1)
        if dist_mm <= close:
            return 0.0
        if dist_mm >= far:
            return 1.0
        return (dist_mm - close) / (far - close)

    def _undershoot_bearing(self, bearing_deg: float, dist_mm: int) -> tuple[float, float]:
        """Rotate toward bearing; less undershoot when person is close. Returns (spin, undershoot°)."""
        if abs(bearing_deg) < self.tof_aim_tolerance_deg:
            return 0.0, 0.0
        mag = abs(bearing_deg)
        undershoot = self.tof_bearing_undershoot_deg * self._undershoot_scale(dist_mm)
        reduced = mag - undershoot
        if reduced < max(1.5, self.tof_aim_tolerance_deg * 0.35):
            return 0.0, undershoot
        return math.copysign(reduced, bearing_deg), undershoot

    def _track_in_range(self, track: dict) -> bool:
        dist = int(track.get("dist_mm", 0))
        return self.tof_min_dist_mm <= dist <= self.tof_max_dist_mm

    def _aimable_kind(self, kind: str, *, dist_mm: int = 9999) -> bool:
        """human/uncertain always; close 'obstacle' is usually a still person."""
        if kind in ("human", "uncertain"):
            return True
        if kind == "obstacle" and dist_mm <= self.tof_aim_obstacle_close_mm:
            return True
        return False

    def _person_in_scene(self, snap: dict) -> bool:
        if not snap:
            return False
        for track in snap.get("tracks") or []:
            dist = int(track.get("dist_mm", 0))
            if not self._track_in_range(track):
                continue
            if self._aimable_kind(track.get("kind", ""), dist_mm=dist):
                return True
        primary = snap.get("primary_target")
        if isinstance(primary, dict):
            dist = int(primary.get("dist_mm", 0))
            if self._track_in_range(primary) and self._aimable_kind(
                primary.get("kind", ""), dist_mm=dist
            ):
                return True
        return False

    def _clip_bearing_step(self, bearing_deg: float, enc_from_home: float) -> float:
        """Body-frame bearing → plate command (honors base.sign like BaseController)."""
        plate_deg = bearing_deg * self.base_sign
        if abs(plate_deg) < 0.05:
            return 0.0
        enc_delta = expected_encoder_delta(plate_deg, self.encoder_sign)
        projected = enc_from_home + enc_delta
        if abs(projected) <= self.max_yaw_deg:
            return plate_deg
        clamped = max(-self.max_yaw_deg, min(self.max_yaw_deg, projected))
        allowed_delta = clamped - enc_from_home
        sign = 1.0 if self.encoder_sign >= 0.0 else -1.0
        clipped = allowed_delta / sign
        if abs(clipped) < 0.5:
            return 0.0
        return clipped

    def _gates_ok(self, now: float, *, aligning: bool = False) -> bool:
        if now < self._post_turn_lockout_until:
            return False
        blank = self.tof_align_blanking_sec if aligning else self.prox_post_motion_blanking_sec
        if (now - self._last_base_motion_done_ts) < blank:
            return False
        cooldown = self.tof_align_cooldown_sec if aligning else self.prox_cooldown_sec
        if (now - self._last_prox_ts) < cooldown:
            return False
        recent = [t for t in self._prox_turn_timestamps if now - t < self.prox_window_sec]
        return len(recent) < self.prox_max_turns

    def _record_turn(self, now: float, *, aligning: bool = False) -> None:
        self._last_prox_ts = now
        recent = [t for t in self._prox_turn_timestamps if now - t < self.prox_window_sec]
        self._prox_turn_timestamps = recent + [now]
        lock = self.prox_lockout_sec
        if aligning:
            lock = min(lock, self.tof_align_cooldown_sec)
        self._post_turn_lockout_until = now + lock

    def _plan_tof_bearing(self, now: float, enc_from_home: float) -> float | None:
        if not self.tof_turn_enabled:
            return None
        snap = self._live_tof_snapshot()
        if snap and self._person_in_scene(snap):
            self._clear_since = None
        if not snap:
            self._bearing_stable_ticks = 0
            return None

        target = self._pick_target_track(snap, now, for_aim=True)
        if not target:
            self._bearing_stable_ticks = 0
            return None

        dist = int(target.get("dist_mm", 0))
        if dist < self.tof_min_dist_mm or dist > self.tof_max_dist_mm:
            return None

        bearing_deg = self._target_bearing(target)
        if (
            self._last_committed_bearing is not None
            and bearing_deg * self._last_committed_bearing < 0
            and min(abs(bearing_deg), abs(self._last_committed_bearing))
            >= self.tof_bearing_flip_reject_deg
        ):
            self._bearing_stable_ticks = 0
            return None

        if abs(bearing_deg - self._last_target_bearing) < 10.0:
            if self._bearing_stable_ticks > 0 and bearing_deg * self._last_target_bearing < 0:
                self._bearing_stable_ticks = 0
            else:
                self._bearing_stable_ticks += 1
        else:
            self._bearing_stable_ticks = 1
        self._last_target_bearing = bearing_deg

        if self._bearing_stable_ticks < self.tof_confirm_ticks:
            return None

        spin_bearing, undershoot_deg = self._undershoot_bearing(bearing_deg, dist)
        if abs(spin_bearing) < 0.5:
            return None
        if abs(bearing_deg) < self.tof_min_bearing_deg:
            return None

        aligning = True
        if not self._gates_ok(now, aligning=aligning):
            return None

        step = self._clip_bearing_step(spin_bearing, enc_from_home)
        if step is None or abs(step) < 0.5:
            return None

        self._pending_log = (
            "TOF",
            target.get("id", "?"),
            len(snap.get("tracks") or []),
            bearing_deg,
            step,
            dist,
            undershoot_deg,
        )
        self._bearing_stable_ticks = 0
        return step

    def _plan_home_return(self, now: float, enc_from_home: float) -> bool:
        if not self.clear_return_enabled:
            return False
        snap = self._live_tof_snapshot()
        if self._person_in_scene(snap):
            self._clear_since = None
            return False

        if abs(enc_from_home) < self.clear_return_tolerance_deg:
            self._clear_since = None
            self._locked_track_id = None
            self._last_committed_bearing = None
            return False

        if self._clear_since is None:
            self._clear_since = now
            return False
        if (now - self._clear_since) < self.clear_return_sec:
            return False
        if not self._gates_ok(now, aligning=True):
            return False

        self._pending_log = ("HOME", enc_from_home)
        self._locked_track_id = None
        self._last_committed_bearing = None
        return True

    def _plan_prox_bearing(self, now: float, enc_from_home: float) -> float | None:
        if not self.prox_enabled or not self._prox_active:
            return None
        if self._prox_approach_ts > 0.0 and self._prox_approach_ts == self._last_prox_reaction_approach_ts:
            return None
        if self._prox_confidence < self.prox_min_confidence:
            return None
        if not self._gates_ok(now):
            return None

        snap = self._live_tof_snapshot()
        target = self._pick_target_track(snap, now, for_aim=True) if snap else None
        if target:
            bearing_deg = self._target_bearing(target)
            dist = int(target.get("dist_mm", 0))
            spin_bearing, _ = self._undershoot_bearing(bearing_deg, dist)
            if abs(spin_bearing) < 0.5:
                return None
            step = self._clip_bearing_step(spin_bearing, enc_from_home)
            self._pending_log = ("PROX+TOF", self._prox_zone, bearing_deg, step)
        elif not self.tof_turn_enabled and self._prox_zone == "L":
            step = self._clip_bearing_step(-self.prox_turn_step, enc_from_home)
            self._pending_log = ("PROX", "L", step)
        elif not self.tof_turn_enabled and self._prox_zone == "R":
            step = self._clip_bearing_step(self.prox_turn_step, enc_from_home)
            self._pending_log = ("PROX", "R", step)
        else:
            return None

        if step is None or abs(step) < 0.5:
            return None

        self._last_prox_reaction_approach_ts = self._prox_approach_ts
        return step

    def _execute_bearing_spin(self, bearing_deg: float, now: float) -> None:
        if self._link is None or not self._link.connected:
            return
        self._last_committed_bearing = self._last_target_bearing
        self._begin_base_motion()
        try:
            self._hold_head_home()
            ok, moved, reason = write_base_step_spin(
                self._link,
                bearing_deg,
                tolerance_deg=self.spin_tolerance_deg,
                timeout_sec=self.spin_timeout_sec,
                poll_hz=50.0,
                positive_uses_left=self.spin_positive_uses_left,
                encoder_sign=self.encoder_sign,
                stall_sec=self.spin_stall_sec,
                on_poll=self._poll_during_spin,
            )
            time.sleep(0.05)
            self._hold_head_home()
            sample = self._resync_after_maneuver()
            enc_home = sample.from_home_enc_deg if sample else 0.0
            tag = "OK" if ok else "FAIL"
            print(
                f"[Approach] SPIN {bearing_deg:+.1f}° {tag} "
                f"moved={moved:+.1f}° enc={enc_home:+.1f}° ({reason})"
            )
            self._record_turn(now, aligning=True)
        except Exception as exc:
            print(f"[Approach] bearing spin failed: {exc}")
            self._resync_after_maneuver()
        finally:
            self._end_base_motion()
            self._last_base_motion_done_ts = time.time()

    def _execute_imu_home(self, now: float) -> None:
        if self._link is None or not self._link.connected:
            return
        self._begin_base_motion()
        try:
            self._hold_head_home()
            ok, final_imu = drive_base_to_imu_zero(
                self._link,
                self._base_cfg,
                query_imu_home=self._query_imu_home,
                log=print,
            )
            time.sleep(0.12)
            self._hold_head_home()
            sample = self._resync_after_maneuver()
            enc_home = sample.from_home_enc_deg if sample else 0.0
            tag = "OK" if ok else "FAIL"
            print(
                f"[Approach] HOME return {tag} "
                f"enc={enc_home:+.1f}° imu={final_imu:+.1f}° from HOME"
            )
            self._record_turn(now, aligning=True)
            self._clear_since = None
        except Exception as exc:
            print(f"[Approach] HOME return failed: {exc}")
            self._resync_after_maneuver()
        finally:
            self._end_base_motion()
            self._last_base_motion_done_ts = time.time()

    def tick(self, now: float | None = None) -> None:
        if now is None:
            now = time.time()
        if not self._running() or self._maneuvering:
            return

        _imu_from_home, enc_from_home = self._refresh_tracker()

        bearing = self._plan_tof_bearing(now, enc_from_home)
        if bearing is not None:
            log = getattr(self, "_pending_log", None)
            if log and log[0] == "TOF":
                _, tid, ntracks, bearing_deg, step, dist_mm, undershoot_deg = log
                us_label = (
                    "spot-on"
                    if undershoot_deg < 0.05
                    else f"−{undershoot_deg:.1f}° undershoot"
                )
                print(
                    f"[Approach] TOF track {tid}/{ntracks} "
                    f"bearing {bearing_deg:+.0f}° dist {dist_mm}mm enc {enc_from_home:+.0f}° "
                    f"→ spin {step:+.1f}° ({us_label})"
                )
            self._execute_bearing_spin(bearing, now)
            return

        if self._plan_home_return(now, enc_from_home):
            log = getattr(self, "_pending_log", None)
            if log and log[0] == "HOME":
                print(f"[Approach] Scene clear — return HOME from enc {log[1]:+.0f}°")
            self._execute_imu_home(now)
            return

        bearing = self._plan_prox_bearing(now, enc_from_home)
        if bearing is not None:
            log = getattr(self, "_pending_log", None)
            if log:
                print(f"[Approach] {log[0]} → spin {bearing:+.1f}° ({log})")
            self._execute_bearing_spin(bearing, now)

    def run(self) -> None:
        print("[Approach] Controller running (bearing spin + HOME return).")
        delay = 1.0 / max(1.0, self.loop_hz)
        while self._running():
            self.tick()
            time.sleep(delay)
        print("[Approach] Controller stopped.")
