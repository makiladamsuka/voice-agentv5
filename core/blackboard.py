"""Thread-safe shared state bus for the face tracking robot.

All inter-module communication flows through this object.
No business logic lives here — only typed storage and atomic read/write.
"""

from __future__ import annotations

import threading
from typing import Any


class Blackboard:
    """Central shared-memory object.

    Usage::

        bb = Blackboard()
        bb.write(face_detected=True, face_norm_x=0.12)
        state = bb.read("face_norm_x", "servo_pan")
        x = state["face_norm_x"]
    """

    # ── Vision (written by FaceTracker) ──────────────────────────────────────
    face_detected: bool = False
    face_norm_x: float = 0.0       # +1.0 (right) … -1.0 (left) vs frame center
    face_norm_y: float = 0.0       # -1.0 (top)  … +1.0 (bottom)
    face_roll_deg: float = 0.0
    face_area_ratio: float = 0.0
    face_count: int = 0
    face_candidates: list = None
    body_detected: bool = False
    track_kind: str = "none"       # "face"|"multi"|"body"|"none"
    stream_frame: Any = None       # latest numpy frame for MJPEG

    # ── Servo targets (written by ServoLoop) ─────────────────────────────────
    servo_pan: float = 80.0        # servo command degrees
    servo_tilt: float = 110.0
    servo_mode: str = "wander"     # "track"|"wander"|"memory_track"|"last_seen"
    servo_forward_return_active: bool = False
    servo_pan_hold: bool = False
    wander_moving: bool = False    # head is gliding to a wander target (ServoLoop)
    wander_last_step_deg: float = 0.0  # last head wander step size

    # ── Hands (written by GestureEngine/Animation) ───────────────────────────
    hand_a0: float = 0.0     # Right raise (0=neutral..180)
    hand_a1: float = 180.0   # Left raise (180=neutral..0)
    hand_a2: float = 90.0    # Right sweep (centered)
    hand_a3: float = 90.0    # Left sweep (centered)
    hand_priority: str = "living"  # "living" | "agent"

    # ── Base rotation (written by BaseController / ServoMixer) ────────────────
    base_step_deg: float = 0.0
    base_step_source: str = ""
    base_step_ready: bool = False  # BaseController signals a new step is pending
    base_world_yaw_deg: float = 0.0
    base_encoder_deg: float = 0.0
    base_encoder_synced: bool = False
    base_motion_busy: bool = False
    base_motion_allowed: bool = True
    base_fault_reason: str | None = None
    base_watchdog_reset: bool = False
    base_comp_pan_deg: float = 0.0   # proactive neck counter-rotation (servo pan cmd)
    base_sustained_hold_active: bool = False
    base_sustained_hold_elapsed_sec: float = 0.0
    base_last_spin_moved_deg: float = 0.0
    base_last_spin_reason: str = ""
    base_fusion_resync_request: bool = False

    # ── Yaw reference (startup lock) ─────────────────────────────────────────
    yaw_reference_locked: bool = False
    imu_calibrated: bool = False
    imu_inferred_base_deg: float = 0.0
    body_yaw_deg: float = 0.0
    head_yaw_on_body_deg: float = 0.0
    imu_yaw_rel_deg: float = 0.0
    head_imu_vs_servo_delta_deg: float = 0.0
    imu_pitch_deg: float = 0.0
    imu_roll_deg: float = 0.0
    imu_gyro_dps: float = 0.0
    imu_gyro_z_dps: float = 0.0
    imu_yaw_integral_deg: float = 0.0
    imu_accel_trusted: bool = True
    imu_horizon_ok: bool = True
    imu_available: bool = False    # False until ImuService confirms hardware
    imu_effective_tilt_center: float = 114.0

    # ── Person Memory (written by FaceTracker) ─────────────────────────────
    person_snapshots: list = None  # list[dict] from PersonMemory.snapshots()
    last_seen_world_yaw: float = None  # world yaw of last-seen-at-edge position

    # ── Emotion (written by EmotionEngine) ───────────────────────────────────
    emotion: str = "idle"
    emotion_intensity: float = 1.0
    manual_emotion: str = None     # set via terminal/API override

    # ── Debug manual control (written by viz POST /api/control) ───────────────
    manual_control_enabled: bool = False
    debug_control_cmd: str = ""
    debug_control_seq: int = 0
    debug_head_step_deg: float = 5.0
    debug_live_tune: dict = None
    debug_tune_seq: int = 0
    imu_yaw_raw_deg: float = 0.0
    imu_drift_correction_deg: float = 0.0
    fusion_stationary: bool = False
    imu_drift_reset_request: bool = False

    # ── Control ──────────────────────────────────────────────────────────────
    running: bool = True

    def __init__(self) -> None:
        self._lock = threading.Lock()
        # Mutable defaults that can't be class-level
        self.face_candidates = []
        self.person_snapshots = []
        self.debug_live_tune = {}

    # ─────────────────────────────────────────────────────────────────────────

    def write(self, **kwargs: Any) -> None:
        """Atomically set one or more fields."""
        with self._lock:
            for key, value in kwargs.items():
                if not hasattr(self, key):
                    raise AttributeError(f"Blackboard has no field '{key}'")
                setattr(self, key, value)

    def read(self, *fields: str) -> dict[str, Any]:
        """Atomically read one or more fields. Returns a plain dict."""
        with self._lock:
            return {f: getattr(self, f) for f in fields}

    def read_all(self) -> dict[str, Any]:
        """Read every public field atomically (for debug / status)."""
        with self._lock:
            return {
                k: getattr(self, k)
                for k in vars(self.__class__)
                if not k.startswith("_") and not callable(getattr(self.__class__, k))
            }
