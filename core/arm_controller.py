"""ArmController: cumulative arm lean per base spin; pose persists until next spin."""

from __future__ import annotations

import json
import time
import random
from pathlib import Path

try:
    import yaml
except ImportError:
    yaml = None

from arm_pose_presets import ArmPosePresets, DEFAULT_PRESETS_PATH
from arm_safety_envelope import ArmSafetyEnvelope, DEFAULT_LIMITS_PATH
from core.blackboard import Blackboard
from lib.arm_base_lean import lean_delta_per_spin
from lib.elastic_head_motion import tick_toward, HeadMotionParams

# Greeting pose parameters
GREETING_SMOOTH_HZ = 8.0     # Faster transition to greeting pose

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


def _clamp(v: float, lo: float, hi: float) -> float:
    return max(lo, min(hi, v))


class ArmController:
    """Accumulate small lean steps per base spin; hold pose between spins."""

    def __init__(
        self,
        bb: Blackboard,
        config_path: Path = DEFAULT_CONFIG_PATH,
    ) -> None:
        self.bb = bb
        cfg = _load_yaml(config_path)
        a = _cfg(cfg, "arms", default={}) or {}

        self.enabled = bool(a.get("enabled", False))
        presets_path = Path(a.get("presets_path", DEFAULT_PRESETS_PATH))
        if not presets_path.is_absolute():
            presets_path = APP_DIR / presets_path
        limits_path = Path(a.get("limits_path", DEFAULT_LIMITS_PATH))
        if not limits_path.is_absolute():
            limits_path = APP_DIR / limits_path

        self.envelope = ArmSafetyEnvelope.from_json(limits_path)
        base_pose = str(a.get("base_pose", "home"))
        presets = ArmPosePresets.load_or_create_home(presets_path, home=self.envelope.homes)
        self._home = presets.get(base_pose)

        limits_data = json.loads(limits_path.read_text(encoding="utf-8"))
        mins = tuple(float(v) for v in limits_data["min"])
        maxs = tuple(float(v) for v in limits_data["max"])
        self._raise_min = (mins[0], mins[1])
        self._raise_max = (maxs[0], maxs[1])
        self._raise_mid = (
            (mins[0] + maxs[0]) * 0.5,
            (mins[1] + maxs[1]) * 0.5,
        )

        self.step_delta_deg = float(a.get("turn_step_delta_deg", 4.0))
        self.turn_sign = float(a.get("turn_sign", 1.0))
        self.ref_step_deg = float(a.get("ref_step_deg", 8.0))
        self.sweep_factor = float(a.get("turn_sweep_factor", 0.45))
        self.blend_hz = float(a.get("blend_hz", 5.0))
        self.loop_hz = float(a.get("loop_hz", 50.0))
        vp = _cfg(cfg, "voice_profile", default={}) or {}
        self.voice_loop_hz = float(vp.get("arms_loop_hz", min(self.loop_hz, 14.0)))
        self.min_spin_moved_deg = float(a.get("min_spin_moved_deg", 2.0))

        self._target = list(self._home)
        self._current = list(self._home)
        self._velocity = [0.0, 0.0, 0.0, 0.0]
        self._was_busy = False
        self._pending_step_deg = 0.0

        # Velocity-based arm motion params (accel / decel for smoothness)
        self._arm_params = HeadMotionParams(
            max_vel_pos=float(a.get("arm_max_vel", 50.0)),
            max_vel_neg=float(a.get("arm_max_vel", 50.0)),
            accel=float(a.get("arm_accel", 120.0)),
            decel=float(a.get("arm_decel", 150.0)),
            goal_deadband_deg=float(a.get("arm_deadband_deg", 0.1)),
            track_gain=float(a.get("arm_track_gain", 6.0)),
        )
        self._greeting_arm_params = HeadMotionParams(
            max_vel_pos=float(a.get("arm_greeting_max_vel", 80.0)),
            max_vel_neg=float(a.get("arm_greeting_max_vel", 80.0)),
            accel=float(a.get("arm_greeting_accel", 200.0)),
            decel=float(a.get("arm_greeting_decel", 250.0)),
            goal_deadband_deg=float(a.get("arm_deadband_deg", 0.1)),
            track_gain=float(a.get("arm_greeting_track_gain", 10.0)),
        )

        # Greeting speed multipliers from face_greeting_arm
        fg = _cfg(cfg, "face_greeting_arm", default={}) or {}
        self.greeting_vertical_speed = float(fg.get("vertical_speed", 1.0))
        self.greeting_horizontal_speed = float(fg.get("horizontal_speed", 1.0))

        # Greeting state
        self._presets = presets
        self._last_greeting_seq = 0
        self._greeting_start_time: float | None = None
        self._greeting_phase: int = 0  # 0=UP, 1=HOLD, 2=DOWN
        self._greeting_pose: tuple[float, float, float, float] | None = None
        self._pre_greeting_target = list(self._home)
        
        # Wander arm state
        self.pan_center = float(_cfg(cfg, "servo", "pan_center", default=100.0))

        self._publish_pose(self._home)

    def _publish_pose(self, pose: tuple[float, float, float, float]) -> None:
        self.bb.write(
            arm_a0=pose[0],
            arm_a1=pose[1],
            arm_a2=pose[2],
            arm_a3=pose[3],
        )

    def _clamp_accum(self, a0: float, a1: float, a2: float, a3: float) -> tuple[float, float, float, float]:
        """Safety envelope + raise capped at midpoint between min and max."""
        a0, a1, a2, a3 = self.envelope.clamp_arms(a0, a1, a2, a3)
        a0 = _clamp(a0, self._raise_min[0], self._raise_mid[0])
        a1 = _clamp(a1, self._raise_mid[1], self._raise_max[1])
        return self.envelope.clamp_arms(a0, a1, a2, a3)

    def _accumulate_spin(self, step_deg: float) -> None:
        d = lean_delta_per_spin(
            step_deg,
            step_delta_deg=self.step_delta_deg,
            turn_sign=self.turn_sign,
            ref_step_deg=self.ref_step_deg,
            sweep_factor=self.sweep_factor,
        )
        pose = [self._target[i] + d[i] for i in range(4)]
        clamped = self._clamp_accum(*pose)
        self._target[:] = list(clamped)

    def _clamp_greeting(self, a0: float, a1: float, a2: float, a3: float) -> tuple[float, float, float, float]:
        """Clamp a greeting/hi pose through the safety envelope only.

        Intentionally does NOT apply the _raise_mid half-range limiter that
        _clamp_accum uses for the lean accumulator — greeting poses like hi1/hi2
        require a1 values below raise_mid that would otherwise be silently
        clipped, preventing the arm from ever reaching the recorded position.
        """
        return self.envelope.clamp_arms(a0, a1, a2, a3)

    def _start_greeting(self, pose_name: str) -> None:
        """Start a greeting pose sequence."""
        pose = self._presets.get(pose_name)
        if pose is None:
            print(f"[ArmController] Warning: greeting pose '{pose_name}' not found, using home")
            pose = self._home

        # Clamp through safety envelope only — NOT the lean raise-mid limiter
        pose = self._clamp_greeting(*pose)

        # Save current target to return to after greeting
        self._pre_greeting_target = list(self._target)

        # Set greeting pose as target
        self._greeting_pose = pose
        self._greeting_start_time = time.time()
        self._greeting_phase = 0
        self._greeting_duration_sec = random.uniform(2.0, 3.0)

        print(f"[ArmController] Starting greeting: {pose_name} → {pose} (holding for {self._greeting_duration_sec:.1f}s)")
        self.bb.write(arm_greeting_active=True)

    def _update_greeting(self, now: float) -> bool:
        """Update greeting state. Returns True if greeting is active."""
        if self._greeting_start_time is None:
            return False

        elapsed = now - self._greeting_start_time

        if self._greeting_phase == 0:
            # PHASE 0: Moving UP
            # Give it a generous fixed time to go up (e.g., 3.0s) to account for slow vertical_speed
            if elapsed >= 3.0:
                self._greeting_phase = 1
                self._greeting_start_time = now
            elif self._greeting_pose is not None:
                self._target[:] = list(self._greeting_pose)
            return True
            
        elif self._greeting_phase == 1:
            # PHASE 1: Holding at the top
            if elapsed >= self._greeting_duration_sec:
                self._greeting_phase = 2
                self._greeting_start_time = now
                # Set target back to home, so tick_toward smoothly brings it down
                self._target[:] = list(self._pre_greeting_target)
            elif self._greeting_pose is not None:
                self._target[:] = list(self._greeting_pose)
            return True
            
        elif self._greeting_phase == 2:
            # PHASE 2: Moving DOWN (back to home)
            # Smoothly travel back home. Give it 3.0s to arrive before releasing control
            if elapsed >= 3.0:
                self._greeting_start_time = None
                self._greeting_pose = None
                print("[ArmController] Greeting complete, returning to normal tracking")
                self.bb.write(arm_greeting_active=False)
                return False
            return True

        return False

    def run(self) -> None:
        if not self.enabled:
            print("[ArmController] Disabled in config.")
            return

        print(
            f"[ArmController] Cumulative lean + greeting gestures "
            f"(+{self.step_delta_deg:.1f}°/spin, mid A0≤{self._raise_mid[0]:.0f} "
            f"A1≥{self._raise_mid[1]:.0f}, home={self._home}, "
            f"loop={self.loop_hz:.0f}Hz voice={self.voice_loop_hz:.0f}Hz)"
        )

        while self.bb.read("running")["running"]:
            t0 = time.time()
            now = time.time()
            voice_active = self.bb.read("voice_session_active")["voice_session_active"]
            hz = self.voice_loop_hz if voice_active else self.loop_hz
            loop_delay = 1.0 / max(1.0, hz)

            # Check for new greeting request
            greeting_state = self.bb.read("arm_greeting_seq", "arm_greeting_pose")
            greeting_seq = greeting_state["arm_greeting_seq"]
            if greeting_seq != self._last_greeting_seq:
                self._last_greeting_seq = greeting_seq
                pose_name = greeting_state["arm_greeting_pose"]
                if pose_name:
                    self._start_greeting(pose_name)

            # Update greeting if active
            greeting_active = self._update_greeting(now)

            # Skip base lean accumulation during bye wave or greeting
            bye_wave_active = self.bb.read("bye_wave_active")["bye_wave_active"]

            if bye_wave_active:
                # ByeWaveService owns arm_a0..arm_a3 while playing — do NOT publish
                # here or we will fight the animation thread and cause toggling jitter.
                # Just track what the animation is writing so we have a sensible
                # starting point when it finishes.
                current_arm = self.bb.read("arm_a0", "arm_a1", "arm_a2", "arm_a3")
                self._current[0] = float(current_arm["arm_a0"])
                self._current[1] = float(current_arm["arm_a1"])
                self._current[2] = float(current_arm["arm_a2"])
                self._current[3] = float(current_arm["arm_a3"])
                self._velocity = [0.0, 0.0, 0.0, 0.0]
                time.sleep(loop_delay)
                continue

            # Yield to TalkGestureService while it is active — same pattern
            # as bye_wave: passively track BB values so we resume cleanly.
            talk_active = self.bb.read("talk_gesture_active").get("talk_gesture_active", False)
            if talk_active:
                current_arm = self.bb.read("arm_a0", "arm_a1", "arm_a2", "arm_a3")
                self._current[0] = float(current_arm["arm_a0"])
                self._current[1] = float(current_arm["arm_a1"])
                self._current[2] = float(current_arm["arm_a2"])
                self._current[3] = float(current_arm["arm_a3"])
                self._target[:] = list(self._current)
                self._velocity = [0.0, 0.0, 0.0, 0.0]
                time.sleep(loop_delay)
                continue

            if greeting_active:
                # During greeting, blend smoothly toward the greeting target
                # using velocity-based accel/decel for natural motion.
                # Use envelope-only clamp (not the lean raise-mid limiter) so
                # hi poses with low a1 values actually reach their target.
                for i in range(4):
                    # i=0,1 (a0, a1) are vertical/shoulder; i=2,3 (a2, a3) are horizontal/elbow/wrist
                    speed_factor = self.greeting_vertical_speed if i < 2 else self.greeting_horizontal_speed
                    effective_dt = loop_delay * max(0.01, speed_factor)

                    self._current[i], self._velocity[i] = tick_toward(
                        self._current[i],
                        self._velocity[i],
                        self._target[i],
                        effective_dt,
                        lo=-360.0,
                        hi=360.0,
                        params=self._greeting_arm_params,
                    )
                pose = self._clamp_greeting(*self._current)
                self._current[:] = list(pose)
                self._publish_pose(pose)
                time.sleep(loop_delay)
                continue

            state = self.bb.read(
                "base_motion_busy",
                "base_step_deg",
                "base_last_spin_moved_deg",
                "servo_pan",
                "servo_mode",
            )
            busy = bool(state.get("base_motion_busy", False))
            step_deg = float(state.get("base_step_deg", 0.0))
            servo_pan = float(state.get("servo_pan", self.pan_center))
            servo_mode = str(state.get("servo_mode", ""))

            if busy and not self._was_busy:
                self._pending_step_deg = step_deg

            if self._was_busy and not busy:
                moved = abs(float(state.get("base_last_spin_moved_deg", 0.0)))
                if moved >= self.min_spin_moved_deg:
                    self._accumulate_spin(self._pending_step_deg)

            effective_target = list(self._target)

            # Apply wander arm gesture offset
            if servo_mode == "wander" and not greeting_active and not bye_wave_active and not talk_active:
                pan_offset = servo_pan - self.pan_center
                max_raise = 15.0
                raise_amount = min(abs(pan_offset) * 0.5, max_raise)
                
                if pan_offset > 0:
                    effective_target[0] += raise_amount
                elif pan_offset < 0:
                    effective_target[1] -= raise_amount

            for i in range(4):
                self._current[i], self._velocity[i] = tick_toward(
                    self._current[i],
                    self._velocity[i],
                    effective_target[i],
                    loop_delay,
                    lo=-360.0,
                    hi=360.0,
                    params=self._arm_params,
                )

            pose = self._clamp_accum(*self._current)
            self._current[:] = list(pose)
            self._publish_pose(pose)
            self._was_busy = busy

            elapsed = time.time() - t0
            time.sleep(max(0.0, loop_delay - elapsed))

        print("[ArmController] Stopped.")

    @property
    def home_pose(self) -> tuple[float, float, float, float]:
        return self._home
