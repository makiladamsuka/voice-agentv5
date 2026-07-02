#!/usr/bin/env python3
"""
Standalone ToF approach test harness.

IMU closed-loop base turns toward detected person; return HOME when clear.

  cd voice-agentv5
  /path/to/voice-agentv4/backend/venv/bin/python3 tests/approach.py --port /dev/ttyUSB0

Or viz + control in one process:

  python tests/tof_viz_server.py /dev/ttyUSB0 --control

Stop start_robot.py / voice-robot before running (single serial port).
Requires head_servo_hands v16+ (TOF stream + PROX).
"""

from __future__ import annotations

import argparse
import signal
import sys
import threading
import time
from http.server import ThreadingHTTPServer
from pathlib import Path

import _bootstrap  # noqa: F401

from approach_controller import ApproachController, lock_home, query_enc, read_imu, start_imu
from base_motor_utils import apply_base_calibration_to_nano
from core.yaw_pose import lock_head_home
from hardware.arduino_servo import ArduinoServoLink
from lib.yaw_home_tracker import YawHomeTracker
from tof_viz_server import (
    Handler,
    STATE,
)

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


def handle_tof_line(line: str) -> None:
    """Legacy entry — prefer ApproachController.handle_tof_line."""
    from core.tof_state import FILTER_BANK, STATE, _TOF_RE

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


def run_approach(
    *,
    serial_port: str = "",
    host: str = "0.0.0.0",
    viz_port: int = 8765,
    no_imu: bool = False,
) -> None:
    cfg = _load_yaml(DEFAULT_CONFIG_PATH)
    base_cfg = dict(cfg.get("base", {}) or {})
    imu_cfg = cfg.get("imu", {}) or {}
    servo_cfg = cfg.get("servo", {}) or {}
    prox_cfg = cfg.get("proximity", {}) or {}
    viz_cfg = cfg.get("debug_viz", {}) or {}
    base_yaw_sign = float(viz_cfg.get("base_yaw_sign", -1.0))
    pan_yaw_sign = float(viz_cfg.get("pan_yaw_sign", -1.0))
    tilt_sign = float(viz_cfg.get("tilt_sign", 1.0))
    imu_pitch_sign = float(viz_cfg.get("imu_pitch_sign", -1.0))
    base_cfg.setdefault("home_imu_burst_sec", 0.45)
    base_cfg.setdefault("home_imu_fine_burst_sec", 0.12)
    base_cfg.setdefault("home_imu_close_ratio", 0.88)
    base_cfg.setdefault("home_imu_gyro_brake_dps", 10.0)
    base_cfg.setdefault("home_imu_overshoot_burst_scale", 0.35)
    base_cfg.setdefault("home_fine_threshold_deg", 6.0)
    base_cfg.setdefault("home_success_tolerance_deg", 2.5)
    base_cfg.setdefault("home_imu_poll_hz", 40.0)

    running = threading.Event()
    running.set()

    link = ArduinoServoLink(serial_port or None)
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
    STATE.set_connected(link._port_name or serial_port or "serial")
    STATE.update_pose(
        base_yaw_sign=base_yaw_sign,
        pan_yaw_sign=pan_yaw_sign,
        tilt_sign=tilt_sign,
        imu_pitch_sign=imu_pitch_sign,
    )

    reader = None
    yaw_sign = float(imu_cfg.get("yaw_sign", -1.0))
    if not no_imu and imu_cfg.get("enabled", True):
        reader = start_imu(imu_cfg)

    tracker = YawHomeTracker(
        counts_per_degree=float(base_cfg.get("counts_per_degree", 31.1667)),
        encoder_sign=float(base_cfg.get("encoder_sign", -1.0)),
        still_hold_sec=float(imu_cfg.get("drift_stationary_hold_sec", 0.35)),
        gyro_max_dps=float(imu_cfg.get("drift_gyro_max_dps", 6.0)),
        snap_max_disagreement_deg=float(
            imu_cfg.get("drift_snap_max_disagreement_deg", 5.0)
        ),
        pan_stable_deg=float(imu_cfg.get("drift_pan_stable_deg", 0.2)),
        enc_stable_deg=float(imu_cfg.get("drift_enc_stable_deg", 0.2)),
    )

    pan = float(servo_cfg.get("pan_center", 100.0))
    tilt = float(servo_cfg.get("tilt_center", 110.0))
    lock_home(
        tracker,
        link,
        reader,
        yaw_sign,
        servo_cfg,
        pan,
        zero_encoder=bool(base_cfg.get("zero_on_start", False)),
    )
    _, imu_pitch, _, _ = read_imu(reader, yaw_sign, imu_pitch_sign)
    lock_head_home(imu_pitch, pan, tilt, servo_cfg)
    _, _, _, cpd0 = query_enc(link, 0.0)
    tracker.counts_per_degree = max(cpd0, 0.05)

    controller = ApproachController(
        link,
        tracker,
        reader,
        yaw_sign=yaw_sign,
        base_cfg=base_cfg,
        servo_cfg=servo_cfg,
        prox_cfg=prox_cfg,
        base_yaw_sign=base_yaw_sign,
        pan_yaw_sign=pan_yaw_sign,
        tilt_sign=tilt_sign,
        imu_pitch_sign=imu_pitch_sign,
        running=running.is_set,
    )
    link._prox_callback = controller.handle_prox_line
    link._tof_callback = controller.handle_tof_line
    controller._fetch_enc()

    approach_thread = threading.Thread(
        target=controller.run,
        name="ApproachController",
        daemon=True,
    )
    approach_thread.start()

    def _serial_pump() -> None:
        while running.is_set():
            link._poll_prox_lines()
            time.sleep(0.008)

    pump_thread = threading.Thread(target=_serial_pump, name="SerialPump", daemon=True)
    pump_thread.start()

    def _pose_publisher() -> None:
        while running.is_set():
            try:
                controller.publish_viz_pose()
            except Exception:
                pass
            time.sleep(0.033)

    pose_thread = threading.Thread(target=_pose_publisher, name="VizPose", daemon=True)
    pose_thread.start()

    viz = start_viz_server(host, viz_port)
    print(f"[Approach] Viz: http://localhost:{viz_port}")
    try:
        import socket

        hostname = socket.gethostname()
        print(f"[Approach] Viz: http://{socket.gethostbyname(hostname)}:{viz_port}")
    except OSError:
        pass
    print(
        "[Approach] Person detected → IMU goto bearing; "
        "clear scene → HOME. Ctrl+C to stop."
    )

    def shutdown(_signum=None, _frame=None) -> None:
        running.clear()
        viz.shutdown()

    signal.signal(signal.SIGINT, shutdown)
    signal.signal(signal.SIGTERM, shutdown)

    try:
        while running.is_set():
            time.sleep(0.25)
    except KeyboardInterrupt:
        shutdown()
    finally:
        running.clear()
        link.close()
        print("[Approach] Stopped.")


def main() -> None:
    parser = argparse.ArgumentParser(description="ToF approach test harness")
    parser.add_argument("--port", default="", help="Serial port e.g. /dev/ttyUSB0")
    parser.add_argument("--viz-port", type=int, default=8765, help="HTTP viz port")
    parser.add_argument("--host", default="0.0.0.0", help="HTTP bind address")
    parser.add_argument("--no-imu", action="store_true", help="Skip IMU (no base turns)")
    args = parser.parse_args()

    run_approach(
        serial_port=args.port,
        host=args.host,
        viz_port=args.viz_port,
        no_imu=args.no_imu,
    )


if __name__ == "__main__":
    main()
