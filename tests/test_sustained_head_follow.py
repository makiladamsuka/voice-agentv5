"""Sustained neck offset → base follow with neck lock."""

from core.base_controller import BaseController
from core.blackboard import Blackboard
from lib.head_mech import signed_pan_mech_deg


class _Link:
    pass


def _make_controller() -> tuple[Blackboard, BaseController]:
    bb = Blackboard()
    ctrl = BaseController(bb, _Link())
    ctrl.sustained_follow_enabled = True
    ctrl.sustained_hold_sec = 30.0
    ctrl.sustained_hold_min_mech = 12.0
    ctrl.sustained_cooldown_sec = 4.0
    ctrl.sustained_comp_gain = 0.95
    ctrl.cooldown_sec = 4.0
    ctrl.min_step = 5.0
    ctrl.max_step = 15.0
    ctrl.head_lead_min = 12.0
    return bb, ctrl


def _mech(pan_cmd: float, ctrl: BaseController) -> float:
    return signed_pan_mech_deg(pan_cmd, ctrl._servo_cfg)


def test_sustained_hold_timer_resets_below_min_offset():
    _, ctrl = _make_controller()
    ctrl._update_sustained_hold(100.0, 8.0)
    assert ctrl._sustained_since is None

    ctrl._update_sustained_hold(100.0, 18.0)
    assert ctrl._sustained_since == 100.0


def test_sustained_hold_resets_on_sign_change():
    _, ctrl = _make_controller()
    ctrl._update_sustained_hold(100.0, 18.0)
    ctrl._update_sustained_hold(110.0, -16.0)
    assert ctrl._sustained_since == 110.0
    assert ctrl._sustained_sign == -1.0


def test_sustained_follow_fires_after_hold_with_neck_lock():
    _, ctrl = _make_controller()
    pan = 70.0
    assert abs(_mech(pan, ctrl)) >= ctrl.sustained_hold_min_mech

    ctrl._sustained_since = 50.0
    ctrl._sustained_sign = 1.0
    ctrl._last_sustained_ts = 0.0
    ctrl._last_nudge_ts = 0.0

    state = {
        "servo_pan": pan,
        "base_encoder_deg": 0.0,
        "base_motion_allowed": True,
        "imu_available": False,
    }
    step, source, comp_pan = ctrl._plan_sustained_follow(85.0, state)
    assert source == "sustained_head"
    assert step is not None
    assert abs(step) >= ctrl.min_step
    assert abs(comp_pan - ctrl.pan_center) < abs(pan - ctrl.pan_center)


def test_sustained_follow_blocked_before_hold_elapsed():
    _, ctrl = _make_controller()
    pan = 70.0
    ctrl._sustained_since = 70.0
    ctrl._last_sustained_ts = 0.0
    ctrl._last_nudge_ts = 0.0

    state = {
        "servo_pan": pan,
        "base_encoder_deg": 0.0,
        "base_motion_allowed": True,
        "imu_available": False,
    }
    step, source, _ = ctrl._plan_sustained_follow(85.0, state)
    assert step is None
    assert source == ""
