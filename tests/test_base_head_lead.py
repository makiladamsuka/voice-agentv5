"""Tests for head-lead base direction helpers."""

from lib.base_head_lead import (
    aim_agrees_with_head,
    head_lead_sign,
    proactive_comp_pan_cmd,
    step_agrees_with_head,
    yaw_error_agrees_with_head,
)


def test_head_lead_sign():
    assert head_lead_sign(10.0) == 1.0
    assert head_lead_sign(-8.0) == -1.0
    assert head_lead_sign(0.2) == 0.0


def test_step_agrees_with_head():
    assert step_agrees_with_head(3.0, 12.0, head_lead_min_deg=8.0) is True
    assert step_agrees_with_head(-3.0, 12.0, head_lead_min_deg=8.0) is False
    assert step_agrees_with_head(3.0, 4.0, head_lead_min_deg=8.0) is False


def test_aim_agrees_with_head():
    assert aim_agrees_with_head(0.5, 10.0) is True
    assert aim_agrees_with_head(-0.5, 10.0) is False
    assert aim_agrees_with_head(0.01, 10.0) is True


def test_yaw_error_agrees_with_head():
    assert yaw_error_agrees_with_head(15.0, 10.0, head_lead_min_deg=8.0) is True
    assert yaw_error_agrees_with_head(-15.0, 10.0, head_lead_min_deg=8.0) is False


def test_proactive_comp_moves_pan_opposite_base():
    pan_center = 100.0
    pan_min = 25.0
    pan_max = 150.0
    mech_left = -40.0
    mech_right = 40.0
    current = 130.0  # head turned right
    compensated = proactive_comp_pan_cmd(
        4.0,
        current,
        compensation_gain=0.95,
        pan_center=pan_center,
        pan_min=pan_min,
        pan_max=pan_max,
        mech_left=mech_left,
        mech_right=mech_right,
    )
    assert compensated < current
