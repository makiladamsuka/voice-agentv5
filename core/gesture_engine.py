"""GestureEngine: Generates 'living' hand gestures during track and wander.

Reads from BB:
    servo_pan, servo_mode, hand_priority, running

Writes to BB:
    hand_a0, hand_a1, hand_a2, hand_a3
"""

from __future__ import annotations

import math
import time
from pathlib import Path
from typing import Optional

try:
    import yaml
except ImportError:
    yaml = None

from core.blackboard import Blackboard
from lib.elastic_head_motion import smooth_toward, clamp

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


class GestureEngine:
    """Computes organic arm movements and writes to Blackboard."""

    def __init__(self, bb: Blackboard, config_path: Path = DEFAULT_CONFIG_PATH) -> None:
        self.bb = bb
        cfg = _load_yaml(config_path)
        s = _cfg(cfg, "servo", default={}) or {}

        # Fetch limits from config or use defaults
        self.pan_min = float(s.get("pan_min", 40.0))
        self.pan_max = float(s.get("pan_max", 120.0))
        self.pan_center = float(s.get("pan_center", (self.pan_min + self.pan_max) * 0.5))
        
        # Loop settings
        self.loop_hz = 25.0

        # Hand states (starting neutral)
        self.a0 = 0.0    # Right raise (0 is down)
        self.a1 = 180.0  # Left raise (180 is down)
        self.a2 = 90.0   # Right sweep (90 is center)
        self.a3 = 90.0   # Left sweep (90 is center)

        self.time_offset = 0.0
        
        # Constants for smoothing
        self.smooth_hz = 4.5

    def run(self) -> None:
        print("[GestureEngine] Started.")
        loop_delay = 1.0 / self.loop_hz
        start_time = time.time()

        while self.bb.read("running")["running"]:
            now = time.time()
            dt = loop_delay
            self.time_offset += dt

            # Read BB state
            state = self.bb.read("servo_pan", "servo_mode", "hand_priority")
            
            # If an external agent took over hands, skip updating living gestures
            if state["hand_priority"] != "living":
                time.sleep(loop_delay)
                continue

            pan = state["servo_pan"]
            mode = state["servo_mode"]

            a0_target = 0.0
            a1_target = 180.0
            a2_target = 90.0
            a3_target = 90.0

            if mode == "track":
                # Attentiveness: arms raised slightly
                a0_target = 15.0
                a1_target = 165.0
                
                # Weight shifting based on pan deviation
                # If pan goes right (+), one arm sweeps forward, the other backward
                offset = (pan - self.pan_center) / 20.0  # normalize
                offset = clamp(offset, -1.0, 1.0)
                
                # Exaggerate sweep based on pan offset
                a2_target = 90.0 + (offset * 25.0)
                a3_target = 90.0 + (offset * 25.0)

            elif mode == "wander":
                # Relaxed arms, slightly lifted
                a0_target = 5.0
                a1_target = 175.0
                
                # Organic breathing / fidgeting (slow sine waves)
                breath_cycle = math.sin(self.time_offset * 1.5)
                a2_target = 90.0 + breath_cycle * 8.0
                a3_target = 90.0 + breath_cycle * 8.0
                
                # Small raise variations
                a0_target += math.sin(self.time_offset * 0.8) * 3.0
                a1_target -= math.sin(self.time_offset * 0.8 + math.pi) * 3.0
                
            else:
                # Default (last_seen, etc.) - return to neutral
                a0_target = 5.0
                a1_target = 175.0

            # Smooth towards target
            self.a0 = smooth_toward(self.a0, a0_target, dt, smooth_hz=self.smooth_hz, lo=0.0, hi=180.0)
            self.a1 = smooth_toward(self.a1, a1_target, dt, smooth_hz=self.smooth_hz, lo=0.0, hi=180.0)
            self.a2 = smooth_toward(self.a2, a2_target, dt, smooth_hz=self.smooth_hz, lo=0.0, hi=180.0)
            self.a3 = smooth_toward(self.a3, a3_target, dt, smooth_hz=self.smooth_hz, lo=0.0, hi=180.0)

            # Write to Blackboard
            self.bb.write(
                hand_a0=self.a0,
                hand_a1=self.a1,
                hand_a2=self.a2,
                hand_a3=self.a3
            )

            time.sleep(loop_delay)
            
        print("[GestureEngine] Stopped.")
