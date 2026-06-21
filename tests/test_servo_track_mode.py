"""ServoLoop track mode tests."""

import time

from core.blackboard import Blackboard
from core.servo_loop import ServoLoop
from lib.head_mech import signed_pan_mech_deg


def _servo_cfg(loop: ServoLoop) -> dict:
    return {
        "pan_center": loop.pan_center,
        "pan_min": loop.pan_min,
        "pan_max": loop.pan_max,
        "pan_mech_left_deg": loop.mech_left,
        "pan_mech_right_deg": loop.mech_right,
        "pan_sign": loop.pan_sign,
    }


def _mech(pan_cmd: float, loop: ServoLoop) -> float:
    return signed_pan_mech_deg(pan_cmd, _servo_cfg(loop))


def test_wander_does_not_immediately_drop_back_when_face_appears():
    bb = Blackboard()
    bb.write(running=True, face_detected=True, body_detected=False)
    loop = ServoLoop(bb)
    loop._mode = "wander"
    loop._last_face_ts = 0.0
    loop._last_body_ts = 0.0

    next_mode = loop._tick_wander(now=100.0, dt=0.01, effective_tilt_center=loop.tilt_center)
    assert next_mode == "track"
    assert loop._last_face_ts == 100.0

    face_gone = (100.0 - loop._last_face_ts) > loop.no_face_home_sec
    assert face_gone is False


def test_effective_tilt_follows_imu_when_idle():
    bb = Blackboard()
    bb.write(
        running=True,
        face_detected=False,
        body_detected=False,
        imu_available=True,
        imu_horizon_ok=True,
        imu_effective_tilt_center=115.0,
    )
    loop = ServoLoop(bb)
    loop._effective_tilt_center_smooth = 110.0

    releveled = loop._effective_tilt_center(0.05)
    assert releveled > 110.0


def test_effective_tilt_drifts_toward_imu_even_while_tracking():
    bb = Blackboard()
    bb.write(
        running=True,
        face_detected=True,
        body_detected=False,
        imu_available=True,
        imu_horizon_ok=True,
        imu_effective_tilt_center=112.0,
    )
    loop = ServoLoop(bb)
    loop._effective_tilt_center_smooth = 110.0

    updated = loop._effective_tilt_center(0.05)
    assert updated >= 110.0


def test_face_low_in_frame_targets_tilt_down():
    """norm_y > 0 (face low) should command tilt below center with tilt_sign=-1."""
    bb = Blackboard()
    bb.write(
        running=True,
        face_detected=True,
        body_detected=False,
        face_norm_x=0.0,
        face_norm_y=0.45,
        face_count=1,
        track_kind="face",
        face_candidates=[],
    )
    loop = ServoLoop(bb)
    loop._mode = "track"
    loop._tilt = loop.tilt_center
    loop._pan = loop.pan_center
    loop.tilt_sign = -1.0
    loop.tilt_center_norm_y = 0.08

    loop._tick_track(now=100.0, dt=0.05, effective_tilt_center=loop.tilt_center)
    assert loop._tilt < loop.tilt_center


def test_track_holds_through_brief_face_dropout():
    """One missed detection frame must not drop to wander (was breaking tracking)."""
    bb = Blackboard()
    bb.write(running=True, face_detected=False, body_detected=False)
    loop = ServoLoop(bb)
    loop._mode = "track"
    loop._last_face_ts = 100.0
    loop._last_body_ts = 0.0
    loop._pan = loop.pan_center + 5.0

    next_mode = loop._tick_track(now=100.1, dt=0.02, effective_tilt_center=loop.tilt_center)
    assert next_mode == "track"
    assert loop._pan == loop.pan_center + 5.0


def test_face_centered_in_frame_holds_tilt_during_track():
    bb = Blackboard()
    bb.write(
        running=True,
        face_detected=True,
        body_detected=False,
        face_norm_x=0.0,
        face_norm_y=0.0,
        face_count=1,
        track_kind="face",
        face_candidates=[],
    )
    loop = ServoLoop(bb)
    loop._mode = "track"
    loop._tilt = loop.tilt_center + 8.0

    loop._tick_track(now=100.0, dt=0.05, effective_tilt_center=loop.tilt_center - 2.0)
    assert abs(loop._tilt - (loop.tilt_center + 8.0)) < 0.01


def test_pan_stabilizes_when_face_centered_filtered():
    """Filtered bearing inside center band holds pan even if raw bbox jitters."""
    bb = Blackboard()
    bb.write(
        running=True,
        face_detected=True,
        body_detected=False,
        face_norm_x=0.07,
        face_norm_y=0.0,
        face_count=1,
        track_kind="face",
        face_candidates=[],
    )
    loop = ServoLoop(bb)
    loop._mode = "track"
    loop._pan = loop.pan_center + 20.0
    loop._pan_track_norm = 0.02
    loop._pan_in_center_band = True

    loop._tick_track(now=100.0, dt=0.05, effective_tilt_center=loop.tilt_center)
    assert abs(loop._pan - (loop.pan_center + 20.0)) < 0.01


