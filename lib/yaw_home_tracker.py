"""HOME-relative base yaw: one lock at script start, encoder + IMU side-by-side.

Design (differs from HeadYawFusion / post-spin resync):
  - HOME is captured once when the harness starts (or on Z / H).
  - Encoder offset from HOME is the viz rotation (hardware ground truth).
  - IMU base offset = (imu_total - imu_home) - (pan_mech - pan_home).
  - When still, a slow bias pulls IMU base toward encoder (no startup re-lock).
"""

from __future__ import annotations

import time
from dataclasses import dataclass

from lib.person_memory import wrap_degrees


def _delta(current: float, home: float) -> float:
    return wrap_degrees(current - home)


@dataclass
class HomeSnapshot:
    encoder_deg: float
    encoder_count: int
    imu_yaw_deg: float
    pan_mech_deg: float
    locked_at: float


@dataclass
class YawHomeSample:
    from_home_enc_deg: float
    from_home_imu_deg: float
    disagreement_deg: float
    encoder_deg: float
    encoder_count: int
    encoder_count_delta: int
    encoder_count_raw_delta: int
    imu_yaw_deg: float
    pan_mech_deg: float
    gyro_dps: float
    stationary: bool
    base_busy: bool
    imu_correction_deg: float
    home_age_sec: float


class YawHomeTracker:
    """Track base rotation from a fixed HOME pose."""

    def __init__(
        self,
        *,
        counts_per_degree: float = 31.1667,
        encoder_sign: float = -1.0,
        still_hold_sec: float = 0.35,
        enc_stable_deg: float = 0.25,
        pan_stable_deg: float = 0.25,
        gyro_max_dps: float = 6.0,
        imu_pull_rate: float = 0.18,
    ) -> None:
        self.counts_per_degree = max(float(counts_per_degree), 0.05)
        sign = float(encoder_sign)
        self.encoder_sign = -1.0 if sign < 0.0 else 1.0
        self.still_hold_sec = still_hold_sec
        self.enc_stable_deg = enc_stable_deg
        self.pan_stable_deg = pan_stable_deg
        self.gyro_max_dps = gyro_max_dps
        self.imu_pull_rate = imu_pull_rate
        self._home: HomeSnapshot | None = None
        self._imu_align = 0.0
        self._still_since: float | None = None
        self._last_enc: float | None = None
        self._last_pan: float | None = None

    @property
    def home_locked(self) -> bool:
        return self._home is not None

    def lock_home(
        self,
        *,
        encoder_deg: float,
        encoder_count: int,
        imu_yaw_deg: float,
        pan_mech_deg: float,
        now: float | None = None,
    ) -> None:
        ts = now if now is not None else time.time()
        self._home = HomeSnapshot(
            encoder_deg=float(encoder_deg),
            encoder_count=int(encoder_count),
            imu_yaw_deg=float(imu_yaw_deg),
            pan_mech_deg=float(pan_mech_deg),
            locked_at=ts,
        )
        self._imu_align = 0.0
        self._still_since = None
        self._last_enc = float(encoder_deg)
        self._last_pan = float(pan_mech_deg)

    def update(
        self,
        *,
        encoder_deg: float,
        encoder_count: int,
        imu_yaw_deg: float,
        pan_mech_deg: float,
        gyro_dps: float,
        base_busy: bool = False,
        now: float | None = None,
    ) -> YawHomeSample | None:
        if self._home is None:
            return None
        ts = now if now is not None else time.time()

        enc_from_home = _delta(encoder_deg, self._home.encoder_deg)
        pan_delta = _delta(pan_mech_deg, self._home.pan_mech_deg)
        imu_total_delta = _delta(imu_yaw_deg, self._home.imu_yaw_deg)
        imu_base_raw = _delta(imu_total_delta, pan_delta)
        imu_base = _delta(imu_base_raw, self._imu_align)

        enc_stable = (
            self._last_enc is None
            or abs(_delta(encoder_deg, self._last_enc)) <= self.enc_stable_deg
        )
        pan_stable = (
            self._last_pan is None
            or abs(_delta(pan_mech_deg, self._last_pan)) <= self.pan_stable_deg
        )
        gyro_stable = abs(gyro_dps) <= self.gyro_max_dps
        self._last_enc = float(encoder_deg)
        self._last_pan = float(pan_mech_deg)

        if enc_stable and pan_stable and gyro_stable and not base_busy:
            if self._still_since is None:
                self._still_since = ts
        else:
            self._still_since = None

        stationary = (
            self._still_since is not None
            and (ts - self._still_since) >= self.still_hold_sec
        )

        disagreement = _delta(enc_from_home, imu_base)

        raw_tick_delta = int(encoder_count) - self._home.encoder_count
        # Firmware: deg = count / (CPD * encoder_sign). Display ticks with the same
        # sign as rotation degrees (+deg → +ticks, −deg → −ticks).
        tick_delta = int(round(enc_from_home * self.counts_per_degree))
        if abs(enc_from_home) <= 0.35:
            tick_delta = 0

        if stationary:
            self._imu_align = _delta(self._imu_align, disagreement * self.imu_pull_rate)
            imu_base = _delta(imu_base_raw, self._imu_align)
            disagreement = _delta(enc_from_home, imu_base)

        return YawHomeSample(
            from_home_enc_deg=enc_from_home,
            from_home_imu_deg=imu_base,
            disagreement_deg=disagreement,
            encoder_deg=float(encoder_deg),
            encoder_count=int(encoder_count),
            encoder_count_delta=tick_delta,
            encoder_count_raw_delta=raw_tick_delta,
            imu_yaw_deg=float(imu_yaw_deg),
            pan_mech_deg=float(pan_mech_deg),
            gyro_dps=float(gyro_dps),
            stationary=stationary,
            base_busy=bool(base_busy),
            imu_correction_deg=float(self._imu_align),
            home_age_sec=ts - self._home.locked_at,
        )
