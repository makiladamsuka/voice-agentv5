#!/usr/bin/env python3
"""Standalone IMU + servo + base encoder closed-loop lab.

Usage:
    python3 tests/imu_orient_viz.py
    python3 tests/imu_orient_viz.py --port 8083

Open http://localhost:8083 (or http://<pi-ip>:8083).
WASD head jog · hold M/N base spin · C center+lock · R reset IMU yaw · Z zero encoder.
"""

from __future__ import annotations

import argparse
import json
import threading
import time
from collections import deque
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any

import _bootstrap  # noqa: F401

from arduino_servo import ArduinoServoLink
from base_motor_utils import apply_base_calibration_to_nano
from lib.elastic_head_motion import clamp
from lib.head_imu_mount import load_head_mount
from lib.head_imu_orient import HeadImuOrient, HeadImuOrientReader, load_imu_hw_config
from lib.head_mech import signed_pan_mech_deg, signed_tilt_mech_deg
from lib.imu_servo_verify import ServoPose, VerifyReference, compute_yaw_verify

APP_DIR = Path(__file__).resolve().parents[1]
DEFAULT_CONFIG = APP_DIR / "config.yaml"
HISTORY_LEN = 180


def _load_yaml(config_path: Path) -> dict:
    try:
        import yaml
    except ImportError:
        return {}
    if not config_path.exists():
        return {}
    with open(config_path, encoding="utf-8") as f:
        return yaml.safe_load(f) or {}


def load_servo_cfg(config_path: Path) -> dict:
    return _load_yaml(config_path).get("servo") or {}


def _load_viz_config(config_path: Path) -> dict:
    cfg = _load_yaml(config_path)
    viz = cfg.get("imu_orient_viz") or {}
    dbg = cfg.get("debug_viz") or {}
    base_cfg = cfg.get("base") or {}
    base_yaw_sign = viz.get("base_yaw_sign")
    if base_yaw_sign is None:
        base_yaw_sign = dbg.get("base_yaw_sign", base_cfg.get("sign", 1.0))
    return {
        "host": str(viz.get("host", "0.0.0.0")),
        "port": int(viz.get("port", 8083)),
        "use_level_cal": bool(viz.get("use_level_cal", False)),
        "servo_enabled": bool(viz.get("servo_enabled", True)),
        "head_step_deg": float(viz.get("head_step_deg", dbg.get("head_step_deg", 5.0))),
        "error_warn_deg": float(viz.get("error_warn_deg", 3.0)),
        "unlock_servo_limits": bool(viz.get("unlock_servo_limits", True)),
        "encoder_poll_hz": float(viz.get("encoder_poll_hz", 5.0)),
        "base_yaw_sign": float(base_yaw_sign),
    }


class BaseBodyController:
    """Encoder poll + hold-to-spin base control (shared serial with head)."""

    def __init__(
        self,
        link: ArduinoServoLink,
        serial_lock: threading.Lock,
        *,
        poll_hz: float = 5.0,
    ) -> None:
        self._link = link
        self._lock = serial_lock
        self._poll_hz = max(0.5, poll_hz)
        self._stop = threading.Event()
        self._poll_thread: threading.Thread | None = None
        self.base_encoder_deg = 0.0
        self.base_motion_busy = False
        self._spin = 0

    @property
    def connected(self) -> bool:
        return self._link.connected

    def start_poll_thread(self) -> None:
        if self._poll_thread is not None:
            return
        self.poll_once()
        self._poll_thread = threading.Thread(
            target=self._poll_loop,
            daemon=True,
            name="BaseEncoderPoll",
        )
        self._poll_thread.start()

    def stop_poll_thread(self) -> None:
        self._stop.set()
        if self._poll_thread is not None:
            self._poll_thread.join(timeout=1.0)
            self._poll_thread = None

    def _poll_loop(self) -> None:
        interval = 1.0 / self._poll_hz
        while not self._stop.is_set():
            self.poll_once()
            self._stop.wait(interval)

    def poll_once(self) -> float | None:
        with self._lock:
            st = self._link.query_status()
        if st is None:
            return None
        self.base_encoder_deg = float(st.degrees)
        self.base_motion_busy = bool(st.busy)
        return self.base_encoder_deg

    def read_encoder_now(self) -> float:
        return self.poll_once() or self.base_encoder_deg

    def apply_cmd(self, cmd: str) -> bool:
        with self._lock:
            if cmd == "base_spin_left":
                if self._spin != -1:
                    self._link.write_base_spin_left()
                    self._spin = -1
                return True
            if cmd == "base_spin_right":
                if self._spin != 1:
                    self._link.write_base_spin_right()
                    self._spin = 1
                return True
            if cmd == "base_spin_stop":
                if self._spin != 0:
                    self._link.write_base_stop()
                    self._spin = 0
                return True
            if cmd == "zero_base":
                if self._spin != 0:
                    self._link.write_base_stop()
                    self._spin = 0
                self._link.zero_base()
                st = self._link.query_status()
                if st is not None:
                    self.base_encoder_deg = float(st.degrees)
                return True
        return False

    def extra_fields(self) -> dict[str, Any]:
        return {
            "base_encoder_deg": self.base_encoder_deg,
            "base_motion_busy": self.base_motion_busy,
        }


