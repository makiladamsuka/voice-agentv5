"""Servo Mixer — blends servo_loop targets with animation/conversation overlays
and physically writes final angles to the ESP32 via ArduinoServoLink.

Reads:  servo_pan, servo_tilt, conv_pan_offset, conv_tilt_offset,
        anim_pan_override, anim_tilt_override, anim_active, anim_blend_weight
Writes: (physical hardware only — no new BB fields)
"""

import math
import time
from pathlib import Path

try:
    import yaml
except ImportError:
    yaml = None

from core.blackboard import Blackboard

APP_DIR = Path(__file__).resolve().parent.parent
DEFAULT_CONFIG_PATH = APP_DIR / "config.yaml"


def _load_yaml(path: Path) -> dict:
    if yaml is None or not path.exists():
        return {}
    with open(path, encoding="utf-8") as f:
        return yaml.safe_load(f) or {}


def _cfg(data, *keys, default=None):
    cur = data
    for key in keys:
        if not isinstance(cur, dict) or key not in cur:
            return default
        cur = cur[key]
    return cur


def clamp(v, lo, hi):
    return max(lo, min(hi, v))


class ServoMixer:
    """Mixes servo targets from multiple sources and sends to hardware."""

    def __init__(self, bb: Blackboard, link, config_path: Path = DEFAULT_CONFIG_PATH):
        """
        Args:
            bb:   Shared Blackboard.
            link: ArduinoServoLink instance (may not be connected — handled gracefully).
        """
        self.bb   = bb
        self.link = link

        cfg = _load_yaml(config_path)
        s = _cfg(cfg, "servo", default={}) or {}

        self.pan_min    = float(s.get("pan_min", 40.0))
        self.pan_max    = float(s.get("pan_max", 120.0))
        self.tilt_min   = float(s.get("tilt_min", 100.0))
        self.tilt_max   = float(s.get("tilt_max", 120.0))
        self.send_hz    = float(s.get("servo_send_hz", 30.0))
        self.send_min   = float(s.get("servo_send_min_deg", 0.04))
        self.quantum    = float(s.get("servo_angle_quantum_deg", 0.1))

        self._last_sent_pan  = None
        self._last_sent_tilt = None

    def _quantize(self, value, quantum):
        if quantum <= 0.0:
            return value
        return round(value / quantum) * quantum

    def run(self):
        print("ServoMixer started.")
        interval = 1.0 / max(1.0, self.send_hz)

        while self.bb.read("running")["running"]:
            try:
                state = self.bb.read(
                    "servo_pan", "servo_tilt",
                    "conv_pan_offset", "conv_tilt_offset",
                    "anim_pan_override", "anim_tilt_override",
                    "anim_active", "anim_blend_weight",
                )

                base_pan  = state["servo_pan"]
                base_tilt = state["servo_tilt"]
                conv_pan  = state["conv_pan_offset"]
                conv_tilt = state["conv_tilt_offset"]
                anim_pan  = state["anim_pan_override"]
                anim_tilt = state["anim_tilt_override"]
                anim_on   = state["anim_active"]
                anim_w    = state["anim_blend_weight"]

                # Layer: base + conversation overlay
                mixed_pan  = base_pan  + conv_pan
                mixed_tilt = base_tilt + conv_tilt

                # Layer: animation blend (if active and override provided)
                if anim_on:
                    if anim_pan is not None:
                        mixed_pan  = mixed_pan  * (1.0 - anim_w) + anim_pan  * anim_w
                    if anim_tilt is not None:
                        mixed_tilt = mixed_tilt * (1.0 - anim_w) + anim_tilt * anim_w

                # Clamp to hardware limits
                final_pan  = clamp(mixed_pan,  self.pan_min,  self.pan_max)
                final_tilt = clamp(mixed_tilt, self.tilt_min, self.tilt_max)

                # Quantize to avoid jitter
                final_pan  = self._quantize(final_pan,  self.quantum)
                final_tilt = self._quantize(final_tilt, self.quantum)

                # Only send if changed enough
                send = False
                if self._last_sent_pan is None or self._last_sent_tilt is None:
                    send = True
                elif (abs(final_pan  - self._last_sent_pan)  >= self.send_min or
                      abs(final_tilt - self._last_sent_tilt) >= self.send_min):
                    send = True

                if send:
                    try:
                        self.link.write_angles(final_pan, final_tilt)
                        self._last_sent_pan  = final_pan
                        self._last_sent_tilt = final_tilt
                    except Exception:
                        pass  # link may not be connected; fail silently

            except Exception as e:
                print(f"ServoMixer error: {e}")

            time.sleep(interval)
