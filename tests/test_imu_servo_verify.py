"""Tests for IMU vs servo closed-loop error math."""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from lib.imu_servo_verify import ServoPose, VerifyReference, compute_errors, compute_yaw_verify


def test_errors_zero_at_center_lock():
    ref = VerifyReference(imu_tilt_deg=12.0)
    servo = ServoPose(pan_cmd=100.0, tilt_cmd=110.0, pan_mech_deg=0.0, tilt_mech_deg=0.0)
    err = compute_errors(imu_yaw_deg=0.0, imu_tilt_deg=12.0, servo=servo, ref=ref)
    assert abs(err.pan_error_deg) < 0.01
    assert abs(err.tilt_error_deg) < 0.01


def test_pan_error_after_servo_move():
    ref = VerifyReference(imu_tilt_deg=12.0)
    servo = ServoPose(pan_cmd=115.0, tilt_cmd=110.0, pan_mech_deg=30.0, tilt_mech_deg=0.0)
    err = compute_errors(imu_yaw_deg=30.0, imu_tilt_deg=12.0, servo=servo, ref=ref)
    assert abs(err.pan_error_deg) < 0.01
    assert abs(err.servo_pan_delta_deg - 30.0) < 0.01


def test_pan_drift_detected():
    ref = VerifyReference(imu_tilt_deg=12.0)
    servo = ServoPose(pan_cmd=115.0, tilt_cmd=110.0, pan_mech_deg=30.0, tilt_mech_deg=0.0)
    err = compute_errors(imu_yaw_deg=33.0, imu_tilt_deg=12.0, servo=servo, ref=ref)
    assert abs(err.pan_error_deg - 3.0) < 0.01


def test_tilt_only_move_pan_error_unchanged():
    ref = VerifyReference(imu_tilt_deg=10.0)
    servo = ServoPose(pan_cmd=100.0, tilt_cmd=125.0, pan_mech_deg=0.0, tilt_mech_deg=15.0)
    err = compute_errors(imu_yaw_deg=0.0, imu_tilt_deg=25.0, servo=servo, ref=ref)
    assert abs(err.pan_error_deg) < 0.01
    assert abs(err.tilt_error_deg) < 0.01


def test_tilt_drift_detected():
    ref = VerifyReference(imu_tilt_deg=10.0)
    servo = ServoPose(pan_cmd=100.0, tilt_cmd=125.0, pan_mech_deg=0.0, tilt_mech_deg=15.0)
    err = compute_errors(imu_yaw_deg=0.0, imu_tilt_deg=27.0, servo=servo, ref=ref)
    assert abs(err.tilt_error_deg - 2.0) < 0.01


def _yaw_verify(
    *,
    imu_yaw: float,
    base_enc: float,
    pan_mech: float,
    imu_tilt: float = 12.0,
    tilt_mech: float = 0.0,
    ref: VerifyReference | None = None,
):
    ref = ref or VerifyReference(imu_tilt_deg=12.0)
    servo = ServoPose(
        pan_cmd=100.0,
        tilt_cmd=110.0,
        pan_mech_deg=pan_mech,
        tilt_mech_deg=tilt_mech,
    )
    return compute_yaw_verify(
        imu_yaw_deg=imu_yaw,
        imu_tilt_deg=imu_tilt,
        base_encoder_deg=base_enc,
        servo=servo,
        ref=ref,
    )


def test_yaw_verify_head_only():
    state = _yaw_verify(imu_yaw=30.0, base_enc=0.0, pan_mech=30.0)
    assert abs(state.body_yaw_deg) < 0.01
    assert abs(state.head_on_body_imu_deg - 30.0) < 0.01
    assert abs(state.head_pan_error_deg) < 0.01


def test_yaw_verify_base_only():
    state = _yaw_verify(imu_yaw=35.0, base_enc=35.0, pan_mech=0.0)
    assert abs(state.body_yaw_deg - 35.0) < 0.01
    assert abs(state.head_on_body_imu_deg) < 0.01
    assert abs(state.head_pan_error_deg) < 0.01


def test_yaw_verify_mixed_body_and_head():
    state = _yaw_verify(imu_yaw=45.0, base_enc=35.0, pan_mech=10.0)
    assert abs(state.body_yaw_deg - 35.0) < 0.01
    assert abs(state.head_on_body_imu_deg - 10.0) < 0.01
    assert abs(state.world_head_yaw_deg - 45.0) < 0.01
    assert abs(state.head_pan_error_deg) < 0.01


def test_yaw_verify_bad_neck_coupling():
    state = _yaw_verify(imu_yaw=45.0, base_enc=35.0, pan_mech=5.0)
    assert abs(state.head_pan_error_deg - 5.0) < 0.01


if __name__ == "__main__":
    tests = [
        test_errors_zero_at_center_lock,
        test_pan_error_after_servo_move,
        test_pan_drift_detected,
        test_tilt_only_move_pan_error_unchanged,
        test_tilt_drift_detected,
        test_yaw_verify_head_only,
        test_yaw_verify_base_only,
        test_yaw_verify_mixed_body_and_head,
        test_yaw_verify_bad_neck_coupling,
    ]
    for t in tests:
        t()
    print(f"OK: {len(tests)} tests passed")
