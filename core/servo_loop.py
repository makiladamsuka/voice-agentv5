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
from lib.head_mech import signed_pan_mech_deg, signed_tilt_mech_deg
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
        self._deriv_filtered = 0.0
        self._initialized = False

    def reset(self):
        self._integral = 0.0
        self._prev_error = 0.0
        self._deriv_filtered = 0.0
        self._initialized = False

    def soften(self, keep: float = 0.35):
        self._integral *= keep
        self._deriv_filtered = 0.0
        self._prev_error *= keep

    def tick(self, error: float, dt: float) -> float:
        dt = max(0.001, min(0.5, dt))
        if abs(error) < 0.02:
            error = 0.0
        self._integral = clamp(self._integral + error * dt, -self.integral_limit, self.integral_limit)
        raw_deriv = 0.0 if not self._initialized else (error - self._prev_error) / dt
        self._deriv_filtered = self._deriv_filtered * 0.82 + raw_deriv * 0.18
        self._prev_error = error
        self._initialized = True
        return self.kp * error + self.ki * self._integral + self.kd * self._deriv_filtered


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


def _track_error_gain(norm: float, full_scale: float, min_gain: float = 0.35) -> float:
    """Scale track range by bearing error — softer near frame center reduces overshoot."""
    return clamp(abs(norm) / max(full_scale, 0.05), min_gain, 1.0)


