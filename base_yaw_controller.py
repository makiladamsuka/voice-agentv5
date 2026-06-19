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
        projected_world = self.base_encoder_deg + step_deg + head_pan_offset_deg
        return abs(projected_world) <= self.max_yaw_deg


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
