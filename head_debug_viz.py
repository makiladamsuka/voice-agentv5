"""Live 3D head debug visualizer for face_tracking_head.py (HTTP + Three.js)."""

from __future__ import annotations

import json
import socket
import socketserver
import threading
import time
from dataclasses import asdict, dataclass, field
from http.server import BaseHTTPRequestHandler, HTTPServer
from typing import Any, Optional


def find_available_port(host: str, preferred: int, *, max_tries: int = 12) -> int:
    for port in range(preferred, preferred + max_tries):
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
            sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            try:
                sock.bind((host, port))
                return port
            except OSError:
                continue
    raise RuntimeError(f"no free port in range {preferred}-{preferred + max_tries - 1}")


def servo_tilt_to_mechanical(
    tilt_cmd: float,
    *,
    center: float,
    t_min: float,
    t_max: float,
    mech_down_deg: float,
    mech_up_deg: float,
) -> float:
    """Map servo command angle to physical tilt degrees (0 = upright at center)."""
    tilt_cmd = max(t_min, min(t_max, tilt_cmd))
    if tilt_cmd >= center:
        span = max(t_max - center, 1e-6)
        return mech_up_deg * (tilt_cmd - center) / span
    span = max(center - t_min, 1e-6)
    return mech_down_deg * (tilt_cmd - center) / span  # mech_down_deg is negative


def servo_pan_to_mechanical(
    pan_cmd: float,
    *,
    center: float,
    p_min: float,
    p_max: float,
    mech_left_deg: float,
    mech_right_deg: float,
) -> float:
    """Map servo pan command to physical yaw degrees (0 = center)."""
    pan_cmd = max(p_min, min(p_max, pan_cmd))
    if pan_cmd >= center:
        span = max(p_max - center, 1e-6)
        return mech_right_deg * (pan_cmd - center) / span
    span = max(center - p_min, 1e-6)
    return mech_left_deg * (pan_cmd - center) / span


@dataclass
class HeadDebugSnapshot:
    ts: float = 0.0
    pan: float = 0.0
    tilt: float = 0.0
    pan_target: float = 0.0
    tilt_target: float = 0.0
    pan_mech_deg: float = 0.0
    tilt_mech_deg: float = 0.0
    pan_target_mech_deg: float = 0.0
    tilt_target_mech_deg: float = 0.0
    pan_center: float = 80.0
    tilt_center: float = 112.0
    tilt_effective_center: float = 112.0
    pan_min: float = 40.0
    pan_max: float = 120.0
    tilt_min: float = 100.0
    tilt_max: float = 127.0
    tilt_mech_up_deg: float = 45.0
    tilt_mech_down_deg: float = -30.0
    pan_mech_left_deg: float = -40.0
    pan_mech_right_deg: float = 40.0
    mode: str = "idle"
    track_kind: str = "none"
    face_seen: bool = False
    face_norm_x: float = 0.0
    face_norm_y: float = 0.0
    face_count: int = 0
    body_seen: bool = False
    base_yaw_deg: float = 0.0
    base_world_yaw_deg: float = 0.0
    base_busy: bool = False
    imu_enabled: bool = False
    imu_pitch_deg: float = 0.0
    imu_roll_deg: float = 0.0
    imu_gyro_dps: float = 0.0
    imu_accel_trusted: bool = True
    imu_horizon_ok: bool = True
    servo_connected: bool = False
    limits: dict[str, float] = field(default_factory=dict)


