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
    .view3d-card { margin-bottom: 1rem; position: relative; }
    #view3d {
      position: relative;
      width: 100%;
      height: min(65vh, 600px);
      min-height: 380px;
      border-radius: 14px;
      overflow: hidden;
      border: 1px solid rgba(56,189,248,0.15);
      background: #080a0f;
      box-shadow: 0 0 40px rgba(56,189,248,0.05), inset 0 0 60px rgba(0,0,0,0.5);
    }
    #view3d canvas { display: block; width: 100% !important; height: 100% !important; }
    #viz-loading {
      position: absolute; inset: 0; display: flex; align-items: center; justify-content: center;
      color: var(--accent); font-size: 0.9rem; letter-spacing: 0.15em; text-transform: uppercase;
    }
    .hud-overlay {
      position: absolute; top: 12px; left: 14px; right: 14px;
      display: flex; justify-content: space-between; align-items: flex-start;
      pointer-events: none; z-index: 10;
    }
    .hud-tag {
      background: rgba(8,10,15,0.75); backdrop-filter: blur(8px);
      border: 1px solid rgba(56,189,248,0.12); border-radius: 8px;
      padding: 6px 12px; font-family: ui-monospace, monospace;
      font-size: 0.72rem; color: var(--muted); line-height: 1.5;
    }
    .hud-tag .val { color: #e2e8f0; font-weight: 600; }
    .hud-tag .human-val { color: #38bdf8; font-weight: 700; }
    .hud-tag .obstacle-val { color: #f97316; font-weight: 700; }
    .viz-legend {
      display: flex; flex-wrap: wrap; gap: 1.2rem; margin-top: 0.75rem;
      font-size: 0.78rem; color: var(--muted); align-items: center;
    }
    .viz-legend span { display: inline-flex; align-items: center; gap: 0.4rem; }
    .viz-legend i {
      display: inline-block; width: 10px; height: 10px; border-radius: 50%;
      box-shadow: 0 0 6px currentColor;
    }
    .object-readout {
      margin-top: 0.65rem; font-size: 0.85rem; color: var(--text);
      font-family: ui-monospace, monospace; letter-spacing: 0.02em;
    }
    .readout-human { color: #38bdf8; }
    .readout-obstacle { color: #f97316; }
    .readout-uncertain { color: #6b7280; }
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
    <div id="view3d">
      <div id="viz-loading">Initializing proximity scanner…</div>
      <div class="hud-overlay">
        <div class="hud-tag" id="hud-left">SCAN <span class="val">ACTIVE</span></div>
        <div class="hud-tag" id="hud-right">OBJECTS <span class="val">0</span></div>
      </div>
    </div>
    <div class="viz-legend">
      <span><i style="color:#38bdf8;background:#38bdf8"></i>person (moving)</span>
      <span><i style="color:#f97316;background:#f97316"></i>obstacle (static)</span>
      <span><i style="color:#6b7280;background:#6b7280"></i>uncertain</span>
      <span><i style="color:#e2e8f0;background:#e2e8f0"></i>robot</span>
      <span style="opacity:0.5">⌖ drag · scroll to zoom</span>
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
  <script type="module" src="/static/tof_viz_3d.mjs"></script>

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
