"""Shared emotion presets and name resolution (ported from voice-agentv4)."""

from __future__ import annotations

import random

EMOTION_PRESETS: dict[str, dict] = {
    "idle": {"scale_w": 1.0, "scale_h": 1.0, "top_lid": 0.0, "bottom_lid": 0.0, "lid_angle": 0.0, "mirror_angle": True},
    "happy": {"scale_w": 1.10, "scale_h": 0.84, "top_lid": 0.0, "bottom_lid": 0.30, "lid_angle": -6.0, "mirror_angle": True},
    "excited": {"scale_w": 1.14, "scale_h": 0.80, "top_lid": 0.0, "bottom_lid": 0.24, "lid_angle": 0.0, "mirror_angle": True},
    "bored": {"scale_w": 1.03, "scale_h": 0.78, "top_lid": 0.48, "bottom_lid": 0.12, "lid_angle": 0.0, "mirror_angle": True},
    "sad": {"scale_w": 0.98, "scale_h": 1.08, "top_lid": 0.20, "bottom_lid": 0.0, "lid_angle": 10.0, "mirror_angle": True},
    "angry": {"scale_w": 1.02, "scale_h": 0.90, "top_lid": 0.24, "bottom_lid": 0.0, "lid_angle": -14.0, "mirror_angle": True},
    "surprised": {"scale_w": 0.98, "scale_h": 1.12, "top_lid": 0.0, "bottom_lid": 0.0, "lid_angle": 0.0, "mirror_angle": True},
    "suspicious": {"scale_w": 1.06, "scale_h": 0.74, "top_lid": 0.38, "bottom_lid": 0.35, "lid_angle": 0.0, "mirror_angle": True},
    "sleepy": {"scale_w": 1.04, "scale_h": 0.88, "top_lid": 0.56, "bottom_lid": 0.0, "lid_angle": 0.0, "mirror_angle": True},
    "looking_left_natural": {"scale_w": 1.02, "scale_h": 0.98, "top_lid": 0.0, "bottom_lid": 0.05, "lid_angle": -3.0, "mirror_angle": False},
    "looking_right_natural": {"scale_w": 1.02, "scale_h": 0.98, "top_lid": 0.0, "bottom_lid": 0.05, "lid_angle": 3.0, "mirror_angle": False},
    "looking_left_happy": {"scale_w": 1.10, "scale_h": 0.84, "top_lid": 0.0, "bottom_lid": 0.30, "lid_angle": -6.0, "mirror_angle": False},
    "looking_right_happy": {"scale_w": 1.10, "scale_h": 0.84, "top_lid": 0.0, "bottom_lid": 0.30, "lid_angle": 6.0, "mirror_angle": False},
    "thinking": {"scale_w": 1.00, "scale_h": 0.92, "top_lid": 0.06, "bottom_lid": 0.02, "lid_angle": 0.0, "mirror_angle": True},
    "concentrating": {"scale_w": 0.96, "scale_h": 0.84, "top_lid": 0.16, "bottom_lid": 0.08, "lid_angle": 0.0, "mirror_angle": True},
    "remembering": {"scale_w": 1.04, "scale_h": 1.03, "top_lid": 0.02, "bottom_lid": 0.0, "lid_angle": 0.0, "mirror_angle": True},
    "attentive": {"scale_w": 1.08, "scale_h": 1.06, "top_lid": 0.0, "bottom_lid": 0.0, "lid_angle": 0.0, "mirror_angle": True},
    "engaged": {"scale_w": 1.02, "scale_h": 1.00, "top_lid": 0.04, "bottom_lid": 0.06, "lid_angle": 5.0, "mirror_angle": True},
    "amused": {"scale_w": 1.00, "scale_h": 0.98, "top_lid": 0.0, "bottom_lid": 0.14, "lid_angle": 3.0, "mirror_angle": True},
    "warm": {"scale_w": 1.06, "scale_h": 1.00, "top_lid": 0.0, "bottom_lid": 0.16, "lid_angle": 2.0, "mirror_angle": True},
    "curious_intense": {"scale_w": 1.04, "scale_h": 1.05, "top_lid": 0.0, "bottom_lid": 0.06, "lid_angle": 8.0, "mirror_angle": False},
    "nodding": {"scale_w": 1.00, "scale_h": 1.00, "top_lid": 0.0, "bottom_lid": 0.0, "lid_angle": 0.0, "mirror_angle": True},
    "awkward": {"scale_w": 0.96, "scale_h": 0.93, "top_lid": 0.10, "bottom_lid": 0.10, "lid_angle": 0.0, "mirror_angle": True},
    "uncertain": {"scale_w": 0.98, "scale_h": 0.96, "top_lid": 0.08, "bottom_lid": 0.04, "lid_angle": 0.0, "mirror_angle": True},
    "apologetic": {"scale_w": 0.95, "scale_h": 0.92, "top_lid": 0.14, "bottom_lid": 0.04, "lid_angle": 6.0, "mirror_angle": True},
    "proud": {"scale_w": 1.06, "scale_h": 1.02, "top_lid": 0.0, "bottom_lid": 0.0, "lid_angle": -2.0, "mirror_angle": True},
    "playful": {"scale_w": 1.02, "scale_h": 1.00, "top_lid": 0.0, "bottom_lid": 0.06, "lid_angle": 0.0, "mirror_angle": False},
    "cheerful": {"scale_w": 1.08, "scale_h": 0.86, "top_lid": 0.0, "bottom_lid": 0.22, "lid_angle": -4.0, "mirror_angle": True},
    "content": {"scale_w": 1.05, "scale_h": 0.96, "top_lid": 0.0, "bottom_lid": 0.10, "lid_angle": 2.0, "mirror_angle": True},
    "looking_left_cheerful": {"scale_w": 1.08, "scale_h": 0.86, "top_lid": 0.0, "bottom_lid": 0.22, "lid_angle": -4.0, "mirror_angle": False},
    "looking_right_cheerful": {"scale_w": 1.08, "scale_h": 0.86, "top_lid": 0.0, "bottom_lid": 0.22, "lid_angle": 4.0, "mirror_angle": False},
    "squint": {"scale_w": 1.0, "scale_h": 0.62, "top_lid": 0.42, "bottom_lid": 0.35, "lid_angle": 0.0, "mirror_angle": True},
}

