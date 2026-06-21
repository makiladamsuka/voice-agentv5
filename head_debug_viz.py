"""Live 3D head debug visualizer for face_tracking_head.py (HTTP + Three.js)."""

from __future__ import annotations

import json
import socket
import socketserver
import threading
import time
from dataclasses import asdict, dataclass, field
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
from typing import Any, Optional

APP_DIR = Path(__file__).resolve().parent
STATIC_DIR = APP_DIR / "static"
_STATIC_MIME = {
    ".js": "application/javascript; charset=utf-8",
    ".mjs": "application/javascript; charset=utf-8",
    ".css": "text/css; charset=utf-8",
    ".json": "application/json; charset=utf-8",
    ".wasm": "application/wasm",
}


def serve_debug_static(handler: BaseHTTPRequestHandler, path: str) -> bool:
    """Serve files under /static/ (Three.js bundle). Returns True if handled."""
    if not path.startswith("/static/"):
        return False
    rel = path[len("/static/") :].lstrip("/")
    if not rel or ".." in rel.replace("\\", "/"):
        handler.send_error(403)
        return True
    fp = (STATIC_DIR / rel).resolve()
    root = STATIC_DIR.resolve()
    if not str(fp).startswith(str(root)) or not fp.is_file():
        handler.send_error(404)
        return True
    data = fp.read_bytes()
    ctype = _STATIC_MIME.get(fp.suffix.lower(), "application/octet-stream")
    handler.send_response(200)
    handler.send_header("Content-Type", ctype)
    handler.send_header("Cache-Control", "public, max-age=3600")
    handler.send_header("Content-Length", str(len(data)))
    handler.end_headers()
    handler.wfile.write(data)
    return True


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


def servo_pan_to_mechanical(
    pan_cmd: float,
    *,
    center: float,
    p_min: float,
    p_max: float,
    mech_left_deg: float,
    mech_right_deg: float,
) -> float:
    """Map servo pan command to signed mechanical yaw (0 = center, + = right, − = left)."""
    pan_cmd = max(p_min, min(p_max, pan_cmd))
    if pan_cmd >= center:
        span = max(p_max - center, 1e-6)
        return mech_right_deg * (pan_cmd - center) / span
    span = max(center - p_min, 1e-6)
    # Use positive left span scale; sign comes from (pan_cmd − center) < 0.
    left_span_deg = abs(mech_left_deg) if mech_left_deg != 0 else abs(mech_right_deg)
    return left_span_deg * (pan_cmd - center) / span


