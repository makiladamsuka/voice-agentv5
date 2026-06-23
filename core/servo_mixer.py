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
from lib.head_mech import signed_pan_mech_deg
from base_safety import BaseMotionGate, BaseMoveWatchdog, BaseSafetyConfig
from lib.elastic_head_motion import smooth_toward

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

    def __init__(
        self,
        bb: Blackboard,
        link,
        config_path: Path = DEFAULT_CONFIG_PATH,
        gate: BaseMotionGate | None = None,
    ) -> None:
        self.bb = bb
        self._link = link
        cfg = _load_yaml(config_path)
        s = _cfg(cfg, "servo", default={}) or {}
        b = _cfg(cfg, "base", default={}) or {}
        self._servo_cfg = s

        self.pan_min = float(s.get("pan_min", 40.0))
        self.pan_max = float(s.get("pan_max", 120.0))
        self.pan_center = float(s.get("pan_center", (self.pan_min + self.pan_max) * 0.5))
        self.mech_left = float(s.get("pan_mech_left_deg", -40.0))
        self.mech_right = float(s.get("pan_mech_right_deg", 40.0))

        self.send_min_deg = float(s.get("servo_send_min_deg", 0.06))
        self.send_hz = float(s.get("servo_send_hz", 25.0))
        self.angle_quantum = float(s.get("servo_angle_quantum_deg", 0.2))
        self.tilt_send_smooth_hz = float(s.get("tilt_send_smooth_hz", 2.5))
        self.pan_send_smooth_hz = float(s.get("pan_send_smooth_hz", 2.5))
        self.loop_hz = float(s.get("loop_hz", 100.0))
        self.base_busy_check_hz = 5.0

        self.use_imu_validation = bool(b.get("use_imu_move_validation", False))
        self.spin_tolerance_deg = float(b.get("spin_stop_tolerance_deg", 1.5))
        self.spin_timeout_sec = float(b.get("spin_timeout_sec", 12.0))
        self.spin_stall_sec = float(b.get("spin_stall_sec", 0.35))
        self.spin_positive_uses_left = bool(b.get("spin_positive_uses_left", False))
        self.encoder_sign = float(b.get("encoder_sign", 1.0))
        self._gate = gate if gate is not None else BaseMotionGate(
            backoff_sec=float(b.get("error_backoff_sec", 45.0))
        )
        self._watchdog: BaseMoveWatchdog | None = None
        if self.use_imu_validation and link is not None:
            self._watchdog = BaseMoveWatchdog(
                link=link,
                bb=bb,
                gate=self._gate,
                config=BaseSafetyConfig(error_backoff_sec=float(b.get("error_backoff_sec", 45.0))),
            )

        self._prev_pan = None
        self._prev_tilt = None
        self._prev_a0 = None
        self._prev_a1 = None
        self._prev_a2 = None
        self._prev_a3 = None
        self._send_pan = None
        self._send_tilt = None
        self._last_send_ts = 0.0
        self._last_busy_check_ts = 0.0
        self._encoder_deg = 0.0
        self._last_encoder_poll_ts = 0.0
        self._encoder_poll_hz = 2.0
        self._last_debug_cmd_seq = 0

    def _pan_mech(self, pan_cmd: float) -> float:
        return signed_pan_mech_deg(pan_cmd, self._servo_cfg)

    def _publish_encoder(self, enc: float, pan: float, busy: bool, *, synced: bool = True) -> None:
        self._encoder_deg = enc
        writes: dict = {
            "base_motion_busy": busy,
            "base_encoder_deg": enc,
            "base_encoder_synced": synced,
        }
        # When IMU fusion is active, ImuService owns decomposed world yaw.
        if not self.bb.read("imu_available")["imu_available"]:
            writes["base_world_yaw_deg"] = self._world_yaw(enc, pan)
            writes["body_yaw_deg"] = enc
            writes["head_yaw_on_body_deg"] = self._pan_mech(pan)
        self.bb.write(**writes)

    def _sync_encoder(self, pan: float) -> bool:
        if self._link is None:
            self.bb.write(base_encoder_synced=False)
            return False
        try:
            st = self._link.query_status()
            if st is None:
                return False
            self._publish_encoder(st.degrees, pan, st.busy)
            return True
        except Exception:
            return False

    def _world_yaw(self, encoder_deg: float, pan_cmd: float) -> float:
        return encoder_deg + self._pan_mech(pan_cmd)

    def _quantize(self, v: float) -> float:
        if self.angle_quantum <= 0:
            return v
        return round(v / self.angle_quantum) * self.angle_quantum

    def _should_send(self, pan: float, tilt: float, a0: float, a1: float, a2: float, a3: float, now: float) -> bool:
        if (now - self._last_send_ts) < (1.0 / self.send_hz):
            return False
        if self._prev_pan is None or self._prev_a0 is None:
            return True
        return (
            abs(pan - self._prev_pan) >= self.send_min_deg
            or abs(tilt - self._prev_tilt) >= self.send_min_deg
            or abs(a0 - self._prev_a0) >= self.send_min_deg
            or abs(a1 - self._prev_a1) >= self.send_min_deg
            or abs(a2 - self._prev_a2) >= self.send_min_deg
            or abs(a3 - self._prev_a3) >= self.send_min_deg
        )

    def _handle_debug_commands(self, now: float) -> bool:
        """Browser debug panel: zero base / fusion reset. Returns True if handled."""
        dbg = self.bb.read(
            "manual_control_enabled",
            "debug_control_cmd",
            "debug_control_seq",
        )
        if not dbg["manual_control_enabled"]:
            return False

        cmd = dbg["debug_control_cmd"]
        cmd_seq = int(dbg["debug_control_seq"])
        if not cmd or cmd_seq <= self._last_debug_cmd_seq:
            return False

        self._last_debug_cmd_seq = cmd_seq
        if cmd == "quit":
            self.bb.write(running=False, debug_control_cmd="")
            return True
        if cmd == "zero_base":
            if self._link is not None:
                self._link.zero_base()
                time.sleep(0.15)
                pan = self._quantize(self.bb.read("servo_pan")["servo_pan"])
                self._sync_encoder(pan)
            self.bb.write(base_fusion_resync_request=True, debug_control_cmd="")
            return True
        if cmd == "fusion_reset":
            self.bb.write(base_fusion_resync_request=True, debug_control_cmd="")
            return True
        return False

    def run(self) -> None:
        if self._link is None or not self._link.connected:
            print("[ServoMixer] No servo link — running in dry-run mode.")
        else:
            pan = self._quantize(self.bb.read("servo_pan")["servo_pan"])
            if self._sync_encoder(pan):
                print(f"[ServoMixer] Encoder synced: {self._encoder_deg:+.1f}°")
            else:
                print("[ServoMixer] WARNING: could not read base encoder — base moves blocked.")

        loop_delay = 1.0 / max(1.0, self.loop_hz)

        while self.bb.read("running")["running"]:
            now = time.time()
            if self._handle_debug_commands(now):
                time.sleep(loop_delay)
                continue

            state = self.bb.read(
                "servo_pan", "servo_tilt",
                "hand_a0", "hand_a1", "hand_a2", "hand_a3",
                "base_step_ready", "base_step_deg", "base_step_source",
                "base_motion_busy",
            )
            pan = self._quantize(state["servo_pan"])
            tilt = self._quantize(state["servo_tilt"])
            a0 = self._quantize(state["hand_a0"])
            a1 = self._quantize(state["hand_a1"])
            a2 = self._quantize(state["hand_a2"])
            a3 = self._quantize(state["hand_a3"])

            if state["base_step_ready"] and not state["base_motion_busy"]:
                step = state["base_step_deg"]
                source = state["base_step_source"]
                self.bb.write(base_step_ready=False)
                self._execute_base_step(pan, tilt, step, source, now)
                time.sleep(loop_delay)
                continue

            if state["base_motion_busy"] and (now - self._last_busy_check_ts) > (1.0 / self.base_busy_check_hz):
                self._last_busy_check_ts = now
                self._poll_base_busy(pan)
            elif (
                not state["base_motion_busy"]
                and self._link is not None
                and (now - self._last_encoder_poll_ts) > (1.0 / self._encoder_poll_hz)
            ):
                self._last_encoder_poll_ts = now
                self._sync_encoder(pan)

            if self._should_send(pan, tilt, a0, a1, a2, a3, now):
                self._send_angles(pan, tilt, a0, a1, a2, a3)
                self._last_send_ts = now
                self._prev_pan = pan
                self._prev_tilt = tilt
                self._prev_a0 = a0
                self._prev_a1 = a1
                self._prev_a2 = a2
                self._prev_a3 = a3

            time.sleep(loop_delay)

        print("[ServoMixer] Stopped.")

    def _tilt_for_send(self, tilt: float) -> float:
        dt = 1.0 / max(1.0, self.loop_hz)
        if self._send_tilt is None:
            self._send_tilt = tilt
        self._send_tilt = smooth_toward(
            self._send_tilt, tilt, dt,
            smooth_hz=self.tilt_send_smooth_hz, lo=-360.0, hi=360.0,
        )
        return self._send_tilt

    def _pan_for_send(self, pan: float) -> float:
        dt = 1.0 / max(1.0, self.loop_hz)
        if self._send_pan is None:
            self._send_pan = pan
        self._send_pan = smooth_toward(
            self._send_pan, pan, dt,
            smooth_hz=self.pan_send_smooth_hz, lo=-360.0, hi=360.0,
        )
        return self._send_pan

    def _send_angles(self, pan: float, tilt: float, a0: float, a1: float, a2: float, a3: float) -> None:
        if self._link is None:
            return
        try:
            self._link.write_angles_and_arms(
                self._pan_for_send(pan),
                self._tilt_for_send(tilt),
                a0, a1, a2, a3
            )
        except Exception as e:
            print(f"[ServoMixer] write_angles_and_arms failed: {e}")

    def _refresh_head_during_spin(self) -> None:
        """Keep head tracking while base L/R spin is in progress."""
        state = self.bb.read("servo_pan", "servo_tilt", "hand_a0", "hand_a1", "hand_a2", "hand_a3")
        pan = self._quantize(state["servo_pan"])
        tilt = self._quantize(state["servo_tilt"])
        a0 = self._quantize(state["hand_a0"])
        a1 = self._quantize(state["hand_a1"])
        a2 = self._quantize(state["hand_a2"])
        a3 = self._quantize(state["hand_a3"])
        self._send_angles(pan, tilt, a0, a1, a2, a3)

    def _execute_base_step(self, pan: float, tilt: float, step: float, source: str, now: float) -> None:
        if self._link is None:
            return
        try:
            enc = self._encoder_deg
            pan_mech = self._pan_mech(pan)
            if self._watchdog is not None:
                self._watchdog.start_move(
                    commanded_deg=step,
                    encoder_deg=enc,
                    pan_offset_deg=pan_mech,
                )
            # Send current arms along with base move
            state = self.bb.read("hand_a0", "hand_a1", "hand_a2", "hand_a3")
            self._send_angles(pan, tilt, state["hand_a0"], state["hand_a1"], state["hand_a2"], state["hand_a3"])
            self.bb.write(base_motion_busy=True)
            from base_spin_motion import write_base_step_spin

            ok, moved_deg, stop_reason = write_base_step_spin(
                self._link,
                step,
                tolerance_deg=self.spin_tolerance_deg,
                timeout_sec=self.spin_timeout_sec,
                positive_uses_left=self.spin_positive_uses_left,
                encoder_sign=self.encoder_sign,
                stall_sec=self.spin_stall_sec,
                on_poll=self._refresh_head_during_spin,
            )
            if self._watchdog is not None:
                self._watchdog.finish_move()
            pan = self._quantize(self.bb.read("servo_pan")["servo_pan"])
            st = self._link.query_status()
            if st is not None:
                self._publish_encoder(st.degrees, pan, False)
                self._last_busy_check_ts = now
                tag = "OK" if ok else "FAIL"
                print(
                    f"[ServoMixer] Base spin {step:+.1f}° ({source}) "
                    f"{tag} moved={moved_deg:+.1f}° enc={st.degrees:+.1f}° ({stop_reason})"
                )
                self.bb.write(
                    base_fusion_resync_request=True,
                    base_last_spin_moved_deg=moved_deg,
                    base_last_spin_reason=stop_reason,
                )
            else:
                self.bb.write(base_motion_busy=False)
            if not ok and abs(moved_deg) < max(0.5, abs(step) * 0.2):
                self._gate.record_fault(
                    f"spin {stop_reason} moved {moved_deg:+.1f}° vs cmd {step:+.1f}° ({source})",
                    now,
                )
        except Exception as e:
            print(f"[ServoMixer] base step failed: {e}")
            if self._watchdog is not None:
                self._watchdog.finish_move()
            self.bb.write(base_motion_busy=False)

    def _poll_base_busy(self, pan: float) -> None:
        if self._link is None:
            self.bb.write(base_motion_busy=False)
            return
        try:
            if self._watchdog is not None and self._watchdog.active:
                reason = self._watchdog.tick(pan_offset_deg=self._pan_mech(pan))
                if reason:
                    print(f"[ServoMixer] base-watchdog: {reason}")
                    self.bb.write(base_motion_busy=False)
                    return
            st = self._link.query_status()
            if st is not None:
                self._publish_encoder(st.degrees, pan, st.busy)
                if self._watchdog is not None and self._watchdog.active and not st.busy:
                    self._watchdog.finish_move()
            else:
                self.bb.write(base_motion_busy=False)
        except Exception:
            self.bb.write(base_motion_busy=False)