class ServoHeadController:
    """Thread-safe pan/tilt commands + closed-loop error vs IMU."""

    def __init__(
        self,
        link: ArduinoServoLink,
        servo_cfg: dict,
        serial_lock: threading.Lock,
    ) -> None:
        self._link = link
        self._cfg = servo_cfg
        self._lock = serial_lock
        self.pan_center = float(servo_cfg.get("pan_center", 100.0))
        self.tilt_center = float(servo_cfg.get("tilt_center", 110.0))
        self.pan_min = float(servo_cfg.get("pan_min", 25.0))
        self.pan_max = float(servo_cfg.get("pan_max", 150.0))
        self.tilt_min = float(servo_cfg.get("tilt_min", 75.0))
        self.tilt_max = float(servo_cfg.get("tilt_max", 150.0))
        self.pan_sign = float(servo_cfg.get("pan_sign", 1.0))
        self.tilt_sign = float(servo_cfg.get("tilt_sign", -1.0))
        self.pan_cmd = self.pan_center
        self.tilt_cmd = self.tilt_center
        self._limits_unlocked = False
        self.ref = VerifyReference()
        self.pan_error_deg = 0.0
        self.tilt_error_deg = 0.0
        self.body_yaw_deg = 0.0
        self.head_on_body_imu_deg = 0.0
        self.world_head_yaw_deg = 0.0
        self._homelocked = False

    @property
    def connected(self) -> bool:
        return self._link.connected

    def _mech(self) -> tuple[float, float]:
        return (
            signed_pan_mech_deg(self.pan_cmd, self._cfg),
            signed_tilt_mech_deg(self.tilt_cmd, self._cfg),
        )

    def home_and_lock(
        self,
        reader: HeadImuOrientReader,
        base: BaseBodyController | None = None,
    ) -> None:
        with self._lock:
            self.pan_cmd = self.pan_center
            self.tilt_cmd = self.tilt_center
            self._link.home_smooth(self.pan_cmd, self.tilt_cmd)
        time.sleep(0.35)
        reader.lock_reference()
        deadline = time.time() + 2.0
        sample = None
        while time.time() < deadline:
            sample = reader.latest()
            if sample is not None:
                break
            time.sleep(0.02)
        pan_mech, tilt_mech = self._mech()
        imu_tilt = sample.pitch_deg if sample is not None else 0.0
        base_enc = base.read_encoder_now() if base is not None else 0.0
        self.ref = VerifyReference(
            imu_yaw_deg=0.0,
            imu_tilt_deg=imu_tilt,
            servo_pan_mech_deg=pan_mech,
            servo_tilt_mech_deg=tilt_mech,
            base_encoder_deg=base_enc,
        )
        self.pan_error_deg = 0.0
        self.tilt_error_deg = 0.0
        self.body_yaw_deg = 0.0
        self.head_on_body_imu_deg = 0.0
        self.world_head_yaw_deg = 0.0
        self._homelocked = True
        print(
            f"[imu_orient_viz] Center lock: body≈0° head_on_body≈0° "
            f"servo pan={pan_mech:+.1f}° tilt={tilt_mech:+.1f}° "
            f"imu_tilt={imu_tilt:+.1f}° base_enc={base_enc:+.1f}°"
        )

    def unlock_firmware_limits(self) -> None:
        """Match test_servo_manual: firmware 0–180° jog range."""
        with self._lock:
            self._link.send_line("U", drain_after=False)
        self._limits_unlocked = True

    def _jog_limits(self) -> tuple[tuple[float, float], tuple[float, float]]:
        if self._limits_unlocked:
            return (0.0, 180.0), (0.0, 180.0)
        return (self.pan_min, self.pan_max), (self.tilt_min, self.tilt_max)

    def apply_cmd(self, cmd: str, step: float) -> bool:
        (pan_lo, pan_hi), (tilt_lo, tilt_hi) = self._jog_limits()
        with self._lock:
            prev_pan, prev_tilt = self.pan_cmd, self.tilt_cmd
            # Same as tests/test_servo_manual.py — cmd units, not track_sign.
            if cmd == "tilt_up":
                self.tilt_cmd = clamp(self.tilt_cmd + step, tilt_lo, tilt_hi)
            elif cmd == "tilt_down":
                self.tilt_cmd = clamp(self.tilt_cmd - step, tilt_lo, tilt_hi)
            elif cmd == "pan_left":
                self.pan_cmd = clamp(self.pan_cmd - step, pan_lo, pan_hi)
            elif cmd == "pan_right":
                self.pan_cmd = clamp(self.pan_cmd + step, pan_lo, pan_hi)
            elif cmd == "center":
                self.pan_cmd = self.pan_center
                self.tilt_cmd = self.tilt_center
            else:
                return False
            if self.pan_cmd == prev_pan and self.tilt_cmd == prev_tilt and cmd != "center":
                print(f"[imu_orient_viz] At jog limit ({cmd}) pan={self.pan_cmd:.1f} tilt={self.tilt_cmd:.1f}")
            self._link.write_angles(self.pan_cmd, self.tilt_cmd, force=True)
        return True

    def update_errors(
        self,
        imu_yaw_deg: float,
        imu_tilt_deg: float,
        base_encoder_deg: float = 0.0,
    ) -> None:
        pan_mech, tilt_mech = self._mech()
        servo = ServoPose(
            pan_cmd=self.pan_cmd,
            tilt_cmd=self.tilt_cmd,
            pan_mech_deg=pan_mech,
            tilt_mech_deg=tilt_mech,
        )
        state = compute_yaw_verify(
            imu_yaw_deg=imu_yaw_deg,
            imu_tilt_deg=imu_tilt_deg,
            base_encoder_deg=base_encoder_deg,
            servo=servo,
            ref=self.ref,
        )
        self.pan_error_deg = state.head_pan_error_deg
        self.tilt_error_deg = state.tilt_error_deg
        self.body_yaw_deg = state.body_yaw_deg
        self.head_on_body_imu_deg = state.head_on_body_imu_deg
        self.world_head_yaw_deg = state.world_head_yaw_deg

    def extra_fields(self) -> dict[str, Any]:
        pan_mech, tilt_mech = self._mech()
        return {
            "servo_connected": self.connected,
            "pan_cmd": self.pan_cmd,
            "tilt_cmd": self.tilt_cmd,
            "servo_pan_mech_deg": pan_mech,
            "servo_tilt_mech_deg": tilt_mech,
            "pan_error_deg": self.pan_error_deg,
            "tilt_error_deg": self.tilt_error_deg,
            "body_yaw_deg": self.body_yaw_deg,
            "head_on_body_imu_deg": self.head_on_body_imu_deg,
            "world_head_yaw_deg": self.world_head_yaw_deg,
            "center_locked": self._homelocked,
        }


