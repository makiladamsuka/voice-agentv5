"""Open-Meteo weather proxy with disk cache for kiosk clock card."""

from __future__ import annotations

import json
import time
import urllib.request
from pathlib import Path

COLOMBO_LAT = 6.7951
COLOMBO_LON = 79.9003

WMO_ICONS = {
    0: "light_mode",
    1: "partly_cloudy_day",
    2: "cloud",
    3: "cloud",
    45: "foggy",
    48: "foggy",
    51: "rainy",
    53: "rainy",
    55: "rainy",
    61: "rainy",
    63: "rainy",
    65: "rainy",
    71: "ac_unit",
    73: "ac_unit",
    75: "ac_unit",
    80: "rainy",
    81: "rainy",
    82: "rainy",
    95: "thunderstorm",
    96: "thunderstorm",
    99: "thunderstorm",
}


def _weather_icon(code: int) -> str:
    return WMO_ICONS.get(code, "cloud")


def get_weather(cache_path: Path | None = None, cache_minutes: int = 10) -> dict:
    """Fetch current weather for Colombo; optional JSON file cache."""
    now_ms = int(time.time() * 1000)
    cache_ms = cache_minutes * 60 * 1000

    if cache_path and cache_path.is_file():
        try:
            cached = json.loads(cache_path.read_text(encoding="utf-8"))
            if now_ms - int(cached.get("timestamp", 0)) < cache_ms:
                return cached.get("data", cached)
        except Exception:
            pass

    url = (
        "https://api.open-meteo.com/v1/forecast"
        f"?latitude={COLOMBO_LAT}&longitude={COLOMBO_LON}&current_weather=true"
    )
    with urllib.request.urlopen(url, timeout=15) as resp:
        payload = json.loads(resp.read().decode("utf-8"))

    cw = payload.get("current_weather") or {}
    code = int(cw.get("weathercode", 0))
    result = {
        "temp": round(float(cw.get("temperature", 0))),
        "weathercode": code,
        "icon": _weather_icon(code),
    }

    if cache_path:
        try:
            cache_path.write_text(
                json.dumps({"timestamp": now_ms, "data": result}, indent=2),
                encoding="utf-8",
            )
        except Exception:
            pass

    return result
