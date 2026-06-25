"""BMI160 head mount frame — PCB orientation only.

The chip is fixed on the neck/head PCB:
  sensor +X = up (toward top of head)
  sensor +Y = left
  sensor +Z = back (away from face)

Head frame after remap:
  forward = -sensor Z
  left    = +sensor Y
  up/yaw  = -sensor X   (yaw_sign applied when integrating gyro)
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Sequence

try:
    import yaml
except ImportError:
    yaml = None

DEFAULT_AXIS_REMAP: tuple[int, ...] = (-3, 2, -1)
DEFAULT_YAW_SIGN: float = -1.0


@dataclass(frozen=True)
class HeadMount:
    """Immutable mount definition for a head-fixed BMI160."""

    axis_remap: tuple[int, ...] = DEFAULT_AXIS_REMAP
    yaw_sign: float = DEFAULT_YAW_SIGN

    def remap_vec(self, vec: Sequence[float]) -> tuple[float, float, float]:
        """Map sensor XYZ into head frame [forward, left, up]."""
        out: list[float] = []
        for idx in self.axis_remap:
            sign = -1.0 if idx < 0 else 1.0
            axis = abs(int(idx))
            if axis in (1, 2, 3):
                out.append(vec[axis - 1] * sign)
            elif axis in (0, 1, 2):
                out.append(vec[axis] * sign)
            else:
                out.append(0.0)
        while len(out) < 3:
            out.append(0.0)
        return out[0], out[1], out[2]

    def signed_yaw_rate_dps(self, gyro_up_dps: float) -> float:
        return gyro_up_dps * (1.0 if self.yaw_sign >= 0.0 else -1.0)


def load_head_mount(config_path: Path | None = None) -> HeadMount:
    """Load mount from config.yaml imu section (axis_remap + yaw_sign only)."""
    if config_path is None:
        config_path = Path(__file__).resolve().parent.parent / "config.yaml"
    if yaml is None or not config_path.exists():
        return HeadMount()
    with open(config_path, encoding="utf-8") as f:
        cfg = yaml.safe_load(f) or {}
    imu = cfg.get("imu") or {}
    axis = imu.get("axis_remap")
    remap = tuple(int(v) for v in axis) if axis else DEFAULT_AXIS_REMAP
    yaw_sign = float(imu.get("yaw_sign", DEFAULT_YAW_SIGN))
    return HeadMount(axis_remap=remap, yaw_sign=yaw_sign)
