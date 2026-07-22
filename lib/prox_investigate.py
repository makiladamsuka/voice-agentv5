"""Helpers for ToF proximity investigate flow."""

from __future__ import annotations

from lib.motion_memory import zone_to_yaw_offset
from lib.person_memory import wrap_degrees


def zone_world_yaw(zone: str, base_world_yaw_deg: float, zone_yaw_deg: float) -> float:
    return wrap_degrees(base_world_yaw_deg + zone_to_yaw_offset(zone, zone_yaw_deg))


def glance_emotion_for_zone(zone: str) -> str:
    if zone == "L":
        return "looking_left_natural"
    if zone == "R":
        return "looking_right_natural"
    return "attentive"
