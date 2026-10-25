"""Motion curves and trajectory generators for organic biological robotics.

Implements minimum-jerk, cartoon anticipation/overshoot, and asymmetric
biological respiration curves (inspired by Reachy Mini and Disney animation principles).
"""

from __future__ import annotations

import math
from typing import Callable, Literal


# ── Mathematical curve functions: domain [0.0, 1.0] -> range [0.0, 1.0] ──────


def minjerk(t: float) -> float:
    """Minimum-jerk normalized trajectory (5th order polynomial).

    Matches human motor control by minimizing derivative of acceleration (jerk).
    Boundary conditions: s(0)=0, s(1)=1, s'(0)=s'(1)=0, s''(0)=s''(1)=0.
    """
    if t <= 0.0:
        return 0.0
    if t >= 1.0:
        return 1.0
    return 10.0 * (t**3) - 15.0 * (t**4) + 6.0 * (t**5)


def minjerk_velocity(t: float) -> float:
    """First derivative (velocity profile) of minimum-jerk trajectory.

    Reaches peak velocity of 1.875 at t = 0.5.
    """
    if t <= 0.0 or t >= 1.0:
        return 0.0
    return 30.0 * (t**2) - 60.0 * (t**3) + 30.0 * (t**4)


def cartoon(t: float, overshoot: float = 0.12, anticipation: float = 0.06) -> float:
    """Cartoon-style curve with slight anticipation (wind-up) and overshoot.

    - First 12%: subtle recoil in opposite direction (anticipation).
    - 12%..75%: rapid snappy acceleration toward target + overshoot.
    - 75%..100%: settles smoothly back to 1.0.
    """
    if t <= 0.0:
        return 0.0
    if t >= 1.0:
        return 1.0

    if t < 0.12:
        # Subtle wind-up in reverse
        u = t / 0.12
        return -anticipation * math.sin(u * math.pi)
    elif t < 0.75:
        # Fast thrust with overshoot peak at t=0.75
        u = (t - 0.12) / (0.75 - 0.12)
        curve_u = minjerk(u)
        return -anticipation * (1.0 - curve_u) + (1.0 + overshoot) * curve_u
    else:
        # Settle from overshoot back to 1.0
        u = (t - 0.75) / (1.0 - 0.75)
        curve_u = minjerk(u)
        return (1.0 + overshoot) - overshoot * curve_u


def ease_in_out(t: float) -> float:
    """Standard sinusoidal ease-in-out curve."""
    if t <= 0.0:
        return 0.0
    if t >= 1.0:
        return 1.0
    return 0.5 * (1.0 - math.cos(math.pi * t))


def ease_out_cubic(t: float) -> float:
    """Fast initial acceleration with gentle deceleration."""
    if t <= 0.0:
        return 0.0
    if t >= 1.0:
        return 1.0
    return 1.0 - math.pow(1.0 - t, 3)


# ── Biological Respiration Oscillator ──────────────────────────────────────────


def biological_respiration(
    time_sec: float,
    period: float = 4.2,
    inhale_ratio: float = 0.42,
) -> float:
    """Asymmetric biological respiration curve in range [-1.0, +1.0].

    In living humans, inhalation is slightly faster (~42% of breath cycle)
    and chest/head rise smoothly; exhalation is longer (~58%) with a gentle relax.

    Args:
        time_sec: Current monotonic clock or timestamp.
        period: Total breath cycle duration in seconds (default ~4.2s = 14 breaths/min).
        inhale_ratio: Fraction of cycle spent inhaling (default 0.42).

    Returns:
        float: Normalized respiratory displacement between -1.0 and +1.0.
    """
    if period <= 0.01:
        return 0.0

    cycle_pos = (time_sec % period) / period  # [0.0, 1.0)

    if cycle_pos < inhale_ratio:
        # Inhaling: -1.0 -> +1.0
        u = cycle_pos / max(0.01, inhale_ratio)
        return -1.0 + 2.0 * ease_in_out(u)
    else:
        # Exhaling: +1.0 -> -1.0
        u = (cycle_pos - inhale_ratio) / max(0.01, 1.0 - inhale_ratio)
        return 1.0 - 2.0 * ease_in_out(u)


# ── Trajectory Interpolator Helper ─────────────────────────────────────────────

CurveType = Literal["minjerk", "cartoon", "ease_in_out", "ease_out_cubic", "linear"]


class Trajectory:
    """Stateful 1D smooth trajectory generator."""

    def __init__(
        self,
        start: float,
        target: float,
        duration: float,
        curve: CurveType = "minjerk",
        overshoot: float = 0.12,
    ):
        self.start = float(start)
        self.target = float(target)
        self.duration = max(0.001, float(duration))
        self.curve = curve
        self.overshoot = float(overshoot)
        self.elapsed = 0.0
        self.current = float(start)
        self.is_done = False

    def tick(self, dt: float) -> tuple[float, bool]:
        """Advance time by dt seconds and return (current_position, is_done)."""
        if self.is_done:
            return self.target, True

        self.elapsed += max(0.0, dt)
        t = min(1.0, self.elapsed / self.duration)

        if self.curve == "minjerk":
            frac = minjerk(t)
        elif self.curve == "cartoon":
            frac = cartoon(t, overshoot=self.overshoot)
        elif self.curve == "ease_in_out":
            frac = ease_in_out(t)
        elif self.curve == "ease_out_cubic":
            frac = ease_out_cubic(t)
        else:
            frac = t  # linear

        self.current = self.start + (self.target - self.start) * frac

        if t >= 1.0:
            self.current = self.target
            self.is_done = True

        return self.current, self.is_done