def servo_tilt_to_mechanical(
    tilt_cmd: float,
    *,
    center: float,
    t_min: float,
    t_max: float,
    mech_down_deg: float,
    mech_up_deg: float,
) -> float:
    """Map servo tilt command to signed mechanical tilt (0 = upright, + = up, − = down)."""
    tilt_cmd = max(t_min, min(t_max, tilt_cmd))
    if tilt_cmd >= center:
        span = max(t_max - center, 1e-6)
        return mech_up_deg * (tilt_cmd - center) / span
    span = max(center - t_min, 1e-6)
    down_span_deg = abs(mech_down_deg) if mech_down_deg != 0 else abs(mech_up_deg)
    return down_span_deg * (tilt_cmd - center) / span


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
    imu_yaw_total_deg: float = 0.0
    imu_pan_delta_deg: float = 0.0
    imu_inferred_base_deg: float = 0.0
    body_yaw_deg: float = 0.0
    head_yaw_on_body_deg: float = 0.0
    imu_yaw_rel_deg: float = 0.0
    world_head_yaw_deg: float = 0.0
    head_imu_vs_servo_delta_deg: float = 0.0
    viz_base_yaw_sign: float = 1.0
    base_max_yaw_deg: float = 120.0
    base_busy: bool = False
    imu_enabled: bool = False
    imu_pitch_deg: float = 0.0
    imu_roll_deg: float = 0.0
    imu_gyro_dps: float = 0.0
    imu_accel_trusted: bool = True
    imu_horizon_ok: bool = True
    servo_connected: bool = False
    person_memories: list[dict[str, Any]] = field(default_factory=list)
    active_memory_id: int = 0
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
    #wrap {
      display: grid;
      grid-template-columns: minmax(0, 1fr) 320px;
      height: 100%;
      min-height: 100vh;
    }
    #view {
      position: relative;
      min-height: 320px;
      min-width: 0;
      height: 100%;
      overflow: hidden;
    }
    #view canvas { display: block; width: 100% !important; height: 100% !important; }
    #viz-loading {
      position: absolute; inset: 0; display: flex; align-items: center; justify-content: center;
      color: #88c0d0; font-size: 12px; pointer-events: none; z-index: 1;
    }
    #ground-hud {
      position: absolute; top: 10px; left: 10px; right: 10px; max-width: 420px;
      background: rgba(15,17,23,0.82); border: 1px solid #334155; border-radius: 8px;
      padding: 10px 12px; font-size: 12px; line-height: 1.55; pointer-events: none;
    }
    #ground-hud .title { color: #88c0d0; margin-bottom: 4px; font-size: 11px; letter-spacing: 0.04em; }
    #ground-hud .grow { display: flex; justify-content: space-between; gap: 10px; }
    #ground-hud .gk { color: #6b7280; }
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
    #legend { position: absolute; left: 10px; bottom: 10px; background: rgba(0,0,0,.45); padding: 8px 10px; border-radius: 6px; font-size: 11px; }
    #legend span { display: inline-block; width: 10px; height: 10px; margin-right: 6px; border-radius: 2px; vertical-align: middle; }
    #controls { margin: 10px 0 12px; padding: 10px; border: 1px solid #2a3142; border-radius: 8px; background: #11151d; }
    #controls h2 { font-size: 12px; margin: 0 0 8px; color: #88c0d0; font-weight: 600; }
    .btn-grid { display: grid; grid-template-columns: repeat(3, 1fr); gap: 6px; margin-bottom: 8px; }
    .btn-grid button, .btn-row button {
      background: #1e293b; color: #e2e8f0; border: 1px solid #334155; border-radius: 6px;
      padding: 8px 4px; font: inherit; cursor: pointer;
    }
    .btn-grid button:hover, .btn-row button:hover { background: #334155; }
    .btn-grid button:active, .btn-row button:active { background: #475569; }
    .btn-row { display: flex; gap: 6px; flex-wrap: wrap; }
    .btn-row button { flex: 1; min-width: 70px; }
    .hint { color: #6b7280; font-size: 11px; margin-top: 6px; }
    #debug-panel { margin-top: 10px; padding-top: 8px; border-top: 1px solid #2a3142; }
    #debug-panel h2 { font-size: 12px; margin: 0 0 6px; color: #88c0d0; font-weight: 600; }
    #mode-banner {
      margin: 0 0 10px; padding: 10px 12px; border-radius: 8px;
      border: 1px solid #2a3142; background: #11151d;
    }
    #mode-banner .mode-line {
      font-size: 15px; font-weight: 700; letter-spacing: 0.06em; margin-bottom: 6px;
    }
    #mode-banner .mode-track { color: #a3be8c; }
    #mode-banner .mode-wander { color: #88c0d0; }
    #mode-banner .mode-return { color: #ebcb8b; }
    #mode-banner .mode-lastseen { color: #d08770; }
    #mode-banner .mode-manual { color: #b48ead; }
    @media (max-width: 900px) {
      #wrap { grid-template-columns: 1fr; grid-template-rows: minmax(240px, 42vh) auto; }
      #hud { border-left: none; border-top: 1px solid #2a3142; max-height: 58vh; }
    }
  </style>
</head>
<body>
<div id="wrap">
  <div id="view">
    <div id="viz-loading">Loading 3D view…</div>
    <div id="ground-hud">
      <div class="title">ANGLES vs GROUND / FORWARD (+Z at startup)</div>
      <div id="ground-hud-body"></div>
    </div>
    <div id="legend">
      <div><span style="background:#e879f9"></span>true north (+Z fixed)</div>
      <div><span style="background:#fb923c"></span>body (encoder, on base)</div>
      <div><span style="background:#f472b6"></span>head on body (IMU − body)</div>
      <div><span style="background:#fbbf24"></span>world aim (body + head)</div>
      <div><span style="background:#4ade80"></span>head mesh</div>
      <div style="margin-top:4px;color:#8899aa">servo pan cross-check: HUD only</div>
    </div>
  </div>
  <div id="hud">
    <h1>Head debug</h1>
    <div id="controls" style="display:none">
      <h2>Manual control</h2>
      <div class="btn-grid">
        <button type="button" data-cmd="tilt_up">W tilt+</button>
        <button type="button" data-cmd="center">C center</button>
        <button type="button" data-cmd="tilt_down">S tilt−</button>
        <button type="button" data-cmd="pan_right">A pan−</button>
        <button type="button" data-cmd="zero_base">Z zero</button>
        <button type="button" data-cmd="pan_left">D pan+</button>
      </div>
      <div class="btn-row">
        <button type="button" data-cmd="fusion_reset">R fusion reset</button>
        <button type="button" data-cmd="quit">Q quit</button>
      </div>
      <div class="hint">WASD keys work when this page is focused. Rotate base by hand.</div>
    </div>
    <div id="mode-banner"></div>
    <div id="debug-panel">
      <h2>Fusion debug</h2>
      <div id="fusion-stats"></div>
    </div>
    <div id="stats"></div>
  </div>
</div>
<script type="importmap">
{
  "imports": {
    "three": "/static/vendor/three.module.js",
    "three/addons/": "/static/vendor/addons/"
  }
}
</script>
<script>
window.addEventListener('error', (ev) => {
  const view = document.getElementById('view');
  const stats = document.getElementById('stats');
  if (!view || view.dataset.vizErr) return;
  view.dataset.vizErr = '1';
  const msg = ev.message || 'unknown error';
  const box = document.createElement('div');
  box.className = 'bad';
  box.style.cssText = 'position:absolute;inset:12px;padding:12px;background:rgba(15,17,23,.92);border:1px solid #bf616a;border-radius:8px;z-index:5;white-space:pre-wrap';
  box.textContent = 'Debug UI failed to load:\\n' + msg;
  view.appendChild(box);
  if (stats) {
    stats.innerHTML = '<div class="bad">Debug UI failed: ' + msg + '</div>';
  }
});
</script>
<script type="module">
import * as THREE from 'three';
import { OrbitControls } from 'three/addons/controls/OrbitControls.js';

const view = document.getElementById('view');
const stats = document.getElementById('stats');
const vizLoading = document.getElementById('viz-loading');
const scene = new THREE.Scene();
scene.background = new THREE.Color(0x0f1117);

const camera = new THREE.PerspectiveCamera(55, 1, 0.05, 50);
camera.position.set(0.9, 0.55, 1.35);

const renderer = new THREE.WebGLRenderer({ antialias: true });
renderer.setPixelRatio(Math.min(window.devicePixelRatio, 2));
view.appendChild(renderer.domElement);
if (vizLoading) vizLoading.style.display = 'none';

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

const bodyDirGeom = new THREE.BufferGeometry().setFromPoints([
  new THREE.Vector3(0, 0.05, 0.08),
  new THREE.Vector3(0, 0.05, 0.95),
]);
const bodyDirLine = new THREE.Line(
  bodyDirGeom,
  new THREE.LineBasicMaterial({ color: 0xfb923c, linewidth: 2 })
);
base.add(bodyDirLine);
const bodyDirTip = new THREE.Mesh(
  new THREE.ConeGeometry(0.045, 0.12, 10),
  new THREE.MeshBasicMaterial({ color: 0xfb923c })
);
bodyDirTip.rotation.x = -Math.PI / 2;
bodyDirTip.position.set(0, 0.05, 0.95);
base.add(bodyDirTip);

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

const trueNorthGroup = new THREE.Group();
scene.add(trueNorthGroup);
const trueNorthGeom = new THREE.BufferGeometry().setFromPoints([
  new THREE.Vector3(0, 0.022, 0),
  new THREE.Vector3(0, 0.022, 0.95),
]);
const trueNorthLine = new THREE.Line(
  trueNorthGeom,
  new THREE.LineBasicMaterial({ color: 0xe879f9, linewidth: 2 })
);
trueNorthGroup.add(trueNorthLine);
const trueNorthTip = new THREE.Mesh(
  new THREE.ConeGeometry(0.045, 0.11, 10),
  new THREE.MeshBasicMaterial({ color: 0xe879f9 })
);
trueNorthTip.rotation.x = -Math.PI / 2;
trueNorthTip.position.set(0, 0.022, 0.95);
trueNorthGroup.add(trueNorthTip);

const worldAimGroup = new THREE.Group();
scene.add(worldAimGroup);
const worldAimGeom = new THREE.BufferGeometry().setFromPoints([
  new THREE.Vector3(0, 0.022, 0),
  new THREE.Vector3(0, 0.022, 1.05),
]);
const worldAimLine = new THREE.Line(
  worldAimGeom,
  new THREE.LineBasicMaterial({ color: 0xfbbf24, linewidth: 2 })
);
worldAimGroup.add(worldAimLine);
const worldAimTip = new THREE.Mesh(
  new THREE.ConeGeometry(0.04, 0.10, 10),
  new THREE.MeshBasicMaterial({ color: 0xfbbf24 })
);
worldAimTip.rotation.x = -Math.PI / 2;
worldAimTip.position.set(0, 0.022, 1.05);
worldAimGroup.add(worldAimTip);

const limitPan = new THREE.Group();
base.add(limitPan);
const limitTilt = new THREE.Group();
panNode.add(limitTilt);
const memoryGroup = new THREE.Group();
scene.add(memoryGroup);

let latest = {};
let lastLimitYaw = null;

function deg(v) { return THREE.MathUtils.degToRad(v); }

function updateBaseLimitArc(maxYawDeg) {
  const yaw = maxYawDeg || 120;
  if (lastLimitYaw === yaw) return;
  lastLimitYaw = yaw;
  limitPan.clear();
  const radius = 0.50;
  const steps = 56;
  const maxRad = deg(yaw);
  const pts = [];
  for (let i = 0; i <= steps; i++) {
    const t = -maxRad + (2 * maxRad * i) / steps;
    pts.push(new THREE.Vector3(Math.sin(t) * radius, 0.012, Math.cos(t) * radius));
  }
  limitPan.add(new THREE.Line(
    new THREE.BufferGeometry().setFromPoints(pts),
    new THREE.LineBasicMaterial({ color: 0x94a3b8, transparent: true, opacity: 0.7 })
  ));
  for (const sign of [-1, 1]) {
    const t = sign * maxRad;
    const tick = new THREE.BufferGeometry().setFromPoints([
      new THREE.Vector3(Math.sin(t) * (radius - 0.04), 0.012, Math.cos(t) * (radius - 0.04)),
      new THREE.Vector3(Math.sin(t) * (radius + 0.04), 0.012, Math.cos(t) * (radius + 0.04)),
    ]);
    limitPan.add(new THREE.Line(
      tick,
      new THREE.LineBasicMaterial({ color: 0xef4444, transparent: true, opacity: 0.85 })
    ));
  }
}
updateBaseLimitArc(120);

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

function updateGroundHud(s) {
  const el = document.getElementById('ground-hud-body');
  if (!el) return;
  const bodyG = s.body_yaw_deg ?? s.base_ground_yaw_deg ?? s.base_fwd_deg ?? s.base_yaw_deg ?? 0;
  const headOnBodyG = s.head_yaw_on_body_deg ?? s.neck_pan_rel_base_deg ?? 0;
  const servoPanG = s.pan_rel_base_deg ?? s.pan_mech_deg ?? 0;
  const trueNorthG = s.true_north_deg ?? 0;
  const worldHeadG = s.world_head_yaw_deg ?? s.head_yaw_ground_deg ?? s.world_fwd_deg ?? 0;
  const headTiltG = s.tilt_ground_deg ?? s.tilt_rel_fwd_deg ?? s.tilt_mech_deg ?? 0;
  const vizYaw = s.viz_world_applied_deg ?? 0;
  const vizTilt = s.viz_tilt_applied_deg ?? 0;
  el.innerHTML = `
    <div class="grow"><span class="gk">true north (+Z)</span><span>${fmtDir(trueNorthG)} <span class="gk">fixed</span></span></div>
    <div class="grow"><span class="gk">body (encoder)</span><span>${fmtDir(bodyG)}</span></div>
    <div class="grow"><span class="gk">head on body (IMU)</span><span>${fmtDir(headOnBodyG)}</span></div>
    <div class="grow"><span class="gk">servo pan</span><span>${fmtDir(servoPanG)} <span class="gk">cross-check</span></span></div>
    <div class="grow"><span class="gk">world aim</span><span>${fmtDir(worldHeadG)} <span class="gk">body+head</span></span></div>
    <div class="grow"><span class="gk">head tilt (ground)</span><span>${fmtTilt(headTiltG)}</span></div>
    <div class="grow"><span class="gk">3D viz yaw</span><span>${fmtDir(vizYaw)}</span></div>
    <div class="grow"><span class="gk">3D viz tilt</span><span>${fmtTilt(vizTilt)}</span></div>
    <div class="grow"><span class="gk">servo cmd</span><span>P ${fmt(s.pan)} T ${fmt(s.tilt)}</span></div>
  `;
}

function setFusionStats(s) {
  const fusionEl = document.getElementById('fusion-stats');
  const controlsEl = document.getElementById('controls');
  if (!fusionEl) return;
  const showControls = !!s.manual_control_enabled || s.mode === 'manual_test';
  if (controlsEl) controlsEl.style.display = showControls ? 'block' : 'none';

  const fusionDelta = (s.head_imu_vs_servo_delta_deg !== undefined)
    ? s.head_imu_vs_servo_delta_deg
    : ((s.fusion_delta_deg !== undefined)
      ? s.fusion_delta_deg
      : ((s.head_yaw_on_body_deg || 0) - (s.pan_mech_deg || 0)));
  const drift = s.imu_drift_correction_deg || 0;
  const stationary = s.fusion_stationary ? 'yes' : 'no';
  const cls = (ok) => ok ? 'ok' : 'warn';
  const bodyYaw = s.body_yaw_deg ?? s.base_fwd_deg ?? s.base_yaw_deg ?? 0;
  const headOnBody = s.head_yaw_on_body_deg ?? 0;
  const worldHead = s.world_head_yaw_deg ?? s.world_fwd_deg ?? s.base_world_yaw_deg ?? 0;

  fusionEl.innerHTML = `
    <div class="row" style="margin-top:4px;color:#88c0d0"><span>vs forward (0° = startup facing +Z)</span></div>
    <div class="row"><span class="k">true north</span><span>${fmtDir(s.true_north_deg ?? 0)} <span class="k">fixed</span></span></div>
    <div class="row"><span class="k">body (encoder)</span><span>${fmtDir(bodyYaw)}</span></div>
    <div class="row"><span class="k">head on body (IMU)</span><span>${fmtDir(headOnBody)} <span class="k">imu−body</span></span></div>
    <div class="row"><span class="k">servo pan</span><span>${fmtDir(s.pan_rel_base_deg ?? s.pan_mech_deg)} <span class="k">cross-check</span></span></div>
    <div class="row"><span class="k">world aim</span><span>${fmtDir(worldHead)} <span class="k">body+head</span></span></div>
    <div class="row"><span class="k">head tilt</span><span>${fmtTilt(s.tilt_rel_fwd_deg ?? s.tilt_mech_deg)} <span class="k">mech on neck</span></span></div>
    <div class="row"><span class="k">3D viz yaw</span><span>${fmtDir(s.viz_world_applied_deg)} <span class="k">(×${fmt(s.viz_base_yaw_sign,0)} body+head)</span></span></div>
    <div class="row"><span class="k">3D viz tilt</span><span>${fmtTilt(vizTiltApplied(s))} <span class="k">(tilt×${fmt(s.viz_tilt_sign,0)})</span></span></div>
    <div class="row"><span class="k">IMU pitch</span><span>${fmtTilt(s.viz_imu_pitch_applied_deg ?? s.imu_pitch_deg)} raw ${fmt(s.imu_pitch_deg)}° <span class="k">(×${fmt(s.viz_imu_pitch_sign,0)})</span></span></div>
    <div class="row"><span class="k">stationary</span><span class="${cls(s.fusion_stationary)}">${stationary}</span></div>
    <div class="row"><span class="k">enc raw</span><span>${fmt(s.base_yaw_deg)}°</span></div>
    <div class="row"><span class="k">imu rel</span><span>${fmtDir(s.imu_yaw_rel_deg ?? s.imu_yaw_total_deg)}</span></div>
    <div class="row"><span class="k">head vs servo</span><span class="${Math.abs(fusionDelta) > 8 ? 'warn' : 'ok'}">${fmt(fusionDelta)}° (imu−servo)</span></div>
    <div class="row"><span class="k">imu total</span><span>${fmtDir(s.imu_yaw_total_deg)}</span></div>
    <div class="row"><span class="k">imu raw</span><span>${fmt(s.imu_yaw_raw_deg)}°</span></div>
    <div class="row"><span class="k">drift fix</span><span class="${Math.abs(drift) > 0.05 ? 'ok' : ''}">${fmt(drift)}°</span></div>
    <div class="row"><span class="k">gyro</span><span>${fmt(s.imu_gyro_dps)} dps</span></div>
    <div class="row"><span class="k">servo cmd</span><span>P ${fmt(s.pan)} T ${fmt(s.tilt)}</span></div>
  `;
}

function setModeBanner(s) {
  const el = document.getElementById('mode-banner');
  if (!el) return;
  const label = s.mode_label || s.mode || 'Unknown';
  const mode = s.mode || '';
  let cls = 'mode-wander';
  if (s.forward_return_active) cls = 'mode-return';
  else if (mode === 'track') cls = 'mode-track';
  else if (mode === 'last_seen') cls = 'mode-lastseen';
  else if (mode === 'manual' || mode === 'manual_test') cls = 'mode-manual';
  const temp = (s.cpu_temp_c !== undefined && s.cpu_temp_c !== null)
    ? `${Number(s.cpu_temp_c).toFixed(1)}°C` : '-';
  const t = Number(s.cpu_temp_c);
  const tempCls = (t >= 80) ? 'bad' : ((t >= 70) ? 'warn' : 'ok');
  el.innerHTML = `
    <div class="mode-line ${cls}">${label}</div>
    <div class="row"><span class="k">servo mode</span><span>${mode || '-'} / ${s.track_kind || '-'}</span></div>
    <div class="row"><span class="k">CPU temp</span><span class="${tempCls}">${temp}</span></div>
  `;
}

function setStats(s) {
  const cls = (ok) => ok ? 'ok' : 'bad';
  const hz = s.ts ? (1 / Math.max(0.001, performance.now() / 1000 - (window._lastTs || s.ts))).toFixed(1) : '-';
  window._lastTs = performance.now() / 1000;
  const mems = s.person_memories || [];
  const memText = mems.length
    ? mems.map(m => `P${m.id}:${m.kind} ${fmt(m.age_sec,0)}s`).join(' ')
    : 'none';
  stats.innerHTML = `
    <div class="row"><span class="k">behavior</span><span class="${s.mode === 'track' ? 'ok' : ''}">${s.mode_label || s.mode || '-'}</span></div>
    <div class="row"><span class="k">mode</span><span>${s.mode || '-'} / ${s.track_kind || '-'}</span></div>
    <div class="row"><span class="k">pan</span><span>${fmt(s.pan)} cmd (${fmt(s.pan_mech_deg)}° mech) → ${fmt(s.pan_target)}</span></div>
    <div class="row"><span class="k">tilt</span><span>${fmt(s.tilt)} cmd (${fmt(s.tilt_mech_deg)}° mech) → ${fmt(s.tilt_target)}</span></div>
    <div class="row"><span class="k">tilt ctr</span><span>${fmt(s.tilt_effective_center)} cmd (0° mech @ ${fmt(s.tilt_center)})</span></div>
    <div class="row"><span class="k">mech cal</span><span>up ${fmt(s.tilt_mech_up_deg,0)}° / down ${fmt(s.tilt_mech_down_deg,0)}°</span></div>
    <div class="row"><span class="k">limits</span><span>P ${fmt(s.pan_min)}..${fmt(s.pan_max)} T ${fmt(s.tilt_min)}..${fmt(s.tilt_max)}</span></div>
    <div class="row"><span class="k">face</span><span>${s.face_seen ? 'yes' : 'no'} x=${fmt(s.face_norm_x,2)} y=${fmt(s.face_norm_y,2)} n=${s.face_count||0}${s.pan_hold ? ' <span class="warn">pan hold</span>' : ''}</span></div>
    <div class="row"><span class="k">memory</span><span>${mems.length} active ${s.active_memory_id ? '#'+s.active_memory_id : '-'}</span></div>
    <div class="row"><span class="k">mem list</span><span>${memText}</span></div>
    <div class="row"><span class="k">base</span><span>body ${fmt(s.body_yaw_deg ?? s.base_yaw_deg)} enc ${fmt(s.base_yaw_deg)} world ${fmt(s.world_head_yaw_deg ?? s.base_world_yaw_deg)} busy ${s.base_busy ? 'yes' : 'no'}</span></div>
    <div class="row"><span class="k">yaw split</span><span>body ${fmt(s.body_yaw_deg ?? s.base_yaw_deg)} head ${fmt(s.head_yaw_on_body_deg ?? s.imu_pan_delta_deg)} imu ${fmt(s.imu_yaw_rel_deg ?? s.imu_yaw_total_deg)}</span></div>
    <div class="row"><span class="k">IMU pitch</span><span class="${Math.abs(s.imu_pitch_deg||0) > 8 ? 'warn' : 'ok'}">${fmt(s.imu_pitch_deg)}° (vs mech ${fmt(s.tilt_mech_deg)}°)</span></div>
    <div class="row"><span class="k">IMU roll</span><span>${fmt(s.imu_roll_deg)}</span></div>
    <div class="row"><span class="k">IMU gyro</span><span class="${(s.imu_gyro_dps||0) > 35 ? 'warn' : ''}">${fmt(s.imu_gyro_dps)} dps</span></div>
    <div class="row"><span class="k">horizon</span><span class="${cls(s.imu_horizon_ok)}">${s.imu_horizon_ok ? 'updating' : 'held'}</span></div>
    <div class="row"><span class="k">servo link</span><span class="${cls(s.servo_connected)}">${s.servo_connected ? 'connected' : 'off'}</span></div>
    <div class="row"><span class="k">CPU temp</span><span class="${(Number(s.cpu_temp_c) >= 80) ? 'bad' : ((Number(s.cpu_temp_c) >= 70) ? 'warn' : '')}">${(s.cpu_temp_c !== undefined && s.cpu_temp_c !== null) ? Number(s.cpu_temp_c).toFixed(1) + '°C' : '-'}</span></div>
  `;
}

function makeTextSprite(text, color='#f8fafc') {
  const canvas = document.createElement('canvas');
  canvas.width = 128;
  canvas.height = 48;
  const ctx = canvas.getContext('2d');
  ctx.fillStyle = 'rgba(15,17,23,0.72)';
  ctx.fillRect(0, 0, canvas.width, canvas.height);
  ctx.font = '24px ui-monospace, monospace';
  ctx.fillStyle = color;
  ctx.fillText(text, 8, 31);
  const texture = new THREE.CanvasTexture(canvas);
  const material = new THREE.SpriteMaterial({ map: texture, transparent: true });
  const sprite = new THREE.Sprite(material);
  sprite.scale.set(0.28, 0.105, 1);
  return sprite;
}

function updateMemoryMarkers(s) {
  memoryGroup.clear();
  const mems = s.person_memories || [];
  const radius = 1.2;
  const floorY = 0.025;
  for (const m of mems) {
    const yaw = deg(m.world_yaw_deg || 0);
    const freshness = Math.max(0.12, Math.min(1, m.freshness ?? 1));
    const color = m.kind === 'body' ? 0x38bdf8 : 0xfacc15;
    const marker = new THREE.Group();
    marker.position.set(Math.sin(yaw) * radius, floorY, Math.cos(yaw) * radius);
    const dot = new THREE.Mesh(
      new THREE.CylinderGeometry(0.055, 0.055, 0.018, 24),
      new THREE.MeshStandardMaterial({ color, transparent: true, opacity: freshness })
    );
    dot.rotation.x = Math.PI / 2;
    marker.add(dot);
    const lineGeom = new THREE.BufferGeometry().setFromPoints([
      new THREE.Vector3(0, floorY, 0),
      new THREE.Vector3(marker.position.x, marker.position.y, marker.position.z),
    ]);
    const line = new THREE.Line(
      lineGeom,
      new THREE.LineBasicMaterial({ color, transparent: true, opacity: freshness * 0.45 })
    );
    memoryGroup.add(line);
    const label = makeTextSprite(`P${m.id} ${Math.round(m.age_sec || 0)}s`, m.kind === 'body' ? '#38bdf8' : '#facc15');
    label.position.y = 0.10;
    marker.add(label);
    if (m.id === s.active_memory_id) {
      const ring = new THREE.Mesh(
        new THREE.TorusGeometry(0.07, 0.006, 8, 24),
        new THREE.MeshBasicMaterial({ color: 0xffffff, transparent: true, opacity: freshness })
      );
      ring.rotation.x = Math.PI / 2;
      marker.add(ring);
    }
    memoryGroup.add(marker);
  }
}

function fmt(v, d=1) {
  if (v === undefined || v === null || Number.isNaN(v)) return '-';
  return Number(v).toFixed(d);
}

function fmtDir(v, d=1) {
  if (v === undefined || v === null || Number.isNaN(v)) return '-';
  const n = Number(v);
  if (Math.abs(n) < 0.05) return `${n.toFixed(d)}° fwd`;
  return `${n.toFixed(d)}° ${n > 0 ? 'R' : 'L'}`;
}

function fmtTilt(v, d=1) {
  if (v === undefined || v === null || Number.isNaN(v)) return '-';
  const n = Number(v);
  if (Math.abs(n) < 0.05) return `${n.toFixed(d)}° level`;
  return `${n.toFixed(d)}° ${n > 0 ? 'up' : 'down'}`;
}

function panMechDeg(s) {
  if (s.pan_mech_deg !== undefined && s.pan_mech_deg !== null) return Number(s.pan_mech_deg);
  return Number(s.pan) - Number(s.pan_center);
}

function tiltMechDeg(s) {
  if (s.tilt_mech_deg !== undefined && s.tilt_mech_deg !== null) return Number(s.tilt_mech_deg);
  return Number(s.tilt) - Number(s.tilt_center);
}

function vizTiltApplied(s) {
  if (s.viz_tilt_applied_deg !== undefined && s.viz_tilt_applied_deg !== null) {
    return Number(s.viz_tilt_applied_deg);
  }
  const tilt = (s.tilt_rel_fwd_deg !== undefined && s.tilt_rel_fwd_deg !== null)
    ? Number(s.tilt_rel_fwd_deg)
    : Number(s.tilt_mech_deg || 0);
  const sign = (s.viz_tilt_sign !== undefined && s.viz_tilt_sign !== null) ? Number(s.viz_tilt_sign) : 1;
  return tilt * sign;
}

async function poll() {
  try {
    const res = await fetch('/api/state', { cache: 'no-store' });
    latest = await res.json();
    setModeBanner(latest);
    setStats(latest);
    setFusionStats(latest);
    updateGroundHud(latest);
    const panMech = panMechDeg(latest);
    const tiltMech = tiltMechDeg(latest);
    const baseSign = Number(latest.viz_base_yaw_sign ?? 1);
    const tiltSign = Number(latest.viz_tilt_sign ?? 1);
    const imuPitchSign = Number(latest.viz_imu_pitch_sign ?? -1);
    const bodyYaw = Number(latest.body_yaw_deg ?? latest.base_yaw_deg ?? 0);
    const headOnBody = Number(latest.head_yaw_on_body_deg ?? 0);
    const baseRad = deg(bodyYaw) * baseSign;
    // Head-on-body is in the body frame — use baseSign (not panSign) so neck
    // rotation matches the chassis convention and rides the base correctly.
    const headRad = deg(headOnBody) * baseSign;
    const tiltRad = deg(tiltMech) * tiltSign;
    root.rotation.y = baseRad;
    panNode.rotation.y = headRad;
    tiltNode.rotation.x = tiltRad;
    worldAimGroup.rotation.y = deg(bodyYaw + headOnBody) * baseSign;
    updateBaseLimitArc(latest.base_max_yaw_deg);
    imuGroup.rotation.z = deg(latest.imu_roll_deg || 0);
    imuGroup.rotation.x = deg(latest.imu_pitch_deg || 0) * imuPitchSign;
    updateMemoryMarkers(latest);
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
const ro = typeof ResizeObserver !== 'undefined' ? new ResizeObserver(() => resize()) : null;
if (ro) ro.observe(view);

let controlSeq = 0;
async function sendControl(cmd) {
  controlSeq += 1;
  try {
    await fetch('/api/control', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ cmd, seq: controlSeq, step: latest.head_step_deg || 5 }),
    });
  } catch (e) {
    console.warn('control failed', e);
  }
}

document.querySelectorAll('#controls [data-cmd]').forEach((btn) => {
  btn.addEventListener('click', () => sendControl(btn.dataset.cmd));
});

const keyToCmd = {
  w: 'tilt_up', s: 'tilt_down', a: 'pan_right', d: 'pan_left',
  c: 'center', z: 'zero_base', r: 'fusion_reset', q: 'quit',
};
window.addEventListener('keydown', (ev) => {
  const controlsEl = document.getElementById('controls');
  if (!controlsEl || controlsEl.style.display === 'none') return;
  if (ev.target && (ev.target.tagName === 'INPUT' || ev.target.tagName === 'TEXTAREA')) return;
  const cmd = keyToCmd[ev.key.toLowerCase()];
  if (!cmd) return;
  ev.preventDefault();
  sendControl(cmd);
});

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
        if serve_debug_static(self, self.path):
            return
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
