"""Unified Debug Dashboard (MJPEG Stream + ToF/yaw 3D map)."""

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
from core.debug_dashboard_html import build_debug_dashboard_html
from head_debug_viz import serve_debug_static
from lib.live_tune import (
    merge_tune_values,
    save_tune_to_config,
    tune_schema_dicts,
)


def _read_cpu_temp_c() -> float | None:
    """Raspberry Pi thermal zone0 in °C, or None if unavailable."""
    try:
        with open("/sys/class/thermal/thermal_zone0/temp", encoding="ascii") as f:
            return int(f.read().strip()) / 1000.0
    except OSError:
        return None



def _mode_display_label(
    mode: str,
    *,
    forward_return: bool,
    track_kind: str,
) -> str:
    if forward_return:
        return "Returning forward"
    if mode == "track":
        kind = track_kind if track_kind not in ("", "none") else "target"
        return f"Tracking ({kind})"
    if mode == "last_seen":
        return "Last seen"
    if mode in ("manual", "manual_test"):
        return "Manual"
    if mode == "wander":
        return "Wandering"
    if mode == "memory_track":
        return "Memory track"
    return mode.capitalize() if mode else "Idle"


def build_merged_state(
    bb_state: dict[str, Any],
    tof_snap: dict[str, Any],
    *,
    servo_cfg: dict[str, Any],
    base_cfg: dict[str, Any],
) -> dict[str, Any]:
    """Merge TofState snapshot with blackboard HUD overlay."""
    merged = dict(tof_snap)
    merged["servo_mode"] = str(bb_state.get("servo_mode", "idle"))
    merged["face_detected"] = bool(bb_state.get("face_detected", False))
    merged["prox_approach_zone"] = str(bb_state.get("prox_approach_zone", ""))
    merged["track_kind"] = str(bb_state.get("track_kind", "none"))
    merged["body_detected"] = bool(bb_state.get("body_detected", False))
    merged["servo_forward_return_active"] = bool(bb_state.get("servo_forward_return_active", False))
    merged["prox_approach_active"] = bool(bb_state.get("prox_approach_active", False))
    merged["mode_label"] = _mode_display_label(
        str(bb_state.get("servo_mode", "idle")),
        forward_return=merged["servo_forward_return_active"],
        track_kind=merged["track_kind"],
    )
    merged["base_motion_busy"] = bool(bb_state.get("base_motion_busy", False))
    merged["manual_control_enabled"] = bool(bb_state.get("manual_control_enabled", False))
    merged["head_step_deg"] = float(bb_state.get("debug_head_step_deg", 5.0))
    merged["live_tune"] = merge_tune_values(
        bb_state.get("debug_live_tune"),
        servo_cfg=servo_cfg,
        base_cfg=base_cfg,
    )
    merged["live_tune_schema"] = tune_schema_dicts()
    cpu_temp = _read_cpu_temp_c()
    if cpu_temp is not None:
        merged["cpu_temp_c"] = cpu_temp
    return merged


class _DashboardHandler(BaseHTTPRequestHandler):
    bb: Blackboard
    servo_cfg: dict[str, Any]
    debug_viz_cfg: dict[str, Any]
    base_cfg: dict[str, Any]
    config_path: Any
    dashboard_html: str
    tof_state: Any

    def log_message(self, format: str, *args: Any) -> None:
        return

    def _send_json(self, code: int, payload: dict[str, Any]) -> None:
        body = json.dumps(payload).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Cache-Control", "no-store")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_POST(self) -> None:
        length = int(self.headers.get("Content-Length", 0))
        raw = self.rfile.read(length) if length > 0 else b"{}"
        try:
            payload = json.loads(raw.decode("utf-8"))
        except json.JSONDecodeError:
            self._send_json(400, {"ok": False, "error": "invalid json"})
            return

        if self.path == "/api/tune":
            values = payload.get("values")
            if not isinstance(values, dict):
                self._send_json(400, {"ok": False, "error": "missing values object"})
                return
            state = self.bb.read("debug_live_tune", "debug_tune_seq")
            merged = merge_tune_values(
                state["debug_live_tune"],
                servo_cfg=self.servo_cfg,
                base_cfg=self.base_cfg,
            )
            for key, val in values.items():
                try:
                    merged[key] = float(val)
                except (TypeError, ValueError):
                    self._send_json(400, {"ok": False, "error": f"bad value for {key}"})
                    return
            seq = int(state["debug_tune_seq"]) + 1
            self.bb.write(debug_live_tune=merged, debug_tune_seq=seq)
            self._send_json(200, {"ok": True, "seq": seq, "live_tune": merged})
            return

        if self.path == "/api/save_config":
            state = self.bb.read("debug_live_tune")
            tune = merge_tune_values(
                state["debug_live_tune"],
                servo_cfg=self.servo_cfg,
                base_cfg=self.base_cfg,
            )
            try:
                updated = save_tune_to_config(self.config_path, tune)
            except (OSError, RuntimeError) as e:
                self._send_json(500, {"ok": False, "error": str(e)})
                return
            self._send_json(200, {"ok": True, "updated": updated, "path": str(self.config_path)})
            return

        if self.path != "/api/control":
            self.send_error(404)
            return
        cmd = str(payload.get("cmd", "")).strip()
        if not cmd:
            self._send_json(400, {"ok": False, "error": "missing cmd"})
            return

        seq = int(payload.get("seq", 0))
        step = payload.get("step")
        writes: dict[str, Any] = {
            "debug_control_cmd": cmd,
            "debug_control_seq": seq,
        }
        if step is not None:
            writes["debug_head_step_deg"] = float(step)
        self.bb.write(**writes)
        self._send_json(200, {"ok": True, "cmd": cmd, "seq": seq})

    def do_GET(self) -> None:
        if serve_debug_static(self, self.path):
            return
        if self.path in ("/", "/index.html"):
            body = self.dashboard_html.encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
            return

        if self.path == "/api/state":
            bb_state = self.bb.read_all()
            tof_snap = (
                self.tof_state.snapshot()
                if self.tof_state is not None
                else {"connected": False, "error": "ToF state unavailable"}
            )
            snap = build_merged_state(
                bb_state,
                tof_snap,
                servo_cfg=self.servo_cfg,
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
        config_path: Any = None,
        include_camera_stream: bool = True,
        tof_state: Any = None,
    ) -> None:
        self.bb = bb
        self.host = host
        self.port = port
        self.servo_cfg = servo_cfg or {}
        self.debug_viz_cfg = debug_viz_cfg or {}
        self.base_cfg = base_cfg or {}
        self.config_path = config_path
        self.include_camera_stream = include_camera_stream
        self.tof_state = tof_state
        self._http = None

    def run(self) -> None:
        poll_ms = int(self.debug_viz_cfg.get("map_poll_ms", 30))
        dashboard_html = build_debug_dashboard_html(
            poll_ms=poll_ms,
            include_camera_stream=self.include_camera_stream,
        )
        handler = type(
            "BoundDashboardHandler",
            (_DashboardHandler,),
            {
                "bb": self.bb,
                "servo_cfg": self.servo_cfg,
                "debug_viz_cfg": self.debug_viz_cfg,
                "base_cfg": self.base_cfg,
                "config_path": self.config_path,
                "dashboard_html": dashboard_html,
                "tof_state": self.tof_state,
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