EMOTION_INTENSITY: dict[str, float] = {
    "idle": 0.45,
    "looking_left_natural": 0.50,
    "looking_right_natural": 0.50,
    "looking_left_happy": 0.52,
    "looking_right_happy": 0.52,
    "happy": 0.55,
    "excited": 0.62,
    "surprised": 0.70,
    "sad": 0.60,
    "angry": 0.58,
    "suspicious": 0.56,
    "sleepy": 0.62,
    "bored": 0.58,
    "thinking": 0.52,
    "concentrating": 0.58,
    "remembering": 0.50,
    "attentive": 0.56,
    "engaged": 0.54,
    "amused": 0.50,
    "warm": 0.52,
    "curious_intense": 0.56,
    "nodding": 0.45,
    "awkward": 0.48,
    "uncertain": 0.48,
    "apologetic": 0.50,
    "proud": 0.54,
    "playful": 0.50,
    "cheerful": 0.54,
    "content": 0.50,
    "looking_left_cheerful": 0.52,
    "looking_right_cheerful": 0.52,
    "squint": 0.85,
}

EMOTION_NAME_MAP: dict[str, str] = {
    "looking_left": "looking_left_happy",
    "looking_right": "looking_right_happy",
    "calm": "content",
    "curious": "curious_intense",
    "afraid": "uncertain",
}


def map_surroundings_emotion(name: str) -> str:
    return EMOTION_NAME_MAP.get(name, name)


def weighted_pick(weights: dict[str, float], fallback: str = "idle") -> str:
    total = sum(w for w in weights.values() if w > 0)
    if total <= 0:
        return fallback
    r = random.uniform(0.0, total)
    acc = 0.0
    for name, w in weights.items():
        if w <= 0:
            continue
        acc += w
        if r <= acc:
            return name
    return fallback


def resolve_emotion_name(emotion_name: str) -> str | None:
    """Map router aliases to registered presets."""
    mapped = map_surroundings_emotion(emotion_name)
    if mapped in EMOTION_PRESETS:
        return mapped
    if emotion_name == "curious":
        return "curious_intense"
    if emotion_name in EMOTION_PRESETS:
        return emotion_name
    return None
