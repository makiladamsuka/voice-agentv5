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
    serve_debug_static,
    servo_pan_to_mechanical,
    servo_tilt_to_mechanical,
)
from lib.live_tune import (
    merge_tune_values,
    save_tune_to_config,
    tune_schema_dicts,
)

_CAMERA_STREAM_HTML = (
    '<div style="margin-bottom: 10px; border: 1px solid #2a3142; background: #000;">'
    '<img src="/stream" style="width: 100%; height: auto; display: block;" '
    'alt="Camera stream"></div>'
)


def _dashboard_html(*, include_camera_stream: bool) -> str:
    html = _DEBUG_HTML
    if include_camera_stream:
        html = html.replace(
            "<h1>Head debug</h1>",
            f"<h1>Head debug</h1>\n    {_CAMERA_STREAM_HTML}",
        )
    return html


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
        return f"TRACKING ({kind})"
    if mode == "last_seen":
        return "Last seen"
    if mode in ("manual", "manual_test"):
        return "Manual"
    if mode == "wander":
        return "Wandering"
    return mode or "Unknown"


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
    pan_sign = float(servo_cfg.get("pan_sign", 1.0))
    tilt_sign = float(servo_cfg.get("tilt_sign", -1.0))
    pan_mech = servo_pan_to_mechanical(pan, **mech_kw) * pan_sign
    tilt_mech = servo_tilt_to_mechanical(tilt, **tilt_kw) * tilt_sign

    imu_yaw_total = float(state.get("imu_yaw_integral_deg", 0.0))
    imu_inferred_base = float(state.get("imu_inferred_base_deg", 0.0))
    base_enc = float(state.get("base_encoder_deg", 0.0))
    body_yaw = float(state.get("body_yaw_deg", base_enc))
    head_on_body = float(state.get("head_yaw_on_body_deg", imu_yaw_total - body_yaw))
    imu_rel = float(state.get("imu_yaw_rel_deg", imu_yaw_total))
    world_head = float(state.get("base_world_yaw_deg", body_yaw + head_on_body))
    head_vs_servo = float(state.get("head_imu_vs_servo_delta_deg", head_on_body - pan_mech))
    true_front_heading = float(state.get("true_front_heading_deg", world_head))
    true_front_body = float(state.get("true_front_body_deg", body_yaw))
    fusion_pan_err = float(state.get("fusion_head_pan_error_deg", head_vs_servo))
    base_spin_active = bool(state.get("base_spin_active", False))
    imu_pan_delta = head_on_body

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
        body_yaw_deg=body_yaw,
        head_yaw_on_body_deg=head_on_body,
        imu_yaw_rel_deg=imu_rel,
        world_head_yaw_deg=world_head,
        head_imu_vs_servo_delta_deg=head_vs_servo,
        true_front_heading_deg=true_front_heading,
        true_front_body_deg=true_front_body,
        fusion_head_pan_error_deg=fusion_pan_err,
        base_spin_active=base_spin_active,
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
        motion_memories=state.get("motion_snapshots") or [],
        active_memory_id=0,
        prox_approach_active=bool(state.get("prox_approach_active", False)),
        prox_approach_zone=str(state.get("prox_approach_zone", "")),
        prox_approach_velocity=float(state.get("prox_approach_velocity", 0.0)),
        prox_approach_distance=int(state.get("prox_approach_distance", 0)),
        prox_approach_confidence=int(state.get("prox_approach_confidence", 0)),
        prox_zone_left=bool(state.get("prox_zone_left", False)),
        prox_zone_center=bool(state.get("prox_zone_center", False)),
        prox_zone_right=bool(state.get("prox_zone_right", False)),
        prox_search_active=bool(state.get("prox_search_active", False)),
        prox_glance_active=bool(state.get("prox_glance_active", False)),
        prox_investigate_active=bool(state.get("prox_investigate_active", False)),
        prox_investigate_phase=str(state.get("prox_investigate_phase", "")),
        prox_investigate_zone=str(state.get("prox_investigate_zone", "")),
    )
    result = asdict(snap)
    result["manual_control_enabled"] = bool(state.get("manual_control_enabled", False))
    result["head_step_deg"] = float(state.get("debug_head_step_deg", 5.0))
    result["imu_yaw_raw_deg"] = float(state.get("imu_yaw_raw_deg", imu_yaw_total))
    result["imu_drift_correction_deg"] = float(state.get("imu_drift_correction_deg", 0.0))
    result["fusion_stationary"] = bool(state.get("fusion_stationary", False))
    result["fusion_delta_deg"] = head_vs_servo
    base_sign = float(debug_viz_cfg.get("base_yaw_sign", 1.0))
    pan_sign = float(debug_viz_cfg.get("pan_yaw_sign", 1.0))
    tilt_sign = float(debug_viz_cfg.get("tilt_sign", 1.0))
    imu_pitch_sign = float(debug_viz_cfg.get("imu_pitch_sign", -1.0))
    result["viz_pan_yaw_sign"] = pan_sign
    result["viz_tilt_sign"] = tilt_sign
    result["viz_imu_pitch_sign"] = imu_pitch_sign
    result["true_front_heading_deg"] = true_front_heading
    result["true_front_body_deg"] = true_front_body
    result["fusion_head_pan_error_deg"] = fusion_pan_err
    result["base_spin_active"] = base_spin_active
    result["base_fwd_deg"] = base_enc
    result["true_north_deg"] = 0.0
    result["pan_rel_base_deg"] = pan_mech
    result["world_fwd_deg"] = world_head
    result["base_ground_yaw_deg"] = body_yaw
    result["neck_pan_rel_base_deg"] = head_on_body
    result["head_yaw_ground_deg"] = world_head
    result["tilt_ground_deg"] = tilt_mech
    result["pan_ground_yaw_deg"] = world_head
    result["viz_world_applied_deg"] = base_sign * (body_yaw + head_on_body)
    result["tilt_rel_fwd_deg"] = tilt_mech
    result["viz_tilt_applied_deg"] = tilt_mech * tilt_sign
    result["viz_imu_pitch_applied_deg"] = float(state.get("imu_pitch_deg", 0.0)) * imu_pitch_sign
    mode = str(state.get("servo_mode", "idle"))
    forward_return = bool(state.get("servo_forward_return_active", False))
    track_kind = str(state.get("track_kind", "none"))
    result["forward_return_active"] = forward_return
    result["pan_hold"] = bool(state.get("servo_pan_hold", False))
    result["mode_label"] = _mode_display_label(
        mode, forward_return=forward_return, track_kind=track_kind,
    )
    cpu_temp = _read_cpu_temp_c()
    if cpu_temp is not None:
        result["cpu_temp_c"] = cpu_temp
    result["live_tune"] = merge_tune_values(
        state.get("debug_live_tune"),
        servo_cfg=servo_cfg,
        base_cfg=base_cfg,
    )
    result["live_tune_schema"] = tune_schema_dicts()
    return result


class _DashboardHandler(BaseHTTPRequestHandler):
    bb: Blackboard
    servo_cfg: dict[str, Any]
    debug_viz_cfg: dict[str, Any]
    base_cfg: dict[str, Any]
    config_path: Any
    dashboard_html: str

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
        config_path: Any = None,
        include_camera_stream: bool = True,
    ) -> None:
        self.bb = bb
        self.host = host
        self.port = port
        self.servo_cfg = servo_cfg or {}
        self.debug_viz_cfg = debug_viz_cfg or {}
        self.base_cfg = base_cfg or {}
        self.config_path = config_path
        self.include_camera_stream = include_camera_stream
        self._http = None

    def run(self) -> None:
        dashboard_html = _dashboard_html(include_camera_stream=self.include_camera_stream)
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
