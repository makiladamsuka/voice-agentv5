"""Thread-safe shared state for the greeting robot.

Every module reads from and writes to this blackboard.
No module imports another module — they all go through here.

Field ownership (one writer per field):
    face_tracking  → face_*, body_*, track_kind
    servo_loop     → servo_pan, servo_tilt, servo_mode
    emotion_engine → emotion, emotion_source, emotion_intensity  (life force)
    voice_agent    → session_active, conv_state                  (overrides emotion)
    amplitude_tts  → amplitude_fast, amplitude_slow
    conv_overlay   → conv_pan_offset, conv_tilt_offset
    anim_player    → anim_*
    tof_presence   → tof_*
"""

from __future__ import annotations

import threading
from dataclasses import dataclass, field
from typing import Any, Callable


@dataclass
class Blackboard:
    """Shared robot state.  All public fields are the data contract."""

    _lock: threading.Lock = field(default_factory=threading.Lock, repr=False)
    _subscribers: dict[str, list[Callable]] = field(
        default_factory=dict, repr=False,
    )

    # ─── Vision (writer: face_tracking) ─────────────────────────
    face_detected: bool = False
    face_norm_x: float = 0.0        # -1 (camera right) → +1 (camera left)
    face_norm_y: float = 0.0        # -1 (top) → +1 (bottom)
    face_roll_deg: float = 0.0
    face_count: int = 0
    face_area_ratio: float = 0.0
    face_candidates: list = field(default_factory=list)
    body_detected: bool = False
    track_kind: str = "none"         # face | body | center | multi | none

    # ─── Servo state (writer: servo_loop) ───────────────────────
    servo_pan: float = 80.0          # current pan degrees
    servo_tilt: float = 110.0        # current tilt degrees
    servo_mode: str = "wander"       # track | predict | lost_search | wander
    servo_target_pan: float = 80.0   # PID target (before smoothing)
    servo_target_tilt: float = 110.0

    # ─── Emotion (writer: emotion_engine OR voice_agent) ────────
    emotion: str = "idle"
    emotion_source: str = "life_force"   # life_force | conversation | animation
    emotion_intensity: float = 1.0

    # ─── Voice session (writer: voice_agent) ────────────────────
    session_active: bool = False
    conv_state: str = "idle"         # idle | listening | speaking | thinking | waiting

    # ─── TTS amplitude (writer: amplitude_tts) ──────────────────
    amplitude_fast: float = 0.0      # syllable-level energy  0.0–1.0
    amplitude_slow: float = 0.0      # phrase-level energy    0.0–1.0

    # ─── Conversation overlay (writer: conversation_overlay) ────
    conv_pan_offset: float = 0.0     # nod/sway pan nudge in degrees
    conv_tilt_offset: float = 0.0    # nod/sway tilt nudge in degrees

    # ─── Animation (writer: animation_player) ───────────────────
    anim_active: bool = False
    anim_clip_id: str = ""
    anim_pan_override: float | None = None   # None = don't override
    anim_tilt_override: float | None = None
    anim_arms: dict = field(default_factory=dict)   # e.g. {"arm_0": 90.0}
    anim_blend_weight: float = 0.45  # how much animation overrides base

    # ─── ToF presence (writer: tof_presence) ────────────────────
    tof_any_present: bool = False
    tof_approach_side: str = "none"  # left | center | right | none

    # ─── Stream frame (writer: face_tracking, reader: MJPEG) ───
    stream_frame: Any = None         # numpy array or None

    # ─── System (writer: start_robot) ───────────────────────────
    running: bool = True

    # ─────────────────────────────────────────────────────────────
    # API
    # ─────────────────────────────────────────────────────────────

    def write(self, **kwargs: Any) -> None:
        """Atomic update of one or more fields + notify subscribers."""
        with self._lock:
            changed: dict[str, Any] = {}
            for key, value in kwargs.items():
                if not hasattr(self, key) or key.startswith("_"):
                    raise KeyError(f"Blackboard has no field '{key}'")
                setattr(self, key, value)
                changed[key] = value
        # Notify outside lock to avoid deadlocks in callbacks.
        for key, value in changed.items():
            for cb in self._subscribers.get(key, []):
                try:
                    cb(key, value)
                except Exception:
                    pass  # subscribers must not crash the writer

    def read(self, *keys: str) -> dict[str, Any]:
        """Atomic snapshot of requested fields."""
        with self._lock:
            return {k: getattr(self, k) for k in keys}

    def snapshot(self) -> dict[str, Any]:
        """Full snapshot of all public fields (for debug / logging)."""
        with self._lock:
            return {
                k: getattr(self, k)
                for k in self.__dataclass_fields__
                if not k.startswith("_")
            }

    def subscribe(self, key: str, callback: Callable[[str, Any], None]) -> None:
        """Register a callback for when a field changes.

        Callbacks run on the writer's thread — keep them fast.
        """
        self._subscribers.setdefault(key, []).append(callback)
