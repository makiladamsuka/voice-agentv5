"""BaseController: decides when and how much to rotate the robot base.

All BASE_* constants live exclusively here. This module reads servo state
and vision state from the Blackboard, computes a nudge step, and writes
base_step_deg + base_step_ready. ServoMixer executes the actual serial write.

Reads from BB:
    face_detected, face_norm_x, face_area_ratio,
    body_detected, track_kind,
    servo_pan, servo_tilt, servo_mode,
    base_encoder_deg, base_world_yaw_deg, base_motion_busy,
    imu_available, imu_gyro_dps,
    person_snapshots, last_seen_world_yaw,
    running

Writes to BB:
    base_step_deg, base_step_source, base_step_ready
"""

from __future__ import annotations

import math
import time
from pathlib import Path
from typing import Optional

try:
    import yaml
except ImportError:
    yaml = None

from core.blackboard import Blackboard
from lib.elastic_head_motion import clamp
from lib.person_memory import angular_error_deg, wrap_degrees
from base_safety import BaseMotionGate, BaseMoveWatchdog, BaseSafetyConfig
from base_yaw_controller import BaseYawState

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


def _pan_to_mech(pan_cmd: float, pan_center: float, pan_min: float, pan_max: float,
                 mech_left: float, mech_right: float) -> float:
    if pan_cmd >= pan_center:
        span = max(pan_max - pan_center, 1e-6)
        return (pan_cmd - pan_center) / span * mech_right
    span = max(pan_center - pan_min, 1e-6)
    return (pan_center - pan_cmd) / span * mech_left