def test_track_proactive_comp_ramps_instead_of_snapping():
    bb = Blackboard()
    bb.write(
        running=True,
        face_detected=True,
        body_detected=False,
        base_step_ready=True,
        base_comp_pan_deg=130.0,
        base_motion_busy=False,
    )
    loop = ServoLoop(bb)
    loop._mode = "track"
    loop._pan = loop.pan_center
    loop.pan_max_step_deg = 0.75

    loop._apply_proactive_base_comp()
    assert loop._pan > loop.pan_center
    assert loop._pan < loop.pan_center + 1.0


def test_pan_stabilizes_when_face_centered():
    """When face is centered, pan should hold steady (not recenter to pan_center)."""
    bb = Blackboard()
    bb.write(
        running=True,
        face_detected=True,
        body_detected=False,
        face_norm_x=0.0,
        face_norm_y=0.0,
        face_count=1,
        track_kind="face",
        face_candidates=[],
    )
    loop = ServoLoop(bb)
    loop._mode = "track"
    loop._pan = loop.pan_center + 20.0
    loop._filtered_norm_x = 0.0
    loop._filtered_norm_y = 0.0

    loop._tick_track(now=100.0, dt=0.05, effective_tilt_center=loop.tilt_center)
    assert abs(loop._pan - (loop.pan_center + 20.0)) < 0.01


def test_wander_to_track_moves_pan_when_face_off_center():
    """Entering track from wander must not freeze pan while filter ramps up."""
    bb = Blackboard()
    bb.write(
        running=True,
        face_detected=True,
        body_detected=False,
        face_norm_x=0.22,
        face_norm_y=0.0,
        face_count=1,
        track_kind="face",
        face_candidates=[],
    )
    loop = ServoLoop(bb)
    loop._mode = "wander"
    loop._pan = loop.pan_center
    mech_before = _mech(loop._pan, loop)

    next_mode = loop._tick_wander(now=100.0, dt=0.05, effective_tilt_center=loop.tilt_center)
    assert next_mode == "track"
    loop._on_mode_change("wander", "track")
    loop._mode = "track"
    loop._tick_track(now=100.0, dt=0.05, effective_tilt_center=loop.tilt_center)

    assert loop._pan_in_center_band is False
    assert _mech(loop._pan, loop) > mech_before
    assert loop._pan < loop.pan_center


def test_pan_turns_right_when_face_moves_right_in_frame():
    """Face right (+norm_x) → signed mech pan increases (head turns right)."""
    bb = Blackboard()
    bb.write(
        running=True,
        face_detected=True,
        body_detected=False,
        face_norm_x=0.35,
        face_norm_y=0.0,
        face_count=1,
        track_kind="face",
        face_candidates=[],
    )
    loop = ServoLoop(bb)
    loop._mode = "track"
    loop._pan = loop.pan_center
    loop._pan_in_center_band = False
    mech_before = _mech(loop._pan, loop)

    loop._tick_track(now=100.0, dt=0.05, effective_tilt_center=loop.tilt_center)
    assert _mech(loop._pan, loop) > mech_before
    assert loop._pan < loop.pan_center


def test_pan_turns_left_when_face_moves_left_in_frame():
    bb = Blackboard()
    bb.write(
        running=True,
        face_detected=True,
        body_detected=False,
        face_norm_x=-0.35,
        face_norm_y=0.0,
        face_count=1,
        track_kind="face",
        face_candidates=[],
    )
    loop = ServoLoop(bb)
    loop._mode = "track"
    loop._pan = loop.pan_center
    loop._pan_in_center_band = False
    mech_before = _mech(loop._pan, loop)

    loop._tick_track(now=100.0, dt=0.05, effective_tilt_center=loop.tilt_center)
    assert _mech(loop._pan, loop) < mech_before
    assert loop._pan > loop.pan_center


def test_forward_return_starts_after_off_forward_timeout():
    bb = Blackboard()
    bb.write(running=True, face_detected=False, body_detected=False, last_seen_world_yaw=None)
    loop = ServoLoop(bb)
    loop._mode = "wander"
    loop._pan = loop.pan_center + 30.0
    loop._off_forward_since = 100.0
    loop.forward_return_timeout_sec = 5.0

    loop._maybe_start_forward_return(106.0, tracking_face=False)
    assert loop._forward_return_active is True

    loop._tick_forward_return(106.0, 0.05, loop.tilt_center)
    assert loop._pan < loop.pan_center + 30.0


def test_forward_return_timer_resets_while_tracking_face():
    bb = Blackboard()
    loop = ServoLoop(bb)
    loop._off_forward_since = 100.0
    loop._update_off_forward_timer(105.0, tracking_face=True)
    assert loop._off_forward_since is None
