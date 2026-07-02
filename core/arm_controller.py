"""ArmController: cumulative arm lean per base spin; pose persists until next spin."""

from __future__ import annotations

import json
import time
from pathlib import Path

try:
    import yaml
except ImportError:
    yaml = None

from arm_pose_presets import ArmPosePresets, DEFAULT_PRESETS_PATH
from arm_safety_envelope import ArmSafetyEnvelope, DEFAULT_LIMITS_PATH
from core.blackboard import Blackboard
from lib.arm_base_lean import lean_delta_per_spin
from lib.elastic_head_motion import smooth_toward

# Greeting pose parameters
GREETING_DURATION_SEC = 2.0  # How long to hold greeting pose
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
        self.min_spin_moved_deg = float(a.get("min_spin_moved_deg", 2.0))

        self._target = list(self._home)
        self._current = list(self._home)
        self._was_busy = False
        self._pending_step_deg = 0.0

        # Greeting state
        self._presets = presets
        self._last_greeting_seq = 0
        self._greeting_start_time: float | None = None
        self._greeting_pose: tuple[float, float, float, float] | None = None
        self._pre_greeting_target = list(self._home)

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

    def _start_greeting(self, pose_name: str) -> None:
        """Start a greeting pose sequence."""
        pose = self._presets.get(pose_name)
        if pose is None:
            print(f"[ArmController] Warning: greeting pose '{pose_name}' not found, using home")
            pose = self._home
        
        # Save current target to return to after greeting
        self._pre_greeting_target = list(self._target)
        
        # Set greeting pose as target
        self._greeting_pose = pose
        self._greeting_start_time = time.time()
        
        print(f"[ArmController] Starting greeting: {pose_name} → {pose}")
        self.bb.write(arm_greeting_active=True)

    def _update_greeting(self, now: float) -> bool:
        """Update greeting state. Returns True if greeting is active."""
        if self._greeting_start_time is None:
            return False
        
        elapsed = now - self._greeting_start_time
        
        if elapsed < GREETING_DURATION_SEC:
            # Still greeting - set target to greeting pose
            if self._greeting_pose is not None:
                self._target[:] = list(self._greeting_pose)
            return True
        else:
            # Greeting complete - return to pre-greeting pose
            self._target[:] = list(self._pre_greeting_target)
            self._greeting_start_time = None
            self._greeting_pose = None
            print("[ArmController] Greeting complete, returning to previous pose")
            self.bb.write(arm_greeting_active=False)
            return False

    def run(self) -> None:
        if not self.enabled:
            print("[ArmController] Disabled in config.")
            return

        loop_delay = 1.0 / max(1.0, self.loop_hz)
        print(
            f"[ArmController] Cumulative lean + greeting gestures "
            f"(+{self.step_delta_deg:.1f}°/spin, mid A0≤{self._raise_mid[0]:.0f} "
            f"A1≥{self._raise_mid[1]:.0f}, home={self._home})"
        )

        while self.bb.read("running")["running"]:
            t0 = time.time()
            now = time.time()

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
            if self.bb.read("bye_wave_active")["bye_wave_active"] or greeting_active:
                # During greeting, use faster smoothing
                smooth_hz = GREETING_SMOOTH_HZ if greeting_active else self.blend_hz
                for i in range(4):
                    self._current[i] = smooth_toward(
                        self._current[i],
                        self._target[i],
                        loop_delay,
                        smooth_hz=smooth_hz,
                        lo=-360.0,
                        hi=360.0,
                    )
                pose = self._clamp_accum(*self._current)
                self._current[:] = list(pose)
                self._publish_pose(pose)
                time.sleep(loop_delay)
                continue

            state = self.bb.read(
                "base_motion_busy",
                "base_step_deg",
                "base_last_spin_moved_deg",
            )
            busy = bool(state["base_motion_busy"])
            step_deg = float(state["base_step_deg"])

            if busy and not self._was_busy:
                self._pending_step_deg = step_deg

            if self._was_busy and not busy:
                moved = abs(float(state["base_last_spin_moved_deg"]))
                if moved >= self.min_spin_moved_deg:
                    self._accumulate_spin(self._pending_step_deg)

            for i in range(4):
                self._current[i] = smooth_toward(
                    self._current[i],
                    self._target[i],
                    loop_delay,
                    smooth_hz=self.blend_hz,
                    lo=-360.0,
                    hi=360.0,
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
