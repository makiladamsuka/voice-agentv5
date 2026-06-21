"""Tests for IMU/base/pan yaw decomposition."""

from base_yaw_controller import HeadYawFusion


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


def test_encoder_drift_corrector_when_stationary():
    from base_yaw_controller import EncoderImuDriftCorrector

    fusion = HeadYawFusion()
    fusion.reset_reference(pan_mech_deg=0.0, base_encoder_deg=0.0, imu_yaw_total_deg=0.0)
    corr = EncoderImuDriftCorrector(stationary_hold_sec=0.1, enc_stable_deg=0.5, pan_stable_deg=0.5)
    t = 1000.0
    raw = 12.0
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
    assert abs(drift - 13.0) < 0.01


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
