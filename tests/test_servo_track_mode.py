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

    next_mode = loop._tick_wander(now=100.0, dt=0.01, effective_tilt_center=loop.tilt_center, camera_world_yaw_deg=0.0)
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


def test_effective_tilt_frozen_mechanical_while_tracking():
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
    assert updated == loop.tilt_center


def test_wander_tilt_ref_uses_full_horizon_when_blend_one():
    loop = ServoLoop(Blackboard())
    loop.tilt_center = 110.0
    loop.wander_imu_tilt_blend = 1.0
    ref = loop._wander_tilt_ref(112.0)
    assert ref == 112.0


def test_leaving_track_resets_tilt_smooth_to_imu_not_track_pose():
    bb = Blackboard()
    bb.write(
        running=True,
        imu_available=True,
        imu_horizon_ok=True,
        imu_effective_tilt_center=112.0,
    )
    loop = ServoLoop(bb)
    loop._mode = "track"
    loop._tilt = 98.0
    loop._on_mode_change("track", "wander")
    assert loop._effective_tilt_center_smooth == 112.0


def test_track_tilt_uses_mechanical_center_when_face_centered():
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
    loop._tilt = loop.tilt_center
    loop._pan = loop.pan_center
    loop._tilt_track_norm = 0.0

    loop._tick_track(now=100.0, dt=0.05, effective_tilt_center=115.0)
    assert abs(loop._tilt - loop.tilt_center) < 0.5


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
    loop.tilt_track_sign = -1.0
    loop._tilt_track_norm = 0.45
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


def test_track_proactive_comp_softens_pid_without_open_loop_pan_jump():
    """Proactive base comp must only soften PID and never pre-rotate pan before encoder moves."""
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
    loop._pan_pid._integral = 0.4

    loop._apply_proactive_base_comp()
    assert loop._pan == loop.pan_center
    assert loop._pan_pid._integral < 0.4


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

    next_mode = loop._tick_wander(now=100.0, dt=0.05, effective_tilt_center=loop.tilt_center, camera_world_yaw_deg=0.0)
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


def test_track_from_wander_offset_follows_face_not_center():
    """Wander left + face still left-of-frame must NOT yank pan toward pan_center.

    Old absolute aim (pan_center + corr) reversed direction whenever the neck was
    already aside and the person appeared in that same direction.
    """
    bb = Blackboard()
    bb.write(
        running=True,
        face_detected=True,
        body_detected=False,
        face_norm_x=-0.30,
        face_norm_y=0.0,
        face_count=1,
        track_kind="face",
        face_candidates=[],
    )
    loop = ServoLoop(bb)
    loop._mode = "track"
    # Head already glanced left (command > center with pan_sign=-1).
    offset_pan = loop.pan_center + 22.0
    loop._pan = offset_pan
    loop._pan_in_center_band = False
    loop._pan_track_norm = -0.30
    loop._filtered_norm_x = -0.30
    mech_before = _mech(loop._pan, loop)

    loop._tick_track(now=100.0, dt=0.05, effective_tilt_center=loop.tilt_center)

    # Face left → head should turn further left (mech decreases, pan command increases).
    assert _mech(loop._pan, loop) < mech_before
    assert loop._pan > offset_pan
    # Must not move toward center (that was the visible left/right jerk).
    assert abs(loop._pan - loop.pan_center) >= abs(offset_pan - loop.pan_center)


def test_track_from_wander_right_follows_face_not_center():
    """Wander right + face still right-of-frame must continue right, not snap inward."""
    bb = Blackboard()
    bb.write(
        running=True,
        face_detected=True,
        body_detected=False,
        face_norm_x=0.30,
        face_norm_y=0.0,
        face_count=1,
        track_kind="face",
        face_candidates=[],
    )
    loop = ServoLoop(bb)
    loop._mode = "track"
    offset_pan = loop.pan_center - 22.0
    loop._pan = offset_pan
    loop._pan_in_center_band = False
    loop._pan_track_norm = 0.30
    loop._filtered_norm_x = 0.30
    mech_before = _mech(loop._pan, loop)

    loop._tick_track(now=100.0, dt=0.05, effective_tilt_center=loop.tilt_center)

    assert _mech(loop._pan, loop) > mech_before
    assert loop._pan < offset_pan
    assert abs(loop._pan - loop.pan_center) >= abs(offset_pan - loop.pan_center)

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


def test_forward_return_skipped_when_sustained_follow_enabled():
    bb = Blackboard()
    bb.write(running=True, face_detected=False, body_detected=False, last_seen_world_yaw=None)
    loop = ServoLoop(bb)
    loop._mode = "wander"
    loop._pan = loop.pan_center + 30.0
    loop._off_forward_since = 100.0
    loop.forward_return_timeout_sec = 5.0
    loop.sustained_head_follow_enabled = True

    loop._maybe_start_forward_return(106.0, 0.0, tracking_face=False)
    assert loop._forward_return_active is False


