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
COLORS = ("#3b82f6", "#a855f7", "#22c55e")


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
            return {
                "connected": self.connected,
                "port": self.port,
                "error": self.error,
                "sample_count": self.sample_count,
                "ok_count": ok,
                "dropouts": list(self.dropouts),
                "mm": list(self.mm),
                "vel": [v if v is None else int(v) for v in self.vel],
                "open": list(self.open),
                "history": [list(h) for h in self.history],
                "boot": list(self.boot),
                "last_ts": self.last_ts,
                "max_mm": MAX_MM,
                "labels": list(LABELS),
                "colors": list(COLORS),
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
  </style>
</head>
<body>
  <header>
    <h1>ToF Sensor Live</h1>
    <span id="status" class="badge warn">connecting…</span>
    <span class="meta" id="port"></span>
    <span class="meta" id="samples"></span>
  </header>

  <div class="grid" id="sensors"></div>

  <div class="card" style="margin-bottom:1rem">
    <h2>Session</h2>
    <dl class="summary" id="summary"></dl>
  </div>

  <div class="card">
    <h2>Boot log</h2>
    <div class="boot" id="boot"></div>
    <p class="note">VL53L0X (GY-VL53L0XV2): trusted range 80–1800&nbsp;mm. Beyond ~1.8&nbsp;m readings jitter — shown as <em>open</em>. Velocity uses a 5-sample average so small noise does not look like approach.</p>
  </div>

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
