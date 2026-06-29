"""HOME-relative base yaw: IMU-primary heading with encoder anti-drift when still.

  - HOME locked once at start (or on Z / H).
  - Base rotation from HOME follows IMU (pan-compensated) while moving.
  - When the base is still (no encoder tick change), IMU base yaw is snapped to
    the encoder offset so gyro integral cannot drift on a stationary base.
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
    """Track base rotation from HOME: IMU while moving, encoder locks drift when still."""

    def __init__(
        self,
        *,
        counts_per_degree: float = 31.1667,
        encoder_sign: float = -1.0,
        still_hold_sec: float = 0.35,
        gyro_max_dps: float = 6.0,
        snap_max_disagreement_deg: float = 5.0,
    ) -> None:
        self.counts_per_degree = max(float(counts_per_degree), 0.05)
        sign = float(encoder_sign)
        self.encoder_sign = -1.0 if sign < 0.0 else 1.0
        self.still_hold_sec = still_hold_sec
        self.gyro_max_dps = gyro_max_dps
        self.snap_max_disagreement_deg = snap_max_disagreement_deg
        self._home: HomeSnapshot | None = None
        self._imu_align = 0.0
        self._still_since: float | None = None
        self._last_enc_count: int | None = None

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
        self._last_enc_count = int(encoder_count)

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

        count = int(encoder_count)
        ticks_stable = self._last_enc_count is None or count == self._last_enc_count
        gyro_stable = abs(gyro_dps) <= self.gyro_max_dps
        self._last_enc_count = count

        if ticks_stable and gyro_stable and not base_busy:
            if self._still_since is None:
                self._still_since = ts
        else:
            self._still_since = None

        stationary = (
            self._still_since is not None
            and (ts - self._still_since) >= self.still_hold_sec
        )

        disagreement = _delta(enc_from_home, imu_base)

        raw_tick_delta = count - self._home.encoder_count
        tick_delta = int(round(enc_from_home * self.counts_per_degree))
        if abs(enc_from_home) <= 0.35:
            tick_delta = 0

        if stationary and abs(disagreement) <= self.snap_max_disagreement_deg:
            # Idle drift only — snap when IMU and encoder already agree.
            self._imu_align = _delta(imu_base_raw, enc_from_home)
            imu_base = enc_from_home
            disagreement = 0.0

        return YawHomeSample(
            from_home_enc_deg=enc_from_home,
            from_home_imu_deg=imu_base,
            disagreement_deg=disagreement,
            encoder_deg=float(encoder_deg),
            encoder_count=count,
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

    def force_snap_imu_to_encoder(
        self,
        *,
        encoder_deg: float,
        imu_yaw_deg: float,
        pan_mech_deg: float,
    ) -> None:
        """After a base maneuver, align IMU base-from-HOME to encoder (any disagreement)."""
        if self._home is None:
            return
        enc_from_home = _delta(encoder_deg, self._home.encoder_deg)
        pan_delta = _delta(pan_mech_deg, self._home.pan_mech_deg)
        imu_total_delta = _delta(imu_yaw_deg, self._home.imu_yaw_deg)
        imu_base_raw = _delta(imu_total_delta, pan_delta)
        self._imu_align = _delta(imu_base_raw, enc_from_home)
        self._still_since = None

    def note_imu_yaw_reset(
        self,
        *,
        encoder_deg: float,
        imu_yaw_deg: float,
        pan_mech_deg: float,
    ) -> None:
        """Re-anchor IMU HOME after watchdog integral reset; keep encoder-from-HOME."""
        if self._home is None:
            return
        enc_from_home = _delta(encoder_deg, self._home.encoder_deg)
        self._home = HomeSnapshot(
            encoder_deg=self._home.encoder_deg,
            encoder_count=self._home.encoder_count,
            imu_yaw_deg=float(imu_yaw_deg),
            pan_mech_deg=float(pan_mech_deg),
            locked_at=self._home.locked_at,
        )
        # imu_base_raw becomes 0; align so compensated yaw matches encoder.
        self._imu_align = _delta(0.0, enc_from_home)
        self._still_since = None
        self._last_enc_count = None
