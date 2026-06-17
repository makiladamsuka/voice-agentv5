"""Base motor runaway guard — compare encoder motion vs head IMU gyro during B moves."""

from __future__ import annotations

import time
from dataclasses import dataclass
from typing import TYPE_CHECKING, Callable, Optional

if TYPE_CHECKING:
    from arduino_servo import ArduinoServoLink
    from imu_sensor import ImuReader


@dataclass
class BaseSafetyConfig:
    error_backoff_sec: float = 45.0
    gyro_runaway_scale: float = 1.5
    gyro_slip_scale: float = 0.25
    encoder_runaway_margin_deg: float = 8.0
    min_gyro_runaway_deg: float = 25.0
    poll_interval_sec: float = 0.04


class BaseMotionGate:
    """Tracks whether auto base moves are allowed after a fault."""

    def __init__(self, backoff_sec: float = 45.0):
        self.backoff_sec = backoff_sec
        self.motion_allowed = True
        self.blocked_until = 0.0
        self.last_reason: Optional[str] = None

    def allowed(self, now: Optional[float] = None) -> bool:
        now = time.time() if now is None else now
        if now < self.blocked_until:
            return False
        return self.motion_allowed

    def record_fault(self, reason: str, now: Optional[float] = None) -> None:
        now = time.time() if now is None else now
        self.motion_allowed = False
        self.blocked_until = now + self.backoff_sec
        self.last_reason = reason
        print(f"Base motion paused {self.backoff_sec:.0f}s ({reason})")

    def clear_backoff(self, now: Optional[float] = None) -> None:
        now = time.time() if now is None else now
        if now >= self.blocked_until:
            self.motion_allowed = True


class BaseMoveWatchdog:
    """Compare encoder delta vs gyro integral while a base move is active."""

    def __init__(
        self,
        link: ArduinoServoLink,
        imu: ImuReader,
        gate: BaseMotionGate,
        config: BaseSafetyConfig,
        *,
        on_fault: Optional[Callable[[str], None]] = None,
    ):
        self._link = link
        self._imu = imu
        self._gate = gate
        self._cfg = config
        self._on_fault = on_fault
        self._active = False
        self._encoder_start_deg = 0.0
        self._commanded_deg = 0.0
        self._last_poll = 0.0
        self._status = "OK"

    @property
    def status(self) -> str:
        return self._status

    @property
    def active(self) -> bool:
        return self._active

    def start_move(self, commanded_deg: float, encoder_deg: Optional[float] = None) -> None:
        if encoder_deg is None:
            st = self._link.query_status()
            encoder_deg = st.degrees if st is not None else 0.0
        self._encoder_start_deg = encoder_deg
        self._commanded_deg = commanded_deg
        self._imu.filter.reset_yaw_integral()
        self._active = True
        self._status = "ARMED"
        self._last_poll = 0.0

    def finish_move(self) -> None:
        self._active = False
        if self._status == "ARMED":
            self._status = "OK"

    def tick(self, now: Optional[float] = None) -> Optional[str]:
        if not self._active:
            return None
        now = time.time() if now is None else now
        if now - self._last_poll < self._cfg.poll_interval_sec:
            return None
        self._last_poll = now

        st = self._link.query_status()
        if st is None:
            return None

        encoder_delta = st.degrees - self._encoder_start_deg
        gyro_integral = self._imu.filter.yaw_integral_deg()
        commanded = abs(self._commanded_deg)
        encoder_abs = abs(encoder_delta)
        gyro_abs = abs(gyro_integral)
        gyro_limit = max(self._cfg.gyro_runaway_scale * commanded, self._cfg.min_gyro_runaway_deg)

        reason: Optional[str] = None
        if encoder_abs > commanded + self._cfg.encoder_runaway_margin_deg:
            reason = (
                f"encoder runaway ({encoder_delta:+.1f}° vs commanded {self._commanded_deg:+.1f}°)"
            )
        elif gyro_abs > gyro_limit:
            reason = (
                f"gyro spin ({gyro_integral:+.1f}° integral vs limit {gyro_limit:.1f}°)"
            )
        elif st.busy and encoder_abs > 3.0 and gyro_abs < self._cfg.gyro_slip_scale * encoder_abs:
            reason = (
                f"encoder/gyro slip (enc {encoder_delta:+.1f}°, gyro {gyro_integral:+.1f}°)"
            )

        if reason is None and not st.busy and encoder_abs >= max(0.5, commanded * 0.5):
            self.finish_move()
            self._status = "OK"
            return None

        if reason is None:
            self._status = "ARMED"
            return None

        self._trip(reason, now)
        return reason

    def _trip(self, reason: str, now: float) -> None:
        self._active = False
        self._status = "FAULT"
        self._link.write_base_stop()
        self._gate.record_fault(reason, now)
        if self._on_fault is not None:
            self._on_fault(reason)


def clamp_base_step(deg: float, max_move_deg: float) -> float:
    cap = max(0.0, max_move_deg)
    if cap <= 0:
        return deg
    return max(-cap, min(cap, deg))
