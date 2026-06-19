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
    fusion.reset_reference(pan_mech_deg=0.0, base_encoder_deg=0.0)

    # Base turns +10°, pan compensates -8° so camera stays near forward.
    fusion.imu_yaw_total_deg = 2.0
    assert fusion.inferred_base_delta_deg(-8.0) == 10.0


def test_yaw_sign_flips_imu_integration():
    fusion = HeadYawFusion(imu_yaw_sign=-1.0)
    fusion.reset_reference(pan_mech_deg=0.0, base_encoder_deg=0.0)
    fusion.integrate_gyro(100.0, 0.1)
    assert fusion.imu_yaw_total_deg == -10.0
