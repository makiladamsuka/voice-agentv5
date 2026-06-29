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
import math
import os
import re
import sys
import threading
import time
from collections import deque
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parent))
from tof_filter import MAX_TRUST_MM, TofFilterBank

try:
    import serial
except ImportError:
    print("Install pyserial: pip install pyserial")
    sys.exit(1)

DEFAULT_PORTS = ("/dev/ttyUSB0", "/dev/ttyUSB1", "/dev/ttyACM0")
BAUD = 115200
MAX_MM = MAX_TRUST_MM
HISTORY_LEN = 150
FILTER_BANK = TofFilterBank(3)

_TOF_RE = re.compile(
    r"TOF\s+L=(-?\d+)\s+C=(-?\d+)\s+R=(-?\d+)"
    r"\s+VL=(-?\d+)\s+VC=(-?\d+)\s+VR=(-?\d+)"
)

LABELS = ("LEFT", "CENTER", "RIGHT")
ZONE_KEYS = ("L", "C", "R")
COLORS = ("#3b82f6", "#a855f7", "#22c55e")
# Front-mounted ToF: ±45° left/right, center forward (robot +Z)
SENSOR_ANGLES_DEG = (-45, 0, 45)

APP_DIR = Path(__file__).resolve().parent.parent
STATIC_DIR = APP_DIR / "static"
_STATIC_MIME = {
    ".js": "application/javascript; charset=utf-8",
    ".mjs": "application/javascript; charset=utf-8",
    ".css": "text/css; charset=utf-8",
    ".json": "application/json; charset=utf-8",
    ".wasm": "application/wasm",
}


def _motion_class(vel: int | None) -> str:
    if vel is None:
        return "still"
    if vel < -45:
        return "approach"
    if vel > 45:
        return "depart"
    if vel < -15:
        return "drift_in"
    if vel > 15:
        return "drift_out"
    return "still"


def _classify_hit(hit: dict[str, Any], spread_mm: float, sample_age: int) -> dict[str, Any]:
    """Heuristic: ToF cannot see humans — infer from motion + stability."""
    motion = hit["motion"]
    vel = abs(hit.get("vel_mm_s") or 0)
    if motion in ("approach", "depart") or vel > 55:
        kind, conf, reason = "human", 0.88, "closing or leaving quickly"
    elif motion in ("drift_in", "drift_out") or spread_mm > 90:
        kind, conf, reason = "human", 0.72, "position shifting"
    elif motion == "still" and spread_mm < 80 and sample_age >= 8:
        kind, conf, reason = "obstacle", min(0.92, 0.55 + sample_age * 0.02), "stable fixed return"
    elif sample_age < 5:
        kind, conf, reason = "uncertain", 0.35, "collecting samples"
    else:
        kind, conf, reason = "obstacle", 0.58, "low motion"
    return {**hit, "kind": kind, "confidence": round(conf, 2), "reason": reason}


class ObjectTracker:
    """Track fused position stability to separate humans from static obstacles."""

    def __init__(self, window: int = 24) -> None:
        self._positions: deque[tuple[int, int, float]] = deque(maxlen=window)

    def reset(self) -> None:
        self._positions.clear()

    def update(
        self,
        fused: dict[str, Any] | None,
        hits: list[dict[str, Any]],
        *,
        now: float,
    ) -> tuple[list[dict[str, Any]], dict[str, Any] | None]:
        if not fused:
            self.reset()
            return hits, None

        self._positions.append((fused["x_mm"], fused["z_mm"], now))
        spread = 0
        if len(self._positions) >= 3:
            xs = [p[0] for p in self._positions]
            zs = [p[1] for p in self._positions]
            spread = max(max(xs) - min(xs), max(zs) - min(zs))

        age = len(self._positions)
        motion = fused["motion"]
        vels = [abs(h.get("vel_mm_s") or 0) for h in hits]
        max_vel = max(vels) if vels else 0

        if motion in ("approach", "depart") or max_vel > 55:
            kind, conf, reason = "human", 0.9, "approach / retreat"
        elif motion in ("drift_in", "drift_out") or spread > 100:
            kind, conf, reason = "human", 0.75, "moving in place"
        elif spread < 85 and motion == "still" and age >= 10:
            kind, conf, reason = "obstacle", min(0.94, 0.5 + age * 0.02), "stationary object"
        elif age < 6:
            kind, conf, reason = "uncertain", 0.4, "observing…"
        else:
            kind, conf, reason = "obstacle", 0.62, "low motion"

        classified_hits = [_classify_hit(h, spread, age) for h in hits]
        classified_fused = {
            **fused,
            "kind": kind,
            "confidence": round(conf, 2),
            "spread_mm": round(spread),
            "reason": reason,
        }
        return classified_hits, classified_fused