def test_forward_return_starts_after_off_forward_timeout():
    bb = Blackboard()
    bb.write(running=True, face_detected=False, body_detected=False, last_seen_world_yaw=None)
    loop = ServoLoop(bb)
    loop._mode = "wander"
    loop._pan = loop.pan_center + 30.0
    loop._off_forward_since = 100.0
    loop.forward_return_timeout_sec = 5.0
    loop.sustained_head_follow_enabled = False

    loop._maybe_start_forward_return(106.0, 0.0, tracking_face=False)
    assert loop._forward_return_active is True

    loop._tick_forward_return(106.0, 0.05, loop.tilt_center)
    assert loop._pan < loop.pan_center + 30.0


def test_forward_return_timer_resets_while_tracking_face():
    bb = Blackboard()
    loop = ServoLoop(bb)
    loop._off_forward_since = 100.0
    loop._update_off_forward_timer(105.0, 0.0, tracking_face=True)
    assert loop._off_forward_since is None


def test_track_pan_step_capped_per_tick():
    """Fast face offset must not jump pan more than pan_max_step per tick."""
    bb = Blackboard()
    bb.write(
        running=True,
        face_detected=True,
        body_detected=False,
        face_norm_x=0.9,
        face_norm_y=0.0,
        face_count=1,
        track_kind="face",
        face_candidates=[],
    )
    loop = ServoLoop(bb)
    loop._mode = "track"
    loop._pan = loop.pan_center
    loop.pan_max_step_deg = 0.75
    loop.pan_track_slew_dps = 38.0
    loop._pan_in_center_band = False

    before = loop._pan
    loop._tick_track(now=100.0, dt=0.05, effective_tilt_center=loop.tilt_center)
    assert abs(loop._pan - before) <= loop.pan_max_step_deg + 0.01


def test_track_to_wander_seeds_from_current_pose():
    bb = Blackboard()
    loop = ServoLoop(bb)
    loop._pan = loop.pan_center + 22.0
    loop._tilt = loop.tilt_center + 5.0
    loop._enter_wander_from_current_pose(100.0)
    assert loop._wander.pan_goal == loop.pan_center + 22.0
    assert loop._wander.tilt_goal == loop.tilt_center + 5.0
    assert loop._wander.hold_until > 100.0


def test_wander_holds_track_pose_after_loss():
    bb = Blackboard()
    bb.write(running=True, face_detected=False, body_detected=False, last_seen_world_yaw=None)
    loop = ServoLoop(bb)
    loop._mode = "wander"
    track_pan = loop.pan_center + 18.0
    loop._pan = track_pan
    loop._tilt = loop.tilt_center + 4.0
    loop._enter_wander_from_current_pose(100.0)
    loop._tick_wander(now=100.05, dt=0.05, effective_tilt_center=loop.tilt_center - 8.0, camera_world_yaw_deg=0.0)
    assert abs(loop._pan - track_pan) < 2.0
    assert abs(loop._tilt - (loop.tilt_center + 4.0)) < 2.0


def test_on_mode_change_preserves_exit_direction_for_wander_edge_search():
    """When a face exits right (+norm_x), wander edge search must step right, not jerk left."""
    bb = Blackboard()
    loop = ServoLoop(bb)
    loop._mode = "track"
    loop._pan = 75.0  # looking right (pan < pan_center with pan_sign=-1)
    loop._prev_face_x = 0.55  # exited on the right edge
    mech_before = _mech(loop._pan, loop)

    loop._on_mode_change("track", "wander")
    assert loop._last_exit_norm_x == 0.55
    assert getattr(loop._wander, "_search_mode", "") == "edge"

    # Advance past hold_until so _wander picks its edge search target
    pan_goal, _ = loop._wander.tick(
        loop._wander.hold_until + 0.1,
        pan_center=loop.pan_center,
        tilt_center=loop.tilt_center,
        pan_current=loop._pan,
        tilt_current=loop._tilt,
        pan_min=loop.pan_min,
        pan_max=loop.pan_max,
        tilt_min=loop.tilt_min,
        tilt_max=loop.tilt_max,
        amp_deg=loop.wander_pan_amp,
        step_min_deg=loop.wander_step_min,
        step_max_deg=loop.wander_step_max,
        hold_min_sec=loop.wander_hold_min,
        hold_max_sec=loop.wander_hold_max,
        jump_chance=loop.wander_jump_chance,
        arrival_deg=loop.wander_arrival,
        tilt_max_up_deg=loop.wander_tilt_max_up,
        tilt_max_down_deg=loop.wander_tilt_max_down,
        tilt_step_max_deg=loop.wander_tilt_step_max,
        world_yaw_deg=mech_before,
        pan_sign=loop.pan_sign,
    )
    # Physical angle of pan_goal must be further right (> mech_before), not jerked left!
    assert _mech(pan_goal, loop) > mech_before


