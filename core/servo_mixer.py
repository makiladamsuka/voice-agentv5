"""ServoMixer: the ONLY module that writes to the ESP32 serial port.

Reads servo_pan, servo_tilt from ServoLoop.
Reads base_step_deg, base_step_ready from BaseController.
Writes base_encoder_deg, base_world_yaw_deg, base_motion_busy back to BB.

This is the hardware boundary — all other modules work with BB fields only.
"""

from __future__ import annotations

import time
from pathlib import Path
from typing import Optional

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


class ServoMixer:
    """Sends pan/tilt angles and base steps to the ESP32 via ArduinoServoLink."""

    def __init__(self, bb: Blackboard, link, config_path: Path = DEFAULT_CONFIG_PATH) -> None:
        self.bb = bb
        self._link = link
        cfg = _load_yaml(config_path)
        s = _cfg(cfg, "servo", default={}) or {}

        self.send_min_deg = float(s.get("servo_send_min_deg", 0.06))
        self.send_hz = float(s.get("servo_send_hz", 25.0))
        self.angle_quantum = float(s.get("servo_angle_quantum_deg", 0.2))
        self.loop_hz = float(s.get("loop_hz", 100.0))
        self.base_busy_check_hz = 5.0  # poll status 5x/sec when base is moving

        self._prev_pan = None
        self._prev_tilt = None
        self._last_send_ts = 0.0
        self._last_busy_check_ts = 0.0
        self._encoder_deg = 0.0

    # ─────────────────────────────────────────────────────────────────────────

    def _quantize(self, v: float) -> float:
        if self.angle_quantum <= 0:
            return v
        return round(v / self.angle_quantum) * self.angle_quantum

    def _should_send(self, pan: float, tilt: float, now: float) -> bool:
        if (now - self._last_send_ts) < (1.0 / self.send_hz):
            return False
        if self._prev_pan is None:
            return True
        return (
            abs(pan - self._prev_pan) >= self.send_min_deg
            or abs(tilt - self._prev_tilt) >= self.send_min_deg
        )

    def run(self) -> None:
        if self._link is None or not self._link.connected:
            print("[ServoMixer] No servo link — running in dry-run mode.")

        loop_delay = 1.0 / max(1.0, self.loop_hz)

        while self.bb.read("running")["running"]:
            now = time.time()
            state = self.bb.read(
                "servo_pan", "servo_tilt",
                "base_step_ready", "base_step_deg", "base_step_source",
                "base_motion_busy",
            )
            pan = self._quantize(state["servo_pan"])
            tilt = self._quantize(state["servo_tilt"])

            # ── Execute base step if flagged ───────────────────────────────
            if state["base_step_ready"] and not state["base_motion_busy"]:
                step = state["base_step_deg"]
                source = state["base_step_source"]
                # Clear the ready flag immediately to prevent double-execution
                self.bb.write(base_step_ready=False)
                self._execute_base_step(pan, tilt, step, source, now)
                time.sleep(loop_delay)
                continue

            # ── Poll base busy state ───────────────────────────────────────
            if state["base_motion_busy"] and (now - self._last_busy_check_ts) > (1.0 / self.base_busy_check_hz):
                self._last_busy_check_ts = now
                self._poll_base_busy()

            # ── Send head angles ───────────────────────────────────────────
            if self._should_send(pan, tilt, now):
                self._send_angles(pan, tilt)
                self._last_send_ts = now
                self._prev_pan = pan
                self._prev_tilt = tilt

            time.sleep(loop_delay)

        print("[ServoMixer] Stopped.")

    def _send_angles(self, pan: float, tilt: float) -> None:
        if self._link is None:
            return
        try:
            self._link.write_angles(pan, tilt)
        except Exception as e:
            print(f"[ServoMixer] write_angles failed: {e}")

    def _execute_base_step(self, pan: float, tilt: float, step: float, source: str, now: float) -> None:
        if self._link is None:
            return
        try:
            ok = self._link.write_combined(pan, tilt, step)
            if ok:
                st = self._link.query_status()
                busy = st.busy if st is not None else True
                enc = st.degrees if st is not None else self._encoder_deg
                self._encoder_deg = enc
                self.bb.write(
                    base_motion_busy=busy,
                    base_encoder_deg=enc,
                    base_world_yaw_deg=enc + (pan - 80.0),  # rough; BaseController refines
                )
                self._last_busy_check_ts = now
                print(f"[ServoMixer] Base step {step:+.1f}° ({source}) enc={enc:+.1f}°")
            else:
                self._send_angles(pan, tilt)
        except Exception as e:
            print(f"[ServoMixer] base step failed: {e}")
            self.bb.write(base_motion_busy=False)

    def _poll_base_busy(self) -> None:
        if self._link is None:
            self.bb.write(base_motion_busy=False)
            return
        try:
            st = self._link.query_status()
            if st is not None:
                enc = st.degrees
                self._encoder_deg = enc
                self.bb.write(
                    base_motion_busy=st.busy,
                    base_encoder_deg=enc,
                    base_world_yaw_deg=enc + (self.bb.read("servo_pan")["servo_pan"] - 80.0),
                )
            else:
                self.bb.write(base_motion_busy=False)
        except Exception:
            self.bb.write(base_motion_busy=False)