class BaseController:
    """Decides base rotation nudges and publishes them to the Blackboard."""

    def __init__(self, bb: Blackboard, link, config_path: Path = DEFAULT_CONFIG_PATH) -> None:
        self.bb = bb
        self._link = link
        cfg = _load_yaml(config_path)
        b = _cfg(cfg, "base", default={}) or {}
        s = _cfg(cfg, "servo", default={}) or {}
        pm = _cfg(cfg, "person_memory", default={}) or {}
        lss = _cfg(cfg, "last_seen_search", default={}) or {}

        # ── Enable / disable ──────────────────────────────────────────────────
        self.enabled = bool(b.get("enabled", False))

        # ── Geometry ──────────────────────────────────────────────────────────
        self.pan_min = float(s.get("pan_min", 40.0))
        self.pan_max = float(s.get("pan_max", 120.0))
        self.pan_center = float(s.get("pan_center", (self.pan_min + self.pan_max) * 0.5))
        self.mech_left = float(s.get("pan_mech_left_deg", -40.0))
        self.mech_right = float(s.get("pan_mech_right_deg", 40.0))
        self.base_sign = float(b.get("sign", 1.0))

        # ── Trigger thresholds ────────────────────────────────────────────────
        self.trigger_norm_x = float(b.get("trigger_norm_x", 0.52))
        self.trigger_hold_sec = float(b.get("trigger_hold_sec", 1.2))
        self.trigger_hold_at_limit_sec = float(b.get("trigger_hold_at_limit_sec", 0.25))
        self.cooldown_sec = float(b.get("cooldown_sec", 2.4))
        self.pan_soft_limit = float(b.get("pan_soft_limit_deg", 18.0))
        self.head_lead_min = float(b.get("head_lead_min_deg", 8.0))
        self.pan_limit_margin = float(b.get("pan_limit_margin", 0.35))

        # ── Step sizes ────────────────────────────────────────────────────────
        self.min_step = float(b.get("min_step_deg", 0.8))
        self.max_step = float(b.get("max_step_deg", 2.5))
        self.norm_to_deg = float(b.get("norm_to_deg_gain", 2.2))
        self.pan_offset_to_step = float(b.get("pan_offset_to_step_gain", 0.30))
        self.track_comp_gain = float(b.get("track_compensation_gain", 0.45))
        self.max_yaw_deg = float(b.get("max_yaw_deg", 120.0))
        self.recenter_deadband = float(b.get("recenter_deadband_deg", 15.0))
        self.recenter_step = float(b.get("recenter_step_deg", 5.0))

        # ── Fast-face step ────────────────────────────────────────────────────
        self.fast_face_enabled = bool(b.get("fast_face_enabled", True))
        self.fast_face_cooldown = float(b.get("fast_face_cooldown_sec", 0.9))
        self.fast_face_min = float(b.get("fast_face_min_step_deg", 4.0))
        self.fast_face_max = float(b.get("fast_face_max_step_deg", 10.0))
        self.fast_face_vel_gain = float(b.get("fast_face_velocity_to_deg_gain", 2.2))
        self.fast_face_comp = float(b.get("fast_face_compensation_gain", 0.55))
        self.fast_face_vel_norm_sec = float(b.get("fast_face_velocity_norm_sec", 3.0))

        # ── Person memory base steps ──────────────────────────────────────────
        self.pm_base_enabled = bool(pm.get("base_enabled", True))
        self.pm_base_min = float(pm.get("base_min_step_deg", 2.0))
        self.pm_base_max = float(pm.get("base_max_step_deg", 5.0))
        self.pm_base_gain = float(pm.get("base_yaw_to_step_gain", 0.20))
        self.pm_base_cooldown = float(pm.get("base_cooldown_sec", 1.2))
        self.pm_base_comp = float(pm.get("base_compensation_gain", 0.35))
        self.pm_base_min_yaw_err = float(pm.get("base_min_yaw_error_deg", 6.0))
        self.pm_hfov = float(pm.get("camera_hfov_deg", 62.0))

        # ── Last-seen base steps ───────────────────────────────────────────────
        self.lss_base_enabled = bool(lss.get("base_enabled", True))
        self.lss_base_min = float(lss.get("base_min_step_deg", 2.5))
        self.lss_base_max = float(lss.get("base_max_step_deg", 5.0))
        self.lss_base_gain = float(lss.get("base_yaw_to_step_gain", 0.24))
        self.lss_base_cooldown = float(lss.get("base_cooldown_sec", 0.85))
        self.lss_base_comp = float(lss.get("base_compensation_gain", 0.35))
        self.lss_base_min_yaw_err = float(lss.get("base_min_yaw_error_deg", 3.0))
        self.lss_edge_norm = float(lss.get("edge_norm", 0.40))
        self.lss_edge_base = bool(lss.get("edge_track_base", True))

        # ── Wander base steps ──────────────────────────────────────────────────
        self.wander_base_enabled = bool(b.get("wander_enabled", True))
        self.wander_base_step = float(b.get("wander_step_deg", 5.0))
        self.wander_base_cooldown = float(b.get("wander_cooldown_sec", 6.0))
        self.wander_base_chance = float(b.get("wander_chance", 0.40))
        self.wander_min_pan = float(b.get("wander_min_pan_offset_deg", 10.0))
        
        # ── Safety gate ──────────────────────────────────────────────────────
        self._gate = BaseMotionGate(backoff_sec=float(b.get("error_backoff_sec", 45.0)))
        self._yaw_state = BaseYawState(max_yaw_deg=self.max_yaw_deg)

        # ── Runtime state ─────────────────────────────────────────────────────
        self._last_nudge_ts = 0.0
        self._last_fast_ts = 0.0
        self._last_pm_ts = 0.0
        self._last_lss_ts = 0.0
        self._last_wander_ts = 0.0
        self._trigger_since = 0.0

    # ── Helpers ────────────────────────────────────────────────────────────────

    def _pan_mech(self, pan_cmd: float) -> float:
        return _pan_to_mech(pan_cmd, self.pan_center, self.pan_min, self.pan_max,
                            self.mech_left, self.mech_right)

    def _head_pan_offset(self, pan_cmd: float) -> float:
        return self._pan_mech(pan_cmd)

    def _pan_at_limit(self, pan_cmd: float) -> bool:
        mech = abs(self._pan_mech(pan_cmd))
        soft = min(self.pan_soft_limit, abs(self.mech_left), abs(self.mech_right))
        return mech >= soft * (1.0 - self.pan_limit_margin)

    def _cap_for_aim(self, step: float, aim_norm_x: float, hfov: float = 62.0) -> float:
        """Prevent base overshoot by capping step to the remaining aim angle."""
        if step == 0.0 or abs(aim_norm_x) < 0.04:
            return step
        aim_deg = abs(clamp(aim_norm_x, -1.0, 1.0)) * (hfov * 0.5)
        if aim_deg < 0.4:
            return 0.0
        aim_sign = 1.0 if aim_norm_x >= 0.0 else -1.0
        step_sign = 1.0 if step >= 0.0 else -1.0
        if aim_sign * step_sign < 0.0:
            return step
        max_useful = aim_deg * 1.05
        if abs(step) <= max_useful:
            return step
        return step_sign * max(max_useful, self.min_step * 0.5)

    def _apply_gate(self, step: float, pan_cmd: float) -> float:
        """Return 0 if base not allowed; apply yaw limit."""
        if not self._gate.allowed():
            return 0.0
        pan_offset = self._head_pan_offset(pan_cmd)
        if not self._yaw_state.allow_base_step(step, pan_offset):
            return 0.0
        return step

    # ── Per-mode planning ──────────────────────────────────────────────────────

    def _plan_track_step(self, now: float, state: dict) -> tuple[Optional[float], str]:
        pan = state["servo_pan"]
        norm_x = state["face_norm_x"]
        if not self._pan_at_limit(pan) and abs(norm_x) < self.trigger_norm_x:
            self._trigger_since = 0.0
            return None, ""

        if self._trigger_since <= 0.0:
            self._trigger_since = now
        hold_req = self.trigger_hold_at_limit_sec if self._pan_at_limit(pan) else self.trigger_hold_sec
        if (now - self._trigger_since) < hold_req:
            return None, ""
        if (now - self._last_nudge_ts) < self.cooldown_sec:
            return None, ""

        pan_mech = self._pan_mech(pan)
        sign = math.copysign(1.0, pan_mech)
        step = clamp(abs(pan_mech) * self.pan_offset_to_step, self.min_step, self.max_step) * sign * self.base_sign
        step = self._cap_for_aim(step, norm_x)
        step = self._apply_gate(step, pan)
        if step == 0.0:
            return None, ""
        # Compensation: head will move back slightly after base moves
        comp = -norm_x * self.track_comp_gain
        return step, f"track|comp={comp:+.2f}"

    def _plan_wander_recenter(self, now: float, state: dict) -> tuple[Optional[float], str]:
        """Gently re-center base yaw during wander, or actively wander if enabled."""
        enc = state["base_encoder_deg"]
        pan = state["servo_pan"]
        
        if (now - self._last_nudge_ts) < self.cooldown_sec:
            return None, ""
            
        # 1. Active Wander
        if self.wander_base_enabled and (now - self._last_wander_ts) > self.wander_base_cooldown:
            import random
            if random.random() < self.wander_base_chance:
                # Decide direction based on current pan to amplify the look, or random if centered
                pan_mech = self._pan_mech(pan)
                if abs(pan_mech) > self.wander_min_pan:
                    sign = math.copysign(1.0, pan_mech)
                else:
                    sign = random.choice([-1.0, 1.0])
                    
                step = self.wander_base_step * sign * self.base_sign
                step = self._apply_gate(step, pan)
                if abs(step) >= self.min_step:
                    self._last_wander_ts = now
                    return step, "wander_explore"

        # 2. Gentle Recenter (only if we didn't wander)
        if abs(enc) > self.recenter_deadband:
            step = clamp(-enc * 0.30, -self.recenter_step, self.recenter_step) * self.base_sign
            step = self._apply_gate(step, pan)
            if abs(step) >= self.min_step:
                return step, "recenter"
                
        return None, ""

    def _plan_memory_step(self, now: float, state: dict) -> tuple[Optional[float], str]:
        if not self.pm_base_enabled:
            return None, ""
        if (now - self._last_pm_ts) < self.pm_base_cooldown:
            return None, ""
        snapshots = state["person_snapshots"]
        if not snapshots:
            return None, ""
        world_yaw = state["base_world_yaw_deg"]
        best = min(snapshots, key=lambda s: abs(angular_error_deg(s["world_yaw_deg"], world_yaw)))
        err = angular_error_deg(best["world_yaw_deg"], world_yaw)
        if abs(err) < self.pm_base_min_yaw_err:
            return None, ""
        step = clamp(err * self.pm_base_gain, -self.pm_base_max, self.pm_base_max)
        if abs(step) < self.pm_base_min:
            return None, ""
        step = self._apply_gate(step * self.base_sign, state["servo_pan"])
        return step, "memory"

    def _plan_last_seen_step(self, now: float, state: dict) -> tuple[Optional[float], str]:
        if not self.lss_base_enabled:
            return None, ""
        if (now - self._last_lss_ts) < self.lss_base_cooldown:
            return None, ""
        last_yaw = state["last_seen_world_yaw"]
        if last_yaw is None:
            return None, ""
        world_yaw = state["base_world_yaw_deg"]
        err = angular_error_deg(last_yaw, world_yaw)
        if abs(err) < self.lss_base_min_yaw_err:
            return None, ""
        step = clamp(err * self.lss_base_gain, -self.lss_base_max, self.lss_base_max)
        if abs(step) < self.lss_base_min:
            return None, ""
        step = self._apply_gate(step * self.base_sign, state["servo_pan"])
        return step, "last_seen"

    # ── Main loop ──────────────────────────────────────────────────────────────

    def run(self) -> None:
        if not self.enabled:
            print("[BaseController] Base rotation disabled in config.")
            return

        loop_delay = 0.02  # 50 Hz
        self._gate.clear_backoff()

        while self.bb.read("running")["running"]:
            now = time.time()

            # Sync yaw state from ServoMixer's latest encoder reading
            state = self.bb.read(
                "face_detected", "face_norm_x", "face_area_ratio",
                "body_detected", "track_kind",
                "servo_pan", "servo_mode",
                "base_encoder_deg", "base_world_yaw_deg", "base_motion_busy",
                "person_snapshots", "last_seen_world_yaw",
            )

            pan_offset = self._head_pan_offset(state["servo_pan"])
            self._yaw_state.update(state["base_encoder_deg"], pan_offset)

            self._gate.clear_backoff(now)

            if state["base_motion_busy"]:
                time.sleep(loop_delay)
                continue

            mode = state["servo_mode"]
            step: Optional[float] = None
            source = ""

            if mode == "track" and state["face_detected"]:
                step, source = self._plan_track_step(now, state)
            elif mode == "last_seen":
                step, source = self._plan_last_seen_step(now, state)
                if step is None:
                    step, source = self._plan_memory_step(now, state)
            elif mode == "wander":
                step, source = self._plan_wander_recenter(now, state)

            if step is not None and abs(step) >= self.min_step:
                self._last_nudge_ts = now
                if "memory" in source:
                    self._last_pm_ts = now
                if "last_seen" in source:
                    self._last_lss_ts = now
                self._trigger_since = 0.0
                self.bb.write(
                    base_step_deg=step,
                    base_step_source=source,
                    base_step_ready=True,
                )
            else:
                # Clear ready flag so ServoMixer doesn't re-execute stale step
                if self.bb.read("base_step_ready")["base_step_ready"]:
                    self.bb.write(base_step_ready=False)

            time.sleep(loop_delay)

        print("[BaseController] Stopped.")
