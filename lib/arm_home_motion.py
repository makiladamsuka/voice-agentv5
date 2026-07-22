"""Smooth arm homing — used by start_robot shutdown."""

from __future__ import annotations

import time
from typing import Protocol

from arm_safety_envelope import ArmSafetyEnvelope

HOME_BLEND_DEG_PER_SEC = 45.0
HOME_JOG_HZ = 30.0


class ArmLink(Protocol):
    def write_arms(
        self, a0: float, a1: float, a2: float, a3: float, *, force: bool = False
    ) -> bool: ...


def _smoothstep(t: float) -> float:
    t = max(0.0, min(1.0, t))
    return t * t * (3.0 - 2.0 * t)


def lerp_pose_clamped(
    start: tuple[float, float, float, float],
    target: tuple[float, float, float, float],
    t: float,
    envelope: ArmSafetyEnvelope,
) -> tuple[float, float, float, float]:
    e = _smoothstep(t)
    raw = tuple(start[i] + (target[i] - start[i]) * e for i in range(4))
    return envelope.clamp_arms(*raw)


def smooth_home_arms(
    link: ArmLink,
    start: tuple[float, float, float, float],
    homes: tuple[float, float, float, float],
    envelope: ArmSafetyEnvelope,
    *,
    blend_sec: float = 0.6,
    jog_hz: float = HOME_JOG_HZ,
) -> tuple[float, float, float, float]:
    """Blend arms to home with safety envelope; returns final pose."""
    target = envelope.clamp_arms(*homes)
    max_delta = max(abs(start[i] - target[i]) for i in range(4))
    if max_delta < 0.5:
        link.write_arms(*target, force=True)
        time.sleep(0.5)
        return target

    duration = max(blend_sec, max_delta / HOME_BLEND_DEG_PER_SEC, 0.5)
    interval = 1.0 / max(1.0, jog_hz)
    t0 = time.time()
    print(f"homing arms ({duration:.1f}s)...", flush=True)
    pose = start
    while True:
        now = time.time()
        t = (now - t0) / duration
        if t >= 1.0:
            pose = target
            link.write_arms(*pose, force=True)
            break
        pose = lerp_pose_clamped(start, target, t, envelope)
        link.write_arms(*pose, force=True)
        time.sleep(interval)
    time.sleep(0.5)
    return pose
