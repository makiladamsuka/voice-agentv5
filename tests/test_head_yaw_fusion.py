"""Tests for IMU/base/pan yaw decomposition."""

from base_yaw_controller import (
    HeadYawFusion,
    angular_delta_deg,
    decompose_yaw,
    resolve_fusion_yaw,
)
from lib.person_memory import wrap_degrees as wrap_pm


def _locked_fusion(**kwargs) -> HeadYawFusion:
    fusion = HeadYawFusion()
    fusion.reset_reference(lock_startup=True, **kwargs)
    return fusion


def test_decompose_yaw_neck_only_40deg():
    fusion = _locked_fusion(pan_mech_deg=0.0, base_encoder_deg=0.0, imu_yaw_total_deg=0.0)
    d = decompose_yaw(fusion, imu_yaw_total=40.0, base_encoder_deg=0.0, pan_mech_deg=40.0)
    assert d.true_north_deg == 0.0
    assert abs(d.body_yaw_deg) < 0.01
    assert abs(d.head_yaw_on_body_deg - 40.0) < 0.01
    assert abs(d.world_head_yaw_deg - 40.0) < 0.01
    assert abs(d.head_imu_vs_servo_delta_deg) < 0.01


def test_decompose_yaw_base_plus_neck():
    fusion = _locked_fusion(pan_mech_deg=0.0, base_encoder_deg=0.0, imu_yaw_total_deg=0.0)
    d = decompose_yaw(fusion, imu_yaw_total=60.0, base_encoder_deg=40.0, pan_mech_deg=20.0)
    assert abs(d.body_yaw_deg - 40.0) < 0.01
    assert abs(d.head_yaw_on_body_deg - 20.0) < 0.01
    assert abs(d.world_head_yaw_deg - 60.0) < 0.01
    assert abs(d.head_imu_vs_servo_delta_deg) < 0.01


def test_decompose_yaw_base_spin_head_on_body_unchanged():
    fusion = _locked_fusion(pan_mech_deg=0.0, base_encoder_deg=0.0, imu_yaw_total_deg=0.0)
    d = decompose_yaw(fusion, imu_yaw_total=30.0, base_encoder_deg=30.0, pan_mech_deg=0.0)
    assert abs(d.body_yaw_deg - 30.0) < 0.01
    assert abs(d.head_yaw_on_body_deg) < 0.01


def test_drift_reanchor_preserves_startup_body_display():
    from base_yaw_controller import EncoderImuDriftCorrector

    fusion = _locked_fusion(pan_mech_deg=0.0, base_encoder_deg=0.0, imu_yaw_total_deg=0.0)
    corr = EncoderImuDriftCorrector(stationary_hold_sec=0.1, enc_stable_deg=0.5, pan_stable_deg=0.5)
    t = 1000.0
    raw = 4.0
    for i in range(12):
        corrected, drift, still = corr.update(
            fusion,
            imu_yaw_raw=raw,
            base_encoder_deg=25.0,
            pan_mech_deg=0.0,
            gyro_dps=1.0,
            now=t + i * 0.05,
        )
    assert still is True
    d = decompose_yaw(fusion, imu_yaw_total=corrected, base_encoder_deg=25.0, pan_mech_deg=0.0)
    assert abs(d.body_yaw_deg - 25.0) < 0.01
    assert abs(fusion.ref_base_encoder_deg - 25.0) < 0.01
    assert abs(fusion.startup_base_encoder_deg) < 0.01


def test_resolved_base_always_matches_encoder():
    fusion = HeadYawFusion()
    fusion.reset_reference(pan_mech_deg=0.0, base_encoder_deg=0.0, imu_yaw_total_deg=0.0)
    fusion.imu_yaw_total_deg = 12.0
    imu_yaw, inferred = resolve_fusion_yaw(
        fusion,
        imu_yaw_corrected=12.0,
        base_encoder_deg=118.0,
        pan_mech_deg=8.0,
        prev_base_encoder_deg=115.0,
    )
    assert inferred == 118.0
    assert imu_yaw == 12.0


def test_encoder_base_delta_wraps_near_limit():
    fusion = HeadYawFusion()
    fusion.reset_reference(pan_mech_deg=0.0, base_encoder_deg=115.0, imu_yaw_total_deg=0.0)
    assert abs(fusion.encoder_base_delta_deg(118.0) - 3.0) < 0.01
    assert abs(fusion.encoder_base_delta_deg(-118.0) - wrap_pm(-118.0 - 115.0)) < 0.01


def test_angular_delta_shortest_path():
    assert abs(angular_delta_deg(118.0, 115.0) - 3.0) < 0.01
    assert abs(angular_delta_deg(120.0, 118.0) - 2.0) < 0.01


