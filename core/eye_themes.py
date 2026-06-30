"""Eye color theme resolution for physical TFT displays."""

from __future__ import annotations

DEFAULT_EYE_COLOR = (255, 255, 255)

THEME_COLORS: dict[str, tuple[int, int, int]] = {
    "default": DEFAULT_EYE_COLOR,
    "": DEFAULT_EYE_COLOR,
    "white": DEFAULT_EYE_COLOR,
    "pistachio": (170, 221, 173),
    "coral": (255, 179, 186),
    "red": (255, 64, 64),
    "green": (64, 220, 100),
    "blue": (80, 160, 255),
    "yellow": (255, 220, 80),
    "cyan": (0, 255, 255),
    "purple": (180, 100, 255),
    "orange": (255, 140, 50),
}


def normalize_rgb(value) -> tuple[int, int, int]:
    """Coerce blackboard/config value to an (r, g, b) tuple."""
    if isinstance(value, (list, tuple)) and len(value) >= 3:
        return (
            max(0, min(255, int(value[0]))),
            max(0, min(255, int(value[1]))),
            max(0, min(255, int(value[2]))),
        )
    return DEFAULT_EYE_COLOR


def resolve_eye_color(name: str) -> tuple[int, int, int]:
    """Resolve a theme or color name to RGB. Unknown names fall back to white."""
    key = (name or "default").strip().lower()
    if key in THEME_COLORS:
        return THEME_COLORS[key]
    print(f"[eye_themes] Unknown color '{name}', using white")
    return DEFAULT_EYE_COLOR
