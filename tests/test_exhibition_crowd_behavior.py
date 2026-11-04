"""Unit tests for exhibition crowd HRI & ToF gathering resilience."""

from __future__ import annotations

from core.base_controller import BaseController
from core.blackboard import Blackboard
from core.servo_loop import ServoLoop
from core.servo_mixer import ServoMixer
from core.surroundings_emotion import SurroundingsEmotionController
from lib.tof_multi_track import MultiTrackTracker
from voice.greetings import generate_random_face_greeting


def test_servo_mixer_prox_sets_surrounded_and_too_close_safety():
    bb = Blackboard()
    mixer = ServoMixer(bb, None)

    mixer._handle_prox_line("ZONE L=1 C=0 R=1")
    state = bb.read("prox_zone_count", "tof_surrounded")
    assert state["prox_zone_count"] == 2
    assert state["tof_surrounded"] is True

    mixer._handle_prox_line("ZONE L=0 C=1 R=0")
    state = bb.read("prox_zone_count", "tof_surrounded")
    assert state["prox_zone_count"] == 1
    assert state["tof_surrounded"] is False

    mixer._handle_prox_line("PROX A=C V=-180 D=280 C=4")
    assert bb.read("tof_too_close_safety")["tof_too_close_safety"] is True

    mixer._handle_prox_line("PROX CLEAR")
    assert bb.read("tof_too_close_safety")["tof_too_close_safety"] is False


def test_tof_multi_track_human_ttl_and_center_bonus():
    tracker = MultiTrackTracker(window=10, human_ttl_sec=45.0)
    # Frame 1..6: approaching visitor in C zone at 700mm -> classified as human
    for i in range(6):
        _, tracks, primary = tracker.update(
            [
                {
                    "zone": "C",
                    "x_mm": 0,
                    "z_mm": 800 - i * 25,
                    "dist_mm": 800 - i * 25,
                    "motion": "approach",
                    "vel_mm_s": -150.0,
                }
            ],
            now=100.0 + i * 0.2,
        )
    assert primary is not None
    assert primary["kind"] == "human"

    # Next 15 frames: visitor stands completely still at 675mm listening
    for i in range(15):
        _, tracks, primary = tracker.update(
            [
                {
                    "zone": "C",
                    "x_mm": 0,
                    "z_mm": 675,
                    "dist_mm": 675,
                    "motion": "still",
                    "vel_mm_s": 0.0,
                }
            ],
            now=102.0 + i * 0.2,
        )
    assert primary is not None
    assert primary["kind"] == "human"
    assert "TTL" in primary["reason"]


def test_base_controller_suppresses_side_glance_in_crowd_and_blocks_turn_when_surrounded():
    bb = Blackboard()
    ctrl = BaseController(bb, None)
    ctrl.prox_enabled = True
    ctrl.prox_investigate_enabled = True
    ctrl.prox_min_confidence = 3

    # 1. Tracking 2 faces -> side ToF event should NOT trigger prox_glance_active
    bb.write(
        face_detected=True,
        face_count=2,
        body_detected=False,
        servo_pan=100.0,
        base_world_yaw_deg=0.0,
        base_encoder_deg=0.0,
        prox_approach_active=True,
        prox_approach_zone="L",
        prox_approach_confidence=4,
        prox_approach_ts=200.0,
    )
    state = bb.read(
        "face_detected", "face_count", "body_detected", "servo_pan",
        "base_world_yaw_deg", "base_encoder_deg", "prox_approach_active",
        "prox_approach_zone", "prox_approach_confidence", "prox_approach_ts",
        "prox_post_turn_lockout_ts", "tof_surrounded", "tof_too_close_safety",
        "voice_session_active",
    )
    step, source, _ = ctrl._plan_proximity_step(200.5, state)
    assert step is None
    assert bb.read("prox_glance_active")["prox_glance_active"] is False

    # 2. Not tracking face, but tof_surrounded=True -> should NOT rotate base
    bb.write(
        face_detected=False,
        face_count=0,
        tof_surrounded=True,
        prox_approach_ts=210.0,
    )
    state = bb.read(
        "face_detected", "face_count", "body_detected", "servo_pan",
        "base_world_yaw_deg", "base_encoder_deg", "prox_approach_active",
        "prox_approach_zone", "prox_approach_confidence", "prox_approach_ts",
        "prox_post_turn_lockout_ts", "tof_surrounded", "tof_too_close_safety",
        "voice_session_active",
    )
    step, source, _ = ctrl._plan_proximity_step(215.0, state)
    assert step is None
    assert source == ""


def test_servo_loop_suppresses_velocity_spike_on_multi_face_switch():
    bb = Blackboard()
    loop = ServoLoop(bb)
    loop._mode = "track"

    # Frame 1: face 0 on the left (norm_x = -0.45)
    bb.write(
        face_detected=True,
        face_norm_x=-0.45,
        face_norm_y=0.0,
        face_count=2,
        track_kind="multi",
        active_face_index=0,
    )
    loop._tick_track(now=100.0, dt=0.0125, effective_tilt_center=loop.tilt_center)

    # Frame 2: MultiFaceAttention switches to face 1 on the right (norm_x = +0.50)
    bb.write(
        face_detected=True,
        face_norm_x=0.50,
        face_norm_y=0.0,
        face_count=2,
        track_kind="multi",
        active_face_index=1,
    )
    loop._tick_track(now=100.1, dt=0.0125, effective_tilt_center=loop.tilt_center)

    # Velocity must be zeroed on target switch instead of spiking to (0.95 / 0.0125 = 76/s)
    assert loop._face_vel_x == 0.0
    assert loop._face_switch_soft_until > 100.1


