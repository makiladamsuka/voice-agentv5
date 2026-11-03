"""Base follow when head pan is stuck at servo limit during face track."""

from core.base_controller import BaseController
from core.blackboard import Blackboard
from lib.head_mech import signed_pan_mech_deg


class _Link:
    pass


def _make_controller() -> BaseController:
    ctrl = BaseController(Blackboard(), _Link())
    ctrl.min_step = 8.0
    ctrl.max_step = 15.0
    ctrl.base_sign = -1.0
    ctrl.cooldown_sec = 4.0
    ctrl.cooldown_at_limit_sec = 1.5
    ctrl.trigger_hold_at_limit_sec = 1.0
    ctrl.limit_base_aim_gain = 3.0
    ctrl.pan_offset_to_step = 0.10
    ctrl.pan_min = 25.0
    ctrl.pan_max = 150.0
    ctrl.pan_center = 100.0
    ctrl.mech_left = -40.0
    ctrl.mech_right = 40.0
    ctrl.pan_limit_margin = 0.35
    return ctrl


def test_pan_at_servo_limit_detects_hardware_stop():
    ctrl = _make_controller()
    assert ctrl._pan_at_servo_limit(25.0)
    assert ctrl._pan_at_servo_limit(149.8)
    assert not ctrl._pan_at_servo_limit(100.0)


def test_track_limit_step_when_head_stuck_and_face_off_center():
    ctrl = _make_controller()
    pan = 25.2
    pan_mech = signed_pan_mech_deg(pan, ctrl._servo_cfg)
    assert ctrl._pan_needs_base_help(pan, pan_mech)

    # With pan_sign=-1.0, pan=25.2 is the right mechanical limit (+40°), so a face
    # further right (+0.75) agrees with head lead.
    state = {
        "servo_pan": pan,
        "face_norm_x": 0.75,
        "base_encoder_deg": 0.0,
        "base_motion_allowed": True,
        "imu_available": False,
    }
    ctrl._trigger_since = 8.0
    ctrl._last_nudge_ts = 0.0
    step, source, comp_pan = ctrl._plan_track_step(10.0, state)
    assert source == "track_limit"
    assert step is not None
    assert abs(step) >= ctrl.min_step
    assert abs(comp_pan - ctrl.pan_center) < abs(pan - ctrl.pan_center)


def test_cap_for_aim_caps_large_step_with_negative_base_sign():
    """With base_sign=-1.0, _cap_for_aim must still cap step to aim angle (no sign mismatch bypass)."""
    ctrl = _make_controller()
    ctrl.min_step = 4.0
    # norm_x = 0.20 -> aim_deg = 0.20 * 31 = 6.2 deg -> max_useful = 6.51 deg
    capped = ctrl._cap_for_aim(-15.0, 0.20, hfov=62.0)
    assert capped < 0.0
    assert abs(capped) < 7.0
    assert abs(capped) >= ctrl.min_step
