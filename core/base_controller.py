"""BaseController: decides when and how much to rotate the robot base.

All BASE_* constants live exclusively here. This module reads servo state
and vision state from the Blackboard, computes a nudge step, and writes
base_step_deg + base_step_ready. ServoMixer executes the actual serial write.

Reads from BB:
    face_detected, face_norm_x, face_area_ratio,
    body_detected, track_kind,
    servo_pan, servo_tilt, servo_mode,
    base_encoder_deg, base_world_yaw_deg, base_motion_busy,
    imu_available, imu_gyro_dps, imu_gyro_z_dps, imu_yaw_integral_deg,
    person_snapshots, last_seen_world_yaw,
    yaw_reference_locked,
    running

Writes to BB:
    base_step_deg, base_step_source, base_step_ready, base_comp_pan_deg,
    imu_inferred_base_deg
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
from lib.elastic_head_motion import clamp
from lib.person_memory import angular_error_deg
from lib.base_head_lead import (
    aim_agrees_with_head,
    head_lead_sign,
    plan_aim_base_step,
    proactive_comp_pan_cmd,
    step_agrees_with_head,
    yaw_error_agrees_with_head,
)
from base_safety import BaseMotionGate
from base_yaw_controller import BaseYawState
from lib.head_mech import signed_pan_mech_deg

APP_DIR = Path(__file__).resolve().parent.parent
DEFAULT_CONFIG_PATH = APP_DIR / "config.yaml"


def _load_yaml(path: Path) -> dict:
    if yaml is None or not path.exists():
        return {}
    with open(path, encoding="utf-8") as f:
        from lib.live_tune import sanitize_config

        return sanitize_config(yaml.safe_load(f) or {})


def _cfg(data, *keys, default=None):
    cur = data
    for key in keys:
        if not isinstance(cur, dict) or key not in cur:
            return default
        cur = cur[key]
    return cur


class BaseController:
    """Decides base rotation nudges and publishes them to the Blackboard."""

    def __init__(
        self,
        bb: Blackboard,
        link,
        config_path: Path = DEFAULT_CONFIG_PATH,
        gate: BaseMotionGate | None = None,
    ) -> None:
        self.bb = bb
        self._link = link
        cfg = _load_yaml(config_path)
        b = _cfg(cfg, "base", default={}) or {}
        s = _cfg(cfg, "servo", default={}) or {}
        pm = _cfg(cfg, "person_memory", default={}) or {}
        lss = _cfg(cfg, "last_seen_search", default={}) or {}
        imu = _cfg(cfg, "imu", default={}) or {}

        # ── Enable / disable ──────────────────────────────────────────────────
        self.enabled = bool(b.get("enabled", False))
        self.use_imu_validation = bool(b.get("use_imu_move_validation", False))
        self.imu_fusion_max_error = float(b.get("imu_fusion_max_error_deg", 15.0))
        self.imu_stationary_gyro = float(imu.get("auto_level_gyro_max_dps", 8.0))

        # ── Geometry ──────────────────────────────────────────────────────────
        self.pan_min = float(s.get("pan_min", 40.0))
        self.pan_max = float(s.get("pan_max", 120.0))
        self.pan_center = float(s.get("pan_center", (self.pan_min + self.pan_max) * 0.5))
        self.mech_left = float(s.get("pan_mech_left_deg", -40.0))
        self.mech_right = float(s.get("pan_mech_right_deg", 40.0))
        self.pan_sign = float(s.get("pan_sign", 1.0))
        self._servo_cfg = s
        self.base_sign = float(b.get("sign", 1.0))

        # ── Trigger thresholds ────────────────────────────────────────────────
        self.trigger_norm_x = float(b.get("trigger_norm_x", 0.52))
        self.trigger_hold_sec = float(b.get("trigger_hold_sec", 1.2))
        self.trigger_hold_at_limit_sec = float(b.get("trigger_hold_at_limit_sec", 0.25))
        self.cooldown_sec = float(b.get("cooldown_sec", 2.4))
        self.pan_soft_limit = float(b.get("pan_soft_limit_deg", 18.0))
        self.head_lead_min = float(b.get("head_lead_min_deg", 8.0))
        self.follow_head_direction = bool(b.get("follow_head_direction", True))
        self.pan_limit_margin = float(b.get("pan_limit_margin", 0.35))

        # ── Step sizes ────────────────────────────────────────────────────────
        self.min_step = float(b.get("min_step_deg", 0.8))
        self.max_step = float(b.get("max_step_deg", 2.5))
        self.single_shot_max = float(b.get("single_shot_max_deg", 3.0))
        self.norm_to_deg = float(b.get("norm_to_deg_gain", 2.2))
        self.pan_offset_to_step = float(b.get("pan_offset_to_step_gain", 0.30))
        self.track_comp_gain = float(b.get("track_compensation_gain", 0.45))
        self.max_yaw_deg = float(b.get("max_yaw_deg", 120.0))

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
        self.wander_min_head_step = float(b.get("wander_min_head_step_deg", 6.0))
        self.wander_random_turn_enabled = bool(b.get("wander_random_turn_enabled", True))
        self.wander_random_turn_deg = float(b.get("wander_random_turn_deg", 30.0))
        self.wander_random_turn_chunk = float(b.get("wander_random_turn_chunk_deg", 3.0))
        self.wander_random_turn_min_sec = float(b.get("wander_random_turn_min_sec", 120.0))
        self.wander_random_turn_max_sec = float(b.get("wander_random_turn_max_sec", 300.0))

        # ── Sustained head-on-base hold → base follows with neck lock ─────────
        self.sustained_follow_enabled = bool(b.get("sustained_head_follow_enabled", True))
        self.sustained_hold_sec = float(b.get("sustained_head_hold_sec", 10.0))
        self.sustained_hold_min_mech = float(b.get("sustained_head_min_mech_deg", 12.0))
        self.sustained_cooldown_sec = float(b.get("sustained_head_cooldown_sec", 4.0))
        self.sustained_comp_gain = float(b.get("sustained_head_compensation_gain", 0.95))

        # ── Body-only base follow ─────────────────────────────────────────────
        self.body_base_enabled = bool(b.get("body_base_enabled", True))
        self.body_base_cooldown = float(b.get("body_base_cooldown_sec", 2.5))
        self.body_base_min = float(b.get("body_base_min_step_deg", 2.0))
        self.body_base_max = float(b.get("body_base_max_step_deg", 5.0))
        self.body_base_aim_gain = float(b.get("body_base_aim_to_step_gain", 1.8))
        self.body_base_comp = float(b.get("body_base_compensation_gain", 0.90))
        self.body_trigger_norm_x = float(b.get("body_trigger_norm_x", 0.08))

        # ── Safety gate ──────────────────────────────────────────────────────
        self._gate = gate if gate is not None else BaseMotionGate(
            backoff_sec=float(b.get("error_backoff_sec", 45.0))
        )
        self._yaw_state = BaseYawState(max_yaw_deg=self.max_yaw_deg)

        # ── Proximity sensing (ToF approach detection) ────────────────────────
        prox = _cfg(cfg, "proximity", default={}) or {}
        self.prox_enabled = bool(prox.get("enabled", True))
        self.prox_lockout_sec = float(prox.get("post_turn_lockout_sec", 2.0))
        self.prox_min_confidence = int(prox.get("min_confidence", 4))
        self.prox_max_turns = int(prox.get("max_turns_per_window", 2))
        self.prox_window_sec = float(prox.get("turn_window_sec", 30.0))
        self.prox_turn_step = float(prox.get("turn_step_deg", 35.0))
        self.prox_comp_gain = float(prox.get("compensation_gain", 0.90))
        self.prox_cooldown_sec = float(prox.get("cooldown_sec", 5.0))
        self.prox_post_motion_blanking_sec = float(prox.get("post_motion_blanking_sec", 1.5))

        # ── Runtime state ─────────────────────────────────────────────────────
        self._last_nudge_ts = 0.0
        self._last_fast_ts = 0.0
        self._last_pm_ts = 0.0
        self._last_lss_ts = 0.0
        self._last_wander_ts = 0.0
        self._last_body_ts = 0.0
        self._was_wander_moving = False
        self._next_random_turn_ts = time.time() + random.uniform(
            self.wander_random_turn_min_sec,
            self.wander_random_turn_max_sec,
        )
        self._random_turn_remaining = 0.0
        self._random_turn_sign = 0.0
        self._trigger_since = 0.0
        self._sustained_since: Optional[float] = None
        self._sustained_sign = 0.0
        self._last_sustained_ts = 0.0
        self._last_tune_seq = 0
        self._last_prox_ts = 0.0
        self._prox_turn_timestamps: list[float] = []
        self._last_base_motion_done_ts = 0.0
        self._was_base_busy = False

    # ── Helpers ────────────────────────────────────────────────────────────────

    def _apply_live_tune_if_changed(self) -> None:
        state = self.bb.read("debug_live_tune", "debug_tune_seq")
        seq = int(state["debug_tune_seq"])
        if seq == self._last_tune_seq:
            return
        self._last_tune_seq = seq
        tune = state["debug_live_tune"] or {}
        if not tune:
            return
        from lib.live_tune import apply_base_tune

        apply_base_tune(self, tune)

    def _pan_mech(self, pan_cmd: float) -> float:
        return signed_pan_mech_deg(pan_cmd, self._servo_cfg)

    def _head_pan_offset(self, pan_cmd: float) -> float:
        return self._pan_mech(pan_cmd)

    def _pan_at_limit(self, pan_cmd: float) -> bool:
        mech = abs(self._pan_mech(pan_cmd))
        soft = min(self.pan_soft_limit, abs(self.mech_left), abs(self.mech_right))
        return mech >= soft * (1.0 - self.pan_limit_margin)

    def _comp_pan_for_step(self, step: float, pan_cmd: float, gain: float) -> float:
        # step is the hardware command sent to the mixer. 
        # Convert it back to physical degrees for compensation math.
        physical_step = step / self.base_sign if abs(self.base_sign) > 1e-6 else step
        return proactive_comp_pan_cmd(
            physical_step,
            pan_cmd,
            compensation_gain=gain,
            pan_center=self.pan_center,
            pan_min=self.pan_min,
            pan_max=self.pan_max,
            mech_left=self.mech_left,
            mech_right=self.mech_right,
            pan_sign=self.pan_sign,
        )

    def _reject_head_lead(self, step: float, pan_cmd: float) -> bool:
        """Return True if step violates head-lead-only policy."""
        pan_mech = self._pan_mech(pan_cmd)
        plate_step = step / self.base_sign if abs(self.base_sign) > 1e-6 else step
        return not step_agrees_with_head(
            plate_step,
            pan_mech,
            head_lead_min_deg=self.head_lead_min,
        )

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

    def _plan_base_follow_step(self, magnitude_deg: float, pan_cmd: float) -> Optional[float]:
        """Base follows only after the head has turned toward interest."""
        pan_offset = self._head_pan_offset(pan_cmd)
        if abs(pan_offset) < self.head_lead_min:
            return None
        head_sign = head_lead_sign(pan_offset)
        if head_sign == 0.0:
            return None
        mag = clamp(abs(magnitude_deg), self.min_step, self.max_step)
        direction = head_sign if self.follow_head_direction else head_sign
        return direction * mag

    def _schedule_next_random_turn(self, now: float) -> None:
        self._next_random_turn_ts = now + random.uniform(
            self.wander_random_turn_min_sec,
            self.wander_random_turn_max_sec,
        )

    def _plan_random_wander_turn(
        self, now: float, pan: float, enc: float, wander_moving: bool, state: dict,
    ) -> tuple[Optional[float], str]:
        """Occasional large turn as several small safe steps (~3° each)."""
        if not self.wander_random_turn_enabled:
            return None, ""
        if wander_moving:
            return None, ""

        if self._random_turn_remaining <= 0.0:
            if now < self._next_random_turn_ts:
                return None, ""
            pan_mech = self._pan_mech(pan)
            head_sign = head_lead_sign(pan_mech)
            if head_sign == 0.0:
                return None, ""
            self._random_turn_sign = head_sign
            self._random_turn_remaining = self.wander_random_turn_deg * random.uniform(0.9, 1.1)
            self._schedule_next_random_turn(now)

        if (now - self._last_wander_ts) < self.wander_base_cooldown:
            return None, ""

        chunk = min(
            self.wander_random_turn_chunk,
            self.single_shot_max,
            self._random_turn_remaining,
        )
        step = self._random_turn_sign * chunk * self.base_sign
        step = self._apply_gate(step, pan, enc, state)
        if abs(step) < self.min_step:
            self._random_turn_remaining = 0.0
            return None, ""

        self._random_turn_remaining = max(0.0, self._random_turn_remaining - abs(step))
        self._last_wander_ts = now
        return step, "wander_random_turn"

    def _apply_gate(
        self,
        step: float,
        pan_cmd: float,
        encoder_deg: float,
        state: dict,
        *,
        require_head_lead: bool = True,
    ) -> float:
        """Return 0 if base not allowed; apply encoder and world-yaw limits."""
        if not self._gate.allowed() or not state.get("base_motion_allowed", True):
            return 0.0
        if require_head_lead and self._reject_head_lead(step, pan_cmd):
            return 0.0
        hard_max = min(self.max_step, self.single_shot_max)
        if abs(step) > hard_max:
            step = math.copysign(hard_max, step)
        pan_offset = self._head_pan_offset(pan_cmd)
        self._yaw_state.update(encoder_deg, pan_offset)
        if not self._yaw_state.allow_base_step(step, pan_offset):
            return 0.0
        if step > 0:
            room = self.max_yaw_deg - encoder_deg
        else:
            room = encoder_deg + self.max_yaw_deg
        if room <= self.min_step * 0.5:
            return 0.0
        if abs(step) > room:
            step = math.copysign(room * 0.95, step)
        if self.use_imu_validation and state.get("imu_available"):
            world_yaw = self._yaw_state.world_yaw_deg
            projected = world_yaw + step
            if abs(projected) > self.max_yaw_deg:
                budget = self.max_yaw_deg - abs(world_yaw)
                if budget <= self.min_step * 0.5:
                    return 0.0
                step = math.copysign(min(abs(step), budget * 0.95), step)
        return step

    # ── Per-mode planning ──────────────────────────────────────────────────────

    def _plan_track_step(self, now: float, state: dict) -> tuple[Optional[float], str, float]:
        pan = state["servo_pan"]
        norm_x = state["face_norm_x"]
        pan_mech = self._pan_mech(pan)

        if not self._pan_at_limit(pan) and abs(norm_x) < self.trigger_norm_x:
            self._trigger_since = 0.0
            return None, "", 0.0

        if not aim_agrees_with_head(norm_x, pan_mech):
            return None, "", 0.0

        if self._trigger_since <= 0.0:
            self._trigger_since = now
        hold_req = self.trigger_hold_at_limit_sec if self._pan_at_limit(pan) else self.trigger_hold_sec
        if (now - self._trigger_since) < hold_req:
            return None, "", 0.0
        if (now - self._last_nudge_ts) < self.cooldown_sec:
            return None, "", 0.0

        sign = math.copysign(1.0, pan_mech)
        step = clamp(abs(pan_mech) * self.pan_offset_to_step, self.min_step, self.max_step) * sign * self.base_sign
        step = self._cap_for_aim(step, norm_x)
        step = self._apply_gate(step, pan, state["base_encoder_deg"], state)
        if step == 0.0:
            return None, "", 0.0
        comp_pan = self._comp_pan_for_step(step, pan, self.track_comp_gain)
        return step, "track", comp_pan

    def _plan_body_step(self, now: float, state: dict) -> tuple[Optional[float], str, float]:
        """Body-only track: rotate base toward person using frame-center aim error."""
        if not self.body_base_enabled:
            return None, "", 0.0
        if not state["body_detected"] or state["track_kind"] != "body":
            return None, "", 0.0
        if state["face_detected"]:
            return None, "", 0.0

        norm_x = state["face_norm_x"]
        if abs(norm_x) < self.body_trigger_norm_x:
            return None, "", 0.0
        if (now - self._last_body_ts) < self.body_base_cooldown:
            return None, "", 0.0
        if (now - self._last_nudge_ts) < self.body_base_cooldown:
            return None, "", 0.0

        pan = state["servo_pan"]
        pan_mech = self._pan_mech(pan)
        raw_step = plan_aim_base_step(
            pan_mech,
            norm_x,
            min_step_deg=self.body_base_min,
            max_step_deg=self.body_base_max,
            aim_gain=self.body_base_aim_gain,
            pan_offset_to_step_gain=self.pan_offset_to_step,
        )
        if raw_step is None:
            return None, "", 0.0

        step = raw_step * self.base_sign
        step = self._cap_for_aim(step, norm_x, hfov=self.pm_hfov)
        step = self._apply_gate(
            step, pan, state["base_encoder_deg"], state, require_head_lead=False,
        )
        if step == 0.0:
            return None, "", 0.0
        comp_pan = self._comp_pan_for_step(step, pan, self.body_base_comp)
        return step, "body", comp_pan

    def _plan_wander_follow(self, now: float, state: dict) -> tuple[Optional[float], str, float]:
        """Wander: base nudges only after the head finishes a look and holds."""
        enc = state["base_encoder_deg"]
        pan = state["servo_pan"]
        wander_moving = state.get("wander_moving", False)

        if (now - self._last_nudge_ts) < self.cooldown_sec:
            self._was_wander_moving = wander_moving
            return None, "", 0.0

        step: Optional[float] = None
        source = ""
        comp_pan = 0.0

        step, source = self._plan_random_wander_turn(now, pan, enc, wander_moving, state)

        head_settled = self._was_wander_moving and not wander_moving
        self._was_wander_moving = wander_moving
        if step is None and (
            head_settled
            and self.wander_base_enabled
            and (now - self._last_wander_ts) >= self.wander_base_cooldown
        ):
            pan_mech = self._pan_mech(pan)
            head_step = state.get("wander_last_step_deg", 0.0)
            if (
                abs(pan_mech) >= self.wander_min_pan
                and head_step >= self.wander_min_head_step
                and random.random() < self.wander_base_chance
            ):
                raw = self._plan_base_follow_step(self.wander_base_step, pan)
                if raw is not None:
                    step = raw * self.base_sign
                    step = self._apply_gate(step, pan, enc, state)
                    if abs(step) >= self.min_step:
                        self._last_wander_ts = now
                        source = "wander_follow"
                        comp_pan = self._comp_pan_for_step(step, pan, self.track_comp_gain)

        if step is None or abs(step) < self.min_step:
            return None, "", 0.0
        return step, source, comp_pan

    def _update_sustained_hold(self, now: float, pan_mech: float) -> None:
        """Track how long the neck has held an offset from base-forward."""
        if abs(pan_mech) < self.sustained_hold_min_mech:
            self._sustained_since = None
            self._sustained_sign = 0.0
            return
        sign = head_lead_sign(pan_mech)
        if self._sustained_since is None or sign != self._sustained_sign:
            self._sustained_since = now
            self._sustained_sign = sign

    def _plan_sustained_follow(self, now: float, state: dict) -> tuple[Optional[float], str, float]:
        """After sustained neck offset, rotate base and counter-rotate neck to lock gaze."""
        if not self.sustained_follow_enabled:
            return None, "", 0.0
        if self._sustained_since is None:
            return None, "", 0.0
        elapsed = now - self._sustained_since
        if elapsed < self.sustained_hold_sec:
            return None, "", 0.0
        if (now - self._last_sustained_ts) < self.sustained_cooldown_sec:
            return None, "", 0.0
        if (now - self._last_nudge_ts) < self.cooldown_sec:
            return None, "", 0.0

        pan = state["servo_pan"]
        pan_mech = self._pan_mech(pan)
        raw = self._plan_base_follow_step(
            clamp(abs(pan_mech) * self.pan_offset_to_step, self.min_step, self.max_step),
            pan,
        )
        if raw is None:
            return None, "", 0.0
        step = raw * self.base_sign
        step = self._apply_gate(step, pan, state["base_encoder_deg"], state)
        if step == 0.0:
            return None, "", 0.0
        self._last_sustained_ts = now
        self._sustained_since = now
        comp_pan = self._comp_pan_for_step(step, pan, self.sustained_comp_gain)
        return step, "sustained_head", comp_pan

    def _plan_memory_step(self, now: float, state: dict) -> tuple[Optional[float], str, float]:
        if not self.pm_base_enabled:
            return None, "", 0.0
        if (now - self._last_pm_ts) < self.pm_base_cooldown:
            return None, "", 0.0
        snapshots = state["person_snapshots"]
        if not snapshots:
            return None, "", 0.0
        pan = state["servo_pan"]
        pan_mech = self._pan_mech(pan)
        world_yaw = state["base_world_yaw_deg"]
        best = min(snapshots, key=lambda s: abs(angular_error_deg(s["world_yaw_deg"], world_yaw)))
        err = angular_error_deg(best["world_yaw_deg"], world_yaw)
        if abs(err) < self.pm_base_min_yaw_err:
            return None, "", 0.0
        if not yaw_error_agrees_with_head(err, pan_mech, head_lead_min_deg=self.head_lead_min):
            return None, "", 0.0
        step = clamp(err * self.pm_base_gain, -self.pm_base_max, self.pm_base_max)
        if abs(step) < self.pm_base_min:
            return None, "", 0.0
        step = self._apply_gate(step * self.base_sign, pan, state["base_encoder_deg"], state)
        if step == 0.0:
            return None, "", 0.0
        comp_pan = self._comp_pan_for_step(step, pan, self.pm_base_comp)
        return step, "memory", comp_pan

    def _plan_last_seen_step(self, now: float, state: dict) -> tuple[Optional[float], str, float]:
        if not self.lss_base_enabled:
            return None, "", 0.0
        if (now - self._last_lss_ts) < self.lss_base_cooldown:
            return None, "", 0.0
        last_yaw = state["last_seen_world_yaw"]
        if last_yaw is None:
            return None, "", 0.0
        pan = state["servo_pan"]
        pan_mech = self._pan_mech(pan)
        world_yaw = state["base_world_yaw_deg"]
        err = angular_error_deg(last_yaw, world_yaw)
        if abs(err) < self.lss_base_min_yaw_err:
            return None, "", 0.0
        if not yaw_error_agrees_with_head(err, pan_mech, head_lead_min_deg=self.head_lead_min):
            return None, "", 0.0
        step = clamp(err * self.lss_base_gain, -self.lss_base_max, self.lss_base_max)
        if abs(step) < self.lss_base_min:
            return None, "", 0.0
        step = self._apply_gate(step * self.base_sign, pan, state["base_encoder_deg"], state)
        if step == 0.0:
            return None, "", 0.0
        comp_pan = self._comp_pan_for_step(step, pan, self.lss_base_comp)
        return step, "last_seen", comp_pan

    def _plan_proximity_step(
        self, now: float, state: dict,
    ) -> tuple[Optional[float], str, float]:
        """Turn base toward an approaching person detected by ToF sensors."""
        if not self.prox_enabled:
            return None, "", 0.0
        if not state.get("prox_approach_active", False):
            return None, "", 0.0
        # Suppress while face tracking is active (already engaged)
        if state.get("face_detected", False):
            return None, "", 0.0
        # Post-turn lockout
        if now < state.get("prox_post_turn_lockout_ts", 0.0):
            return None, "", 0.0
        # Post-motion blanking
        if (now - self._last_base_motion_done_ts) < self.prox_post_motion_blanking_sec:
            return None, "", 0.0
        # Cooldown
        if (now - self._last_prox_ts) < self.prox_cooldown_sec:
            return None, "", 0.0
        # Confidence gate
        if state.get("prox_approach_confidence", 0) < self.prox_min_confidence:
            return None, "", 0.0
        # Budget: max turns per window
        recent = [t for t in self._prox_turn_timestamps if now - t < self.prox_window_sec]
        if len(recent) >= self.prox_max_turns:
            return None, "", 0.0

        zone = state.get("prox_approach_zone", "")
        
        # Voice conversation: glance instead of base turn
        if state.get("voice_session_active", False):
            if zone in ("L", "R"):
                sign = 1.0 if zone == "L" else -1.0
                pan_offset = sign * self.prox_turn_step * 0.4
                glance_pan = state["servo_pan"] + pan_offset
                self.bb.write(
                    prox_glance_active=True,
                    prox_glance_target_pan=glance_pan,
                    prox_glance_phase="toward",
                    prox_glance_since=now,
                )
            return None, "", 0.0

        if zone == "L":
            step_sign = 1.0
        elif zone == "R":
            step_sign = -1.0
        elif zone == "C":
            self.bb.write(
                prox_search_active=True,
                prox_search_since=now,
                prox_search_zone="C",
            )
            return None, "", 0.0
        else:
            return None, "", 0.0

        step = step_sign * self.prox_turn_step * self.base_sign
        pan = state["servo_pan"]
        step = self._apply_gate(
            step, pan, state["base_encoder_deg"], state,
            require_head_lead=False,
        )
        if abs(step) < self.min_step:
            return None, "", 0.0

        # Commit: set lockout and record in budget
        self._last_prox_ts = now
        self._prox_turn_timestamps = [t for t in self._prox_turn_timestamps if now - t < self.prox_window_sec]
        self._prox_turn_timestamps.append(now)
        self.bb.write(
            prox_post_turn_lockout_ts=now + self.prox_lockout_sec,
            prox_search_active=True,
            prox_search_since=now,
            prox_search_zone=zone,
        )

        comp_pan = self._comp_pan_for_step(step, pan, self.prox_comp_gain)
        return step, "proximity", comp_pan

    # ── Main loop ──────────────────────────────────────────────────────────────

    def run(self) -> None:
        if not self.enabled:
            print("[BaseController] Base rotation disabled in config.")
            return

        loop_delay = 0.02  # 50 Hz
        self._gate.clear_backoff()

        while self.bb.read("running")["running"]:
            now = time.time()
            self._apply_live_tune_if_changed()

            state = self.bb.read(
                "face_detected", "face_norm_x", "face_area_ratio",
                "body_detected", "track_kind",
                "servo_pan", "servo_mode",
                "wander_moving", "wander_last_step_deg",
                "base_encoder_deg", "base_world_yaw_deg", "base_motion_busy",
                "base_encoder_synced",
                "base_motion_allowed",
                "person_snapshots", "last_seen_world_yaw",
                "imu_available", "imu_gyro_dps", "imu_gyro_z_dps",
                "imu_yaw_integral_deg",
                "yaw_reference_locked",
                "prox_approach_active", "prox_approach_zone",
                "prox_approach_confidence", "prox_post_turn_lockout_ts",
            )

            pan_offset = self._head_pan_offset(state["servo_pan"])
            self._yaw_state.update(state["base_encoder_deg"], pan_offset)
            self._update_sustained_hold(now, pan_offset)
            elapsed = 0.0 if self._sustained_since is None else (now - self._sustained_since)
            self.bb.write(
                base_sustained_hold_active=self._sustained_since is not None,
                base_sustained_hold_elapsed_sec=elapsed,
            )

            self._gate.clear_backoff(now)
            if self._gate.allowed(now):
                self.bb.write(base_motion_allowed=True)
            self.bb.write(base_fault_reason=self._gate.last_reason)

            if not state["base_motion_busy"] and self._was_base_busy:
                self._last_base_motion_done_ts = now
            self._was_base_busy = state["base_motion_busy"]

            if not state["yaw_reference_locked"]:
                time.sleep(loop_delay)
                continue

            if not state["base_encoder_synced"]:
                time.sleep(loop_delay)
                continue

            if state["base_motion_busy"]:
                time.sleep(loop_delay)
                continue

            mode = state["servo_mode"]
            step: Optional[float] = None
            source = ""
            comp_pan = 0.0

            if mode == "track" and state["face_detected"]:
                step, source, comp_pan = self._plan_track_step(now, state)
            elif mode == "track":
                step, source, comp_pan = self._plan_body_step(now, state)
            elif mode == "last_seen":
                step, source, comp_pan = self._plan_last_seen_step(now, state)
                if step is None:
                    step, source, comp_pan = self._plan_memory_step(now, state)
            elif mode == "wander":
                step, source, comp_pan = self._plan_wander_follow(now, state)

            if step is None and mode in ("wander", "track", "last_seen"):
                step, source, comp_pan = self._plan_sustained_follow(now, state)

            # Proximity approach: react in any mode when not already tracking
            if step is None and self.prox_enabled:
                step, source, comp_pan = self._plan_proximity_step(now, state)

            if step is not None and abs(step) >= self.min_step:
                self._last_nudge_ts = now
                if "memory" in source:
                    self._last_pm_ts = now
                if "last_seen" in source:
                    self._last_lss_ts = now
                if source == "body":
                    self._last_body_ts = now
                self._trigger_since = 0.0
                self.bb.write(
                    base_step_deg=step,
                    base_step_source=source,
                    base_step_ready=True,
                    base_comp_pan_deg=comp_pan,
                )
            else:
                if self.bb.read("base_step_ready")["base_step_ready"]:
                    self.bb.write(base_step_ready=False, base_comp_pan_deg=0.0)

            time.sleep(loop_delay)

        print("[BaseController] Stopped.")
