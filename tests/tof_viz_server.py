#!/usr/bin/env python3
"""
ToF Sensor Web Visualizer — live dashboard in your browser.

Usage:
    python3 tests/tof_viz_server.py
    python3 tests/tof_viz_server.py /dev/ttyUSB0
    python3 tests/tof_viz_server.py /dev/ttyUSB0 --port 8765

Open http://localhost:8765 (or http://<pi-ip>:8765 from another device).
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any

APP_DIR = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(APP_DIR))

from core.tof_dashboard_html import build_tof_dashboard_html
from core.tof_state import (
    FILTER_BANK,
    MAX_MM,
    STATE,
    TofState,
    _TOF_RE,
)

try:
    import serial
except ImportError:
    print("Install pyserial: pip install pyserial")
    sys.exit(1)

DEFAULT_PORTS = ("/dev/ttyUSB0", "/dev/ttyUSB1", "/dev/ttyACM0")
BAUD = 115200
_BASE_STATUS_RE = re.compile(
    r"^POS\s+(-?\d+)\s+DEG\s+(-?\d+(?:\.\d+)?)\s+CPD\s+(-?\d+(?:\.\d+)?)\s+BUSY\s+([01])\s*$"
)
ENCODER_POLL_SEC = 0.2
CONFIG_PATH = APP_DIR / "config.yaml"
STATIC_DIR = APP_DIR / "static"


def _load_viz_config() -> dict[str, Any]:
    try:
        import yaml

        if CONFIG_PATH.exists():
            with open(CONFIG_PATH, encoding="utf-8") as f:
                data = yaml.safe_load(f) or {}
            return data.get("debug_viz", {}) or {}
    except Exception:
        pass
    return {}


_STATIC_MIME = {
    ".js": "application/javascript; charset=utf-8",
    ".mjs": "application/javascript; charset=utf-8",
    ".css": "text/css; charset=utf-8",
    ".json": "application/json; charset=utf-8",
    ".wasm": "application/wasm",
}

def list_serial_ports() -> list[str]:
    return [p for p in DEFAULT_PORTS if os.path.exists(p)]


def probe_port(port: str, timeout: float = 2.5) -> bool:
    """True if this port looks like our ESP32 ToF test firmware."""
    try:
        ser = serial.Serial(port, BAUD, timeout=0.25)
        deadline = time.time() + timeout
        while time.time() < deadline:
            line = ser.readline().decode("utf-8", errors="ignore")
            if _TOF_RE.search(line):
                ser.close()
                return True
            if "ToF Sensor Test" in line or "Streaming readings" in line:
                ser.close()
                return True
        ser.close()
    except Exception:
        pass
    return False


def find_port(hint: str) -> str:
    candidates = list_serial_ports()
    if not candidates:
        raise FileNotFoundError(
            f"No serial port found. Tried: {', '.join(DEFAULT_PORTS)}"
        )
    if hint:
        if os.path.exists(hint):
            return hint
        STATE.set_error(f"{hint} missing — scanning {', '.join(candidates)}")
    # Prefer port that actually streams ToF (handles ttyUSB0 → ttyUSB1 hops)
    for p in reversed(candidates):
        if probe_port(p):
            return p
    return candidates[-1]


def _handle_serial_line(line: str) -> None:
    pos = _BASE_STATUS_RE.match(line)
    if pos:
        STATE.update_pose(body_yaw_deg=float(pos.group(2)))
        return

    m = _TOF_RE.search(line)
    if m:
        raw = [int(m.group(i)) for i in range(1, 4)]
        mm, vel, open_flags = FILTER_BANK.update_all(raw)
        STATE.update_sample(mm, vel, open_flags=open_flags)
        return

    if line and not line.startswith("TOF"):
        STATE.add_boot(line)


def serial_reader(port_hint: str, *, base_yaw_sign: float) -> None:
    STATE.update_pose(base_yaw_sign=base_yaw_sign)
    while True:
        try:
            port = find_port(port_hint)
            ser = serial.Serial(port, BAUD, timeout=0.5)
            time.sleep(1.5)
            STATE.set_connected(port)
            deadline = time.time() + 4.0
            while time.time() < deadline:
                line = ser.readline().decode("utf-8", errors="ignore").strip()
                if line:
                    _handle_serial_line(line)
                if "Streaming readings" in line:
                    break

            last_enc_poll = 0.0
            while True:
                now = time.time()
                if now - last_enc_poll >= ENCODER_POLL_SEC:
                    ser.write(b"?\n")
                    ser.flush()
                    last_enc_poll = now

                line = ser.readline().decode("utf-8", errors="ignore").strip()
                if not line:
                    continue
                _handle_serial_line(line)
        except Exception as exc:
            STATE.set_error(str(exc))
            time.sleep(2.0)
            port_hint = ""  # rescan all ports after disconnect


def _html_body() -> bytes:
    cfg = _load_viz_config()
    poll_ms = int(cfg.get("map_poll_ms", 30))
    return build_tof_dashboard_html(poll_ms=poll_ms).encode("utf-8")


class Handler(BaseHTTPRequestHandler):
    def log_message(self, fmt: str, *args: Any) -> None:
        pass

    def do_GET(self) -> None:
        if serve_static(self, self.path):
            return
        if self.path.split("?", 1)[0] in ("/", "/index.html"):
            body = _html_body()
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Cache-Control", "no-store")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
        elif self.path == "/api/state":
            body = json.dumps(STATE.snapshot()).encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Cache-Control", "no-store")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
        else:
            self.send_error(404)


def main() -> None:
    parser = argparse.ArgumentParser(description="ToF web visualizer")
    parser.add_argument("serial_port", nargs="?", default="", help="e.g. /dev/ttyUSB0")
    parser.add_argument("--port", type=int, default=8765, help="HTTP port (default 8765)")
    parser.add_argument("--host", default="0.0.0.0", help="bind address")
    parser.add_argument(
        "--control",
        action="store_true",
        help="IMU base control: aim at person, return HOME when clear",
    )
    parser.add_argument(
        "--no-imu",
        action="store_true",
        help="With --control: skip IMU (viz only, no turns)",
    )
    args = parser.parse_args()

    if args.control:
        from approach import run_approach

        run_approach(
            serial_port=args.serial_port,
            host=args.host,
            viz_port=args.port,
            no_imu=args.no_imu,
        )
        return

    viz_cfg = _load_viz_config()
    base_yaw_sign = float(viz_cfg.get("base_yaw_sign", -1.0))

    try:
        port = find_port(args.serial_port)
    except FileNotFoundError as exc:
        print(exc)
        sys.exit(1)

    t = threading.Thread(
        target=serial_reader,
        kwargs={"port_hint": port, "base_yaw_sign": base_yaw_sign},
        daemon=True,
    )
    t.start()

    server = ThreadingHTTPServer((args.host, args.port), Handler)
    print(f"ToF visualizer:  http://localhost:{args.port}")
    print(f"                 http://127.0.0.1:{args.port}")
    try:
        import socket
        hostname = socket.gethostname()
        local_ip = socket.gethostbyname(hostname)
        print(f"                 http://{local_ip}:{args.port}")
    except OSError:
        pass
    print(f"Serial: {port} @ {BAUD}")
    print("Ctrl+C to stop  (add --control for IMU person-aim + HOME return)")

    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nStopped.")
        server.shutdown()


if __name__ == "__main__":
    main()
