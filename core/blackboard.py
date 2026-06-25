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

    # ── Arm targets (written by ArmController, sent by ServoMixer) ───────────
    arm_a0: float = 47.0
    arm_a1: float = 65.0
    arm_a2: float = 64.0
    arm_a3: float = 87.0

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
    base_spin_active: bool = False

    # ── Yaw reference (startup lock) ─────────────────────────────────────────
    yaw_reference_locked: bool = False
    imu_calibrated: bool = False
    imu_inferred_base_deg: float = 0.0
    body_yaw_deg: float = 0.0
    head_yaw_on_body_deg: float = 0.0
    imu_yaw_rel_deg: float = 0.0
    head_imu_vs_servo_delta_deg: float = 0.0
    true_front_heading_deg: float = 0.0
    true_front_body_deg: float = 0.0
    fusion_head_pan_error_deg: float = 0.0
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
    motion_snapshots: list = None    # list[dict] from MotionMemory (ToF ghosts)

    # ── Proximity Sensing (written by ServoMixer from ESP32 PROX lines) ───
    prox_approach_zone: str = ""          # "" | "L" | "C" | "R"
    prox_approach_velocity: float = 0.0   # mm/s (negative = approaching)
    prox_approach_distance: int = 0       # mm
    prox_approach_confidence: int = 0     # consecutive confirm frames
    prox_approach_active: bool = False    # True when valid approach detected
    prox_approach_ts: float = 0.0        # timestamp of last PROX event
    prox_depart_zone: str = ""           # zone someone departed from
    prox_depart_active: bool = False
    prox_depart_ts: float = 0.0
    prox_zone_left: bool = False         # someone lingering in left zone
    prox_zone_center: bool = False
    prox_zone_right: bool = False
    prox_zone_count: int = 0             # how many zones occupied (0-3)
    prox_post_turn_lockout_ts: float = 0.0  # suppress prox turns until this time
    prox_search_active: bool = False
    prox_search_since: float = 0.0
    prox_search_zone: str = ""
    prox_glance_active: bool = False
    prox_glance_target_pan: float = 0.0
    prox_glance_phase: str = ""
    prox_glance_since: float = 0.0
    prox_glance_emotion: str = ""       # short-lived looking_left/right during track glance
    prox_investigate_active: bool = False
    prox_investigate_phase: str = ""    # "" | "turn" | "scan" | "done"
    prox_investigate_zone: str = ""
    prox_investigate_yaw: float = 0.0
    prox_investigate_since: float = 0.0
    prox_investigate_motion_id: int = 0
    prox_verified_priority_yaw: float = None
    prox_scan_complete_ts: float = 0.0

    # ── Emotion (written by EmotionEngine) ───────────────────────────────────
    emotion: str = "idle"
    emotion_intensity: float = 1.0
    manual_emotion: str = None     # set via terminal/API override

    # ── Voice / Conversation (written by VoiceService) ────────────────────────
    # Layer 2 priority: overrides surroundings emotion when voice session active.
    voice_session_active: bool = False       # True when LiveKit room is connected
    conv_state: str = "idle"                 # "idle"|"listening"|"speaking"|"thinking"|"nodding"|"waiting"|"remembering"
    conv_emotion: str = None                 # VADER-derived emotion (overrides surroundings when set)
    amplitude_fast: float = 0.0              # TTS RMS fast signal (syllable punches)
    amplitude_slow: float = 0.0              # TTS RMS slow signal (emotional momentum)
    user_speaking: bool = False              # True when user VAD triggers
    agent_speaking: bool = False             # True when agent TTS is playing

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
        self.motion_snapshots = []
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