def test_inferred_base_is_imu_minus_pan_delta():
    fusion = HeadYawFusion(imu_yaw_sign=1.0)
    fusion.reset_reference(pan_mech_deg=0.0, base_encoder_deg=0.0)

    # IMU sees 20° total inertial rotation; head pan moved +5° on its own.
    fusion.imu_yaw_total_deg = 20.0
    assert fusion.inferred_base_delta_deg(5.0) == 15.0
    assert fusion.inferred_base_encoder_deg(5.0) == 15.0


def test_base_turn_with_pan_compensation():
    fusion = HeadYawFusion(imu_yaw_sign=1.0)
    fusion.reset_reference(pan_mech_deg=0.0, base_encoder_deg=0.0, imu_yaw_total_deg=0.0)

    # Base turns +10°, pan compensates -8° so camera stays near forward.
    fusion.imu_yaw_total_deg = 2.0
    assert fusion.inferred_base_delta_deg(-8.0) == 10.0


def test_expected_imu_from_encoder_and_pan():
    fusion = HeadYawFusion(imu_yaw_sign=1.0)
    fusion.reset_reference(pan_mech_deg=0.0, base_encoder_deg=0.0, imu_yaw_total_deg=5.0)
    assert fusion.expected_imu_total_deg(10.0, 15.0) == 30.0  # 5 + 10 pan + 15 base


def test_drift_corrector_reanchors_after_large_base_move():
    from base_yaw_controller import EncoderImuDriftCorrector

    fusion = HeadYawFusion()
    fusion.reset_reference(
        pan_mech_deg=0.0, base_encoder_deg=0.0, imu_yaw_total_deg=0.0, lock_startup=True,
    )
    corr = EncoderImuDriftCorrector(stationary_hold_sec=0.1, enc_stable_deg=0.5, pan_stable_deg=0.5)
    t = 1000.0
    raw = 4.0
    for i in range(12):
        corrected, drift, still = corr.update(
            fusion,
            imu_yaw_raw=raw,
            base_encoder_deg=25.0,
            pan_mech_deg=0.0,
            gyro_dps=1.0,
            now=t + i * 0.05,
        )
    assert still is True
    assert abs(corrected - raw) < 0.01
    assert abs(drift) < 0.01
    assert abs(fusion.ref_base_encoder_deg - 25.0) < 0.01


def test_encoder_drift_corrector_when_stationary():
    from base_yaw_controller import EncoderImuDriftCorrector

    fusion = HeadYawFusion()
    fusion.reset_reference(pan_mech_deg=0.0, base_encoder_deg=0.0, imu_yaw_total_deg=0.0)
    corr = EncoderImuDriftCorrector(stationary_hold_sec=0.1, enc_stable_deg=0.5, pan_stable_deg=0.5)
    t = 1000.0
    raw = 24.0
    for i in range(12):
        corrected, drift, still = corr.update(
            fusion,
            imu_yaw_raw=raw,
            base_encoder_deg=20.0,
            pan_mech_deg=5.0,
            gyro_dps=1.0,
            now=t + i * 0.05,
        )
    assert still is True
    assert abs(corrected - 25.0) < 0.01
    assert abs(drift - 1.0) < 0.01


def test_drift_corrector_near_yaw_limit():
    from base_yaw_controller import EncoderImuDriftCorrector

    fusion = HeadYawFusion()
    fusion.reset_reference(pan_mech_deg=0.0, base_encoder_deg=100.0, imu_yaw_total_deg=100.0)
    corr = EncoderImuDriftCorrector(stationary_hold_sec=0.1, enc_stable_deg=0.5, pan_stable_deg=0.5)
    t = 1000.0
    raw = 117.0
    for i in range(12):
        corrected, drift, still = corr.update(
            fusion,
            imu_yaw_raw=raw,
            base_encoder_deg=118.0,
            pan_mech_deg=0.0,
            gyro_dps=1.0,
            now=t + i * 0.05,
        )
    assert still is True
    assert abs(corrected - 118.0) < 0.01
    assert abs(drift - 1.0) < 0.01


def test_yaw_sign_flips_imu_integration():
    fusion = HeadYawFusion(imu_yaw_sign=-1.0)
    fusion.reset_reference(pan_mech_deg=0.0, base_encoder_deg=0.0)
    fusion.integrate_gyro(100.0, 0.1)
    assert fusion.imu_yaw_total_deg == -10.0


def test_world_yaw_step_budget_at_limit():
    from base_yaw_controller import BaseYawState

    yaw = BaseYawState(max_yaw_deg=120.0)
    yaw.update(118.0, 0.0)
    assert yaw.allow_base_step(5.0, 0.0) is False
    assert yaw.allow_base_step(-3.0, 0.0) is True
