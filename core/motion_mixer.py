"""MotionMixer: Layered organic alive motion engine for Nema.

Implements Reachy Mini-inspired layered motion blending:
  - Layer 0 (Base): Asymmetric biological breathing & subtle chassis sway
  - Layer 1 (Speech): Syllable-reactive micro-wobble and conversational cadence
  - Layer 2 (Intent): Face tracking, gestures, and user interaction

Deltas from Layer 0 & 1 are blended additively and scaled dynamically based
on current robot engagement state (fading during rapid face tracking so PID
remains crisp, while keeping Nema continuously alive during idle and dialogue).
"""

from __future__ import annotations

import math
import time
from typing import TYPE_CHECKING

from lib.motion_curves import biological_respiration

if TYPE_CHECKING:
    from core.blackboard import Blackboard


class MotionMixer:
    """Computes organic motion offsets and blends layers into Blackboard state."""

    def __init__(
        self,
        bb: Blackboard,
        *,
        breathing_period_sec: float = 4.2,
        breathing_tilt_amp: float = 1.2,
        breathing_pan_amp: float = 0.5,
        breathing_pan_period_sec: float = 7.8,
        base_sway_amp: float = 0.35,
        base_sway_period_sec: float = 11.0,
        speech_wobble_gain: float = 2.4,
    ):
        self.bb = bb
        self.breathing_period = max(0.5, float(breathing_period_sec))
        self.breathing_tilt_amp = float(breathing_tilt_amp)
        self.breathing_pan_amp = float(breathing_pan_amp)
        self.breathing_pan_period = max(0.5, float(breathing_pan_period_sec))
        self.base_sway_amp = float(base_sway_amp)
        self.base_sway_period = max(1.0, float(base_sway_period_sec))
        self.speech_wobble_gain = float(speech_wobble_gain)

        self._current_weight = 1.0
        self._last_tick_ts = time.time()
        self._speech_phase = 0.0
        self._last_deltas: dict[str, float] | None = None

    def compute_deltas(self, now: float | None = None) -> dict[str, float]:
        """Compute current motion deltas without writing to Blackboard."""
        t = time.time() if now is None else float(now)

        state = self.bb.read(
            "motion_alive_enabled",
            "face_detected",
            "face_norm_x",
            "servo_mode",
            "agent_speaking",
            "user_speaking",
            "amplitude_fast",
            "base_motion_busy",
        )

        if not state.get("motion_alive_enabled", True):
            return {
                "breathing_delta_tilt": 0.0,
                "breathing_delta_pan": 0.0,
                "breathing_delta_base": 0.0,
                "speech_wobble_delta_tilt": 0.0,
                "speech_wobble_delta_pan": 0.0,
                "alive_blend_weight": 0.0,
            }

        # ── 1. Dynamic Engagement Weight ──────────────────────────────────────
        # When actively tracking an off-center moving face, attenuate breathing
        # so PID tracking stays sharp. When face is centered or idle, full alive weight.
        face_detected = bool(state.get("face_detected", False))
        face_norm_x = float(state.get("face_norm_x", 0.0) or 0.0)
        mode = str(state.get("servo_mode", "wander"))

        if face_detected and mode == "track":
            tracking_error = abs(face_norm_x)
            if tracking_error > 0.25:
                # Fast motion: reduce breathing influence
                target_weight = 0.25
            else:
                # Locked on face near center: maintain subtle breathing presence
                target_weight = 0.75
        else:
            # Idle, wander, or conversational listening: 100% alive
            target_weight = 1.0

        # Smooth weight transition
        dt = max(0.001, min(0.1, t - self._last_tick_ts))
        self._last_tick_ts = t
        weight_smooth_rate = 3.0  # 1/sec
        self._current_weight += (target_weight - self._current_weight) * min(1.0, dt * weight_smooth_rate)

        # ── 2. Layer 0: Biological Respiration & Chassis Sway ─────────────────
        # Asymmetric vertical breathing
        resp_norm = biological_respiration(t, period=self.breathing_period)
        raw_tilt_resp = resp_norm * self.breathing_tilt_amp

        # Micro-drift in yaw (different frequency so head path forms subtle figure-8)
        raw_pan_drift = math.sin((2.0 * math.pi * t) / self.breathing_pan_period) * self.breathing_pan_amp

        # Chassis posture sway (suppressed if base motor is currently spinning)
        if state.get("base_motion_busy", False) or face_detected:
            raw_base_sway = 0.0
        else:
            raw_base_sway = math.sin((2.0 * math.pi * t) / self.base_sway_period) * self.base_sway_amp

        delta_tilt = raw_tilt_resp * self._current_weight
        delta_pan = raw_pan_drift * self._current_weight
        delta_base = raw_base_sway * self._current_weight

        # ── 3. Layer 1: Speech-Reactive Head Wobble ───────────────────────────
        wobble_tilt = 0.0
        wobble_pan = 0.0
        agent_speaking = bool(state.get("agent_speaking", False))

        if agent_speaking:
            amp = float(state.get("amplitude_fast", 0.0) or 0.0)
            effective_amp = max(abs(amp), 0.28)
            # Syllable cadence oscillation (~4.5 Hz conversational pacing)
            self._speech_phase = (self._speech_phase + dt * 4.5 * 2.0 * math.pi) % (2.0 * math.pi)
            
            # Subtly dip tilt on emphasis (human nodding gesture while speaking)
            wobble_tilt = -effective_amp * self.speech_wobble_gain * (0.6 + 0.4 * math.sin(self._speech_phase))
            # Micro-pan accent
            wobble_pan = math.sin(self._speech_phase * 0.5) * effective_amp * 0.8

        return {
            "breathing_delta_tilt": delta_tilt,
            "breathing_delta_pan": delta_pan,
            "breathing_delta_base": delta_base,
            "speech_wobble_delta_tilt": wobble_tilt,
            "speech_wobble_delta_pan": wobble_pan,
            "alive_blend_weight": self._current_weight,
        }

    def tick(self, now: float | None = None) -> dict[str, float]:
        """Compute deltas and commit them atomically to Blackboard."""
        deltas = self.compute_deltas(now)
        self._last_deltas = deltas
        self.bb.write(**deltas)
        return deltas

    def get_blended_pose(self, commanded_pan: float, commanded_tilt: float) -> tuple[float, float]:
        """Add active Layer 0 and Layer 1 offsets to commanded head servo targets."""
        deltas = self._last_deltas if self._last_deltas is not None else self.compute_deltas()
        total_delta_pan = deltas["breathing_delta_pan"] + deltas["speech_wobble_delta_pan"]
        total_delta_tilt = deltas["breathing_delta_tilt"] + deltas["speech_wobble_delta_tilt"]
        return commanded_pan + total_delta_pan, commanded_tilt + total_delta_tilt
