"""Tests for debug dashboard snapshot mapping."""

from core.debug_dashboard import build_debug_snapshot


def test_build_debug_snapshot_mech_angles_and_imu_fields():
    state = {
        "servo_pan": 100.0,
        "servo_tilt": 110.0,
        "base_encoder_deg": 15.0,
        "base_world_yaw_deg": 20.0,
        "imu_yaw_integral_deg": 25.0,
        "imu_inferred_base_deg": 10.0,
        "base_motion_busy": True,
    }
    servo_cfg = {
        "pan_center": 100.0,
        "tilt_center": 110.0,
        "pan_min": 25.0,
        "pan_max": 150.0,
        "tilt_min": 100.0,
        "tilt_max": 150.0,
        "tilt_max_mechanical_deg": 45.0,
        "tilt_min_mechanical_deg": -35.0,
        "pan_mech_left_deg": -40.0,
        "pan_mech_right_deg": 40.0,
    }
    snap = build_debug_snapshot(
        state,
        servo_cfg=servo_cfg,
        debug_viz_cfg={"base_yaw_sign": -1.0},
        base_cfg={"max_yaw_deg": 120.0},
    )
    assert snap["pan_mech_deg"] == 0.0
    assert snap["tilt_mech_deg"] == 0.0
    assert snap["base_yaw_deg"] == 15.0
    assert snap["base_world_yaw_deg"] == 20.0
    assert snap["viz_base_yaw_sign"] == -1.0
    assert snap["base_max_yaw_deg"] == 120.0
    assert snap["imu_yaw_total_deg"] == 25.0
    assert snap["imu_pan_delta_deg"] == 10.0
    assert snap["body_yaw_deg"] == 15.0
    assert snap["head_yaw_on_body_deg"] == 10.0
    assert snap["base_busy"] is True


def test_build_debug_snapshot_pan_mech_nonzero():
    state = {"servo_pan": 125.0, "servo_tilt": 110.0}
    servo_cfg = {
        "pan_center": 100.0,
        "tilt_center": 110.0,
        "pan_min": 25.0,
        "pan_max": 150.0,
        "tilt_min": 100.0,
        "tilt_max": 150.0,
        "pan_mech_left_deg": -40.0,
        "pan_mech_right_deg": 40.0,
        "tilt_max_mechanical_deg": 45.0,
        "tilt_min_mechanical_deg": -35.0,
    }
    snap = build_debug_snapshot(state, servo_cfg=servo_cfg, debug_viz_cfg={}, base_cfg={})
    assert abs(snap["pan_mech_deg"] - 20.0) < 0.01


def test_debug_html_js_has_no_nullish_logical_mix():
    """JS modules reject mixing ?? and || without parentheses."""
    import re

    from head_debug_viz import _DEBUG_HTML

    assert not re.search(r"\?\?[^;\n`]*\|\|", _DEBUG_HTML)


def test_dashboard_html_has_single_mode_banner():
    from core.debug_dashboard import _dashboard_html

    html = _dashboard_html(include_camera_stream=True)
    assert html.count('id="mode-banner"') == 1
    html_plain = _dashboard_html(include_camera_stream=False)
    assert "/stream" not in html_plain
