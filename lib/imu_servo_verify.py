"""Closed-loop IMU vs servo head pose verification (head + optional base encoder)."""

from __future__ import annotations

from dataclasses import dataclass

from base_yaw_controller import angular_delta_deg


def wrap_degrees(value: float) -> float:
    return (value + 180.0) % 360.0 - 180.0


@dataclass(frozen=True)
class VerifyReference:
    """Pose locked at center homing."""

    imu_yaw_deg: float = 0.0
    imu_tilt_deg: float = 0.0
    servo_pan_mech_deg: float = 0.0
    servo_tilt_mech_deg: float = 0.0
    base_encoder_deg: float = 0.0


@dataclass(frozen=True)
class ServoPose:
    pan_cmd: float
    tilt_cmd: float
    pan_mech_deg: float
    tilt_mech_deg: float


@dataclass(frozen=True)
class VerifyErrors:
    pan_error_deg: float
    tilt_error_deg: float
    imu_yaw_delta_deg: float
    imu_tilt_delta_deg: float
    servo_pan_delta_deg: float
    servo_tilt_delta_deg: float


@dataclass(frozen=True)
class YawVerifyState:
    """Base-aware yaw decomposition for closed-loop lab."""

    body_yaw_deg: float
    head_on_body_imu_deg: float
    world_head_yaw_deg: float
    imu_yaw_delta_deg: float
    servo_pan_delta_deg: float
    head_pan_error_deg: float
    tilt_error_deg: float
    imu_tilt_delta_deg: float
    servo_tilt_delta_deg: float
    base_encoder_deg: float


def compute_errors(
    *,
    imu_yaw_deg: float,
    imu_tilt_deg: float,
    servo: ServoPose,
    ref: VerifyReference,
) -> VerifyErrors:
    """pan_error = IMU yaw change minus servo pan mech change since lock (head-only)."""
    imu_yaw_delta = wrap_degrees(imu_yaw_deg - ref.imu_yaw_deg)
    imu_tilt_delta = imu_tilt_deg - ref.imu_tilt_deg
    servo_pan_delta = servo.pan_mech_deg - ref.servo_pan_mech_deg
    servo_tilt_delta = servo.tilt_mech_deg - ref.servo_tilt_mech_deg
    pan_error = wrap_degrees(imu_yaw_delta - servo_pan_delta)
    tilt_error = imu_tilt_delta - servo_tilt_delta
    return VerifyErrors(
        pan_error_deg=pan_error,
        tilt_error_deg=tilt_error,
        imu_yaw_delta_deg=imu_yaw_delta,
        imu_tilt_delta_deg=imu_tilt_delta,
        servo_pan_delta_deg=servo_pan_delta,
        servo_tilt_delta_deg=servo_tilt_delta,
    )


def compute_yaw_verify(
    *,
    imu_yaw_deg: float,
    imu_tilt_deg: float,
    base_encoder_deg: float,
    servo: ServoPose,
    ref: VerifyReference,
) -> YawVerifyState:
    """Split yaw into body (encoder) + head-on-body; pan error excludes base rotation."""
    imu_yaw_delta = angular_delta_deg(imu_yaw_deg, ref.imu_yaw_deg)
    imu_tilt_delta = imu_tilt_deg - ref.imu_tilt_deg
    body_yaw = angular_delta_deg(base_encoder_deg, ref.base_encoder_deg)
    servo_pan_delta = servo.pan_mech_deg - ref.servo_pan_mech_deg
    servo_tilt_delta = servo.tilt_mech_deg - ref.servo_tilt_mech_deg
    head_on_body = angular_delta_deg(imu_yaw_delta, body_yaw)
    world_head_yaw = wrap_degrees(body_yaw + head_on_body)
    head_pan_error = angular_delta_deg(head_on_body, servo_pan_delta)
    tilt_error = imu_tilt_delta - servo_tilt_delta
    return YawVerifyState(
        body_yaw_deg=body_yaw,
        head_on_body_imu_deg=head_on_body,
        world_head_yaw_deg=world_head_yaw,
        imu_yaw_delta_deg=imu_yaw_delta,
        servo_pan_delta_deg=servo_pan_delta,
        head_pan_error_deg=head_pan_error,
        tilt_error_deg=tilt_error,
        imu_tilt_delta_deg=imu_tilt_delta,
        servo_tilt_delta_deg=servo_tilt_delta,
        base_encoder_deg=base_encoder_deg,
    )
