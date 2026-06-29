#!/usr/bin/env python3
"""
Standalone ToF approach test harness.

IMU + encoder fusion, PROX zone base turns, live proximity viz.

  cd voice-agentv5
  /path/to/voice-agentv4/backend/venv/bin/python3 tests/approach.py --port /dev/ttyUSB0

Stop start_robot.py / voice-robot before running (single serial port).
Requires head_servo_hands v16+ (TOF stream + PROX).
"""

from __future__ import annotations

import argparse
import re
import signal
import sys
import threading
import time
from http.server import ThreadingHTTPServer
from pathlib import Path

import _bootstrap  # noqa: F401

from approach_controller import ApproachController
from base_motor_utils import apply_base_calibration_to_nano
from core.blackboard import Blackboard
from core.imu_service import ImuService
from hardware.arduino_servo import ArduinoServoLink
from tof_viz_server import (
    FILTER_BANK,
    Handler,
    STATE,
    _TOF_RE,
)

def _pose_publisher(bb: Blackboard, base_yaw_sign: float) -> None:
    """Push IMU + encoder fusion into the shared ToF viz API."""
    STATE.update_pose(base_yaw_sign=base_yaw_sign)
    while bb.read("running")["running"]:
        s = bb.read(
            "body_yaw_deg",
            "head_yaw_on_body_deg",
            "base_encoder_deg",
            "imu_available",
            "imu_drift_correction_deg",
            "imu_yaw_rel_deg",
            "fusion_stationary",
        )
        enc = float(s["base_encoder_deg"])
        body = (
            float(s["body_yaw_deg"])
            if s["imu_available"]
            else enc
        )
        STATE.update_pose(
            body_yaw_deg=body,
            head_yaw_on_body_deg=float(s["head_yaw_on_body_deg"]),
            encoder_yaw_deg=enc,
            front_offset_deg=enc,
            imu_drift_correction_deg=float(s["imu_drift_correction_deg"]),
            imu_yaw_rel_deg=float(s["imu_yaw_rel_deg"]),
            fusion_stationary=bool(s["fusion_stationary"]),
        )
        time.sleep(0.1)

APP_DIR = Path(__file__).resolve().parents[1]
DEFAULT_CONFIG_PATH = APP_DIR / "config.yaml"

try:
    import yaml
except ImportError:
    yaml = None


def _load_yaml(path: Path) -> dict:
    if yaml is None or not path.exists():
        return {}
    with open(path, encoding="utf-8") as f:
        return yaml.safe_load(f) or {}


def _wait_imu_ready(bb: Blackboard, timeout_sec: float = 12.0) -> None:
    deadline = time.time() + timeout_sec
    while time.time() < deadline:
        if bb.read("imu_calibrated")["imu_calibrated"]:
            return
        time.sleep(0.05)
    print("[Approach] WARNING: IMU calibration wait timed out.")


def _wait_fusion_ready(bb: Blackboard, timeout_sec: float = 3.0) -> None:
    deadline = time.time() + timeout_sec
    while time.time() < deadline:
        if not bb.read("base_fusion_resync_request")["base_fusion_resync_request"]:
            return
        time.sleep(0.05)
    print("[Approach] WARNING: fusion resync wait timed out.")


def _lock_yaw_reference(bb: Blackboard, link: ArduinoServoLink, base_cfg: dict) -> None:
    if link.connected and base_cfg.get("zero_on_start", False):
        link.zero_base()
        print("[Approach] Base encoder zeroed at startup.")
        time.sleep(0.2)
    if link.connected:
        try:
            st = link.query_status()
            if st is not None:
                bb.write(
                    base_encoder_deg=st.degrees,
                    base_encoder_synced=True,
                    base_motion_busy=st.busy,
                )
                print(f"[Approach] Encoder synced: {st.degrees:+.1f}°")
        except Exception as exc:
            print(f"[Approach] WARNING: encoder sync failed: {exc}")
    bb.write(base_watchdog_reset=True, yaw_reference_locked=True)
    time.sleep(0.15)
    print("[Approach] Yaw reference locked.")