def test_wander_does_not_immediately_abort_into_last_seen_on_center_loss():
    """Losing a centered face must hold wander pose rather than ping-ponging into last_seen."""
    bb = Blackboard()
    bb.write(
        running=True,
        face_detected=False,
        body_detected=False,
        last_seen_world_yaw=12.0,
    )
    loop = ServoLoop(bb)
    loop._mode = "track"
    loop._last_face_ts = 98.7
    loop._prev_face_x = 0.08  # near center, not an edge exit
    loop._on_mode_change("track", "wander")
    loop._mode = "wander"

    mode = loop._tick_wander(now=100.0, dt=0.02, effective_tilt_center=loop.tilt_center, camera_world_yaw_deg=0.0)
    assert mode == "wander"


def test_base_compensation_counter_rotates_in_correct_direction_with_negative_base_sign():
    """When base physically rotates right (+delta_enc), head must counter-rotate left."""
    bb = Blackboard()
    bb.write(running=True, base_encoder_deg=0.0, face_detected=False, body_detected=False)
    loop = ServoLoop(bb)
    loop.base_sign = -1.0
    loop._mode = "wander"
    loop._pan = 70.0  # head turned right (+mech)
    loop._last_base_enc = 0.0
    mech_before = _mech(loop._pan, loop)

    # Base rotates right by +1.2 deg
    bb.write(base_encoder_deg=1.2)
    loop._apply_base_compensation()
    mech_after = _mech(loop._pan, loop)

    # Head mechanical yaw must decrease (counter-rotate left toward center), not increase!
    assert mech_after < mech_before


def test_speaking_drift_anchors_to_current_pan_not_pan_center():
    """Speaking while looking at an off-center user must not yank pan back to pan_center."""
    bb = Blackboard()
    loop = ServoLoop(bb)
    loop._pan = 60.0  # looking right at user (40 cmd units from pan_center=100)
    loop._speaking_drift_active = False

    loop._tick_speaking_drift(now=100.0, dt=0.05)
    assert abs(loop._speaking_drift_pan_start - 60.0) < 0.01
    assert abs(loop._pan - 60.0) < 1.0


def test_last_seen_does_not_double_count_head_pan_in_camera_yaw():
    """When camera is already aimed at last_seen_world_yaw, _tick_last_seen must hold steady."""
    bb = Blackboard()
    loop = ServoLoop(bb)
    loop._pan = 70.0  # mech = +16.0 deg
    pan_mech = _mech(loop._pan, loop)
    # base_world_yaw_deg on BB is body_yaw (0.0) + head_yaw_on_body (pan_mech)
    bb.write(
        running=True,
        face_detected=False,
        body_detected=False,
        base_world_yaw_deg=pan_mech,
        head_yaw_on_body_deg=pan_mech,
        last_seen_world_yaw=pan_mech,
    )
    loop._mode = "last_seen"
    loop._lss_active = True
    loop._lss_start_ts = 100.0

    loop._tick_last_seen(now=100.1, dt=0.05, effective_tilt_center=loop.tilt_center)
    assert abs(loop._pan - 70.0) < 0.01


def test_base_compensation_is_1_to_1_without_right_side_amplification():
    """When pan < pan_center (right side), 1 deg of base rotation must NOT move pan by 1.875 deg."""
    bb = Blackboard()
    bb.write(running=True, base_encoder_deg=0.0, face_detected=True, body_detected=False)
    loop = ServoLoop(bb)
    loop._mode = "track"
    loop._pan = 70.0  # right side (pan < 100)
    loop._last_base_enc = 0.0
    loop.base_comp_gain = 1.0

    # Base rotates right by +1.0 deg
    bb.write(base_encoder_deg=1.0)
    loop._apply_base_compensation()

    # Exact 1:1 counter-rotation (+1.0 cmd unit), not +1.875 cmd units!
    assert abs(loop._pan - 71.0) < 0.01


def test_multi_tick_between_camera_frames_subtracts_ego_motion():
    """80 Hz servo ticks between 10 FPS camera frames must decay _pan_track_norm by servo step."""
    bb = Blackboard()
    bb.write(
        running=True,
        face_detected=True,
        body_detected=False,
        face_norm_x=0.35,  # ~10.85 deg off-center
        face_norm_y=0.0,
        face_count=1,
        track_kind="face",
        face_candidates=[],
    )
    loop = ServoLoop(bb)
    loop._mode = "track"
    loop._pan = loop.pan_center
    loop._pan_track_norm = 0.35
    loop._filtered_norm_x = 0.35
    loop._pan_in_center_band = False

    # Run 8 ticks at 80 Hz with the same camera frame on Blackboard
    for i in range(8):
        loop._tick_track(now=100.0 + i * 0.0125, dt=0.0125, effective_tilt_center=loop.tilt_center)

    # Head must move toward the face and subtract that ego-motion from _pan_track_norm
    total_pan_moved = abs(loop._pan - loop.pan_center)
    assert total_pan_moved > 1.0
    assert total_pan_moved < 6.0
    assert loop._pan_track_norm < 0.35
