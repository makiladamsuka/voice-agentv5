"""Head-lead direction rules for base rotation.

Base may only rotate in the same direction the neck has already turned.
"""

from __future__ import annotations

import math


def head_lead_sign(pan_mech_deg: float) -> float:
    """Return +1 / -1 for head pan direction, 0 if near center."""
    if pan_mech_deg > 0.5:
        return 1.0
    if pan_mech_deg < -0.5:
        return -1.0
    return 0.0


def step_agrees_with_head(
    step_deg: float,
    pan_mech_deg: float,
    *,
    head_lead_min_deg: float,
) -> bool:
    """True when step is non-zero, head has led, and signs match."""
    if abs(step_deg) < 1e-6:
        return False
    if abs(pan_mech_deg) < head_lead_min_deg:
        return False
    head_sign = head_lead_sign(pan_mech_deg)
    if head_sign == 0.0:
        return False
    step_sign = 1.0 if step_deg > 0.0 else -1.0
    return step_sign == head_sign


def aim_agrees_with_head(aim_norm_x: float, pan_mech_deg: float, *, deadzone: float = 0.04) -> bool:
    """True when face/body aim direction matches head pan direction."""
    if abs(aim_norm_x) <= deadzone:
        return True
    head_sign = head_lead_sign(pan_mech_deg)
    if head_sign == 0.0:
        return False
    aim_sign = 1.0 if aim_norm_x > 0.0 else -1.0
    return aim_sign * head_sign > 0.0


def plan_aim_base_step(
    pan_mech_deg: float,
    aim_norm_x: float,
    *,
    min_step_deg: float,
    max_step_deg: float,
    aim_gain: float,
    pan_offset_to_step_gain: float = 0.0,
    deadzone: float = 0.04,
) -> float | None:
    """Turn base toward an off-center aim (face/body bbox vs frame center)."""
    if abs(aim_norm_x) <= deadzone:
        return None
    aim_sign = 1.0 if aim_norm_x >= 0.0 else -1.0
    head_sign = head_lead_sign(pan_mech_deg) or aim_sign
    if aim_sign * head_sign < 0.0:
        return None
    mag = max(
        min_step_deg,
        min(
            max_step_deg,
            abs(aim_norm_x) * aim_gain + abs(pan_mech_deg) * pan_offset_to_step_gain,
        ),
    )
    return head_sign * mag


def yaw_error_agrees_with_head(
    yaw_error_deg: float,
    pan_mech_deg: float,
    *,
    head_lead_min_deg: float,
) -> bool:
    """True when memory/last-seen bearing matches head lead direction."""
    if abs(yaw_error_deg) < 1e-6:
        return False
    err_sign = 1.0 if yaw_error_deg > 0.0 else -1.0
    return step_agrees_with_head(
        err_sign,
        pan_mech_deg,
        head_lead_min_deg=head_lead_min_deg,
    )


def pan_cmd_to_mech(
    pan_cmd: float,
    *,
    pan_center: float,
    pan_min: float,
    pan_max: float,
    mech_left: float,
    mech_right: float,
) -> float:
    if pan_cmd >= pan_center:
        span = max(pan_max - pan_center, 1e-6)
        return (pan_cmd - pan_center) / span * mech_right
    span = max(pan_center - pan_min, 1e-6)
    return (pan_center - pan_cmd) / span * mech_left


def mech_to_pan_cmd(
    pan_mech_deg: float,
    *,
    pan_center: float,
    pan_min: float,
    pan_max: float,
    mech_left: float,
    mech_right: float,
) -> float:
    if pan_mech_deg >= 0.0:
        span = max(mech_right, 1e-6)
        return pan_center + (pan_mech_deg / span) * (pan_max - pan_center)
    span = max(abs(mech_left), 1e-6)
    return pan_center - (abs(pan_mech_deg) / span) * (pan_center - pan_min)


def proactive_comp_pan_cmd(
    base_step_deg: float,
    current_pan_cmd: float,
    *,
    compensation_gain: float,
    pan_center: float,
    pan_min: float,
    pan_max: float,
    mech_left: float,
    mech_right: float,
) -> float:
    """Servo pan command after proactive neck counter-rotation for a base step."""
    pan_mech = pan_cmd_to_mech(
        current_pan_cmd,
        pan_center=pan_center,
        pan_min=pan_min,
        pan_max=pan_max,
        mech_left=mech_left,
        mech_right=mech_right,
    )
    comp_mech = base_step_deg * compensation_gain
    compensated_mech = pan_mech - comp_mech
    return mech_to_pan_cmd(
        compensated_mech,
        pan_center=pan_center,
        pan_min=pan_min,
        pan_max=pan_max,
        mech_left=mech_left,
        mech_right=mech_right,
    )
