"""Per-spin arm lean deltas for cumulative base-turn gestures."""

from __future__ import annotations


def lean_magnitude_deg(
    step_deg: float,
    *,
    step_delta_deg: float,
    ref_step_deg: float = 15.0,
) -> float:
    """Small delta per spin; scales slightly with commanded step size."""
    if abs(step_deg) < 0.01 or step_delta_deg <= 0.0:
        return 0.0
    ref = max(ref_step_deg, 1.0)
    scale = min(abs(step_deg) / ref, 1.0)
    return step_delta_deg * scale


def lean_direction(step_deg: float, *, turn_sign: float = 1.0) -> float:
    """+1 = right-turn lean; flip with turn_sign."""
    if abs(step_deg) < 0.01:
        return 0.0
    base = 1.0 if step_deg > 0.0 else -1.0
    return turn_sign * base


def lean_delta_per_spin(
    step_deg: float,
    *,
    step_delta_deg: float,
    turn_sign: float = 1.0,
    ref_step_deg: float = 15.0,
    sweep_factor: float = 0.45,
) -> tuple[float, float, float, float]:
    """Return (dA0, dA1, dA2, dA3) to add to the current pose after each base spin.

    Right turn: left raise up (A1↓), right sweep in (A2↓), slight right raise (A0↑).
    Left turn: opposite deltas (accumulates back toward home over alternating turns).
    """
    direction = lean_direction(step_deg, turn_sign=turn_sign)
    if direction == 0.0:
        return (0.0, 0.0, 0.0, 0.0)
    mag = lean_magnitude_deg(
        step_deg, step_delta_deg=step_delta_deg, ref_step_deg=ref_step_deg
    )
    if mag <= 0.0:
        return (0.0, 0.0, 0.0, 0.0)
    sweep = mag * sweep_factor
    if direction > 0.0:
        return (mag * 0.35, -mag, -sweep, 0.0)
    return (-mag * 0.35, mag, sweep, 0.0)


def apply_base_lean(
    home: tuple[float, float, float, float],
    step_deg: float,
    *,
    max_delta_deg: float,
    turn_sign: float = 1.0,
    ref_step_deg: float = 15.0,
    progress: float = 1.0,
    sweep_factor: float = 0.55,
) -> tuple[float, float, float, float]:
    """Absolute lean pose from home (used by tests / jogger helpers)."""
    d = lean_delta_per_spin(
        step_deg,
        step_delta_deg=max_delta_deg,
        turn_sign=turn_sign,
        ref_step_deg=ref_step_deg,
        sweep_factor=sweep_factor,
    )
    scale = max(0.0, min(1.0, progress))
    return tuple(home[i] + d[i] * scale for i in range(4))
