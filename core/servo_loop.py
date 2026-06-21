"""ServoLoop: head PAN/TILT PID + wander + target smoothing.

All servo smoothing constants live exclusively in this file.
Changing SERVO_FACE_ALPHA_X or PAN_PID_KP here cannot affect
base rotation, IMU, or eye rendering.

Reads from BB:
    face_detected, face_norm_x, face_norm_y, face_roll_deg,
    face_area_ratio, face_count, track_kind, face_candidates,
    body_detected,
    imu_pitch_deg, imu_horizon_ok, imu_available,
    person_snapshots, last_seen_world_yaw,
    base_encoder_deg, base_world_yaw_deg,
    running

Writes to BB:
    servo_pan, servo_tilt, servo_mode
"""

from __future__ import annotations

import math
import random
import time
from pathlib import Path
from typing import Optional

try:
    import yaml
except ImportError:
    yaml = None

from core.blackboard import Blackboard
from lib.elastic_head_motion import (
    OrganicWanderSearch,
    clamp,
    smooth_toward,
)
from lib.person_memory import angular_error_deg, wrap_degrees

APP_DIR = Path(__file__).resolve().parent.parent
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


# ── PID Controller ─────────────────────────────────────────────────────────────

class _PidAxis:
    def __init__(self, kp: float, ki: float, kd: float, integral_limit: float):
        self.kp = kp
        self.ki = ki
        self.kd = kd
        self.integral_limit = integral_limit
        self._integral = 0.0
        self._prev_error = 0.0
        self._initialized = False

    def reset(self):
        self._integral = 0.0
        self._prev_error = 0.0
        self._initialized = False

    def soften(self, keep: float = 0.35):
        self._integral *= keep
        self._prev_error *= keep

    def tick(self, error: float, dt: float) -> float:
        dt = max(0.001, min(0.5, dt))
        self._integral = clamp(self._integral + error * dt, -self.integral_limit, self.integral_limit)
        deriv = 0.0 if not self._initialized else (error - self._prev_error) / dt
        deriv = clamp(deriv, -6.0, 6.0)
        self._prev_error = error
        self._initialized = True
        return self.kp * error + self.ki * self._integral + self.kd * deriv


# ── Target glide (cross-axis smoothing) ───────────────────────────────────────

class _TargetGlide:
    def __init__(self, smooth_hz: float = 4.5):
        self.smooth_hz = smooth_hz
        self.target_x = 0.0
        self.target_y = 0.0

    def soften(self):
        self.target_x *= 0.5
        self.target_y *= 0.5

    def tick(self, target_x: float, target_y: float, dt: float, alpha_scale: float = 1.0) -> tuple[float, float]:
        dt = max(0.001, dt)
        alpha = (1.0 - math.exp(-self.smooth_hz * dt)) * alpha_scale
        self.target_x += (target_x - self.target_x) * alpha
        self.target_y += (target_y - self.target_y) * alpha
        return self.target_x, self.target_y


# ── Helpers ────────────────────────────────────────────────────────────────────

def _apply_deadzone(value: float, deadzone: float) -> float:
    if abs(value) <= deadzone:
        return 0.0
    sign = 1.0 if value > 0 else -1.0
    return sign * (abs(value) - deadzone) / (1.0 - deadzone)


def _looking_emotion_for_pan_goal(pan_goal: float, pan_center: float) -> str:
    offset = pan_goal - pan_center
    if offset < -6.0:
        return "looking_left_natural"
    if offset > 6.0:
        return "looking_right_natural"
    return "attentive"


def _motion_emotion_from_hint(hint: str) -> str:
    mapping = {
        "thinking": "thinking",
        "concentrating": "concentrating",
        "uncertain": "uncertain",
        "remembering": "remembering",
        "long_stare": "attentive",
    }
    return mapping.get(hint, "attentive")


# ── ServoLoop ─────────────────────────────────────────────────────────────────