def _compute_hits(
    mm: list[int],
    vel: list[int | None],
    open_flags: list[bool],
) -> tuple[list[dict[str, Any]], dict[str, Any] | None]:
    hits: list[dict[str, Any]] = []
    for i, zone in enumerate(ZONE_KEYS):
        if mm[i] < 0 or open_flags[i]:
            continue
        angle = SENSOR_ANGLES_DEG[i]
        rad = math.radians(angle)
        hits.append(
            {
                "zone": zone,
                "label": LABELS[i],
                "angle_deg": angle,
                "dist_mm": mm[i],
                "x_mm": round(math.sin(rad) * mm[i]),
                "z_mm": round(math.cos(rad) * mm[i]),
                "vel_mm_s": vel[i],
                "motion": _motion_class(vel[i]),
            }
        )

    fused: dict[str, Any] | None = None
    if hits:
        xs = [h["x_mm"] for h in hits]
        zs = [h["z_mm"] for h in hits]
        motions = [h["motion"] for h in hits]
        if any(m in ("approach", "drift_in") for m in motions):
            motion = "approach"
        elif any(m in ("depart", "drift_out") for m in motions):
            motion = "depart"
        else:
            motion = "still"
        fused = {
            "x_mm": round(sum(xs) / len(xs)),
            "z_mm": round(sum(zs) / len(zs)),
            "zones": [h["zone"] for h in hits],
            "motion": motion,
        }
    return hits, fused


def serve_static(handler: BaseHTTPRequestHandler, path: str) -> bool:
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


class TofState:
    def __init__(self) -> None:
        self._lock = threading.Lock()
        self.connected = False
        self.port = ""
        self.error = ""
        self.sample_count = 0
        self.dropouts = [0, 0, 0]
        self.mm = [-1, -1, -1]
        self.vel: list[int | None] = [None, None, None]
        self.open = [True, True, True]
        self.history: list[deque[int]] = [
            deque(maxlen=HISTORY_LEN) for _ in range(3)
        ]
        self.boot: deque[str] = deque(maxlen=40)
        self.last_ts = 0.0
        self._tracker = ObjectTracker()

    def update_sample(
        self,
        mm: list[int],
        vel: list[int | None],
        *,
        open_flags: list[bool] | None = None,
    ) -> None:
        with self._lock:
            self.sample_count += 1
            self.mm = mm
            self.vel = vel
            if open_flags is not None:
                self.open = open_flags
            self.last_ts = time.time()
            for i in range(3):
                if mm[i] < 0:
                    self.dropouts[i] += 1
                else:
                    self.history[i].append(mm[i])

    def add_boot(self, line: str) -> None:
        with self._lock:
            self.boot.append(line)

    def set_connected(self, port: str) -> None:
        with self._lock:
            self.connected = True
            self.port = port
            self.error = ""

    def set_error(self, msg: str) -> None:
        with self._lock:
            self.connected = False
            self.error = msg

    def snapshot(self) -> dict[str, Any]:
        with self._lock:
            ok = sum(1 for d in self.mm if d >= 0)
            mm = list(self.mm)
            vel = [v if v is None else int(v) for v in self.vel]
            open_flags = list(self.open)
            hits, fused = _compute_hits(mm, vel, open_flags)
            hits, fused = self._tracker.update(fused, hits, now=self.last_ts)
            return {
                "connected": self.connected,
                "port": self.port,
                "error": self.error,
                "sample_count": self.sample_count,
                "ok_count": ok,
                "dropouts": list(self.dropouts),
                "mm": mm,
                "vel": vel,
                "open": open_flags,
                "history": [list(h) for h in self.history],
                "boot": list(self.boot),
                "last_ts": self.last_ts,
                "max_mm": MAX_MM,
                "labels": list(LABELS),
                "colors": list(COLORS),
                "sensor_angles_deg": list(SENSOR_ANGLES_DEG),
                "hits": hits,
                "fused": fused,
            }