class ImuOrientState:
    def __init__(self, error_warn_deg: float = 3.0, base_yaw_sign: float = 1.0) -> None:
        self._lock = threading.Lock()
        self.error_warn_deg = error_warn_deg
        self.base_yaw_sign = base_yaw_sign
        self.connected = False
        self.error = ""
        self.sample_count = 0
        self.head_step_deg = 5.0
        self.mount_axis_remap: list[int] = []
        self.mount_yaw_sign = 1.0
        self.latest: dict[str, Any] = {}
        self.history_tilt: deque[float] = deque(maxlen=HISTORY_LEN)
        self.history_pan: deque[float] = deque(maxlen=HISTORY_LEN)
        self.history_pan_err: deque[float] = deque(maxlen=HISTORY_LEN)
        self.history_tilt_err: deque[float] = deque(maxlen=HISTORY_LEN)

    def set_mount(self, axis_remap: tuple[int, ...], yaw_sign: float) -> None:
        with self._lock:
            self.mount_axis_remap = list(axis_remap)
            self.mount_yaw_sign = yaw_sign

    def set_error(self, msg: str) -> None:
        with self._lock:
            self.connected = False
            self.error = msg

    def set_connected(self) -> None:
        with self._lock:
            self.connected = True
            self.error = ""

    def update_sample(self, sample_dict: dict[str, Any]) -> None:
        with self._lock:
            self.sample_count += 1
            self.latest = sample_dict
            self.history_tilt.append(float(sample_dict.get("tilt_deg", 0.0)))
            self.history_pan.append(float(sample_dict.get("yaw_deg", 0.0)))
            self.history_pan_err.append(float(sample_dict.get("pan_error_deg", 0.0)))
            self.history_tilt_err.append(float(sample_dict.get("tilt_error_deg", 0.0)))

    def snapshot(self) -> dict[str, Any]:
        with self._lock:
            return {
                "connected": self.connected,
                "error": self.error,
                "sample_count": self.sample_count,
                "head_step_deg": self.head_step_deg,
                "error_warn_deg": self.error_warn_deg,
                "base_yaw_sign": self.base_yaw_sign,
                "mount": {
                    "axis_remap": list(self.mount_axis_remap),
                    "yaw_sign": self.mount_yaw_sign,
                    "note": "PCB +X up, +Y left, +Z back → forward=-Z, left=+Y, up=-X",
                },
                "latest": dict(self.latest),
                "history": {
                    "tilt": list(self.history_tilt),
                    "pan": list(self.history_pan),
                    "pan_err": list(self.history_pan_err),
                    "tilt_err": list(self.history_tilt_err),
                },
            }


STATE: ImuOrientState | None = None
READER: HeadImuOrientReader | None = None
SERVO: ServoHeadController | None = None
BASE: BaseBodyController | None = None


