"""Unified Debug Dashboard (MJPEG Stream + 3D Visualization).

Serves a single HTTP dashboard showing both the camera stream and the 3D head physics.
"""

from __future__ import annotations

import io
import json
import socketserver
import threading
import time
from dataclasses import asdict
from http.server import BaseHTTPRequestHandler, HTTPServer
from typing import Any

from PIL import Image

from core.blackboard import Blackboard
from head_debug_viz import (
    HeadDebugSnapshot,
    _DEBUG_HTML,
    servo_pan_to_mechanical,
    servo_tilt_to_mechanical,
)

# Modify the HTML to include the camera stream at the top of the HUD
_MODIFIED_HTML = _DEBUG_HTML.replace(
    '<h1>Head debug</h1>',
    '<h1>Head debug</h1>\n    <div style="margin-bottom: 10px; border: 1px solid #2a3142; background: #000;"><img src="/stream" style="width: 100%; height: auto; display: block;" alt="Camera Stream (disabled)"></div>',
)


def build_debug_snapshot(
    state: dict[str, Any],
    *,
    servo_cfg: dict[str, Any],
    debug_viz_cfg: dict[str, Any],
    base_cfg: dict[str, Any],
) -> dict[str, Any]:
    """Map blackboard state to the 3D visualizer snapshot format."""
    pan_center = float(servo_cfg.get("pan_center", 100.0))
    tilt_center = float(servo_cfg.get("tilt_center", 110.0))
    pan_min = float(servo_cfg.get("pan_min", 25.0))
    pan_max = float(servo_cfg.get("pan_max", 150.0))
    tilt_min = float(servo_cfg.get("tilt_min", 100.0))
    tilt_max = float(servo_cfg.get("tilt_max", 150.0))
    tilt_mech_up = float(servo_cfg.get("tilt_max_mechanical_deg", 45.0))
    tilt_mech_down = float(servo_cfg.get("tilt_min_mechanical_deg", -35.0))
    pan_mech_left = float(servo_cfg.get("pan_mech_left_deg", -40.0))
    pan_mech_right = float(servo_cfg.get("pan_mech_right_deg", 40.0))

    mech_kw = dict(
        center=pan_center,
        p_min=pan_min,
        p_max=pan_max,
        mech_left_deg=pan_mech_left,
        mech_right_deg=pan_mech_right,
    )
    tilt_kw = dict(
        center=tilt_center,
        t_min=tilt_min,
        t_max=tilt_max,
        mech_down_deg=tilt_mech_down,
        mech_up_deg=tilt_mech_up,
    )

    pan = float(state.get("servo_pan", pan_center))
    tilt = float(state.get("servo_tilt", tilt_center))
    pan_mech = servo_pan_to_mechanical(pan, **mech_kw)
    tilt_mech = servo_tilt_to_mechanical(tilt, **tilt_kw)

    imu_yaw_total = float(state.get("imu_yaw_integral_deg", 0.0))
    imu_inferred_base = float(state.get("imu_inferred_base_deg", 0.0))
    imu_pan_delta = imu_yaw_total - imu_inferred_base

    snap = HeadDebugSnapshot(
        ts=time.time(),
        pan=pan,
        tilt=tilt,
        pan_target=pan,
        tilt_target=tilt,
        pan_mech_deg=pan_mech,
        tilt_mech_deg=tilt_mech,
        pan_target_mech_deg=pan_mech,
        tilt_target_mech_deg=tilt_mech,
        pan_center=pan_center,
        tilt_center=tilt_center,
        tilt_effective_center=float(state.get("imu_effective_tilt_center", tilt_center)),
        pan_min=pan_min,
        pan_max=pan_max,
        tilt_min=tilt_min,
        tilt_max=tilt_max,
        tilt_mech_up_deg=tilt_mech_up,
        tilt_mech_down_deg=tilt_mech_down,
        pan_mech_left_deg=pan_mech_left,
        pan_mech_right_deg=pan_mech_right,
        mode=str(state.get("servo_mode", "idle")),
        track_kind=str(state.get("track_kind", "none")),
        face_seen=bool(state.get("face_detected", False)),
        face_norm_x=float(state.get("face_norm_x", 0.0)),
        face_norm_y=float(state.get("face_norm_y", 0.0)),
        face_count=int(state.get("face_count", 0)),
        body_seen=bool(state.get("body_detected", False)),
        base_yaw_deg=float(state.get("base_encoder_deg", 0.0)),
        base_world_yaw_deg=float(state.get("base_world_yaw_deg", 0.0)),
        imu_yaw_total_deg=imu_yaw_total,
        imu_pan_delta_deg=imu_pan_delta,
        imu_inferred_base_deg=imu_inferred_base,
        viz_base_yaw_sign=float(debug_viz_cfg.get("base_yaw_sign", 1.0)),
        base_max_yaw_deg=float(base_cfg.get("max_yaw_deg", 120.0)),
        base_busy=bool(state.get("base_motion_busy", False)),
        imu_enabled=bool(state.get("imu_available", False)),
        imu_pitch_deg=float(state.get("imu_pitch_deg", 0.0)),
        imu_roll_deg=float(state.get("imu_roll_deg", 0.0)),
        imu_gyro_dps=float(state.get("imu_gyro_dps", 0.0)),
        imu_accel_trusted=bool(state.get("imu_accel_trusted", True)),
        imu_horizon_ok=bool(state.get("imu_horizon_ok", True)),
        servo_connected=True,
        person_memories=state.get("person_snapshots") or [],
        active_memory_id=0,
    )
    return asdict(snap)


