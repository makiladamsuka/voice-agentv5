"""Signed servo command → mechanical head angles (shared by core services)."""

from __future__ import annotations

from head_debug_viz import servo_pan_to_mechanical, servo_tilt_to_mechanical


def pan_mech_kwargs(servo_cfg: dict) -> dict:
    return dict(
        center=float(servo_cfg.get("pan_center", 100.0)),
        p_min=float(servo_cfg.get("pan_min", 25.0)),
        p_max=float(servo_cfg.get("pan_max", 150.0)),
        mech_left_deg=float(servo_cfg.get("pan_mech_left_deg", -40.0)),
        mech_right_deg=float(servo_cfg.get("pan_mech_right_deg", 40.0)),
    )


def tilt_mech_kwargs(servo_cfg: dict) -> dict:
    return dict(
        center=float(servo_cfg.get("tilt_center", 110.0)),
        t_min=float(servo_cfg.get("tilt_min", 100.0)),
        t_max=float(servo_cfg.get("tilt_max", 150.0)),
        mech_down_deg=float(servo_cfg.get("tilt_min_mechanical_deg", -35.0)),
        mech_up_deg=float(servo_cfg.get("tilt_max_mechanical_deg", 45.0)),
    )


def signed_pan_mech_deg(pan_cmd: float, servo_cfg: dict) -> float:
    sign = float(servo_cfg.get("pan_sign", 1.0))
    return servo_pan_to_mechanical(pan_cmd, **pan_mech_kwargs(servo_cfg)) * sign


def signed_tilt_mech_deg(tilt_cmd: float, servo_cfg: dict) -> float:
    sign = float(servo_cfg.get("tilt_sign", -1.0))
    return servo_tilt_to_mechanical(tilt_cmd, **tilt_mech_kwargs(servo_cfg)) * sign