STATE = TofState()


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


def serial_reader(port_hint: str) -> None:
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
                    STATE.add_boot(line)
                if "Streaming readings" in line:
                    break

            while True:
                line = ser.readline().decode("utf-8", errors="ignore").strip()
                if not line:
                    continue
                m = _TOF_RE.search(line)
                if m:
                    raw = [int(m.group(i)) for i in range(1, 4)]
                    mm, vel, open_flags = FILTER_BANK.update_all(raw)
                    STATE.update_sample(mm, vel, open_flags=open_flags)
                elif line and not line.startswith("TOF"):
                    STATE.add_boot(line)
        except Exception as exc:
            STATE.set_error(str(exc))
            time.sleep(2.0)
            port_hint = ""  # rescan all ports after disconnect


HTML_PAGE = """<!DOCTYPE html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>ToF Live — VL53L0X</title>
  <style>
    :root {
      --bg: #0f1419;
      --card: #1a2332;
      --border: #2d3a4d;
      --text: #e8edf4;
      --muted: #8b9cb3;
      --na: #4a5568;
      --accent: #38bdf8;
    }
    * { box-sizing: border-box; margin: 0; padding: 0; }
    body {
      font-family: "Segoe UI", system-ui, sans-serif;
      background: var(--bg);
      color: var(--text);
      min-height: 100vh;
      padding: 1.25rem;
    }
    header {
      display: flex;
      flex-wrap: wrap;
      align-items: baseline;
      gap: 1rem;
      margin-bottom: 1.25rem;
      border-bottom: 1px solid var(--border);
      padding-bottom: 1rem;
    }
    h1 { font-size: 1.35rem; font-weight: 600; }
    .meta { color: var(--muted); font-size: 0.9rem; }
    .badge {
      display: inline-block;
      padding: 0.2rem 0.55rem;
      border-radius: 999px;
      font-size: 0.75rem;
      font-weight: 600;
    }
    .badge.ok { background: #14532d; color: #86efac; }
    .badge.warn { background: #713f12; color: #fde68a; }
    .badge.err { background: #7f1d1d; color: #fca5a5; }
    .grid {
      display: grid;
      grid-template-columns: repeat(auto-fit, minmax(280px, 1fr));
      gap: 1rem;
      margin-bottom: 1rem;
    }
    .card {
      background: var(--card);
      border: 1px solid var(--border);
      border-radius: 12px;
      padding: 1rem 1.1rem;
    }
    .card h2 {
      font-size: 0.8rem;
      text-transform: uppercase;
      letter-spacing: 0.08em;
      color: var(--muted);
      margin-bottom: 0.5rem;
    }
    .card .label {
      font-size: 1.1rem;
      font-weight: 700;
      margin-bottom: 0.35rem;
    }
    .distance {
      font-size: 2.4rem;
      font-weight: 700;
      font-variant-numeric: tabular-nums;
      line-height: 1.1;
    }
    .distance.na { color: var(--na); font-size: 1.8rem; }
    .unit { font-size: 1rem; color: var(--muted); font-weight: 500; }
    .bar-wrap {
      height: 28px;
      background: #0d1117;
      border-radius: 6px;
      margin: 0.75rem 0 0.5rem;
      overflow: hidden;
      border: 1px solid var(--border);
    }
    .bar-fill {
      height: 100%;
      border-radius: 5px;
      transition: width 0.15s ease, background 0.15s;
      min-width: 2px;
    }
    .vel {
      font-size: 0.9rem;
      color: var(--muted);
    }
    .vel strong { color: var(--text); }
    canvas.spark {
      width: 100%;
      height: 56px;
      display: block;
      margin-top: 0.6rem;
      border-radius: 6px;
      background: #0d1117;
    }
    .summary {
      display: flex;
      flex-wrap: wrap;
      gap: 1.5rem;
      font-size: 0.9rem;
    }
    .summary dt { color: var(--muted); }
    .summary dd { font-weight: 600; font-size: 1.1rem; }
    .boot {
      font-family: ui-monospace, monospace;
      font-size: 0.72rem;
      color: var(--muted);
      max-height: 140px;
      overflow-y: auto;
      line-height: 1.45;
      white-space: pre-wrap;
    }
    .note {
      margin-top: 1rem;
      font-size: 0.8rem;
      color: var(--muted);
      line-height: 1.5;
    }
    .view3d-card { margin-bottom: 1rem; }
    #view3d {
      position: relative;
      width: 100%;
      height: min(58vh, 520px);
      min-height: 320px;
      border-radius: 10px;
      overflow: hidden;
      border: 1px solid #2a2a2a;
      background: #111111;
    }
    #view3d canvas { display: block; width: 100% !important; height: 100% !important; }
    #viz-loading {
      position: absolute; inset: 0; display: flex; align-items: center; justify-content: center;
      color: var(--muted); font-size: 0.9rem;
    }
    .viz-legend {
      display: flex; flex-wrap: wrap; gap: 1rem; margin-top: 0.75rem; font-size: 0.78rem; color: var(--muted);
    }
    .viz-legend span { display: inline-flex; align-items: center; gap: 0.35rem; }
    .viz-legend i {
      display: inline-block; width: 10px; height: 10px; border-radius: 50%;
    }
    .object-readout {
      margin-top: 0.65rem; font-size: 0.85rem; color: var(--text);
      font-family: ui-monospace, monospace;
    }
  </style>
</head>
<body>
  <header>
    <h1>ToF Sensor Live</h1>
    <span id="status" class="badge warn">connecting…</span>
    <span class="meta" id="port"></span>
    <span class="meta" id="samples"></span>
  </header>

  <div class="card view3d-card">
    <h2>Proximity map</h2>
    <div id="view3d"><div id="viz-loading">Loading proximity map…</div></div>
    <div class="viz-legend">
      <span><i style="background:#5b9bd5"></i>person (moving)</span>
      <span><i style="background:#9ca3af"></i>obstacle (still)</span>
      <span><i style="background:#6b7280"></i>uncertain</span>
      <span><i style="background:#e5e7eb;border:1px solid #555"></i>robot</span>
      <span>top-down · drag to pan · scroll to zoom</span>
    </div>
    <div class="object-readout" id="object-readout">Scanning…</div>
  </div>

  <div class="grid" id="sensors"></div>

  <div class="card" style="margin-bottom:1rem">
    <h2>Session</h2>
    <dl class="summary" id="summary"></dl>
  </div>

  <div class="card">
    <h2>Boot log</h2>
    <div class="boot" id="boot"></div>
    <p class="note">Tesla-style proximity map from 3× VL53L0X (±45° front corners). <strong>Person</strong> = motion or shifting position; <strong>obstacle</strong> = stable fixed return. ToF-only heuristic — not true human detection.</p>
  </div>

  <script type="importmap">
  {
    "imports": {
      "three": "/static/vendor/three.module.js",
      "three/addons/": "/static/vendor/addons/"
    }
  }
  </script>
  <script type="module">
  import * as THREE from 'three';
  import { OrbitControls } from 'three/addons/controls/OrbitControls.js';

  const view = document.getElementById('view3d');
  const vizLoading = document.getElementById('viz-loading');
  const KIND_COL = { human: 0x5b9bd5, obstacle: 0x9ca3af, uncertain: 0x6b7280 };
  const ZONE_ANGLE = { L: -45, C: 0, R: 45 };
  const deg = (d) => d * Math.PI / 180;
  const TRAIL_LEN = 20;
  const MAP_R = 2.0;

  const scene = new THREE.Scene();
  scene.background = new THREE.Color(0x111111);
  const camera = new THREE.OrthographicCamera(-1, 1, 1, -1, 0.1, 30);
  camera.position.set(0.15, 3.2, 0.35);
  camera.lookAt(0, 0, 0.45);
  const renderer = new THREE.WebGLRenderer({ antialias: true });
  renderer.setPixelRatio(Math.min(window.devicePixelRatio, 2));
  view.appendChild(renderer.domElement);
  if (vizLoading) vizLoading.style.display = 'none';

  const controls = new OrbitControls(camera, renderer.domElement);
  controls.target.set(0, 0, 0.45);
  controls.enableDamping = true;
  controls.maxPolarAngle = Math.PI * 0.22;
  controls.minPolarAngle = Math.PI * 0.08;
  controls.enableRotate = true;

  // Floor + range rings (Tesla-style)
  const floor = new THREE.Mesh(
    new THREE.PlaneGeometry(MAP_R * 2.2, MAP_R * 2.2),
    new THREE.MeshBasicMaterial({ color: 0x141414 })
  );
  floor.rotation.x = -Math.PI / 2;
  scene.add(floor);
  for (const r of [0.5, 1.0, 1.5, 2.0]) {
    const ring = new THREE.Mesh(
      new THREE.RingGeometry(r - 0.004, r, 64),
      new THREE.MeshBasicMaterial({ color: 0x2a2a2a, side: THREE.DoubleSide, transparent: true, opacity: 0.85 })
    );
    ring.rotation.x = -Math.PI / 2;
    ring.position.y = 0.002;
    scene.add(ring);
  }
  // Cross hairs
  const crossMat = new THREE.LineBasicMaterial({ color: 0x252525 });
  for (const sign of [-1, 1]) {
    scene.add(new THREE.Line(
      new THREE.BufferGeometry().setFromPoints([new THREE.Vector3(0, 0.003, 0), new THREE.Vector3(sign * MAP_R, 0.003, 0)]),
      crossMat
    ));
    scene.add(new THREE.Line(
      new THREE.BufferGeometry().setFromPoints([new THREE.Vector3(0, 0.003, 0), new THREE.Vector3(0, 0.003, sign * MAP_R)]),
      crossMat
    ));
  }

  // Robot top-down (rounded body)
  const robot = new THREE.Group();
  scene.add(robot);
  const bodyShape = new THREE.Shape();
  const bw = 0.28, bd = 0.32, cr = 0.08;
  bodyShape.moveTo(-bw + cr, -bd);
  bodyShape.lineTo(bw - cr, -bd);
  bodyShape.quadraticCurveTo(bw, -bd, bw, -bd + cr);
  bodyShape.lineTo(bw, bd - cr);
  bodyShape.quadraticCurveTo(bw, bd, bw - cr, bd);
  bodyShape.lineTo(-bw + cr, bd);
  bodyShape.quadraticCurveTo(-bw, bd, -bw, bd - cr);
  bodyShape.lineTo(-bw, -bd + cr);
  bodyShape.quadraticCurveTo(-bw, -bd, -bw + cr, -bd);
  const body = new THREE.Mesh(
    new THREE.ShapeGeometry(bodyShape),
    new THREE.MeshBasicMaterial({ color: 0xe5e7eb })
  );
  body.rotation.x = -Math.PI / 2;
  body.position.y = 0.01;
  robot.add(body);
  const nose = new THREE.Mesh(
    new THREE.PlaneGeometry(0.12, 0.14),
    new THREE.MeshBasicMaterial({ color: 0xf3f4f6, side: THREE.DoubleSide })
  );
  nose.rotation.x = -Math.PI / 2;
  nose.position.set(0, 0.012, 0.36);
  robot.add(nose);

  // Sensor FOV wedges
  const fovGroup = new THREE.Group();
  robot.add(fovGroup);
  for (const [key, angle] of Object.entries(ZONE_ANGLE)) {
    const g = new THREE.Group();
    g.rotation.y = deg(angle);
    const wedge = new THREE.Mesh(
      new THREE.CircleGeometry(1.8, 32, -deg(12), deg(24)),
      new THREE.MeshBasicMaterial({ color: 0x3b82f6, transparent: true, opacity: 0.06, side: THREE.DoubleSide })
    );
    wedge.rotation.x = -Math.PI / 2;
    wedge.position.set(0, 0.004, 0.2);
    g.add(wedge);
    fovGroup.add(g);
  }

  function makePerson() {
    const g = new THREE.Group();
    const mat = new THREE.MeshBasicMaterial({ color: KIND_COL.human, transparent: true, opacity: 0.92 });
    const shadow = new THREE.Mesh(
      new THREE.CircleGeometry(0.16, 20),
      new THREE.MeshBasicMaterial({ color: 0x5b9bd5, transparent: true, opacity: 0.25 })
    );
    shadow.rotation.x = -Math.PI / 2;
    shadow.position.y = 0.005;
    g.add(shadow);
    const torso = new THREE.Mesh(new THREE.CapsuleGeometry(0.07, 0.14, 4, 10), mat);
    torso.rotation.x = Math.PI / 2;
    torso.position.y = 0.09;
    g.add(torso);
    const head = new THREE.Mesh(new THREE.SphereGeometry(0.055, 12, 12), mat);
    head.position.y = 0.2;
    g.add(head);
    g.userData.label = 'PERSON';
    return g;
  }

  function makeObstacle() {
    const g = new THREE.Group();
    const mat = new THREE.MeshBasicMaterial({ color: KIND_COL.obstacle, transparent: true, opacity: 0.9 });
    const block = new THREE.Mesh(new THREE.BoxGeometry(0.26, 0.08, 0.22), mat);
    block.position.y = 0.04;
    g.add(block);
    const base = new THREE.Mesh(
      new THREE.PlaneGeometry(0.3, 0.26),
      new THREE.MeshBasicMaterial({ color: 0x9ca3af, transparent: true, opacity: 0.2, side: THREE.DoubleSide })
    );
    base.rotation.x = -Math.PI / 2;
    base.position.y = 0.003;
    g.add(base);
    g.userData.label = 'OBSTACLE';
    return g;
  }

  function makeUncertain() {
    const g = new THREE.Group();
    const ring = new THREE.Mesh(
      new THREE.RingGeometry(0.1, 0.14, 24),
      new THREE.MeshBasicMaterial({ color: KIND_COL.uncertain, transparent: true, opacity: 0.7, side: THREE.DoubleSide })
    );
    ring.rotation.x = -Math.PI / 2;
    ring.position.y = 0.01;
    g.add(ring);
    g.userData.label = '?';
    return g;
  }

  const entityGroup = new THREE.Group();
  scene.add(entityGroup);
  let mainEntity = null;
  const hitEntities = { L: null, C: null, R: null };
  const trailDots = [];
  const trailHistory = [];

  function getEntity(kind) {
    if (kind === 'human') return makePerson();
    if (kind === 'obstacle') return makeObstacle();
    return makeUncertain();
  }

  function placeEntity(ent, xMm, zMm, kind, conf) {
    ent.position.set(xMm / 1000, 0, zMm / 1000);
    ent.visible = true;
    ent.userData.kind = kind;
    ent.userData.conf = conf;
    const col = KIND_COL[kind] || KIND_COL.uncertain;
    ent.traverse((c) => {
      if (c.material && c.material.color) c.material.color.setHex(col);
    });
  }

  function hideEntity(ent) {
    if (ent) ent.visible = false;
  }

  function makeLabelSprite(text, color) {
    const canvas = document.createElement('canvas');
    canvas.width = 256; canvas.height = 64;
    const ctx = canvas.getContext('2d');
    ctx.fillStyle = 'rgba(0,0,0,0.55)';
    ctx.fillRect(8, 12, 240, 44);
    ctx.fillStyle = color;
    ctx.font = 'bold 28px system-ui,sans-serif';
    ctx.textAlign = 'center';
    ctx.fillText(text, 128, 42);
    const tex = new THREE.CanvasTexture(canvas);
    const sp = new THREE.Sprite(new THREE.SpriteMaterial({ map: tex, transparent: true }));
    sp.scale.set(0.42, 0.11, 1);
    sp.position.y = 0.28;
    return sp;
  }

  let pulse = 0;
  let latest3d = null;

  function resize3d() {
    const w = view.clientWidth, h = view.clientHeight;
    if (!w || !h) return;
    const aspect = w / h;
    const frustum = 1.15;
    if (aspect > 1) {
      camera.left = -frustum * aspect; camera.right = frustum * aspect;
      camera.top = frustum; camera.bottom = -frustum;
    } else {
      camera.left = -frustum; camera.right = frustum;
      camera.top = frustum / aspect; camera.bottom = -frustum / aspect;
    }
    camera.updateProjectionMatrix();
    renderer.setSize(w, h, false);
  }
  window.addEventListener('resize', resize3d);
  resize3d();

  function updateScene3d(data) {
    latest3d = data;
    const hits = data.hits || [];
    const fused = data.fused;

    // Per-sensor mini markers (small dots on arc)
    for (const key of ['L', 'C', 'R']) {
      const hit = hits.find((h) => h.zone === key);
      if (!hit) {
        hideEntity(hitEntities[key]);
        continue;
      }
      if (!hitEntities[key]) {
        hitEntities[key] = new THREE.Mesh(
          new THREE.SphereGeometry(0.035, 10, 10),
          new THREE.MeshBasicMaterial({ color: 0x555555, transparent: true, opacity: 0.5 })
        );
        entityGroup.add(hitEntities[key]);
      }
      const col = KIND_COL[hit.kind] || KIND_COL.uncertain;
      hitEntities[key].material.color.setHex(col);
      hitEntities[key].material.opacity = 0.45;
      hitEntities[key].position.set(hit.x_mm / 1000, 0.02, hit.z_mm / 1000);
      hitEntities[key].visible = true;
    }

    if (fused) {
      const kind = fused.kind || 'uncertain';
      if (!mainEntity || mainEntity.userData.kind !== kind) {
        if (mainEntity) entityGroup.remove(mainEntity);
        mainEntity = getEntity(kind);
        const labelText = kind === 'human' ? 'PERSON' : (kind === 'obstacle' ? 'OBSTACLE' : '?');
        const labelCol = kind === 'human' ? '#5b9bd5' : (kind === 'obstacle' ? '#d1d5db' : '#9ca3af');
        mainEntity.add(makeLabelSprite(labelText, labelCol));
        entityGroup.add(mainEntity);
      }
      placeEntity(mainEntity, fused.x_mm, fused.z_mm, kind, fused.confidence);
      if (kind === 'human') {
        mainEntity.scale.setScalar(1 + 0.04 * pulse);
      } else {
        mainEntity.scale.setScalar(1);
      }
      trailHistory.unshift(new THREE.Vector3(fused.x_mm / 1000, 0.015, fused.z_mm / 1000));
      if (trailHistory.length > TRAIL_LEN) trailHistory.length = TRAIL_LEN;
      while (trailDots.length < trailHistory.length) {
        const d = new THREE.Mesh(
          new THREE.SphereGeometry(0.018, 6, 6),
          new THREE.MeshBasicMaterial({ color: KIND_COL.human, transparent: true, opacity: 0 })
        );
        entityGroup.add(d);
        trailDots.push(d);
      }
      trailDots.forEach((d, i) => {
        const p = trailHistory[i];
        if (p && kind === 'human') {
          d.position.copy(p);
          d.material.opacity = 0.5 * (1 - i / TRAIL_LEN);
          d.material.color.setHex(KIND_COL.human);
        } else {
          d.material.opacity = 0;
        }
      });
    } else {
      if (mainEntity) { entityGroup.remove(mainEntity); mainEntity = null; }
      trailHistory.length = 0;
      trailDots.forEach((d) => { d.material.opacity = 0; });
    }

    const ro = document.getElementById('object-readout');
    if (!fused) {
      ro.textContent = 'Clear — no object in sensor field';
    } else {
      const tag = fused.kind === 'human' ? 'PERSON' : (fused.kind === 'obstacle' ? 'OBSTACLE' : 'UNCERTAIN');
      const dist = Math.round(Math.hypot(fused.x_mm, fused.z_mm));
      const bearing = Math.round(Math.atan2(fused.x_mm, fused.z_mm) * 180 / Math.PI);
      ro.textContent = `${tag} · ${dist} mm ahead · ${bearing}° · ${Math.round(fused.confidence * 100)}% — ${fused.reason}`;
    }
  }

  window.updateScene3d = updateScene3d;

  function animate3d() {
    requestAnimationFrame(animate3d);
    pulse = 0.5 + 0.5 * Math.sin(performance.now() * 0.006);
    controls.update();
    if (latest3d) updateScene3d(latest3d);
    renderer.render(scene, camera);
  }
  animate3d();
  </script>

  <script>
    const sensorNames = ["LEFT", "CENTER", "RIGHT"];
    const colors = ["#3b82f6", "#a855f7", "#22c55e"];

    function velLabel(v) {
      if (v === null || v === undefined) return "—";
      if (v < -120) return "approaching fast";
      if (v < -45) return "approaching";
      if (v < -15) return "drifting closer";
      if (v > 120) return "leaving fast";
      if (v > 45) return "departing";
      if (v > 15) return "drifting away";
      return "still";
    }

    function barColor(mm, maxMm) {
      if (mm < 0) return "#4a5568";
      const t = Math.min(mm / maxMm, 1);
      if (t < 0.15) return "#ef4444";
      if (t < 0.4) return "#f59e0b";
      return "#22c55e";
    }

    function initCards() {
      const root = document.getElementById("sensors");
      root.innerHTML = sensorNames.map((name, i) => `
        <div class="card" id="card-${i}">
          <h2>Sensor ${i}</h2>
          <div class="label" style="color:${colors[i]}">${name}</div>
          <div class="distance na" id="dist-${i}">open</div>
          <div class="bar-wrap"><div class="bar-fill" id="bar-${i}" style="width:0"></div></div>
          <div class="vel" id="vel-${i}">velocity —</div>
          <canvas class="spark" id="spark-${i}" width="400" height="56"></canvas>
        </div>
      `).join("");
    }

    function drawSpark(canvas, data, color, maxMm) {
      const ctx = canvas.getContext("2d");
      const w = canvas.width, h = canvas.height;
      ctx.clearRect(0, 0, w, h);
      if (!data.length) return;
      ctx.strokeStyle = color;
      ctx.lineWidth = 2;
      ctx.beginPath();
      data.forEach((mm, idx) => {
        const x = (idx / Math.max(data.length - 1, 1)) * (w - 4) + 2;
        const y = mm < 0 ? h - 4 : h - 4 - (Math.min(mm, maxMm) / maxMm) * (h - 8);
        if (idx === 0) ctx.moveTo(x, y); else ctx.lineTo(x, y);
      });
      ctx.stroke();
    }

    function render(data) {
      const st = document.getElementById("status");
      const port = document.getElementById("port");
      const samples = document.getElementById("samples");

      if (data.error) {
        st.textContent = "serial error";
        st.className = "badge err";
        port.textContent = data.error;
      } else if (data.connected) {
        st.textContent = `${data.ok_count}/3 OK`;
        st.className = data.ok_count >= 2 ? "badge ok" : "badge warn";
        port.textContent = data.port;
      } else {
        st.textContent = "connecting…";
        st.className = "badge warn";
      }
      samples.textContent = `samples: ${data.sample_count}`;

      sensorNames.forEach((_, i) => {
        const mm = data.mm[i];
        const vel = data.vel[i];
        const dist = document.getElementById(`dist-${i}`);
        const bar = document.getElementById(`bar-${i}`);
        const velEl = document.getElementById(`vel-${i}`);
        const spark = document.getElementById(`spark-${i}`);

        const isOpen = data.open && data.open[i];
        if (mm < 0 || isOpen) {
          dist.textContent = "open";
          dist.className = "distance na";
          bar.style.width = "0%";
          bar.style.background = "#4a5568";
          velEl.innerHTML = "<strong>—</strong> (open / beyond ~1.8&nbsp;m)";
        } else {
          dist.innerHTML = `${mm}<span class="unit"> mm</span>`;
          dist.className = "distance";
          const pct = Math.min(100, (mm / data.max_mm) * 100);
          bar.style.width = pct + "%";
          bar.style.background = barColor(mm, data.max_mm);
          if (vel === null || vel === undefined) {
            velEl.innerHTML = "<strong>—</strong> (averaging…)";
          } else {
            velEl.innerHTML = `<strong>${vel >= 0 ? "+" : ""}${vel} mm/s</strong> — ${velLabel(vel)}`;
          }
        }
        drawSpark(spark, data.history[i], colors[i], data.max_mm);
      });

      document.getElementById("summary").innerHTML = `
        <div><dt>Sensors OK</dt><dd>${data.ok_count} / 3</dd></div>
        <div><dt>Dropouts L / C / R</dt><dd>${data.dropouts.join(" / ")}</dd></div>
        <div><dt>Max scale</dt><dd>${data.max_mm} mm</dd></div>
      `;
      document.getElementById("boot").textContent = data.boot.join("\\n");
      if (window.updateScene3d) window.updateScene3d(data);
    }

    initCards();
    async function poll() {
      try {
        const r = await fetch("/api/state");
        render(await r.json());
      } catch (e) { /* retry */ }
      setTimeout(poll, 120);
    }
    poll();
  </script>
</body>
</html>
"""


class Handler(BaseHTTPRequestHandler):
    def log_message(self, fmt: str, *args: Any) -> None:
        pass

    def do_GET(self) -> None:
        if serve_static(self, self.path):
            return
        if self.path in ("/", "/index.html"):
            body = HTML_PAGE.encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
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
    args = parser.parse_args()

    try:
        port = find_port(args.serial_port)
    except FileNotFoundError as exc:
        print(exc)
        sys.exit(1)

    t = threading.Thread(target=serial_reader, args=(port,), daemon=True)
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
    print("Ctrl+C to stop")

    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nStopped.")
        server.shutdown()


if __name__ == "__main__":
    main()