def _smooth_toward_stepped(
    pos: float,
    target: float,
    dt: float,
    *,
    smooth_hz: float,
    lo: float,
    hi: float,
    max_step: float,
) -> float:
    """Exponential smooth with per-tick step cap — prevents pan overshoot on fast motion."""
    next_pos = smooth_toward(pos, target, dt, smooth_hz=smooth_hz, lo=lo, hi=hi)
    step = clamp(next_pos - pos, -max_step, max_step)
    return clamp(pos + step, lo, hi)


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
        dv = _cfg(cfg, "debug_viz", default={}) or {}

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
        self.pan_track_range = min(
            float(s.get("pan_track_range", 26.0)),
            max(self.pan_max - self.pan_min, 1.0) * 0.5,
        )
        self.pan_track_slew_dps = float(s.get("pan_track_slew_dps", 55.0))
        self.pan_recenter_hz = float(s.get("pan_recenter_hz", 1.5))
        self.pan_center_hysteresis = float(s.get("pan_center_hysteresis", 0.04))
        self.pan_max_step_deg = float(s.get("pan_max_step_deg", 1.2))
        self.pan_track_alpha = float(s.get("pan_track_alpha", 0.25))
        self.pan_track_sign = float(s.get("pan_track_sign", -1.0))
        self.tilt_track_range = min(
            float(s.get("tilt_track_range", 28.0)),
            max(self.tilt_max - self.tilt_min, 1.0) * 0.55,
        )
        self.pan_error_full_scale = float(s.get("pan_error_full_scale", 0.40))
        self.pan_track_min_gain = float(s.get("pan_track_min_gain", 0.35))

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
        self.pan_track_smooth_hz = float(s.get("pan_track_smooth_hz", 4.5))
        self.tilt_smooth_hz = float(s.get("tilt_smooth_hz", 5.5))
        
        # ── Base compensation ─────────────────────────────────────────────────
        self.base_sign = float(b.get("sign", 1.0))
        self.base_comp_gain = float(b.get("track_compensation_gain", 0.95))
        self.sustained_head_follow_enabled = bool(b.get("sustained_head_follow_enabled", True))

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
        self.wander_track_loss_hold_min = float(s.get("wander_track_loss_hold_min_sec", 1.2))
        self.wander_track_loss_hold_max = float(s.get("wander_track_loss_hold_max_sec", 2.8))
        self.wander_imu_tilt_blend = float(s.get("wander_imu_tilt_blend", 0.18))

        # ── IMU horizon compensation ──────────────────────────────────────────
        self.imu_horizon_gain = float(imu.get("horizon_tilt_gain", 1.0))
        self.imu_horizon_sign = float(imu.get("horizon_tilt_sign", 1.0))
        self.imu_horizon_smooth = float(imu.get("horizon_pitch_smooth_hz", 4.0))
        self.imu_max_bias = float(imu.get("horizon_max_bias_deg", 4.0))
        self.imu_max_up = float(imu.get("horizon_max_up_from_center_deg", 2.0))
        self.imu_max_down = float(imu.get("horizon_max_down_from_center_deg", 4.0))
        self.horizon_relevel_sec = float(imu.get("horizon_relevel_after_sec", 30.0))
        self._imu_pitch_smooth = 0.0
        self._effective_tilt_center_smooth = self.tilt_center
        self._no_face_since: float | None = None

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

        # ── Return to forward after staring off-axis ───────────────────────────
        self.forward_return_timeout_sec = float(s.get("forward_return_timeout_sec", 10.0))
        self.forward_return_min_pan_deg = float(s.get("forward_return_min_pan_deg", 15.0))
        self.forward_return_min_tilt_deg = float(s.get("forward_return_min_tilt_deg", 10.0))
        self.forward_return_smooth_hz = float(s.get("forward_return_smooth_hz", 4.5))
        self._servo_cfg = dict(s)

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
        self._prev_face_raw_x = 0.0
        self._prev_face_raw_y = 0.0
        self._lss_active = False
        self._lss_start_ts = 0.0
        self._memory_reacquire_ts = 0.0
        self._last_base_enc = None
        self._proactive_comp_applied = False
        self._last_debug_cmd_seq = 0
        self._debug_head_step = float(dv.get("head_step_deg", 5.0))
        self._filtered_norm_x = 0.0
        self._filtered_norm_y = 0.0
        self._pan_track_norm = 0.0
        self._prev_pan_err_x = 0.0
        self._pan_in_center_band = True
        self._off_forward_since: Optional[float] = None
        self._forward_return_active = False

    def _pan_mech_offset(self) -> float:
        return signed_pan_mech_deg(self._pan, self._servo_cfg)

    def _tilt_mech_offset(self) -> float:
        return signed_tilt_mech_deg(self._tilt, self._servo_cfg)

    def _is_off_forward(self) -> bool:
        return (
            abs(self._pan_mech_offset()) >= self.forward_return_min_pan_deg
            or abs(self._tilt_mech_offset()) >= self.forward_return_min_tilt_deg
        )

    def _update_off_forward_timer(self, now: float, *, tracking_face: bool) -> None:
        if tracking_face or self._forward_return_active:
            self._off_forward_since = None
            return
        if self._is_off_forward():
            if self._off_forward_since is None:
                self._off_forward_since = now
        else:
            self._off_forward_since = None

    def _off_forward_timed_out(self, now: float) -> bool:
        if self._off_forward_since is None:
            return False
        return (now - self._off_forward_since) >= self.forward_return_timeout_sec

    def _maybe_start_forward_return(self, now: float, *, tracking_face: bool) -> None:
        if self.sustained_head_follow_enabled:
            return
        if tracking_face or self._forward_return_active:
            return
        if self._mode not in ("wander", "last_seen"):
            return
        if not self._off_forward_timed_out(now):
            return
        self._forward_return_active = True
        self._off_forward_since = None
        if self._mode == "last_seen":
            self._lss_active = False

    def _tick_forward_return(self, now: float, dt: float, tilt_center: float) -> str:
        """Glide head back to forward-facing pan/tilt center."""
        pan_goal = self.pan_center
        tilt_goal = tilt_center
        self._wander.pan_goal = pan_goal
        self._wander.tilt_goal = tilt_goal
        self._wander.moving = True

        self._pan = smooth_toward(
            self._pan, pan_goal, dt,
            smooth_hz=self.forward_return_smooth_hz, lo=self.pan_min, hi=self.pan_max,
        )
        tilt_hz = self.forward_return_smooth_hz * 0.35
        self._tilt = smooth_toward(
            self._tilt, tilt_goal, dt,
            smooth_hz=tilt_hz, lo=self.tilt_min, hi=self.tilt_max,
        )

        pan_done = abs(self._pan - pan_goal) <= self.wander_arrival
        tilt_done = abs(self._tilt - tilt_goal) <= self.wander_arrival
        if pan_done and tilt_done:
            self._forward_return_active = False
            self._wander.reset(self.pan_center, tilt_center, now)

        self.bb.write(
            wander_moving=self._wander.moving,
            wander_last_step_deg=abs(self._pan - pan_goal),
        )
        return "wander"

    def _enter_wander_from_current_pose(self, now: float) -> None:
        """Switch to wander without snapping — hold track pose, then glance from here."""
        self._wander.seed_from_pose(
            self._pan,
            self._tilt,
            now,
            hold_min_sec=self.wander_track_loss_hold_min,
            hold_max_sec=self.wander_track_loss_hold_max,
        )

    def _wander_tilt_ref(self, effective_tilt_center: float) -> float:
        """Mechanical center + light IMU bias — wander glances stay visible."""
        blend = max(0.0, min(1.0, self.wander_imu_tilt_blend))
        return self.tilt_center + (effective_tilt_center - self.tilt_center) * blend

    def _apply_debug_head_cmd(self, cmd: str, step: float) -> bool:
        """Browser WASD when manual_control_enabled. Returns True if cmd consumed."""
        if cmd == "tilt_up":
            self._tilt = clamp(self._tilt + self.tilt_sign * step, self.tilt_min, self.tilt_max)
        elif cmd == "tilt_down":
            self._tilt = clamp(self._tilt - self.tilt_sign * step, self.tilt_min, self.tilt_max)
        elif cmd == "pan_left":
            self._pan = clamp(self._pan + self.pan_sign * step, self.pan_min, self.pan_max)
        elif cmd == "pan_right":
            self._pan = clamp(self._pan - self.pan_sign * step, self.pan_min, self.pan_max)
        elif cmd == "center":
            self._pan = self.pan_center
            self._tilt = self.tilt_center
            self._wander.reset(self.pan_center, self.tilt_center, time.time())
        else:
            return False
        self._wander.pan_goal = self._pan
        self._wander.tilt_goal = self._tilt
        self._pan_pid.reset()
        self._tilt_pid.reset()
        self.bb.write(imu_drift_reset_request=True)
        return True

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
        if self._mode == "track":
            # During face track, ramp compensation — never snap pan away from the PID aim.
            delta = comp_pan - self._pan
            delta = clamp(delta, -self.pan_max_step_deg, self.pan_max_step_deg)
            if abs(delta) > 0.01:
                self._pan = clamp(self._pan + delta, self.pan_min, self.pan_max)
        else:
            self._pan = clamp(comp_pan, self.pan_min, self.pan_max)
            self._wander.pan_goal = clamp(comp_pan, self.pan_min, self.pan_max)
        self._pan_pid.soften(0.45)
        self._target_glide.soften()
        self._proactive_comp_applied = True
    
    def _apply_base_compensation(self) -> None:
        """Feed-forward compensation: counter-rotate head when base moves."""
        if self._mode == "track":
            state = self.bb.read("base_encoder_deg")
            self._last_base_enc = state.get("base_encoder_deg", 0.0)
            return

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
        """Tilt reference from IMU horizon (when still). Frozen briefly only while tracking face."""
        state = self.bb.read(
            "face_detected",
            "body_detected",
            "imu_available",
            "imu_horizon_ok",
            "imu_effective_tilt_center",
        )

        tracking = state["face_detected"] or state["body_detected"]
        if tracking:
            center_alpha = 1.0 - math.exp(-2.0 * max(0.001, dt))
            if state["imu_available"] and state["imu_horizon_ok"]:
                target = state["imu_effective_tilt_center"]
                self._effective_tilt_center_smooth += (target - self._effective_tilt_center_smooth) * center_alpha * 0.35
            return self._effective_tilt_center_smooth

        if not state["imu_available"]:
            target = self.tilt_center
        elif state["imu_horizon_ok"]:
            target = state["imu_effective_tilt_center"]
        else:
            target = self._effective_tilt_center_smooth

        center_alpha = 1.0 - math.exp(-5.0 * max(0.001, dt))
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
        self._prev_pan_err_x = 0.0
        self._off_forward_since = None
        self._forward_return_active = False
        if new_mode == "track":
            state = self.bb.read("face_norm_x", "face_norm_y")
            nx = float(state["face_norm_x"])
            ny = float(state["face_norm_y"])
            self._filtered_norm_x = nx
            self._filtered_norm_y = ny
            self._pan_track_norm = nx
            self._prev_face_x = nx
            self._prev_face_y = ny
            self._prev_face_raw_x = nx
            self._prev_face_raw_y = ny
            self._pan_in_center_band = abs(nx) <= self.pan_center_norm_x
        else:
            self._filtered_norm_x = 0.0
            self._filtered_norm_y = 0.0
            self._pan_track_norm = 0.0
            self._prev_face_x = 0.0
            self._prev_face_y = 0.0
            self._prev_face_raw_x = 0.0
            self._prev_face_raw_y = 0.0
            self._pan_in_center_band = True
        if new_mode == "wander" and old_mode in ("track", "last_seen"):
            self._enter_wander_from_current_pose(time.time())
        elif new_mode == "wander":
            self._wander.tilt_goal = self._tilt
            self._wander.pan_goal = self._pan
        if old_mode == "track" and new_mode != "track":
            self._effective_tilt_center_smooth = self._tilt
            self._no_face_since = time.time()

    def _pan_center_band_active(self, norm_x: float) -> bool:
        """True when face is close enough to frame center to hold pan steady."""
        raw_mag = abs(norm_x)
        if self._pan_in_center_band:
            if raw_mag > self.pan_center_norm_x + self.pan_center_hysteresis:
                self._pan_in_center_band = False
        elif raw_mag <= self.pan_center_norm_x:
            self._pan_in_center_band = True
        return self._pan_in_center_band

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
        tracking_active = face_detected or body_detected

        if face_detected:
            self._last_face_ts = now
            self._no_face_since = None
            self._lss_active = False
            self._forward_return_active = False
        if body_detected:
            self._last_body_ts = now
            self._no_face_since = None
            self._lss_active = False
            self._forward_return_active = False

        if not tracking_active:
            # Hold pose until main loop drops track after no_face_home_sec.
            return "track"

        # Smooth face bearing for tilt / velocity (pan uses raw frame-center error).
        glide_y_scale = self.multi_face_alpha if track_kind in ("multi", "center") else 1.0
        self._filtered_norm_x += (norm_x - self._filtered_norm_x) * self.face_alpha_x
        self._filtered_norm_y += (norm_y - self._filtered_norm_y) * self.face_alpha_y * glide_y_scale

        self._face_vel_x = (norm_x - self._prev_face_raw_x) / max(dt, 0.001)
        self._face_vel_y = (norm_y - self._prev_face_raw_y) / max(dt, 0.001)
        self._prev_face_raw_x = norm_x
        self._prev_face_raw_y = norm_y
        self._prev_face_x = self._filtered_norm_x
        self._prev_face_y = self._filtered_norm_y

        # Pan error: lightly filtered bearing reduces bbox jitter; follow faster when face moves quickly.
        vel_x = abs(self._face_vel_x)
        pan_alpha = self.pan_track_alpha
        if vel_x > 2.0:
            pan_alpha = min(0.58, pan_alpha + (vel_x - 2.0) * 0.05)
            self._pan_pid.soften(0.55)
        self._pan_track_norm += (norm_x - self._pan_track_norm) * pan_alpha
        pan_err_x = _apply_deadzone(self._pan_track_norm, self.deadzone_x)

        if self._prev_pan_err_x * pan_err_x < 0.0 and abs(pan_err_x) < 0.30:
            self._pan_pid.soften(0.15)
        self._prev_pan_err_x = pan_err_x

        if self._pan_center_band_active(self._pan_track_norm):
            # Face near frame center — hold pan (do not snap to pan_center).
            self._pan_pid.reset()
            pan_target = self._pan
        else:
            pan_corr = clamp(self._pan_pid.tick(pan_err_x, dt), -1.0, 1.0)
            pan_gain = _track_error_gain(
                self._pan_track_norm,
                self.pan_error_full_scale,
                self.pan_track_min_gain,
            )
            # Absolute pan aim: center + correction * range * track sign.
            pan_target = clamp(
                self.pan_center
                + pan_corr * self.pan_track_range * self.pan_track_sign * pan_gain,
                self.pan_min,
                self.pan_max,
            )

        pan_max_step = min(self.pan_max_step_deg, self.pan_track_slew_dps * max(dt, 0.001))
        self._pan = _smooth_toward_stepped(
            self._pan, pan_target, dt,
            smooth_hz=self.pan_track_smooth_hz, lo=self.pan_min, hi=self.pan_max,
            max_step=pan_max_step,
        )

        # Tilt: face-relative on fixed center (IMU horizon applies in wander/idle only).
        if abs(self._filtered_norm_y) <= self.tilt_center_norm_y:
            self._tilt_pid.reset()
            tilt_target = self._tilt
        else:
            err_y = _apply_deadzone(self._filtered_norm_y, self.deadzone_y)
            tilt_corr = clamp(self._tilt_pid.tick(err_y, dt), -1.0, 1.0)
            tilt_target = clamp(
                self.tilt_center + self.tilt_sign * tilt_corr * self.tilt_track_range,
                self.tilt_min, self.tilt_max,
            )

        self._tilt = smooth_toward(
            self._tilt, tilt_target, dt,
            smooth_hz=self.tilt_smooth_hz, lo=self.tilt_min, hi=self.tilt_max,
        )

        return "track"

    # ── Wander mode ────────────────────────────────────────────────────────────

    def _tick_wander(self, now: float, dt: float, effective_tilt_center: float) -> str:
        if self._forward_return_active:
            return self._tick_forward_return(now, dt, effective_tilt_center)

        state = self.bb.read("face_detected", "body_detected", "last_seen_world_yaw")
        if state["face_detected"] or state["body_detected"]:
            if state["face_detected"]:
                self._last_face_ts = now
                self._no_face_since = None
            if state["body_detected"]:
                self._last_body_ts = now
                self._no_face_since = None
            self._lss_active = False
            self._forward_return_active = False
            self._pan_pid.soften()
            self._tilt_pid.soften()
            self._target_glide.soften()
            return "track"

        # Only search last-seen shortly after we were tracking (avoid stale memory hijack).
        last_yaw = state["last_seen_world_yaw"]
        recently_tracked = (now - max(self._last_face_ts, self._last_body_ts)) < self.no_face_home_sec * 2.0
        if last_yaw is not None and recently_tracked and not self._lss_active:
            self._lss_active = True
            self._lss_start_ts = now
            return "last_seen"

        pan_goal, tilt_goal = self._wander.tick(
            now,
            pan_center=self.pan_center,
            tilt_center=self._wander_tilt_ref(effective_tilt_center),
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

        holding_track_pose = (not self._wander.moving) and (now < self._wander.hold_until)
        if holding_track_pose:
            pan_target = self._wander.pan_goal
            tilt_target = self._wander.tilt_goal
            pan_hz = self.wander_pan_smooth * 0.12
            tilt_hz = self.wander_tilt_smooth * 0.12
        else:
            pan_target = pan_goal
            tilt_target = tilt_goal
            pan_hz = self.wander_pan_smooth
            tilt_hz = self.wander_tilt_smooth * (0.9 if self._wander.moving else 0.65)

        self._pan = smooth_toward(
            self._pan, pan_target, dt, smooth_hz=pan_hz, lo=self.pan_min, hi=self.pan_max,
        )
        self._tilt = smooth_toward(
            self._tilt, tilt_target, dt, smooth_hz=tilt_hz, lo=self.tilt_min, hi=self.tilt_max,
        )
        return "wander"

    # ── Last-seen mode ─────────────────────────────────────────────────────────

    def _tick_last_seen(self, now: float, dt: float, effective_tilt_center: float) -> str:
        if self._forward_return_active:
            return self._tick_forward_return(now, dt, effective_tilt_center)

        state = self.bb.read("face_detected", "body_detected", "last_seen_world_yaw", "base_world_yaw_deg")
        if state["face_detected"] or state["body_detected"]:
            self._lss_active = False
            self._forward_return_active = False
            self._pan_pid.soften()
            return "track"

        last_yaw = state["last_seen_world_yaw"]
        if last_yaw is None or (now - self._lss_start_ts) > self.lss_timeout:
            self._lss_active = False
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
        now0 = time.time()
        self._wander.reset(self.pan_center, self.tilt_center, now0)
        self._last_face_ts = now0
        self._last_body_ts = now0

        prev_ts = time.perf_counter()
        self.bb.write(servo_pan=self._pan, servo_tilt=self._tilt, servo_mode="wander")

        while self.bb.read("running")["running"]:
            now_pc = time.perf_counter()
            dt = max(0.001, min(0.5, now_pc - prev_ts))
            prev_ts = now_pc
            now = time.time()

            dbg = self.bb.read(
                "manual_control_enabled",
                "debug_control_cmd",
                "debug_control_seq",
                "debug_head_step_deg",
            )
            manual = dbg["manual_control_enabled"]
            cmd = dbg["debug_control_cmd"]
            cmd_seq = int(dbg["debug_control_seq"])
            if manual and cmd and cmd_seq > self._last_debug_cmd_seq:
                self._last_debug_cmd_seq = cmd_seq
                step = float(dbg["debug_head_step_deg"] or self._debug_head_step)
                if cmd in ("tilt_up", "tilt_down", "pan_left", "pan_right", "center"):
                    self._apply_debug_head_cmd(cmd, step)
                    self.bb.write(
                        debug_control_cmd="",
                        servo_pan=self._pan,
                        servo_tilt=self._tilt,
                        servo_mode="manual",
                        wander_moving=False,
                        wander_last_step_deg=0.0,
                    )
                    time.sleep(loop_delay)
                    continue

            self._apply_proactive_base_comp()
            self._apply_base_compensation()
            effective_tilt_center = self._effective_tilt_center(dt)

            vision = self.bb.read("face_detected", "body_detected")
            tracking_face = vision["face_detected"] or vision["body_detected"]
            self._update_off_forward_timer(now, tracking_face=tracking_face)
            self._maybe_start_forward_return(now, tracking_face=tracking_face)

            old_mode = self._mode
            if old_mode == "track":
                next_mode = self._tick_track(now, dt, effective_tilt_center)
                face_gone = (now - self._last_face_ts) > self.no_face_home_sec
                body_gone = (now - self._last_body_ts) > self.no_face_home_sec
                if face_gone and body_gone:
                    next_mode = "wander"
            elif old_mode == "last_seen":
                next_mode = self._tick_last_seen(now, dt, effective_tilt_center)
            else:
                next_mode = self._tick_wander(now, dt, effective_tilt_center)

            if next_mode != old_mode:
                self._on_mode_change(old_mode, next_mode)
                self._mode = next_mode
                if self._mode == "track":
                    self._tick_track(now, dt, effective_tilt_center)
            else:
                self._mode = next_mode

            # Publish
            publish = dict(
                servo_pan=self._pan,
                servo_tilt=self._tilt,
                servo_mode=self._mode,
                servo_forward_return_active=self._forward_return_active,
                servo_pan_hold=self._pan_in_center_band,
            )
            if self._mode != "wander":
                publish["wander_moving"] = False
                publish["wander_last_step_deg"] = 0.0
            self.bb.write(**publish)

            time.sleep(loop_delay)

        print("[ServoLoop] Stopped.")
