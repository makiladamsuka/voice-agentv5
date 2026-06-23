"""Raise-dependent sweep safety envelope from captured arm calibration."""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Literal

DEFAULT_LIMITS_PATH = Path(__file__).resolve().parent / "tests" / "captured_arm_limits.json"


def _clamp(v: float, lo: float, hi: float) -> float:
    return max(lo, min(hi, v))


def _lerp(a: float, b: float, t: float) -> float:
    return a + (b - a) * t


def _ordered_pair(a: float, b: float) -> tuple[float, float]:
    return (min(a, b), max(a, b))


@dataclass(frozen=True)
class _CoupledArm:
    """Raise axis index and sweep axis index with interpolation anchors."""

    raise_idx: int
    sweep_idx: int
    raise_low: float
    raise_high: float
    sweep_home_low: float  # sweep at home (low raise)
    sweep_out_low: float  # max sweep reach at low raise
    sweep_at_high: float  # sweep limit at max raise (narrows / decreases)
    raise_inverted: bool
    raise_min: float
    raise_max: float

    def raise_t(self, raise_deg: float) -> float:
        if self.raise_inverted:
            span = self.raise_low - self.raise_high
            if span <= 0.0:
                return 1.0
            return _clamp((self.raise_low - raise_deg) / span, 0.0, 1.0)
        span = self.raise_high - self.raise_low
        if span <= 0.0:
            return 1.0
        return _clamp((raise_deg - self.raise_low) / span, 0.0, 1.0)

    def sweep_range(self, raise_deg: float) -> tuple[float, float]:
        t = self.raise_t(raise_deg)
        lo = _lerp(self.sweep_out_low, self.sweep_at_high, t)
        hi = _lerp(self.sweep_home_low, self.sweep_at_high, t)
        return _ordered_pair(lo, hi)

    def clamp_raise(self, raise_deg: float) -> float:
        return _clamp(raise_deg, self.raise_min, self.raise_max)


class ArmSafetyEnvelope:
    """Clamp arm pose to global raise limits and raise-dependent sweep zones."""

    def __init__(
        self,
        *,
        homes: tuple[float, float, float, float],
        right: _CoupledArm,
        left: _CoupledArm,
    ) -> None:
        self._homes = homes
        self._right = right
        self._left = left

    @property
    def homes(self) -> tuple[float, float, float, float]:
        return self._homes

    @classmethod
    def from_json(cls, path: Path | str = DEFAULT_LIMITS_PATH) -> ArmSafetyEnvelope:
        p = Path(path)
        if not p.is_file():
            raise FileNotFoundError(
                f"Calibration not found: {p}\n"
                "Run test_servo_arms_manual.py and complete the Y capture wizard first."
            )
        data = json.loads(p.read_text(encoding="utf-8"))
        poses = {k: tuple(v) for k, v in data["poses"].items()}
        homes = tuple(data["homes"])
        mins = tuple(data["min"])
        maxs = tuple(data["max"])

        low = poses["raise_low_min_sweep"]
        max_sw_low = poses["max_sweep_at_low"]
        high_raise = poses["max_raise"]
        min_sw_high = poses["min_sweep_at_high"]

        right = _CoupledArm(
            raise_idx=0,
            sweep_idx=2,
            raise_low=low[0],
            raise_high=high_raise[0],
            sweep_home_low=low[2],
            sweep_out_low=max_sw_low[2],
            sweep_at_high=high_raise[2],
            raise_inverted=False,
            raise_min=mins[0],
            raise_max=maxs[0],
        )
        left = _CoupledArm(
            raise_idx=1,
            sweep_idx=3,
            raise_low=low[1],
            raise_high=high_raise[1],
            sweep_home_low=low[3],
            sweep_out_low=max_sw_low[3],
            sweep_at_high=min_sw_high[3],
            raise_inverted=True,
            raise_min=mins[1],
            raise_max=maxs[1],
        )
        return cls(homes=homes, right=right, left=left)

    def _arm_for_side(self, side: Literal["right", "left"]) -> _CoupledArm:
        return self._right if side == "right" else self._left

    def sweep_range(self, *, side: Literal["right", "left"], raise_deg: float) -> tuple[float, float]:
        arm = self._arm_for_side(side)
        return arm.sweep_range(raise_deg)

    def clamp_arms(
        self,
        a0: float,
        a1: float,
        a2: float,
        a3: float,
    ) -> tuple[float, float, float, float]:
        a0 = self._right.clamp_raise(a0)
        a1 = self._left.clamp_raise(a1)
        a2_lo, a2_hi = self._right.sweep_range(a0)
        a3_lo, a3_hi = self._left.sweep_range(a1)
        a2 = _clamp(a2, a2_lo, a2_hi)
        a3 = _clamp(a3, a3_lo, a3_hi)
        return (a0, a1, a2, a3)
