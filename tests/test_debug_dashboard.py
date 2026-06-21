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
    assert snap["imu_pan_delta_deg"] == 15.0
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
