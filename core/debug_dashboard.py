"""Unified Debug Dashboard (MJPEG Stream + 3D Visualization).

Serves a single HTTP dashboard on port 8080 showing both the 
camera stream and the 3D head physics.
"""

from __future__ import annotations

import io
import json
import socketserver
import threading
import time
from http.server import BaseHTTPRequestHandler, HTTPServer
from typing import Any

from PIL import Image

from core.blackboard import Blackboard
from head_debug_viz import _DEBUG_HTML

# Modify the HTML to include the camera stream at the top of the HUD
_MODIFIED_HTML = _DEBUG_HTML.replace(
    '<h1>Head debug</h1>',
    '<h1>Head debug</h1>\n    <div style="margin-bottom: 10px; border: 1px solid #2a3142; background: #000;"><img src="/stream" style="width: 100%; height: auto; display: block;" alt="Camera Stream (disabled)"></div>'
)

class _DashboardHandler(BaseHTTPRequestHandler):
    bb: Blackboard

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
            
            # Map blackboard state to the 3D visualizer's expected format
            snap = {
                "ts": time.time(),
                "pan": state.get("servo_pan", 80.0),
                "tilt": state.get("servo_tilt", 110.0),
                "pan_target": state.get("servo_pan", 80.0),
                "tilt_target": state.get("servo_tilt", 110.0),
                "pan_center": 80.0,
                "tilt_center": 110.0,
                "tilt_effective_center": state.get("imu_effective_tilt_center", 114.0),
                "pan_min": 40.0,
                "pan_max": 120.0,
                "tilt_min": 100.0,
                "tilt_max": 127.0,
                "tilt_mech_up_deg": 45.0,
                "tilt_mech_down_deg": -30.0,
                "pan_mech_left_deg": -40.0,
                "pan_mech_right_deg": 40.0,
                "mode": state.get("servo_mode", "idle"),
                "track_kind": state.get("track_kind", "none"),
                "face_seen": state.get("face_detected", False),
                "face_norm_x": state.get("face_norm_x", 0.0),
                "face_norm_y": state.get("face_norm_y", 0.0),
                "face_count": state.get("face_count", 0),
                "body_seen": state.get("body_detected", False),
                "base_yaw_deg": state.get("base_encoder_deg", 0.0),
                "base_world_yaw_deg": state.get("base_world_yaw_deg", 0.0),
                "base_busy": state.get("base_motion_busy", False),
                "base_motion_allowed": state.get("base_motion_allowed", True),
                "base_fault_reason": state.get("base_fault_reason"),
                "imu_pitch_deg": state.get("imu_pitch_deg", 0.0),
                "imu_roll_deg": state.get("imu_roll_deg", 0.0),
                "imu_gyro_dps": state.get("imu_gyro_dps", 0.0),
                "imu_yaw_integral_deg": state.get("imu_yaw_integral_deg", 0.0),
                "imu_effective_tilt_center": state.get("imu_effective_tilt_center", 114.0),
                "imu_enabled": state.get("imu_available", False),
                "imu_horizon_ok": state.get("imu_horizon_ok", True),
                "person_memories": state.get("person_snapshots", []),
                "servo_connected": True,
            }
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

                    # Encode to JPEG
                    img = Image.fromarray(frame)
                    buf = io.BytesIO()
                    img.save(buf, format="JPEG", quality=65)
                    jpg = buf.getvalue()

                    self.wfile.write(b"--frame\r\n")
                    self.wfile.write(b"Content-Type: image/jpeg\r\n")
                    self.wfile.write(f"Content-Length: {len(jpg)}\r\n\r\n".encode("utf-8"))
                    self.wfile.write(jpg)
                    self.wfile.write(b"\r\n")
                    time.sleep(0.05)  # ~20fps max
            except (BrokenPipeError, ConnectionResetError):
                return
            return

        self.send_error(404)


class ThreadingDebugHTTPServer(socketserver.ThreadingMixIn, HTTPServer):
    daemon_threads = True
    allow_reuse_address = True


class DebugDashboard:
    """Unified Background Service for Debug Visuals and MJPEG Streaming."""

    def __init__(self, bb: Blackboard, host: str = "0.0.0.0", port: int = 8080) -> None:
        self.bb = bb
        self.host = host
        self.port = port
        self._http = None

    def run(self) -> None:
        handler = type("BoundDashboardHandler", (_DashboardHandler,), {"bb": self.bb})
        try:
            self._http = ThreadingDebugHTTPServer((self.host, self.port), handler)
        except OSError as e:
            print(f"[DebugDashboard] Could not bind to port {self.port}: {e}")
            return
            
        print(f"[DebugDashboard] Started on http://{self.host if self.host != '0.0.0.0' else 'localhost'}:{self.port}/")
        
        # We start the server in a separate thread so this run() method can poll 'running' to shutdown gracefully
        server_thread = threading.Thread(target=self._http.serve_forever, daemon=True)
        server_thread.start()

        while self.bb.read("running")["running"]:
            time.sleep(0.5)

        self._http.shutdown()
        self._http.server_close()
        print("[DebugDashboard] Stopped.")
