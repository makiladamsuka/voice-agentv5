"""Base motor runaway guard with head-pan-aware IMU comparison."""

from __future__ import annotations

import time
from dataclasses import dataclass
from typing import Callable, Optional, TYPE_CHECKING

if TYPE_CHECKING:
    from core.blackboard import Blackboard


@dataclass
class BaseSafetyConfig:
    error_backoff_sec: float = 45.0
    gyro_runaway_scale: float = 1.5
    gyro_slip_scale: float = 0.25
    encoder_runaway_margin_deg: float = 8.0
    encoder_lag_ratio: float = 0.25
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
        return now >= self.blocked_until and self.motion_allowed

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
    """Compare encoder delta vs IMU gyro integral during active base moves."""

    def __init__(
        self,
        *,
        link,
        gate: BaseMotionGate,
        config: BaseSafetyConfig,
        imu_reader=None,
        bb: "Blackboard | None" = None,
        on_fault: Optional[Callable[[str], None]] = None,
    ):
        self._link = link
        self._imu = imu_reader
        self._bb = bb
        self._gate = gate
        self._cfg = config
        self._on_fault = on_fault
        self._active = False
        self._encoder_start_deg = 0.0
        self._pan_start_deg = 0.0
        self._commanded_deg = 0.0
        self._last_poll = 0.0
        self._status = "OK"

    @property
    def status(self) -> str:
        return self._status

    @property
    def active(self) -> bool:
        return self._active

    def start_move(self, *, commanded_deg: float, encoder_deg: float, pan_offset_deg: float) -> None:
        self._encoder_start_deg = encoder_deg
        self._pan_start_deg = pan_offset_deg
        self._commanded_deg = abs(commanded_deg)
        if self._imu is not None:
            self._imu.filter.reset_yaw_integral()
        elif self._bb is not None:
            self._bb.write(base_watchdog_reset=True)
        self._active = True
        self._status = "ARMED"
        self._last_poll = 0.0

    def finish_move(self) -> None:
        self._active = False
        if self._status == "ARMED":
            self._status = "OK"

    def _gyro_integral(self) -> float:
        if self._imu is not None:
            return self._imu.filter.yaw_integral_deg() * getattr(self._imu, "yaw_sign", 1.0)
        if self._bb is not None:
            return self._bb.read("imu_yaw_integral_deg")["imu_yaw_integral_deg"]
        return 0.0

    def tick(self, *, pan_offset_deg: float, now: Optional[float] = None) -> Optional[str]:
        if not self._active:
            return None
        now = time.time() if now is None else now
        if now - self._last_poll < self._cfg.poll_interval_sec:
            return None
        self._last_poll = now

        st = self._link.query_status()
        if st is None:
            base_error = getattr(self._link, "last_base_error", None)
            if base_error:
                self._trip(f"firmware {base_error}", now)
                return base_error
            return None

        encoder_delta = st.degrees - self._encoder_start_deg
        pan_delta = pan_offset_deg - self._pan_start_deg
        expected_total = encoder_delta + pan_delta
        gyro_integral = self._gyro_integral()

        commanded = max(0.1, self._commanded_deg)
        encoder_abs = abs(encoder_delta)
        gyro_abs = abs(gyro_integral)
        expected_abs = abs(expected_total)
        gyro_limit = max(self._cfg.gyro_runaway_scale * commanded, self._cfg.min_gyro_runaway_deg)

        reason: Optional[str] = None
        if encoder_abs > commanded + self._cfg.encoder_runaway_margin_deg:
            reason = f"encoder runaway ({encoder_delta:+.1f}° vs commanded {commanded:+.1f}°)"
        elif gyro_abs > gyro_limit:
            reason = f"gyro spin ({gyro_integral:+.1f}° integral vs limit {gyro_limit:.1f}°)"
        elif st.busy and commanded >= 3.0 and gyro_abs > max(8.0, commanded * 0.35) and encoder_abs < commanded * self._cfg.encoder_lag_ratio:
            reason = (
                f"encoder lag vs gyro "
                f"(enc {encoder_delta:+.1f}°, gyro {gyro_integral:+.1f}°, cmd {commanded:.1f}°)"
            )
        elif st.busy and expected_abs > 3.0 and gyro_abs < self._cfg.gyro_slip_scale * expected_abs:
            reason = (
                f"encoder/pan vs gyro slip "
                f"(enc {encoder_delta:+.1f}°, pan {pan_delta:+.1f}°, gyro {gyro_integral:+.1f}°)"
            )

        if reason is None and not st.busy:
            if commanded >= 3.0 and encoder_abs < commanded * 0.35 and gyro_abs > max(6.0, commanded * 0.25):
                self._trip(
                    f"encoder did not track move (enc {encoder_delta:+.1f}°, gyro {gyro_integral:+.1f}°)",
                    now,
                )
                return self._gate.last_reason
            if encoder_abs >= max(0.5, commanded * 0.35):
                self.finish_move()
                return None
        if reason is None:
            return None

        self._trip(reason, now)
        return reason

    def _trip(self, reason: str, now: float) -> None:
        self._active = False
        self._status = "FAULT"
        self._link.write_base_stop()
        self._gate.record_fault(reason, now)
        if self._bb is not None:
            self._bb.write(base_motion_allowed=False, base_fault_reason=reason)
        if self._on_fault is not None:
            self._on_fault(reason)