def imu_reader_loop(
    config_path: Path,
    *,
    use_level_cal: bool = False,
) -> None:
    global READER
    mount = load_head_mount(config_path)
    hw = load_imu_hw_config(config_path)
    if STATE is not None:
        STATE.set_mount(mount.axis_remap, mount.yaw_sign)
    while True:
        try:
            orient = HeadImuOrient(
                mount=mount,
                bus=hw["bus"],
                address=hw["address"],
                sample_hz=hw["sample_hz"],
                roll_pitch_alpha=hw["roll_pitch_alpha"],
            )
            orient.open()
            warmup = float(hw["auto_level_warmup_sec"])
            if warmup > 0:
                time.sleep(warmup)
            if use_level_cal:
                print(
                    f"[imu_orient_viz] Level calibrating {hw['auto_level_sec']:.1f}s "
                    f"(hold head upright, still)…"
                )
                n = orient.calibrate_level_stationary(
                    duration_sec=float(hw["auto_level_sec"]),
                    max_gyro_dps=float(hw["auto_level_gyro_max_dps"]),
                    min_samples=int(hw["auto_level_min_samples"]),
                )
                print(f"[imu_orient_viz] Level offsets applied ({n} samples)")
            READER = HeadImuOrientReader(orient)
            READER.start()
            if STATE is not None:
                STATE.set_connected()
            while True:
                sample = READER.latest()
                if sample is not None:
                    payload = sample.as_dict()
                    base_enc = BASE.base_encoder_deg if BASE is not None else 0.0
                    if SERVO is not None:
                        SERVO.update_errors(sample.yaw_deg, sample.tilt_deg, base_enc)
                        payload.update(SERVO.extra_fields())
                    if BASE is not None:
                        payload.update(BASE.extra_fields())
                    if STATE is not None:
                        STATE.update_sample(payload)
                if READER.error:
                    raise RuntimeError(READER.error)
                time.sleep(0.02)
        except Exception as exc:
            if STATE is not None:
                STATE.set_error(str(exc))
            print(f"[imu_orient_viz] IMU error: {exc}")
            if READER is not None:
                try:
                    READER.stop()
                except Exception:
                    pass
                READER = None
            time.sleep(2.0)


def init_robot(
    config_path: Path,
    *,
    unlock_limits: bool = True,
    encoder_poll_hz: float = 5.0,
) -> tuple[ServoHeadController | None, BaseBodyController | None]:
    servo_cfg = load_servo_cfg(config_path)
    port = str(servo_cfg.get("port") or "")
    baud = int(servo_cfg.get("baud", 115200))
    link = ArduinoServoLink(port=port, baud=baud)
    if not link.connect():
        print("[imu_orient_viz] WARNING: ESP32 not connected — WASD/M/N disabled")
        link.close(skip_home=True)
        return None, None
    serial_lock = threading.Lock()
    apply_base_calibration_to_nano(link)
    base = BaseBodyController(link, serial_lock, poll_hz=encoder_poll_hz)
    base.start_poll_thread()
    ctrl = ServoHeadController(link, servo_cfg, serial_lock)
    if unlock_limits:
        ctrl.unlock_firmware_limits()
        print("[imu_orient_viz] Firmware limits unlocked (0–180° jog, like test_servo_manual.py)")
    port_label = getattr(link, "_port_name", None) or getattr(getattr(link, "_ser", None), "port", "serial")
    print(f"[imu_orient_viz] Servo + base encoder on {port_label}")
    return ctrl, base