class HeadDebugState:
    """Thread-safe latest snapshot for the debug HTTP server."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._snap = HeadDebugSnapshot()

    def update(self, **kwargs: Any) -> None:
        with self._lock:
            for key, value in kwargs.items():
                if hasattr(self._snap, key):
                    setattr(self._snap, key, value)
            self._snap.ts = time.time()

    def snapshot(self) -> dict[str, Any]:
        with self._lock:
            return asdict(self._snap)


_DEBUG_HTML = """<!DOCTYPE html>
<html lang="en">
<head>
  <meta charset="utf-8" />
  <meta name="viewport" content="width=device-width, initial-scale=1" />
  <title>Head Debug 3D</title>
  <style>
    html, body { margin: 0; height: 100%; background: #0f1117; color: #d8dee9; font: 13px/1.4 ui-monospace, monospace; }
    #wrap { display: grid; grid-template-columns: 1fr 320px; height: 100%; }
    #view { position: relative; min-height: 320px; }
    #hud {
      padding: 12px 14px; overflow: auto; border-left: 1px solid #2a3142;
      background: #151922;
    }
    h1 { font-size: 14px; margin: 0 0 8px; color: #88c0d0; }
    .row { display: flex; justify-content: space-between; gap: 8px; margin: 2px 0; }
    .k { color: #6b7280; }
    .warn { color: #ebcb8b; }
    .bad { color: #bf616a; }
    .ok { color: #a3be8c; }
    #legend { position: absolute; left: 10px; bottom: 10px; background: rgba(0,0,0,.45); padding: 8px 10px; border-radius: 6px; }
    #legend span { display: inline-block; width: 10px; height: 10px; margin-right: 6px; border-radius: 2px; vertical-align: middle; }
  </style>
</head>
<body>
<div id="wrap">
  <div id="view">
    <div id="legend">
      <div><span style="background:#4ade80"></span>actual head</div>
      <div><span style="background:#fb923c"></span>target</div>
      <div><span style="background:#60a5fa"></span>IMU frame</div>
      <div><span style="background:#f472b6"></span>look ray</div>
    </div>
  </div>
  <div id="hud">
    <h1>Head debug</h1>
    <div id="stats"></div>
  </div>
</div>
<script type="importmap">
{
  "imports": {
    "three": "https://cdn.jsdelivr.net/npm/three@0.161.0/build/three.module.js",
    "three/addons/": "https://cdn.jsdelivr.net/npm/three@0.161.0/examples/jsm/"
  }
}
</script>
<script type="module">
import * as THREE from 'three';
import { OrbitControls } from 'three/addons/controls/OrbitControls.js';

const view = document.getElementById('view');
const stats = document.getElementById('stats');
const scene = new THREE.Scene();
scene.background = new THREE.Color(0x0f1117);

const camera = new THREE.PerspectiveCamera(55, 1, 0.05, 50);
camera.position.set(0.9, 0.55, 1.35);

const renderer = new THREE.WebGLRenderer({ antialias: true });
renderer.setPixelRatio(Math.min(window.devicePixelRatio, 2));
view.appendChild(renderer.domElement);

const controls = new OrbitControls(camera, renderer.domElement);
controls.target.set(0, 0.35, 0);
controls.enableDamping = true;

scene.add(new THREE.GridHelper(2.0, 20, 0x334155, 0x1e293b));
scene.add(new THREE.AxesHelper(0.35));

const light = new THREE.DirectionalLight(0xffffff, 1.1);
light.position.set(2, 3, 2);
scene.add(light);
scene.add(new THREE.AmbientLight(0xffffff, 0.35));

function box(w, h, d, color, opacity=1) {
  const m = new THREE.Mesh(
    new THREE.BoxGeometry(w, h, d),
    new THREE.MeshStandardMaterial({ color, transparent: opacity < 1, opacity })
  );
  return m;
}

const root = new THREE.Group();
scene.add(root);

const base = box(0.55, 0.08, 0.55, 0x475569);
base.position.y = 0.04;
root.add(base);

const neck = box(0.12, 0.16, 0.12, 0x64748b);
neck.position.y = 0.16;
base.add(neck);

const panNode = new THREE.Group();
panNode.position.y = 0.24;
neck.add(panNode);

const tiltNode = new THREE.Group();
panNode.add(tiltNode);

const headMesh = box(0.34, 0.22, 0.18, 0x4ade80);
headMesh.position.y = 0.11;
tiltNode.add(headMesh);

const camLens = box(0.08, 0.08, 0.06, 0x111827);
camLens.position.set(0, 0.11, 0.12);
tiltNode.add(camLens);

const targetGroup = new THREE.Group();
neck.add(targetGroup);
const targetMesh = box(0.28, 0.16, 0.14, 0xfb923c, 0.45);
targetMesh.position.y = 0.11;
targetGroup.add(targetMesh);

const imuGroup = new THREE.Group();
headMesh.add(imuGroup);
const imuMesh = box(0.08, 0.04, 0.06, 0x60a5fa, 0.85);
imuMesh.position.set(0.06, 0.08, 0);
imuGroup.add(imuMesh);

const lookGeom = new THREE.BufferGeometry().setFromPoints([
  new THREE.Vector3(0, 0.11, 0.12),
  new THREE.Vector3(0, 0.11, 1.2),
]);
const lookLine = new THREE.Line(
  lookGeom,
  new THREE.LineBasicMaterial({ color: 0xf472b6 })
);
tiltNode.add(lookLine);

const limitPan = new THREE.Group();
base.add(limitPan);
const limitTilt = new THREE.Group();
panNode.add(limitTilt);

let latest = {};

function deg(v) { return THREE.MathUtils.degToRad(v); }

function servoToRot(pan, tilt, panCenter, tiltCenter, s) {
  const panMech = (s.pan_mech_deg !== undefined)
    ? s.pan_mech_deg
    : (pan - panCenter);
  const tiltMech = (s.tilt_mech_deg !== undefined)
    ? s.tilt_mech_deg
    : (tilt - tiltCenter);
  return {
    pan: deg(panMech),
    tilt: -deg(tiltMech),
  };
}

function setStats(s) {
  const cls = (ok) => ok ? 'ok' : 'bad';
  const hz = s.ts ? (1 / Math.max(0.001, performance.now() / 1000 - (window._lastTs || s.ts))).toFixed(1) : '-';
  window._lastTs = performance.now() / 1000;
  stats.innerHTML = `
    <div class="row"><span class="k">mode</span><span>${s.mode || '-'} / ${s.track_kind || '-'}</span></div>
    <div class="row"><span class="k">pan</span><span>${fmt(s.pan)} cmd (${fmt(s.pan_mech_deg)}° mech) → ${fmt(s.pan_target)}</span></div>
    <div class="row"><span class="k">tilt</span><span>${fmt(s.tilt)} cmd (${fmt(s.tilt_mech_deg)}° mech) → ${fmt(s.tilt_target)}</span></div>
    <div class="row"><span class="k">tilt ctr</span><span>${fmt(s.tilt_effective_center)} cmd (0° mech @ ${fmt(s.tilt_center)})</span></div>
    <div class="row"><span class="k">mech cal</span><span>up ${fmt(s.tilt_mech_up_deg,0)}° / down ${fmt(s.tilt_mech_down_deg,0)}°</span></div>
    <div class="row"><span class="k">limits</span><span>P ${fmt(s.pan_min)}..${fmt(s.pan_max)} T ${fmt(s.tilt_min)}..${fmt(s.tilt_max)}</span></div>
    <div class="row"><span class="k">face</span><span>${s.face_seen ? 'yes' : 'no'} x=${fmt(s.face_norm_x,2)} y=${fmt(s.face_norm_y,2)} n=${s.face_count||0}</span></div>
    <div class="row"><span class="k">base</span><span>world ${fmt(s.base_world_yaw_deg)} busy ${s.base_busy ? 'yes' : 'no'}</span></div>
    <div class="row"><span class="k">IMU pitch</span><span class="${Math.abs(s.imu_pitch_deg||0) > 8 ? 'warn' : 'ok'}">${fmt(s.imu_pitch_deg)}° (vs mech ${fmt(s.tilt_mech_deg)}°)</span></div>
    <div class="row"><span class="k">IMU roll</span><span>${fmt(s.imu_roll_deg)}</span></div>
    <div class="row"><span class="k">IMU gyro</span><span class="${(s.imu_gyro_dps||0) > 35 ? 'warn' : ''}">${fmt(s.imu_gyro_dps)} dps</span></div>
    <div class="row"><span class="k">horizon</span><span class="${cls(s.imu_horizon_ok)}">${s.imu_horizon_ok ? 'updating' : 'held'}</span></div>
    <div class="row"><span class="k">servo link</span><span class="${cls(s.servo_connected)}">${s.servo_connected ? 'connected' : 'off'}</span></div>
  `;
}

function fmt(v, d=1) {
  if (v === undefined || v === null || Number.isNaN(v)) return '-';
  return Number(v).toFixed(d);
}

async function poll() {
  try {
    const res = await fetch('/api/state', { cache: 'no-store' });
    latest = await res.json();
    setStats(latest);
    const a = servoToRot(latest.pan, latest.tilt, latest.pan_center, latest.tilt_center, latest);
    const tPanMech = latest.pan_target_mech_deg ?? (latest.pan_target - latest.pan_center);
    const tTiltMech = latest.tilt_target_mech_deg ?? (latest.tilt_target - latest.tilt_center);
    const t = { pan: deg(tPanMech), tilt: -deg(tTiltMech) };
    root.rotation.y = deg(latest.base_world_yaw_deg || 0);
    panNode.rotation.y = a.pan;
    tiltNode.rotation.x = a.tilt;
    targetGroup.rotation.y = t.pan;
    targetGroup.rotation.x = t.tilt;
    imuGroup.rotation.z = deg(latest.imu_roll_deg || 0);
    imuGroup.rotation.x = deg(latest.imu_pitch_deg || 0);
    const nearTiltMax = latest.tilt_mech_deg >= (latest.tilt_mech_up_deg - 1);
    const nearTiltMin = latest.tilt_mech_deg <= (latest.tilt_mech_down_deg + 1);
    headMesh.material.color.setHex(nearTiltMax || nearTiltMin ? 0xbf616a : 0x4ade80);
  } catch (e) {
    stats.innerHTML = `<div class="bad">poll failed: ${e}</div>`;
  }
}

function resize() {
  const w = view.clientWidth;
  const h = view.clientHeight;
  renderer.setSize(w, h, false);
  camera.aspect = w / Math.max(h, 1);
  camera.updateProjectionMatrix();
}

window.addEventListener('resize', resize);
resize();
setInterval(poll, 100);
poll();

function animate() {
  requestAnimationFrame(animate);
  controls.update();
  renderer.render(scene, camera);
}
animate();
</script>
</body>
</html>
"""


class _DebugHandler(BaseHTTPRequestHandler):
    state: HeadDebugState

    def log_message(self, format: str, *args: Any) -> None:  # noqa: A003
        return

    def do_GET(self) -> None:  # noqa: N802
        if self.path in ("/", "/index.html"):
            body = _DEBUG_HTML.encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
            return
        if self.path == "/api/state":
            body = json.dumps(self.state.snapshot()).encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Cache-Control", "no-store")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
            return
        self.send_error(404)


class ThreadingDebugHTTPServer(socketserver.ThreadingMixIn, HTTPServer):
    daemon_threads = True
    allow_reuse_address = True


class HeadDebugServer:
    def __init__(
        self,
        state: HeadDebugState,
        *,
        host: str = "0.0.0.0",
        port: int = 8082,
    ) -> None:
        self.state = state
        self.host = host
        self.port = find_available_port(host, port)
        self._http: Optional[ThreadingDebugHTTPServer] = None
        self._thread: Optional[threading.Thread] = None

    @property
    def url(self) -> str:
        display_host = "localhost" if self.host in ("0.0.0.0", "") else self.host
        return f"http://{display_host}:{self.port}/"

    def start(self) -> None:
        handler = type("BoundDebugHandler", (_DebugHandler,), {"state": self.state})
        self._http = ThreadingDebugHTTPServer((self.host, self.port), handler)
        self._thread = threading.Thread(target=self._http.serve_forever, daemon=True)
        self._thread.start()

    def stop(self) -> None:
        if self._http is not None:
            self._http.shutdown()
            self._http.server_close()
            self._http = None