class ServoLoop:
    """Head PAN/TILT control loop — the only place servo smoothing constants live."""

    def __init__(self, bb: Blackboard, config_path: Path = DEFAULT_CONFIG_PATH) -> None:
        self.bb = bb
        cfg = _load_yaml(config_path)
        s = _cfg(cfg, "servo", default={}) or {}
        imu = _cfg(cfg, "imu", default={}) or {}
        pm = _cfg(cfg, "person_memory", default={}) or {}
        lss = _cfg(cfg, "last_seen_search", default={}) or {}
        b = _cfg(cfg, "base", default={}) or {}

        # ── Servo limits ──────────────────────────────────────────────────────
        self.pan_min = float(s.get("pan_min", 40.0))
        self.pan_max = float(s.get("pan_max", 120.0))
        self.tilt_min = float(s.get("tilt_min", 100.0))
        self.tilt_max = float(s.get("tilt_max", 120.0))
        self.mech_left = float(s.get("pan_mech_left_deg", -40.0))
        self.mech_right = float(s.get("pan_mech_right_deg", 40.0))
        self.pan_center = float(s.get("pan_center", (self.pan_min + self.pan_max) * 0.5))
        self.tilt_center = float(s.get("tilt_center", (self.tilt_min + self.tilt_max) * 0.5))
        self.pan_sign = float(s.get("pan_sign", 1.0))
        self.tilt_sign = float(s.get("tilt_sign", -1.0))
        self.pan_track_range = float(s.get("pan_track_range", 26.0))
        self.tilt_track_range = float(s.get("tilt_track_range", 12.0))

        # ── Servo smoothing (only place these constants exist) ────────────────
        self.deadzone_x = float(s.get("deadzone_x", 0.04))
        self.deadzone_y = float(s.get("deadzone_y", 0.05))
        self.pan_center_norm_x = float(s.get("pan_center_norm_x", 0.08))
        self.tilt_center_norm_y = float(s.get("tilt_center_norm_y", 0.12))
        self.face_alpha_x = float(s.get("face_alpha_x", 0.22))
        self.face_alpha_y = float(s.get("face_alpha_y", 0.06))
        self.pan_pid_kp = float(s.get("pan_pid_kp", 0.95))
        self.pan_pid_ki = float(s.get("pan_pid_ki", 0.02))
        self.pan_pid_kd = float(s.get("pan_pid_kd", 0.14))
        self.tilt_pid_kp = float(s.get("tilt_pid_kp", 0.38))
        self.tilt_pid_ki = float(s.get("tilt_pid_ki", 0.0))
        self.tilt_pid_kd = float(s.get("tilt_pid_kd", 0.08))
        self.pid_integral_limit = float(s.get("pid_integral_limit", 0.6))
        self.target_smooth_hz = float(s.get("target_smooth_hz", 4.5))
        self.pan_smooth_hz = float(s.get("pan_smooth_hz", 7.0))
        self.tilt_smooth_hz = float(s.get("tilt_smooth_hz", 5.5))
        
        # ── Base compensation ─────────────────────────────────────────────────
        self.base_sign = float(b.get("sign", 1.0))
        self.base_comp_gain = float(b.get("track_compensation_gain", 0.95))

        # ── Loop timing ───────────────────────────────────────────────────────
        self.loop_hz = float(s.get("loop_hz", 100.0))
        self.no_face_home_sec = float(s.get("no_face_home_sec", 0.8))
        self.debug_hz = float(s.get("debug_hz", 2.0))

        # ── Predict / lost search ─────────────────────────────────────────────
        self.predict_hold_sec = float(s.get("predict_hold_sec", 1.2))
        self.predict_edge_norm = float(s.get("predict_edge_norm", 0.55))
        self.predict_gain = float(s.get("predict_gain", 0.72))
        self.lost_search_hold_sec = float(s.get("lost_search_hold_sec", 3.2))

        # ── Wander ───────────────────────────────────────────────────────────
        self.wander_pan_amp = float(s.get("wander_pan_amp_deg", 26.0))
        self.wander_step_min = float(s.get("wander_step_min_deg", 6.0))
        self.wander_step_max = float(s.get("wander_step_max_deg", 28.0))
        self.wander_hold_min = float(s.get("wander_hold_min_sec", 1.3))
        self.wander_hold_max = float(s.get("wander_hold_max_sec", 5.8))
        self.wander_jump_chance = float(s.get("wander_jump_chance", 0.34))
        self.wander_arrival = float(s.get("wander_arrival_deg", 2.0))
        self.wander_tilt_step_max = float(s.get("wander_tilt_step_max_deg", 1.8))
        self.wander_tilt_max_up = float(s.get("wander_tilt_max_up_deg", 1.1))
        self.wander_tilt_max_down = float(s.get("wander_tilt_max_down_deg", 1.6))
        self.wander_thinking_chance = float(s.get("wander_thinking_hold_chance", 0.34))
        self.wander_thinking_min = float(s.get("wander_thinking_hold_min_sec", 3.0))
        self.wander_thinking_max = float(s.get("wander_thinking_hold_max_sec", 7.5))
        self.wander_long_stare = float(s.get("wander_long_stare_chance", 0.12))
        self.wander_pan_smooth = float(s.get("wander_pan_smooth_hz", 4.8))
        self.wander_tilt_smooth = float(s.get("wander_tilt_smooth_hz", 4.2))

        # ── IMU horizon compensation ──────────────────────────────────────────
        self.imu_horizon_gain = float(imu.get("horizon_tilt_gain", 1.0))
        self.imu_horizon_sign = float(imu.get("horizon_tilt_sign", 1.0))
        self.imu_horizon_smooth = float(imu.get("horizon_pitch_smooth_hz", 4.0))
        self.imu_max_bias = float(imu.get("horizon_max_bias_deg", 4.0))
        self.imu_max_up = float(imu.get("horizon_max_up_from_center_deg", 2.0))
        self.imu_max_down = float(imu.get("horizon_max_down_from_center_deg", 4.0))
        self._imu_pitch_smooth = 0.0
        self._effective_tilt_center_smooth = self.tilt_center

        # ── Person memory / last-seen ─────────────────────────────────────────
        self.pm_reacquire_deg = float(pm.get("reacquire_angle_deg", 28.0))
        self.pm_reacquire_after = float(pm.get("reacquire_after_sec", 5.0))
        self.pm_track_gain = float(pm.get("track_gain", 0.75))
        self.pm_max_step = float(pm.get("max_pan_step_deg", 10.0))
        self.pm_hfov = float(pm.get("camera_hfov_deg", 62.0))
        self.lss_track_gain = float(lss.get("track_gain", 0.65))
        self.lss_max_step = float(lss.get("max_pan_step_deg", 8.0))
        self.lss_timeout = float(lss.get("timeout_sec", 5.0))
        self.lss_edge_norm = float(lss.get("edge_norm", 0.40))

        # ── Multi-face ────────────────────────────────────────────────────────
        self.multi_face_alpha = float(s.get("multi_face_track_servo_alpha", 0.34))
        self.multi_face_gain = float(s.get("multi_face_track_gain", 0.62))

        # ── Runtime state ─────────────────────────────────────────────────────
        self._pan = self.pan_center
        self._tilt = self.tilt_center
        self._servo_pan = self.pan_center
        self._servo_tilt = self.tilt_center
        self._pan_pid = _PidAxis(self.pan_pid_kp, self.pan_pid_ki, self.pan_pid_kd, self.pid_integral_limit)
        self._tilt_pid = _PidAxis(self.tilt_pid_kp, self.tilt_pid_ki, self.tilt_pid_kd, self.pid_integral_limit)
        self._target_glide = _TargetGlide(self.target_smooth_hz)
        self._wander = OrganicWanderSearch()
        self._mode = "wander"
        self._last_face_ts = 0.0
        self._last_body_ts = 0.0
        self._lost_search_since = 0.0
        self._face_vel_x = 0.0
        self._face_vel_y = 0.0
        self._prev_face_x = 0.0
        self._prev_face_y = 0.0
        self._lss_active = False
        self._lss_start_ts = 0.0
        self._memory_reacquire_ts = 0.0
        self._last_base_enc = None
        self._proactive_comp_applied = False

    # ── Base Compensation ──────────────────────────────────────────────────────

    def _apply_proactive_base_comp(self) -> None:
        """Apply planned neck counter-rotation when BaseController flags a base step."""
        state = self.bb.read("base_step_ready", "base_comp_pan_deg", "base_motion_busy")
        if not state["base_step_ready"] or state["base_motion_busy"]:
            self._proactive_comp_applied = False
            return
        comp_pan = state["base_comp_pan_deg"]
        if comp_pan == 0.0 or self._proactive_comp_applied:
            return
        self._pan = clamp(comp_pan, self.pan_min, self.pan_max)
        self._wander.pan_goal = clamp(comp_pan, self.pan_min, self.pan_max)
        self._pan_pid.soften(0.45)
        self._target_glide.soften()
        self._proactive_comp_applied = True
    
    def _apply_base_compensation(self) -> None:
        """Feed-forward compensation: counter-rotate head when base moves."""
        state = self.bb.read("base_encoder_deg")
        enc = state.get("base_encoder_deg", 0.0)
        
        if self._last_base_enc is None or abs(enc - self._last_base_enc) > 40.0:
            self._last_base_enc = enc
            return
            
        delta = enc - self._last_base_enc
        self._last_base_enc = enc
        
        if abs(delta) < 0.05:
            return
            
        # Counter-rotate by subtracting the base delta from the pan
        pan_shift = -delta * self.base_sign * self.base_comp_gain
        
        self._pan = clamp(self._pan + pan_shift, self.pan_min, self.pan_max)
        self._wander.pan_goal = clamp(self._wander.pan_goal + pan_shift, self.pan_min, self.pan_max)

    # ── IMU tilt compensation ──────────────────────────────────────────────────

    def _effective_tilt_center(self, dt: float) -> float:
        """IMU-leveled tilt center from ImuService (held when head is moving fast)."""
        state = self.bb.read(
            "imu_available", "imu_horizon_ok", "imu_effective_tilt_center",
        )
        if not state["imu_available"]:
            return self.tilt_center

        if state["imu_horizon_ok"]:
            target = state["imu_effective_tilt_center"]
        else:
            target = self._effective_tilt_center_smooth

        center_alpha = 1.0 - math.exp(-3.0 * max(0.001, dt))
        self._effective_tilt_center_smooth += (target - self._effective_tilt_center_smooth) * center_alpha
        return self._effective_tilt_center_smooth

    def _on_mode_change(self, old_mode: str, new_mode: str) -> None:
        """Avoid PID derivative spikes when switching track ↔ wander."""
        if old_mode == new_mode:
            return
        self._pan_pid.reset()
        self._tilt_pid.reset()
        self._target_glide.target_x = 0.0
        self._target_glide.target_y = 0.0
        self._prev_face_x = 0.0
        self._prev_face_y = 0.0
        if new_mode == "wander":
            self._wander.tilt_goal = self._tilt
            self._wander.pan_goal = self._pan

    # ── Track mode ─────────────────────────────────────────────────────────────

    def _tick_track(self, now: float, dt: float, effective_tilt_center: float) -> str:
        state = self.bb.read(
            "face_detected", "face_norm_x", "face_norm_y",
            "face_count", "track_kind", "face_candidates",
            "body_detected",
        )
        face_detected = state["face_detected"]
        norm_x = state["face_norm_x"]
        norm_y = state["face_norm_y"]
        track_kind = state["track_kind"]
        body_detected = state["body_detected"]

        if not face_detected and not body_detected:
            return "wander"

        if face_detected:
            self._last_face_ts = now
        if body_detected:
            self._last_body_ts = now

        # Deadzone + centre offset
        err_x = _apply_deadzone(norm_x - self.pan_center_norm_x, self.deadzone_x)
        err_y = _apply_deadzone(norm_y - self.tilt_center_norm_y, self.deadzone_y)

        # Smooth the input target
        alpha_scale = self.multi_face_alpha if track_kind in ("multi", "center") else 1.0
        smooth_x, smooth_y = self._target_glide.tick(err_x, err_y, dt, alpha_scale)

        # Face velocity (for prediction)
        self._face_vel_x = (smooth_x - self._prev_face_x) / max(dt, 0.001)
        self._face_vel_y = (smooth_y - self._prev_face_y) / max(dt, 0.001)
        self._prev_face_x = smooth_x
        self._prev_face_y = smooth_y

        # PID → servo correction
        pan_corr = self._pan_pid.tick(smooth_x, dt)
        tilt_corr = self._tilt_pid.tick(smooth_y, dt)

        pan_target = clamp(
            self._pan + self.pan_sign * pan_corr * self.pan_track_range,
            self.pan_min, self.pan_max,
        )
        tilt_target = clamp(
            effective_tilt_center + self.tilt_sign * tilt_corr * self.tilt_track_range,
            self.tilt_min, self.tilt_max,
        )

        self._pan = smooth_toward(self._pan, pan_target, dt, smooth_hz=self.pan_smooth_hz, lo=self.pan_min, hi=self.pan_max)

        tilt_hz = self.tilt_smooth_hz * 0.28
        self._tilt = smooth_toward(self._tilt, tilt_target, dt, smooth_hz=tilt_hz, lo=self.tilt_min, hi=self.tilt_max)

        return "track"

    # ── Wander mode ────────────────────────────────────────────────────────────

    def _tick_wander(self, now: float, dt: float, effective_tilt_center: float) -> str:
        state = self.bb.read("face_detected", "body_detected", "last_seen_world_yaw")
        if state["face_detected"] or state["body_detected"]:
            self._pan_pid.soften()
            self._tilt_pid.soften()
            self._target_glide.soften()
            return "track"

        # Check if we should start last-seen search
        last_yaw = state["last_seen_world_yaw"]
        if last_yaw is not None and not self._lss_active:
            self._lss_active = True
            self._lss_start_ts = now
            return "last_seen"

        pan_goal, tilt_goal = self._wander.tick(
            now,
            pan_center=self.pan_center,
            tilt_center=effective_tilt_center,
            pan_current=self._pan,
            tilt_current=self._tilt,
            pan_min=self.pan_min,
            pan_max=self.pan_max,
            tilt_min=self.tilt_min,
            tilt_max=self.tilt_max,
            amp_deg=self.wander_pan_amp,
            step_min_deg=self.wander_step_min,
            step_max_deg=self.wander_step_max,
            hold_min_sec=self.wander_hold_min,
            hold_max_sec=self.wander_hold_max,
            jump_chance=self.wander_jump_chance,
            arrival_deg=self.wander_arrival,
            tilt_max_up_deg=self.wander_tilt_max_up,
            tilt_max_down_deg=self.wander_tilt_max_down,
            tilt_step_max_deg=self.wander_tilt_step_max,
            thinking_hold_chance=self.wander_thinking_chance,
            thinking_hold_min_sec=self.wander_thinking_min,
            thinking_hold_max_sec=self.wander_thinking_max,
            long_stare_chance=self.wander_long_stare,
        )
        self.bb.write(
            wander_moving=self._wander.moving,
            wander_last_step_deg=self._wander._last_step_deg,
        )

        self._pan = smooth_toward(self._pan, pan_goal, dt, smooth_hz=self.wander_pan_smooth, lo=self.pan_min, hi=self.pan_max)

        tilt_hz = self.wander_tilt_smooth * 0.28
        self._tilt = smooth_toward(self._tilt, tilt_goal, dt, smooth_hz=tilt_hz, lo=self.tilt_min, hi=self.tilt_max)
        return "wander"

    # ── Last-seen mode ─────────────────────────────────────────────────────────

    def _tick_last_seen(self, now: float, dt: float, effective_tilt_center: float) -> str:
        state = self.bb.read("face_detected", "body_detected", "last_seen_world_yaw", "base_world_yaw_deg")
        if state["face_detected"] or state["body_detected"]:
            self._lss_active = False
            self._pan_pid.soften()
            return "track"

        last_yaw = state["last_seen_world_yaw"]
        if last_yaw is None or (now - self._lss_start_ts) > self.lss_timeout:
            self._lss_active = False
            self._wander.reset(self.pan_center, effective_tilt_center, now)
            return "wander"

        # Aim pan toward last-seen world yaw
        world_yaw = state["base_world_yaw_deg"]
        yaw_err = angular_error_deg(last_yaw, world_yaw)
        pan_step = clamp(yaw_err * self.lss_track_gain, -self.lss_max_step, self.lss_max_step)
        pan_target = clamp(self._pan + pan_step, self.pan_min, self.pan_max)
        self._pan = smooth_toward(self._pan, pan_target, dt, smooth_hz=self.pan_smooth_hz, lo=self.pan_min, hi=self.pan_max)

        tilt_hz = self.tilt_smooth_hz * 0.28
        self._tilt = smooth_toward(self._tilt, effective_tilt_center, dt, smooth_hz=tilt_hz, lo=self.tilt_min, hi=self.tilt_max)
        return "last_seen"

    # ── Main loop ──────────────────────────────────────────────────────────────

    def run(self) -> None:
        loop_delay = 1.0 / max(1.0, self.loop_hz)
        self._wander.reset(self.pan_center, self.tilt_center, time.time())

        prev_ts = time.perf_counter()
        self.bb.write(servo_pan=self._pan, servo_tilt=self._tilt, servo_mode="wander")

        while self.bb.read("running")["running"]:
            now_pc = time.perf_counter()
            dt = max(0.001, min(0.5, now_pc - prev_ts))
            prev_ts = now_pc
            now = time.time()

            self._apply_proactive_base_comp()
            self._apply_base_compensation()
            effective_tilt_center = self._effective_tilt_center(dt)

            if self._mode == "track":
                next_mode = self._tick_track(now, dt, effective_tilt_center)
            elif self._mode == "last_seen":
                next_mode = self._tick_last_seen(now, dt, effective_tilt_center)
            else:
                next_mode = self._tick_wander(now, dt, effective_tilt_center)

            # Transition to wander after no face for no_face_home_sec
            if next_mode == "track":
                face_gone = (now - self._last_face_ts) > self.no_face_home_sec
                body_gone = (now - self._last_body_ts) > self.no_face_home_sec
                if face_gone and body_gone:
                    next_mode = "wander"
                    self._wander.reset(self.pan_center, effective_tilt_center, now)
                    self._wander.tilt_goal = self._tilt
                    self._wander.pan_goal = self._pan

            if next_mode != self._mode:
                self._on_mode_change(self._mode, next_mode)

            self._mode = next_mode

            # Publish
            publish = dict(
                servo_pan=self._pan,
                servo_tilt=self._tilt,
                servo_mode=self._mode,
            )
            if self._mode != "wander":
                publish["wander_moving"] = False
                publish["wander_last_step_deg"] = 0.0
            self.bb.write(**publish)

            time.sleep(loop_delay)

        print("[ServoLoop] Stopped.")