def handle_tof_line(line: str) -> None:
    m = _TOF_RE.search(line)
    if not m:
        return
    raw = [int(m.group(i)) for i in range(1, 4)]
    mm, vel, open_flags = FILTER_BANK.update_all(raw)
    STATE.update_sample(mm, vel, open_flags=open_flags)


def start_viz_server(host: str, port: int) -> ThreadingHTTPServer:
    server = ThreadingHTTPServer((host, port), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    return server


def main() -> None:
    parser = argparse.ArgumentParser(description="ToF approach test harness")
    parser.add_argument("--port", default="", help="Serial port e.g. /dev/ttyUSB0")
    parser.add_argument("--viz-port", type=int, default=8765, help="HTTP viz port")
    parser.add_argument("--host", default="0.0.0.0", help="HTTP bind address")
    parser.add_argument("--no-imu", action="store_true", help="Skip IMU service")
    args = parser.parse_args()

    cfg = _load_yaml(DEFAULT_CONFIG_PATH)
    base_cfg = cfg.get("base", {}) or {}
    imu_cfg = cfg.get("imu", {}) or {}
    viz_cfg = cfg.get("debug_viz", {}) or {}
    base_yaw_sign = float(viz_cfg.get("base_yaw_sign", -1.0))

    bb = Blackboard()
    bb.write(running=True)

    link = ArduinoServoLink(args.port or None)
    if not link.connect():
        print("ERROR: Could not connect to ESP32.")
        sys.exit(1)

    banner = link.firmware_banner()
    if banner:
        print(f"[Approach] Firmware: {banner.strip().split(chr(10))[-1]}")
    if "tof_stream" not in banner and "v16" not in banner:
        print(
            "[Approach] WARNING: expected v16_tof_stream firmware for TOF viz. "
            "Reflash: ./firmware/flash.sh prod"
        )

    apply_base_calibration_to_nano(link)
    STATE.set_connected(link._port_name or args.port or "serial")

    controller = ApproachController(bb, link, config_path=DEFAULT_CONFIG_PATH)
    link._prox_callback = controller.handle_prox_line
    link._tof_callback = handle_tof_line

    imu_thread: threading.Thread | None = None
    if not args.no_imu and imu_cfg.get("enabled", True):
        imu = ImuService(bb, config_path=DEFAULT_CONFIG_PATH)
        imu_thread = threading.Thread(target=imu.run, name="ImuService", daemon=True)
        imu_thread.start()
        _wait_imu_ready(bb)
        _lock_yaw_reference(bb, link, base_cfg)
        bb.write(base_fusion_resync_request=True)
        _wait_fusion_ready(bb)
    else:
        _lock_yaw_reference(bb, link, base_cfg)

    approach_thread = threading.Thread(
        target=controller.run,
        name="ApproachController",
        daemon=True,
    )
    approach_thread.start()

    pose_thread = threading.Thread(
        target=_pose_publisher,
        args=(bb, base_yaw_sign),
        name="PosePublisher",
        daemon=True,
    )
    pose_thread.start()

    viz = start_viz_server(args.host, args.viz_port)
    print(f"[Approach] Viz: http://localhost:{args.viz_port}")
    try:
        import socket
        hostname = socket.gethostname()
        print(f"[Approach] Viz: http://{socket.gethostbyname(hostname)}:{args.viz_port}")
    except OSError:
        pass
    print("[Approach] ToF zones aim base at person; clears return to front. Ctrl+C to stop.")

    def shutdown(_signum=None, _frame=None) -> None:
        bb.write(running=False)
        viz.shutdown()

    signal.signal(signal.SIGINT, shutdown)
    signal.signal(signal.SIGTERM, shutdown)

    try:
        while bb.read("running")["running"]:
            time.sleep(0.25)
    except KeyboardInterrupt:
        shutdown()
    finally:
        bb.write(running=False)
        link.close()
        print("[Approach] Stopped.")


if __name__ == "__main__":
    main()