HTML_PAGE = r"""<!DOCTYPE html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>IMU + Servo Closed Loop</title>
  <style>
    :root {
      --bg: #0a0e14; --card: #141b24; --border: #243044; --text: #e6edf5;
      --muted: #7d8da6; --tilt: #38bdf8; --pan: #a855f7; --err: #f59e0b; --ok: #22c55e;
    }
    * { box-sizing: border-box; margin: 0; padding: 0; }
    body { font-family: "Segoe UI", system-ui, sans-serif; background: var(--bg); color: var(--text);
      min-height: 100vh; padding: 1rem 1.25rem 2rem; }
    header { display: flex; flex-wrap: wrap; align-items: center; gap: 0.75rem 1.25rem;
      margin-bottom: 1.25rem; padding-bottom: 1rem; border-bottom: 1px solid var(--border); }
    h1 { font-size: 1.25rem; font-weight: 600; }
    .badge { padding: 0.2rem 0.6rem; border-radius: 999px; font-size: 0.72rem;
      font-weight: 700; text-transform: uppercase; }
    .badge.ok { background: #14532d; color: #86efac; }
    .badge.err { background: #7f1d1d; color: #fca5a5; }
    .badge.wait { background: #713f12; color: #fde68a; }
    .meta { color: var(--muted); font-size: 0.85rem; }
    .metrics { display: grid; grid-template-columns: repeat(2, 1fr); gap: 0.75rem; margin-bottom: 1rem; }
    @media (max-width: 700px) { .metrics { grid-template-columns: 1fr; } }
    .metric { background: var(--card); border: 1px solid var(--border); border-radius: 12px; padding: 0.9rem 1rem; }
    .metric .label { font-size: 0.72rem; text-transform: uppercase; letter-spacing: 0.08em; margin-bottom: 0.35rem; }
    .metric.tilt .label { color: var(--tilt); }
    .metric.pan .label { color: var(--pan); }
    .metric.body .label { color: #34d399; }
    .metric.errpan .label, .metric.errtilt .label { color: var(--err); }
    .metric .value { font-size: 2rem; font-weight: 700; font-variant-numeric: tabular-nums; }
    .metric .value.warn { color: #fbbf24; }
    .metric .sub { font-size: 0.8rem; color: var(--muted); margin-top: 0.35rem; }
    canvas.spark { width: 100%; height: 52px; display: block; margin-top: 0.5rem;
      border-radius: 6px; background: #0a0e14; }
    .layout { display: grid; grid-template-columns: 1fr 1fr 1fr; gap: 1rem; }
    @media (max-width: 1100px) { .layout { grid-template-columns: 1fr; } }
    .card { background: var(--card); border: 1px solid var(--border); border-radius: 14px; padding: 1rem 1.1rem; }
    .card h2 { font-size: 0.72rem; text-transform: uppercase; letter-spacing: 0.1em;
      color: var(--muted); margin-bottom: 0.75rem; }
    .scene-wrap { display: flex; align-items: center; justify-content: center; min-height: 280px; perspective: 900px; }
    .scene-tilt { transform: rotateX(16deg); transform-style: preserve-3d; }
    .robot-stack { display: flex; flex-direction: column; align-items: center; transform-style: preserve-3d; }
    .head-mount { width: 140px; height: 100px; transform-style: preserve-3d;
      transition: transform 0.06s linear; transform-origin: center bottom; }
    .neck-bar { width: 28px; height: 10px; margin: 2px 0; background: #475569; border-radius: 4px;
      box-shadow: 0 1px 0 #1e293b; transform: translateZ(2px); }
    .body-mount { width: 168px; height: 80px; transform-style: preserve-3d;
      transform-origin: center top; display: flex; align-items: flex-start; justify-content: center; }
    .body-box { width: 160px; height: 52px; position: relative; transform-style: preserve-3d;
      transition: transform 0.06s linear; }
    .head-box { width: 140px; height: 100px; position: relative; transform-style: preserve-3d;
      transition: transform 0.06s linear; }
    .face, .bface { position: absolute; border: 2px solid var(--border); border-radius: 8px;
      display: flex; align-items: center; justify-content: center; font-size: 0.65rem;
      font-weight: 700; backface-visibility: hidden; }
    .face.front { inset: 0; background: linear-gradient(160deg, #1e3a5f, #0f172a); transform: translateZ(50px); color: #93c5fd; }
    .face.back { inset: 0; background: #1a2332; transform: rotateY(180deg) translateZ(50px); color: var(--muted); }
    .face.top { background: #334155; height: 100px; width: 140px; transform: rotateX(90deg) translateZ(50px); }
    .face.bottom { background: #1e293b; height: 100px; width: 140px; transform: rotateX(-90deg) translateZ(50px); }
    .face.left { background: #253044; width: 100px; height: 100px; transform: rotateY(-90deg) translateZ(70px); }
    .face.right { background: #253044; width: 100px; height: 100px; transform: rotateY(90deg) translateZ(70px); }
    .bface.front { width: 160px; height: 52px; left: 0; top: 0;
      background: linear-gradient(160deg, #134e4a, #0f172a); transform: translateZ(36px); color: #34d399; }
    .bface.back { width: 160px; height: 52px; left: 0; top: 0;
      background: #1a2332; transform: rotateY(180deg) translateZ(36px); color: var(--muted); }
    .bface.top { width: 160px; height: 72px; left: 0; top: 0;
      background: #334155; transform: rotateX(90deg) translateZ(26px); }
    .bface.bottom { width: 160px; height: 72px; left: 0; top: 0;
      background: #1e293b; transform: rotateX(-90deg) translateZ(26px); }
    .bface.left { width: 72px; height: 52px; left: 44px; top: 0;
      background: #253044; transform: rotateY(-90deg) translateZ(80px); }
    .bface.right { width: 72px; height: 52px; left: 44px; top: 0;
      background: #253044; transform: rotateY(90deg) translateZ(80px); }
    .keys { display: grid; grid-template-columns: repeat(3, 1fr); gap: 0.35rem; max-width: 200px; margin: 0.75rem auto; }
    .keys button { padding: 0.5rem; background: #1e293b; border: 1px solid var(--border);
      color: var(--text); border-radius: 8px; cursor: pointer; font-size: 0.85rem; }
    .keys button:hover { background: #334155; }
    .keys .sp { visibility: hidden; }
    .hint { font-size: 0.8rem; color: var(--muted); line-height: 1.5; margin-top: 0.75rem; text-align: center; }
    .row { display: flex; justify-content: space-between; font-size: 0.85rem; margin: 0.25rem 0; }
    .row span { color: var(--muted); }
    .row strong { font-variant-numeric: tabular-nums; }
  </style>
</head>
<body>
  <header>
    <h1>IMU + Servo Closed Loop</h1>
    <span id="status" class="badge wait">connecting</span>
    <span class="meta" id="samples"></span>
    <span class="meta" id="servoStatus"></span>
  </header>

  <div class="metrics">
    <div class="metric tilt">
      <div class="label">IMU tilt (pitch)</div>
      <div class="value" id="tiltVal">—</div>
      <div class="sub">servo <span id="servoTilt">—</span>° · gyro <span id="tiltRate">—</span> dps</div>
      <canvas class="spark" id="sparkTilt" width="400" height="52"></canvas>
    </div>
    <div class="metric pan">
      <div class="label">IMU pan (yaw)</div>
      <div class="value" id="panVal">—</div>
      <div class="sub">servo <span id="servoPan">—</span>° · gyro <span id="panRate">—</span> dps</div>
      <canvas class="spark" id="sparkPan" width="400" height="52"></canvas>
    </div>
    <div class="metric errpan">
      <div class="label">Head pan error (neck IMU − servo)</div>
      <div class="value" id="panErr">—</div>
      <div class="sub">excludes base rotation · warn &gt; <span id="warnDeg">3</span>°</div>
      <canvas class="spark" id="sparkPanErr" width="400" height="52"></canvas>
    </div>
    <div class="metric errtilt">
      <div class="label">Tilt error (IMU − servo)</div>
      <div class="value" id="tiltErr">—</div>
      <div class="sub">drift / coupling</div>
      <canvas class="spark" id="sparkTiltErr" width="400" height="52"></canvas>
    </div>
  </div>

  <div class="metrics" style="margin-top:0">
    <div class="metric body">
      <div class="label">Body yaw (encoder Δ)</div>
      <div class="value" id="bodyYaw">—</div>
      <div class="sub">enc <span id="baseEnc">—</span>° · busy <span id="baseBusy">—</span></div>
    </div>
    <div class="metric body">
      <div class="label">Head on body</div>
      <div class="value" id="headOnBody">—</div>
      <div class="sub">servo pan <span id="servoPanHob">—</span>° · world <span id="worldYaw">—</span>°</div>
    </div>
  </div>

  <div class="layout">
    <div class="card">
      <h2>Head + body preview</h2>
      <div class="scene-wrap">
        <div class="scene-tilt">
          <div class="robot-stack">
            <div class="head-mount" id="headMount">
              <div class="head-box" id="headBox">
                <div class="face front">FACE</div>
                <div class="face back">BACK</div>
                <div class="face top"></div>
                <div class="face bottom"></div>
                <div class="face left"></div>
                <div class="face right"></div>
              </div>
            </div>
            <div class="neck-bar"></div>
            <div class="body-mount">
              <div class="body-box" id="bodyBox">
                <div class="bface front">BODY</div>
                <div class="bface back">BACK</div>
                <div class="bface top"></div>
                <div class="bface bottom"></div>
                <div class="bface left"></div>
                <div class="bface right"></div>
              </div>
            </div>
          </div>
        </div>
      </div>
      <div class="keys">
        <span class="sp"></span><button type="button" data-cmd="tilt_up">W</button><span class="sp"></span>
        <button type="button" data-cmd="pan_left">A</button><button type="button" data-cmd="center">C</button>
        <button type="button" data-cmd="pan_right">D</button>
        <span class="sp"></span><button type="button" data-cmd="tilt_down">S</button><span class="sp"></span>
      </div>
      <p class="hint">W/S tilt · A/D pan · C center+lock · R reset yaw · Z zero encoder</p>
    </div>
    <div class="card">
      <h2>Body spin</h2>
      <div class="keys" style="max-width:160px">
        <span class="sp"></span><button type="button" data-base-cmd="base_spin_left">M</button>
        <button type="button" data-base-cmd="base_spin_right">N</button><span class="sp"></span>
      </div>
      <p class="hint">Hold M/N to spin base left/right · release to stop</p>
      <div class="row" style="margin-top:1rem"><span>base encoder</span><strong id="baseEncRow">—</strong></div>
      <div class="row"><span>body Δ</span><strong id="bodyYawRow">—</strong></div>
      <div class="row"><span>head on body (IMU)</span><strong id="hobRow">—</strong></div>
      <div class="row"><span>world aim</span><strong id="worldRow">—</strong></div>
    </div>
    <div class="card">
      <h2>Servo commands</h2>
      <div class="row"><span>pan cmd</span><strong id="panCmd">—</strong></div>
      <div class="row"><span>tilt cmd</span><strong id="tiltCmd">—</strong></div>
      <div class="row"><span>pan mech</span><strong id="panMech">—</strong></div>
      <div class="row"><span>tilt mech</span><strong id="tiltMech">—</strong></div>
      <p class="hint" style="text-align:left;margin-top:1rem">
        Head pan error = (IMU yaw − body encoder) − servo pan since lock.
        Tilt error = IMU pitch change minus servo tilt since lock.
      </p>
    </div>
  </div>

  <script>
    const fmt = (v, d=1) => (v == null || Number.isNaN(v)) ? "—" : Number(v).toFixed(d);
    let cmdSeq = 0;

    function sendCmd(cmd) {
      cmdSeq += 1;
      fetch("/api/control", {
        method: "POST",
        headers: {"Content-Type": "application/json"},
        body: JSON.stringify({cmd, seq: cmdSeq}),
      }).catch(() => {});
    }

    document.querySelectorAll("[data-cmd]").forEach(btn => {
      btn.addEventListener("click", () => sendCmd(btn.dataset.cmd));
    });

    function bindBaseHold(btn) {
      const cmd = btn.dataset.baseCmd;
      const start = (ev) => { ev.preventDefault(); sendCmd(cmd); };
      const stop = (ev) => { ev.preventDefault(); sendCmd("base_spin_stop"); };
      btn.addEventListener("mousedown", start);
      btn.addEventListener("mouseup", stop);
      btn.addEventListener("mouseleave", stop);
      btn.addEventListener("touchstart", start, {passive: false});
      btn.addEventListener("touchend", stop);
      btn.addEventListener("touchcancel", stop);
    }
    document.querySelectorAll("[data-base-cmd]").forEach(bindBaseHold);

    const KEY_MAP = {w:"tilt_up", s:"tilt_down", a:"pan_left", d:"pan_right", c:"center", r:"reset_yaw", z:"zero_base"};
    const BASE_KEY_DOWN = {m:"base_spin_left", n:"base_spin_right"};
    const baseHeld = new Set();

    window.addEventListener("keydown", (ev) => {
      const k = ev.key.toLowerCase();
      if (BASE_KEY_DOWN[k]) {
        if (baseHeld.has(k)) return;
        baseHeld.add(k);
        ev.preventDefault();
        sendCmd(BASE_KEY_DOWN[k]);
        return;
      }
      const cmd = KEY_MAP[k];
      if (!cmd) return;
      ev.preventDefault();
      if (cmd === "reset_yaw") {
        fetch("/api/reset_yaw", {method: "POST"}).catch(() => {});
      } else {
        sendCmd(cmd);
      }
    });

    window.addEventListener("keyup", (ev) => {
      const k = ev.key.toLowerCase();
      if (!BASE_KEY_DOWN[k]) return;
      if (!baseHeld.has(k)) return;
      baseHeld.delete(k);
      ev.preventDefault();
      sendCmd("base_spin_stop");
    });

    function drawSpark(canvas, data, color, span=45) {
      const ctx = canvas.getContext("2d");
      const w = canvas.width, h = canvas.height;
      ctx.clearRect(0, 0, w, h);
      if (!data.length) return;
      const mid = h / 2;
      ctx.strokeStyle = "#243044";
      ctx.beginPath(); ctx.moveTo(0, mid); ctx.lineTo(w, mid); ctx.stroke();
      ctx.strokeStyle = color; ctx.lineWidth = 2; ctx.beginPath();
      data.forEach((v, i) => {
        const x = (i / Math.max(data.length - 1, 1)) * (w - 4) + 2;
        const y = mid - (v / span) * (mid - 4);
        if (i === 0) ctx.moveTo(x, y); else ctx.lineTo(x, y);
      });
      ctx.stroke();
    }

    function render(data) {
      const st = document.getElementById("status");
      if (data.error) { st.textContent = "error"; st.className = "badge err"; }
      else if (data.connected) { st.textContent = "live"; st.className = "badge ok"; }
      else { st.textContent = "waiting"; st.className = "badge wait"; }
      document.getElementById("samples").textContent = "samples: " + (data.sample_count || 0);
      const L = data.latest || {};
      const warn = data.error_warn_deg || 3;
      document.getElementById("warnDeg").textContent = fmt(warn, 0);
      document.getElementById("servoStatus").textContent = L.servo_connected ? "servo OK" : "servo off";

      document.getElementById("tiltVal").textContent = fmt(L.tilt_deg) + "°";
      document.getElementById("panVal").textContent = fmt(L.yaw_deg) + "°";
      document.getElementById("servoTilt").textContent = fmt(L.servo_tilt_mech_deg);
      document.getElementById("servoPan").textContent = fmt(L.servo_pan_mech_deg);
      document.getElementById("tiltRate").textContent = fmt(L.gyro_pitch_dps);
      document.getElementById("panRate").textContent = fmt(L.gyro_yaw_dps);

      document.getElementById("bodyYaw").textContent = fmt(L.body_yaw_deg) + "°";
      document.getElementById("baseEnc").textContent = fmt(L.base_encoder_deg);
      document.getElementById("baseBusy").textContent = L.base_motion_busy ? "yes" : "no";
      document.getElementById("headOnBody").textContent = fmt(L.head_on_body_imu_deg) + "°";
      document.getElementById("servoPanHob").textContent = fmt(L.servo_pan_mech_deg);
      document.getElementById("worldYaw").textContent = fmt(L.world_head_yaw_deg) + "°";
      document.getElementById("baseEncRow").textContent = fmt(L.base_encoder_deg) + "°";
      document.getElementById("bodyYawRow").textContent = fmt(L.body_yaw_deg) + "°";
      document.getElementById("hobRow").textContent = fmt(L.head_on_body_imu_deg) + "°";
      document.getElementById("worldRow").textContent = fmt(L.world_head_yaw_deg) + "°";

      const pe = document.getElementById("panErr");
      const te = document.getElementById("tiltErr");
      pe.textContent = fmt(L.pan_error_deg) + "°";
      te.textContent = fmt(L.tilt_error_deg) + "°";
      pe.className = "value" + (Math.abs(L.pan_error_deg||0) > warn ? " warn" : "");
      te.className = "value" + (Math.abs(L.tilt_error_deg||0) > warn ? " warn" : "");

      document.getElementById("panCmd").textContent = fmt(L.pan_cmd, 1);
      document.getElementById("tiltCmd").textContent = fmt(L.tilt_cmd, 1);
      document.getElementById("panMech").textContent = fmt(L.servo_pan_mech_deg) + "°";
      document.getElementById("tiltMech").textContent = fmt(L.servo_tilt_mech_deg) + "°";

      const tilt = L.pitch_deg || 0;
      const baseSign = data.base_yaw_sign ?? 1;
      const bodyYaw = (L.body_yaw_deg || 0) * baseSign;
      const headOnBody = (L.head_on_body_imu_deg || 0) * baseSign;
      const worldYaw = bodyYaw + headOnBody;
      document.getElementById("bodyBox").style.transform = "rotateY(" + bodyYaw + "deg)";
      document.getElementById("headMount").style.transform = "rotateY(" + worldYaw + "deg)";
      document.getElementById("headBox").style.transform = "rotateX(" + (-tilt) + "deg)";

      const hist = data.history || {};
      drawSpark(document.getElementById("sparkTilt"), hist.tilt || [], "#38bdf8", 60);
      drawSpark(document.getElementById("sparkPan"), hist.pan || [], "#a855f7", 90);
      drawSpark(document.getElementById("sparkPanErr"), hist.pan_err || [], "#f59e0b", 15);
      drawSpark(document.getElementById("sparkTiltErr"), hist.tilt_err || [], "#f59e0b", 15);
    }

    async function poll() {
      try { render(await (await fetch("/api/state")).json()); } catch (e) {}
      setTimeout(poll, 80);
    }
    poll();
  </script>
</body>
</html>
"""