def test_servo_loop_wander_biases_tilt_up_on_close_center_tof():
    bb = Blackboard()
    loop = ServoLoop(bb)
    loop._mode = "wander"
    loop._tilt = loop.tilt_center  # 70.0
    loop._wander.moving = False
    loop._wander.hold_until = 0.0

    bb.write(
        face_detected=False,
        body_detected=False,
        hand_detected=False,
        prox_zone_center=True,
    )
    loop._tick_wander(
        now=100.0,
        dt=0.05,
        effective_tilt_center=loop.tilt_center,
        camera_world_yaw_deg=0.0,
    )
    # With tilt_sign = -1.0 (50=UP, 90=DOWN), upward bias pulls tilt < tilt_center (toward 64.0)
    assert loop._wander.tilt_goal <= loop.tilt_center


def test_servo_loop_speaking_drift_sweeps_cached_group_candidates():
    bb = Blackboard()
    loop = ServoLoop(bb)
    loop._pan = 100.0
    candidates = [
        {"norm_x": -0.5, "norm_y": 0.0, "pan_cmd": 100.0, "area_ratio": 0.04},
        {"norm_x": 0.5, "norm_y": 0.0, "pan_cmd": 100.0, "area_ratio": 0.035},
    ]
    bb.write(face_candidates=candidates, face_count=2)

    # Initialize speaking drift
    loop._tick_speaking_drift(now=100.0, dt=0.02)
    assert loop._speaking_drift_active is True

    # Expire hold timer -> should target candidate[1] (+0.5 norm_x)
    loop._speaking_drift_hold_until = 100.0
    loop._tick_speaking_drift(now=100.1, dt=0.02)
    expected_pan_1 = 100.0 + 0.5 * 31.0 * loop.pan_track_sign * 0.75
    assert abs(loop._speaking_drift_pan_target - expected_pan_1) < 0.01

    # Expire hold timer again -> should cycle to candidate[0] (-0.5 norm_x)
    loop._speaking_drift_hold_until = 100.1
    loop._tick_speaking_drift(now=100.2, dt=0.02)
    expected_pan_0 = 100.0 + (-0.5) * 31.0 * loop.pan_track_sign * 0.75
    assert abs(loop._speaking_drift_pan_target - expected_pan_0) < 0.01


def test_crowd_greetings_and_emotions():
    g1 = generate_random_face_greeting(face_count=1)
    g2 = generate_random_face_greeting(face_count=2)
    g3 = generate_random_face_greeting(face_count=3)
    gg = generate_random_face_greeting(face_count=4, is_crowd_growth=True)

    assert isinstance(g1, str) and len(g1) > 0
    assert any(tok in g2.lower() for tok in ("two", "both", "friends"))
    assert any(tok in g3.lower() for tok in ("everyone", "group", "friends"))
    assert "everyone" in gg.lower()

    se = SurroundingsEmotionController()
    # High roll angle should strongly favor curious
    picks = {
        se.choose_person_emotion("mid", 0.3, 0.0, face_count=1, face_roll_deg=12.0)
        for _ in range(25)
    }
    assert "curious" in picks


def test_tof_1m_orienting_reflex_eye_base_head():
    from core.tof_state import TofState
    from core.tof_stream import TofStreamHandler

    bb = Blackboard()
    mixer = ServoMixer(bb, None)
    ctrl = BaseController(bb, None)
    loop = ServoLoop(bb)
    tof_state = TofState()
    handler = TofStreamHandler(tof_state, bb)

    # 1. Above 1m (1250mm) PROX and TOF lines are strictly ignored
    mixer._handle_prox_line("PROX A=L V=-150 D=1250 C=5")
    assert bb.read("prox_approach_active")["prox_approach_active"] is False

    for _ in range(4):
        handler.handle_tof_line("TOF L=1250 C=-1 R=-1 VL=-80 VC=0 VR=0")
    assert bb.read("prox_approach_active")["prox_approach_active"] is False

    # 2. Person stands at 750mm on Left (within 1m) -> TofStreamHandler triggers approach
    for _ in range(3):
        handler.handle_tof_line("TOF L=750 C=-1 R=-1 VL=0 VC=0 VR=0")
    state = bb.read(
        "face_detected", "face_count", "body_detected", "servo_pan",
        "base_world_yaw_deg", "base_encoder_deg", "prox_approach_active",
        "prox_approach_zone", "prox_approach_distance", "prox_approach_confidence",
        "prox_approach_ts", "prox_post_turn_lockout_ts", "tof_surrounded",
        "tof_too_close_safety", "voice_session_active",
    )
    assert state["prox_approach_active"] is True
    assert state["prox_approach_zone"] == "L"
    assert state["prox_approach_distance"] == 750

    # 3. BaseController plans full unclamped 28° step toward Left, sets eye emotion, and 0 comp_pan
    step, source, comp_pan = ctrl._plan_proximity_step(500.0, state)
    assert source == "proximity"
    assert abs(step) == ctrl.prox_turn_step  # 28.0°, not clamped to 15.0°
    assert comp_pan == 0.0
    assert bb.read("prox_glance_emotion")["prox_glance_emotion"] == "looking_left_natural"
    assert bb.read("prox_investigate_active")["prox_investigate_active"] is True

    # 4. ServoLoop pans head Left (> 100.0°) and suppresses base counter-rotation during turn
    loop._pan = 100.0
    loop._last_base_enc = 0.0
    bb.write(base_encoder_deg=-15.0)
    loop._apply_base_compensation()
    assert loop._pan == 100.0  # Not counter-rotated right!

    for _ in range(10):
        loop._tick_prox_investigate(now=500.2, dt=0.03, effective_tilt_center=loop.tilt_center)
    assert loop._pan > 105.0  # Head actively panning Left toward 116.0°