class _DashboardHandler(BaseHTTPRequestHandler):
    bb: Blackboard
    servo_cfg: dict[str, Any]
    debug_viz_cfg: dict[str, Any]
    base_cfg: dict[str, Any]

    def log_message(self, format: str, *args: Any) -> None:
        return

    def do_GET(self) -> None:
        if self.path in ("/", "/index.html"):
            body = _MODIFIED_HTML.encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
            return

        if self.path == "/api/state":
            state = self.bb.read_all()
            snap = build_debug_snapshot(
                state,
                servo_cfg=self.servo_cfg,
                debug_viz_cfg=self.debug_viz_cfg,
                base_cfg=self.base_cfg,
            )
            body = json.dumps(snap).encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Cache-Control", "no-store")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
            return

        if self.path == "/stream":
            self.send_response(200)
            self.send_header("Age", "0")
            self.send_header("Cache-Control", "no-cache, private")
            self.send_header("Pragma", "no-cache")
            self.send_header("Content-Type", "multipart/x-mixed-replace; boundary=frame")
            self.end_headers()

            try:
                while self.bb.read("running")["running"]:
                    frame = self.bb.read("stream_frame")["stream_frame"]
                    if frame is None:
                        time.sleep(0.05)
                        continue

                    img = Image.fromarray(frame)
                    buf = io.BytesIO()
                    img.save(buf, format="JPEG", quality=65)
                    jpg = buf.getvalue()

                    self.wfile.write(b"--frame\r\n")
                    self.wfile.write(b"Content-Type: image/jpeg\r\n")
                    self.wfile.write(f"Content-Length: {len(jpg)}\r\n\r\n".encode("utf-8"))
                    self.wfile.write(jpg)
                    self.wfile.write(b"\r\n")
                    time.sleep(0.05)
            except (BrokenPipeError, ConnectionResetError):
                return
            return

        self.send_error(404)


class ThreadingDebugHTTPServer(socketserver.ThreadingMixIn, HTTPServer):
    daemon_threads = True
    allow_reuse_address = True


class DebugDashboard:
    """Unified Background Service for Debug Visuals and MJPEG Streaming."""

    def __init__(
        self,
        bb: Blackboard,
        *,
        host: str = "0.0.0.0",
        port: int = 8082,
        servo_cfg: dict[str, Any] | None = None,
        debug_viz_cfg: dict[str, Any] | None = None,
        base_cfg: dict[str, Any] | None = None,
    ) -> None:
        self.bb = bb
        self.host = host
        self.port = port
        self.servo_cfg = servo_cfg or {}
        self.debug_viz_cfg = debug_viz_cfg or {}
        self.base_cfg = base_cfg or {}
        self._http = None

    def run(self) -> None:
        handler = type(
            "BoundDashboardHandler",
            (_DashboardHandler,),
            {
                "bb": self.bb,
                "servo_cfg": self.servo_cfg,
                "debug_viz_cfg": self.debug_viz_cfg,
                "base_cfg": self.base_cfg,
            },
        )
        try:
            self._http = ThreadingDebugHTTPServer((self.host, self.port), handler)
        except OSError as e:
            print(f"[DebugDashboard] Could not bind to port {self.port}: {e}")
            return

        print(
            f"[DebugDashboard] Started on http://"
            f"{self.host if self.host != '0.0.0.0' else 'localhost'}:{self.port}/"
        )

        server_thread = threading.Thread(target=self._http.serve_forever, daemon=True)
        server_thread.start()

        while self.bb.read("running")["running"]:
            time.sleep(0.5)

        self._http.shutdown()
        self._http.server_close()
        print("[DebugDashboard] Stopped.")