class Handler(BaseHTTPRequestHandler):
    def log_message(self, format: str, *args) -> None:
        pass

    def _read_json(self) -> dict:
        length = int(self.headers.get("Content-Length", 0))
        if length <= 0:
            return {}
        return json.loads(self.rfile.read(length).decode("utf-8"))

    def _json_response(self, payload: dict, status: int = 200) -> None:
        body = json.dumps(payload).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self) -> None:
        if self.path in ("/", "/index.html"):
            body = HTML_PAGE.encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
            return
        if self.path == "/api/state" and STATE is not None:
            self._json_response(STATE.snapshot())
            return
        self.send_error(404)

    def do_POST(self) -> None:
        if self.path == "/api/control":
            payload = self._read_json()
            cmd = str(payload.get("cmd", "")).strip()
            if not cmd:
                self._json_response({"ok": False, "error": "missing cmd"}, 400)
                return
            step = float(payload.get("step", STATE.head_step_deg if STATE else 5.0))
            base_cmds = {"base_spin_left", "base_spin_right", "base_spin_stop", "zero_base"}
            if cmd in base_cmds:
                if BASE is None:
                    self._json_response({"ok": False, "error": "base unavailable"}, 503)
                    return
                BASE.apply_cmd(cmd)
                if cmd == "zero_base" and SERVO is not None and READER is not None:
                    SERVO.home_and_lock(READER, BASE)
                self._json_response({"ok": True, "cmd": cmd})
                return
            if cmd == "center" and SERVO is not None and READER is not None:
                SERVO.apply_cmd("center", step)
                SERVO.home_and_lock(READER, BASE)
                self._json_response({"ok": True, "cmd": cmd})
                return
            if SERVO is not None and SERVO.apply_cmd(cmd, step):
                self._json_response({"ok": True, "cmd": cmd})
                return
            self._json_response({"ok": False, "error": "servo unavailable or bad cmd"}, 503)
            return
        if self.path == "/api/reset_yaw" and READER is not None:
            READER.reset_yaw()
            self._json_response({"ok": True})
            return
        self.send_error(404)


