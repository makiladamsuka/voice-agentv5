"""Base yaw helpers: world heading fusion, sector limits, and heading PID."""

from __future__ import annotations

import math
from dataclasses import dataclass


def _clamp(value: float, lo: float, hi: float) -> float:
    return max(lo, min(hi, value))


@dataclass
class BaseYawState:
    max_yaw_deg: float = 120.0
    base_encoder_deg: float = 0.0
    world_yaw_deg: float = 0.0

    def update(self, base_encoder_deg: float, head_pan_offset_deg: float) -> None:
        self.base_encoder_deg = base_encoder_deg
        self.world_yaw_deg = base_encoder_deg + head_pan_offset_deg

    def target_clamped(self, target_world_yaw_deg: float) -> float:
        return _clamp(target_world_yaw_deg, -self.max_yaw_deg, self.max_yaw_deg)

    def allow_base_step(self, step_deg: float, head_pan_offset_deg: float) -> bool:
        projected_base = self.base_encoder_deg + step_deg
        if abs(projected_base) > self.max_yaw_deg:
            return False
        projected_world = projected_base + head_pan_offset_deg
        return abs(projected_world) <= self.max_yaw_deg


@dataclass
class HeadYawFusion:
    """Decompose inertial head yaw into base vs neck-pan using gyro + known pan."""

    imu_yaw_sign: float = 1.0
    ref_pan_mech_deg: float = 0.0
    ref_base_encoder_deg: float = 0.0
    ref_imu_yaw_total_deg: float = 0.0
    imu_yaw_total_deg: float = 0.0
    _last_ts: float | None = None

    def reset_reference(
        self,
        *,
        pan_mech_deg: float,
        base_encoder_deg: float,
        imu_yaw_total_deg: float = 0.0,
        now: float | None = None,
    ) -> None:
        self.ref_pan_mech_deg = pan_mech_deg
        self.ref_base_encoder_deg = base_encoder_deg
        self.ref_imu_yaw_total_deg = imu_yaw_total_deg
        self.imu_yaw_total_deg = imu_yaw_total_deg
        self._last_ts = now

    def expected_imu_total_deg(self, pan_mech_deg: float, base_encoder_deg: float) -> float:
        """IMU total yaw consistent with encoder base + known pan (ground truth when still)."""
        return (
            self.ref_imu_yaw_total_deg
            + self.encoder_base_delta_deg(base_encoder_deg)
            + self.pan_delta_deg(pan_mech_deg)
        )

    def integrate_gyro(self, gyro_z_dps: float, dt: float) -> float:
        dt = max(0.0, min(0.2, dt))
        delta = gyro_z_dps * self.imu_yaw_sign * dt
        self.imu_yaw_total_deg += delta
        return delta

    def pan_delta_deg(self, pan_mech_deg: float) -> float:
        return pan_mech_deg - self.ref_pan_mech_deg

    def encoder_base_delta_deg(self, base_encoder_deg: float) -> float:
        return base_encoder_deg - self.ref_base_encoder_deg

    def inferred_base_delta_deg(self, pan_mech_deg: float) -> float:
        """Base rotation ≈ total IMU yaw minus neck pan change."""
        return self.imu_yaw_total_deg - self.pan_delta_deg(pan_mech_deg)

    def inferred_base_encoder_deg(self, pan_mech_deg: float) -> float:
        return self.ref_base_encoder_deg + self.inferred_base_delta_deg(pan_mech_deg)

    def world_yaw_deg(self, *, base_encoder_deg: float, pan_mech_deg: float) -> float:
        return base_encoder_deg + pan_mech_deg


@dataclass
class EncoderImuDriftCorrector:
    """When encoder + pan are stable, snap IMU yaw integral to match encoder decomposition."""

    stationary_hold_sec: float = 0.35
    enc_stable_deg: float = 0.2
    pan_stable_deg: float = 0.2
    gyro_max_dps: float = 6.0
    _still_since: float | None = None
    _last_enc: float | None = None
    _last_pan_mech: float | None = None

    def reset_motion_tracking(self) -> None:
        self._still_since = None
        self._last_enc = None
        self._last_pan_mech = None

    def update(
        self,
        fusion: HeadYawFusion,
        *,
        imu_yaw_raw: float,
        base_encoder_deg: float,
        pan_mech_deg: float,
        gyro_dps: float,
        now: float,
    ) -> tuple[float, float, bool]:
        """Returns (corrected_imu_yaw, drift_correction_deg, is_stationary)."""
        enc_stable = (
            self._last_enc is None
            or abs(base_encoder_deg - self._last_enc) <= self.enc_stable_deg
        )
        pan_stable = (
            self._last_pan_mech is None
            or abs(pan_mech_deg - self._last_pan_mech) <= self.pan_stable_deg
        )
        gyro_stable = abs(gyro_dps) <= self.gyro_max_dps
        self._last_enc = base_encoder_deg
        self._last_pan_mech = pan_mech_deg

        if enc_stable and pan_stable and gyro_stable:
            if self._still_since is None:
                self._still_since = now
        else:
            self._still_since = None

        stationary = (
            self._still_since is not None
            and (now - self._still_since) >= self.stationary_hold_sec
        )
        if not stationary:
            return imu_yaw_raw, 0.0, False

        expected = fusion.expected_imu_total_deg(pan_mech_deg, base_encoder_deg)
        correction = expected - imu_yaw_raw
        return expected, correction, True


class HeadingPid:
    def __init__(self, kp: float, kd: float):
        self.kp = kp
        self.kd = kd
        self._prev_error = 0.0
        self._initialized = False

    def reset(self) -> None:
        self._prev_error = 0.0
        self._initialized = False

    def step(
        self,
        *,
        current_world_yaw_deg: float,
        target_world_yaw_deg: float,
        dt: float,
        min_step_deg: float,
        max_step_deg: float,
    ) -> float:
        dt = max(0.001, min(0.2, dt))
        error = target_world_yaw_deg - current_world_yaw_deg
        deriv = 0.0 if not self._initialized else (error - self._prev_error) / dt
        self._prev_error = error
        self._initialized = True

        out = (self.kp * error) + (self.kd * deriv)
        if abs(out) < min_step_deg:
            if abs(error) < (min_step_deg * 0.5):
                return 0.0
            out = math.copysign(min_step_deg, error)
        return _clamp(out, -max_step_deg, max_step_deg)
