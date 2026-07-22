"""Signed servo → mechanical angle mapping (pan/tilt)."""

from head_debug_viz import servo_pan_to_mechanical, servo_tilt_to_mechanical


def test_pan_left_of_center_is_negative():
    mech = servo_pan_to_mechanical(
        95.0,
        center=100.0,
        p_min=25.0,
        p_max=150.0,
        mech_left_deg=-40.0,
        mech_right_deg=40.0,
    )
    assert mech < 0.0


def test_pan_right_of_center_is_positive():
    mech = servo_pan_to_mechanical(
        105.0,
        center=100.0,
        p_min=25.0,
        p_max=150.0,
        mech_left_deg=-40.0,
        mech_right_deg=40.0,
    )
    assert mech > 0.0


def test_tilt_below_center_is_negative():
    mech = servo_tilt_to_mechanical(
        105.0,
        center=110.0,
        t_min=100.0,
        t_max=150.0,
        mech_down_deg=-35.0,
        mech_up_deg=45.0,
    )
    assert mech < 0.0


def test_tilt_above_center_is_positive():
    mech = servo_tilt_to_mechanical(
        115.0,
        center=110.0,
        t_min=100.0,
        t_max=150.0,
        mech_down_deg=-35.0,
        mech_up_deg=45.0,
    )
    assert mech > 0.0