def main() -> int:
    global STATE, SERVO, BASE
    viz_cfg = _load_viz_config(DEFAULT_CONFIG)
    parser = argparse.ArgumentParser(description="IMU + servo + base closed-loop lab")
    parser.add_argument("--host", default=viz_cfg["host"])
    parser.add_argument("--port", type=int, default=viz_cfg["port"])
    parser.add_argument("--config", type=Path, default=DEFAULT_CONFIG)
    parser.add_argument("--level-cal", action="store_true", default=viz_cfg.get("use_level_cal", False))
    parser.add_argument("--no-servo", action="store_true", default=not viz_cfg.get("servo_enabled", True))
    args = parser.parse_args()

    STATE = ImuOrientState(
        error_warn_deg=viz_cfg["error_warn_deg"],
        base_yaw_sign=viz_cfg["base_yaw_sign"],
    )
    STATE.head_step_deg = viz_cfg["head_step_deg"]

    imu_thread = threading.Thread(
        target=imu_reader_loop,
        args=(args.config,),
        kwargs={"use_level_cal": args.level_cal},
        daemon=True,
        name="ImuOrientReader",
    )
    imu_thread.start()

    if not args.no_servo:
        deadline = time.time() + 8.0
        while READER is None and time.time() < deadline:
            time.sleep(0.1)
        SERVO, BASE = init_robot(
            args.config,
            unlock_limits=viz_cfg.get("unlock_servo_limits", True),
            encoder_poll_hz=viz_cfg["encoder_poll_hz"],
        )
        if SERVO is not None and READER is not None:
            SERVO.home_and_lock(READER, BASE)

    server = ThreadingHTTPServer((args.host, args.port), Handler)
    print(f"IMU + servo + base lab: http://{args.host}:{args.port}/")
    if args.host == "0.0.0.0":
        print(f"  (LAN: http://<pi-ip>:{args.port}/)")
    print("  WASD head · hold M/N base spin · C center+lock · R reset yaw · Z zero encoder")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nStopped.")
    finally:
        if READER is not None:
            READER.stop()
        if BASE is not None:
            BASE.apply_cmd("base_spin_stop")
            BASE.stop_poll_thread()
        if SERVO is not None and SERVO._link is not None:
            SERVO._link.close()
        server.server_close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
