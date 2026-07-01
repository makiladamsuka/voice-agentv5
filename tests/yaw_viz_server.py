"""HTTP server + shared state for yaw-only robot visualization."""

from __future__ import annotations

import json
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any

APP_DIR = Path(__file__).resolve().parents[1]
STATIC_DIR = APP_DIR / "static"
CONFIG_PATH = APP_DIR / "config.yaml"

_STATIC_MIME = {
    ".js": "application/javascript; charset=utf-8",
    ".mjs": "application/javascript; charset=utf-8",
    ".css": "text/css; charset=utf-8",
}


def _load_viz_config() -> dict[str, Any]:
    try:
        import yaml

        if CONFIG_PATH.exists():
            with open(CONFIG_PATH, encoding="utf-8") as f:
                return (yaml.safe_load(f) or {}).get("debug_viz", {}) or {}
    except Exception:
        pass
    return {}


def serve_static(handler: BaseHTTPRequestHandler, path: str) -> bool:
    if not path.startswith("/static/"):
        return False
    rel = path[len("/static/") :].lstrip("/").split("?", 1)[0]
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
    handler.send_header("Cache-Control", "no-store")
    handler.send_header("Content-Length", str(len(data)))
    handler.end_headers()
    handler.wfile.write(data)
    return True


class YawVizState:
    def __init__(self) -> None:
        self._lock = threading.Lock()
        viz_cfg = _load_viz_config()
        base_cfg: dict[str, Any] = {}
        try:
            import yaml

            if CONFIG_PATH.exists():
                with open(CONFIG_PATH, encoding="utf-8") as f:
                    base_cfg = (yaml.safe_load(f) or {}).get("base", {}) or {}
        except Exception:
            pass
        self.base_yaw_sign = float(viz_cfg.get("base_yaw_sign", -1.0))
        self.max_yaw_deg = float(base_cfg.get("max_yaw_deg", 120.0))
        self.connected = False
        self.port = ""
        self.imu_online = False
        self.home_locked = False
        self.base_busy = False
        self.stationary = False
        self.spin_label = "stop"
        self.last_ts = 0.0
        self.from_home_enc_deg = 0.0
        self.from_home_imu_deg = 0.0
        self.disagreement_deg = 0.0
        self.encoder_deg = 0.0
        self.encoder_count = 0
        self.encoder_count_delta = 0
        self.encoder_count_raw_delta = 0
        self.imu_yaw_deg = 0.0
        self.pan_mech_deg = 0.0
        self.gyro_dps = 0.0
        self.imu_correction_deg = 0.0
        self.head_pan = 0.0
        self.head_tilt = 0.0

    def update(self, **fields: Any) -> None:
        with self._lock:
            for key, val in fields.items():
                if hasattr(self, key):
                    setattr(self, key, val)
            self.last_ts = time.time()

    def snapshot(self) -> dict[str, Any]:
        with self._lock:
            return {
                "connected": self.connected,
                "port": self.port,
                "imu_online": self.imu_online,
                "home_locked": self.home_locked,
                "base_busy": self.base_busy,
                "stationary": self.stationary,
                "spin_label": self.spin_label,
                "last_ts": self.last_ts,
                "base_yaw_sign": self.base_yaw_sign,
                "max_yaw_deg": self.max_yaw_deg,
                "from_home_enc_deg": self.from_home_enc_deg,
                "from_home_imu_deg": self.from_home_imu_deg,
                "disagreement_deg": self.disagreement_deg,
                "encoder_deg": self.encoder_deg,
                "encoder_count": self.encoder_count,
                "encoder_count_delta": self.encoder_count_delta,
                "encoder_count_raw_delta": self.encoder_count_raw_delta,
                "imu_yaw_deg": self.imu_yaw_deg,
                "pan_mech_deg": self.pan_mech_deg,
                "gyro_dps": self.gyro_dps,
                "imu_correction_deg": self.imu_correction_deg,
                "head_pan": self.head_pan,
                "head_tilt": self.head_tilt,
                # Viz map follows IMU base yaw from HOME (encoder used only for drift snap).
                "map_yaw_deg": self.from_home_imu_deg,
            }


STATE = YawVizState()


class YawControl:
    """Thread-safe browser / API control queue (head_debug_viz style)."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._pending: list[str] = []
        self.last_m = 0.0
        self.last_n = 0.0
        self.head_step_deg = 5.0

    def post_spin(self, *, m: bool = False, n: bool = False) -> None:
        now = time.time()
        with self._lock:
            if m:
                self.last_m = now
            if n:
                self.last_n = now

    def post_cmd(self, cmd: str, *, step: float | None = None) -> None:
        cmd = cmd.strip().lower()
        if not cmd:
            return
        with self._lock:
            self._pending.append(cmd)
            if step is not None:
                self.head_step_deg = float(step)

    def drain(self) -> tuple[list[str], float, float, float]:
        with self._lock:
            cmds = self._pending
            self._pending = []
            return cmds, self.last_m, self.last_n, self.head_step_deg


CONTROL = YawControl()


HTML_PAGE = """<!DOCTYPE html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>Base Yaw — HOME relative</title>
  <style>
    :root {
      --bg: #0a0c10;
      --text: #e2e8f0;
      --muted: #94a3b8;
      --accent: #38bdf8;
      --enc: #38bdf8;
      --imu: #fb923c;
      --ok: #4ade80;
      --warn: #fbbf24;
    }
    * { box-sizing: border-box; margin: 0; padding: 0; }
    body {
      font-family: "Segoe UI", system-ui, sans-serif;
      background: var(--bg);
      color: var(--text);
      min-height: 100vh;
      padding: 1rem 1.25rem 1.5rem;
    }
    header {
      display: flex;
      flex-wrap: wrap;
      align-items: baseline;
      gap: 0.75rem 1.25rem;
      margin-bottom: 1rem;
      border-bottom: 1px solid rgba(148,163,184,0.15);
      padding-bottom: 0.85rem;
    }
    h1 { font-size: 1.2rem; font-weight: 600; letter-spacing: 0.02em; }
    .meta { color: var(--muted); font-size: 0.85rem; }
    .badge {
      display: inline-block;
      padding: 0.15rem 0.55rem;
      border-radius: 999px;
      font-size: 0.72rem;
      font-weight: 600;
    }
    .badge.ok { background: #14532d; color: #86efac; }
    .badge.warn { background: #713f12; color: #fde68a; }
    .stats {
      display: grid;
      grid-template-columns: repeat(auto-fit, minmax(9rem, 1fr));
      gap: 0.65rem;
      margin-bottom: 1rem;
    }
    .stat {
      background: rgba(15,20,28,0.9);
      border: 1px solid rgba(56,189,248,0.12);
      border-radius: 10px;
      padding: 0.65rem 0.85rem;
    }
    .stat .k {
      font-size: 0.68rem;
      text-transform: uppercase;
      letter-spacing: 0.12em;
      color: var(--muted);
    }
    .stat .v {
      font-family: ui-monospace, monospace;
      font-size: 1.35rem;
      font-weight: 700;
      margin-top: 0.2rem;
    }
    .stat.enc .v { color: var(--enc); }
    .stat.imu .v { color: var(--imu); }
    .stat.delta .v { color: var(--warn); }
    .stat.delta.ok .v { color: var(--ok); }
    .stats-label {
      font-size: 0.68rem;
      text-transform: uppercase;
      letter-spacing: 0.14em;
      color: var(--muted);
      margin: 0.35rem 0 0.5rem;
    }
    .stats-label:first-of-type { margin-top: 0; }
    #view3d-wrap {
      position: relative;
      width: 100%;
      height: min(72vh, 640px);
      min-height: 400px;
      border-radius: 14px;
      overflow: hidden;
      border: 1px solid rgba(56,189,248,0.15);
      background: #080a0f;
    }
    #view3d { width: 100%; height: 100%; }
    #view3d canvas { display: block; width: 100% !important; height: 100% !important; }
    .hud {
      position: absolute; left: 14px; right: 14px; bottom: 12px;
      display: flex; justify-content: center; pointer-events: none;
    }
    .hud-inner {
      background: rgba(8,10,15,0.8);
      backdrop-filter: blur(8px);
      border: 1px solid rgba(56,189,248,0.12);
      border-radius: 8px;
      padding: 8px 16px;
      font-family: ui-monospace, monospace;
      font-size: 0.75rem;
      color: var(--muted);
      text-align: center;
    }
    .hud-inner strong { color: var(--text); }
    .help {
      margin-top: 1rem;
      font-size: 0.8rem;
      color: var(--muted);
      line-height: 1.55;
    }
    code { color: var(--accent); font-size: 0.85em; }
    .focus-hint {
      font-size: 0.75rem;
      color: var(--accent);
      margin-bottom: 0.65rem;
    }
    .focus-hint.off { color: var(--muted); }
    #controls {
      display: flex;
      flex-wrap: wrap;
      gap: 0.45rem;
      margin-bottom: 0.85rem;
      align-items: center;
    }
    #controls button {
      font-family: ui-monospace, monospace;
      font-size: 0.72rem;
      padding: 0.35rem 0.65rem;
      border-radius: 8px;
      border: 1px solid rgba(56,189,248,0.2);
      background: rgba(15,20,28,0.95);
      color: var(--text);
      cursor: pointer;
    }
    #controls button:hover { border-color: var(--accent); color: var(--accent); }
    #controls button.hold { min-width: 2.8rem; font-weight: 700; }
    #controls button.hold.active {
      background: rgba(56,189,248,0.2);
      border-color: var(--accent);
      color: var(--accent);
    }
    #controls .sep { width: 1px; height: 1.4rem; background: rgba(148,163,184,0.2); margin: 0 0.2rem; }
    #goto-row {
      display: flex;
      flex-wrap: wrap;
      align-items: center;
      gap: 0.45rem;
      margin-bottom: 0.85rem;
      padding: 0.55rem 0.75rem;
      border-radius: 10px;
      border: 1px solid rgba(56,189,248,0.15);
      background: rgba(15,20,28,0.65);
    }
    #goto-row label {
      font-size: 0.72rem;
      color: var(--muted);
      text-transform: uppercase;
      letter-spacing: 0.08em;
    }
    #goto-angle {
      width: 4.5rem;
      font-family: ui-monospace, monospace;
      font-size: 0.9rem;
      padding: 0.35rem 0.5rem;
      border-radius: 8px;
      border: 1px solid rgba(148,163,184,0.25);
      background: #0a0c10;
      color: var(--text);
    }
    #goto-row button {
      font-family: ui-monospace, monospace;
      font-size: 0.72rem;
      padding: 0.35rem 0.65rem;
      border-radius: 8px;
      border: 1px solid rgba(148,163,184,0.25);
      background: rgba(30,41,59,0.8);
      color: var(--text);
      cursor: pointer;
    }
    #goto-row button:hover { border-color: var(--accent); color: var(--accent); }
    #goto-row button.primary {
      border-color: rgba(56,189,248,0.45);
      color: var(--accent);
      font-weight: 600;
    }
  </style>
  <script type="importmap">
  {
    "imports": {
      "three": "/static/vendor/three.module.js",
      "three/addons/": "/static/vendor/addons/"
    }
  }
  </script>
</head>
<body>
  <header>
    <h1>Base yaw from HOME</h1>
    <span class="meta" id="port-meta">—</span>
    <span class="badge warn" id="home-badge">HOME …</span>
    <span class="badge warn" id="imu-badge">IMU …</span>
  </header>

  <p class="focus-hint off" id="focus-hint">Click the page (or 3D view) then use keyboard — same keys as terminal</p>

  <div id="controls">
    <button type="button" class="hold" data-hold="m">M ◀ spin</button>
    <button type="button" class="hold" data-hold="n">N ▶ spin</button>
    <span class="sep"></span>
    <button type="button" data-cmd="tilt_up">W ↑</button>
    <button type="button" data-cmd="tilt_down">S ↓</button>
    <button type="button" data-cmd="pan_left">A ←</button>
    <button type="button" data-cmd="pan_right">D →</button>
    <span class="sep"></span>
    <button type="button" data-cmd="center">C center</button>
    <button type="button" data-cmd="home_lock">H → HOME IMU</button>
    <button type="button" data-cmd="zero_home">Z zero here</button>
  </div>

  <div id="goto-row">
    <label for="goto-angle">Go to ° from HOME</label>
    <input type="number" id="goto-angle" value="45" step="1" min="-120" max="120" title="+ = clockwise">
    <button type="button" class="primary" id="goto-cw">Go ↻ CW</button>
    <button type="button" id="goto-ccw">Go ↺ CCW</button>
    <span class="meta" style="font-size:0.72rem">+ clockwise · IMU closed-loop</span>
  </div>

  <p class="stats-label">From HOME</p>
  <div class="stats">
    <div class="stat enc"><div class="k">Encoder from HOME</div><div class="v" id="v-enc">—</div></div>
    <div class="stat imu"><div class="k">IMU base from HOME</div><div class="v" id="v-imu">—</div></div>
    <div class="stat delta" id="delta-card"><div class="k">ENC − IMU</div><div class="v" id="v-delta">—</div></div>
    <div class="stat"><div class="k">Ticks Δ (rotation)</div><div class="v" id="v-ticks">—</div></div>
  </div>

  <div id="view3d-wrap">
    <div id="view3d"></div>
    <div class="hud"><div class="hud-inner" id="hud-bottom">—</div></div>
  </div>

  <p class="help">
    <strong>Browser or terminal:</strong> <code>M</code>/<code>N</code> hold base spin · <code>WASD</code> head ·
    <code>C</code> center · <code>H</code> spin to HOME IMU yaw 0° · <code>Z</code> zero encoder here (no move) ·
    <code>?</code> status · <code>Q</code> quit.
    Grey cone = startup HOME forward (fixed). Black strip = base heading; robot base rotates with IMU yaw from HOME.
  </p>

  <script type="module" src="/static/yaw_robot_viz.mjs"></script>
  <script>
    function fmtDeg(v) {
      const n = Math.round(Number(v) || 0);
      return (n >= 0 ? '+' : '') + n + '°';
    }
    function fmtTicks(v) {
      const n = Math.round(Number(v) || 0);
      return (n >= 0 ? '+' : '') + n;
    }
    async function poll() {
      try {
        const data = await (await fetch('/api/state')).json();
        document.getElementById('port-meta').textContent =
          (data.connected ? data.port : 'disconnected') +
          (data.spin_label !== 'stop' ? ' · spin ' + data.spin_label : '') +
          (data.base_busy ? ' · BUSY' : '');
        const homeEl = document.getElementById('home-badge');
        homeEl.textContent = data.home_locked ? 'HOME locked' : 'HOME not set';
        homeEl.className = 'badge ' + (data.home_locked ? 'ok' : 'warn');
        const imuEl = document.getElementById('imu-badge');
        imuEl.textContent = data.imu_online ? (data.stationary ? 'IMU still' : 'IMU move') : 'IMU off';
        imuEl.className = 'badge ' + (data.imu_online ? 'ok' : 'warn');
        document.getElementById('v-enc').textContent = fmtDeg(data.from_home_enc_deg);
        document.getElementById('v-imu').textContent = fmtDeg(data.from_home_imu_deg);
        const d = Math.abs(Number(data.disagreement_deg) || 0);
        document.getElementById('v-delta').textContent = fmtDeg(data.disagreement_deg);
        document.getElementById('delta-card').className = 'stat delta' + (d < 3 ? ' ok' : '');
        document.getElementById('v-ticks').textContent = fmtTicks(data.encoder_count_delta);
        document.getElementById('hud-bottom').innerHTML =
          '<strong>FROM HOME</strong> imu ' + fmtDeg(data.from_home_imu_deg) +
          ' · enc ' + fmtDeg(data.from_home_enc_deg) +
          ' · Δ ' + fmtDeg(data.disagreement_deg) +
          ' · ticksΔ ' + fmtTicks(data.encoder_count_delta) +
          (data.stationary ? ' · <span style="color:#4ade80">still</span>' : '');
        if (window.updateYawScene) window.updateYawScene(data);
        const gotoInput = document.getElementById('goto-angle');
        if (gotoInput && data.max_yaw_deg) {
          gotoInput.min = String(-Math.round(data.max_yaw_deg));
          gotoInput.max = String(Math.round(data.max_yaw_deg));
        }
      } catch (e) { /* retry */ }
      setTimeout(poll, 30);
    }
    poll();

    const held = new Set();
    const holdBtns = document.querySelectorAll('[data-hold]');
    const focusHint = document.getElementById('focus-hint');

    function setHoldUi() {
      holdBtns.forEach((btn) => {
        const k = btn.dataset.hold;
        btn.classList.toggle('active', held.has(k));
      });
    }

    async function postSpin() {
      try {
        await fetch('/api/control', {
          method: 'POST',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({ m: held.has('m'), n: held.has('n') }),
        });
      } catch (e) { /* retry on next tick */ }
    }

    async function sendCmd(cmd) {
      try {
        await fetch('/api/control', {
          method: 'POST',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({ cmd }),
        });
      } catch (e) { console.warn('control', e); }
    }

    async function sendGoto(signedDeg) {
      try {
        await fetch('/api/control', {
          method: 'POST',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({ cmd: 'goto_imu', angle: signedDeg }),
        });
      } catch (e) { console.warn('goto', e); }
    }

    document.getElementById('goto-cw').addEventListener('click', () => {
      const mag = Math.abs(Number(document.getElementById('goto-angle').value) || 0);
      sendGoto(mag);
    });
    document.getElementById('goto-ccw').addEventListener('click', () => {
      const mag = Math.abs(Number(document.getElementById('goto-angle').value) || 0);
      sendGoto(-mag);
    });
    document.getElementById('goto-angle').addEventListener('keydown', (ev) => {
      if (ev.key === 'Enter') {
        ev.preventDefault();
        const v = Number(ev.target.value) || 0;
        sendGoto(v);
      }
    });

    document.querySelectorAll('#controls [data-cmd]').forEach((btn) => {
      btn.addEventListener('click', () => sendCmd(btn.dataset.cmd));
    });

    function holdDown(key) {
      const k = key.toLowerCase();
      if (k !== 'm' && k !== 'n') return;
      held.add(k);
      setHoldUi();
      postSpin();
    }

    function holdUp(key) {
      const k = key.toLowerCase();
      if (k !== 'm' && k !== 'n') return;
      held.delete(k);
      setHoldUi();
      postSpin();
    }

    holdBtns.forEach((btn) => {
      const k = btn.dataset.hold;
      btn.addEventListener('mousedown', (ev) => { ev.preventDefault(); holdDown(k); });
      btn.addEventListener('mouseup', () => holdUp(k));
      btn.addEventListener('mouseleave', () => holdUp(k));
      btn.addEventListener('touchstart', (ev) => { ev.preventDefault(); holdDown(k); }, { passive: false });
      btn.addEventListener('touchend', () => holdUp(k));
    });

    const keyToCmd = {
      w: 'tilt_up', s: 'tilt_down', a: 'pan_left', d: 'pan_right',
      c: 'center', h: 'home_lock', z: 'zero_home', '?': 'status', q: 'quit',
    };

    function onFocusIn() {
      focusHint.textContent = 'Keyboard active — M/N hold spin, WASD head';
      focusHint.classList.remove('off');
    }
    function onFocusOut() {
      focusHint.textContent = 'Click the page (or 3D view) then use keyboard';
      focusHint.classList.add('off');
    }
    window.addEventListener('focus', onFocusIn);
    window.addEventListener('blur', onFocusOut);
    document.body.addEventListener('pointerdown', onFocusIn, { once: false });

    window.addEventListener('keydown', (ev) => {
      if (ev.target && (ev.target.tagName === 'INPUT' || ev.target.tagName === 'TEXTAREA')) return;
      const k = ev.key.length === 1 ? ev.key.toLowerCase() : ev.key;
      if (k === 'm' || k === 'n') {
        if (ev.repeat) return;
        ev.preventDefault();
        holdDown(k);
        return;
      }
      const cmd = keyToCmd[k];
      if (!cmd) return;
      if (ev.repeat && (k === 'h' || k === 'z')) return;
      ev.preventDefault();
      sendCmd(cmd);
    });

    window.addEventListener('keyup', (ev) => {
      const k = ev.key.toLowerCase();
      if (k === 'm' || k === 'n') {
        ev.preventDefault();
        holdUp(k);
      }
    });

    setInterval(() => {
      if (held.has('m') || held.has('n')) postSpin();
    }, 90);

    onFocusOut();
  </script>
</body>
</html>
"""


class Handler(BaseHTTPRequestHandler):
    def log_message(self, fmt: str, *args: Any) -> None:
        pass

    def _read_json_body(self) -> dict[str, Any]:
        length = int(self.headers.get("Content-Length", "0") or "0")
        if length <= 0:
            return {}
        try:
            return json.loads(self.rfile.read(length).decode("utf-8"))
        except (json.JSONDecodeError, UnicodeDecodeError):
            return {}

    def _send_json(self, code: int, payload: dict[str, Any]) -> None:
        body = json.dumps(payload).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Cache-Control", "no-store")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_POST(self) -> None:
        if self.path != "/api/control":
            self.send_error(404)
            return
        payload = self._read_json_body()
        if "m" in payload or "n" in payload:
            CONTROL.post_spin(m=bool(payload.get("m")), n=bool(payload.get("n")))
            self._send_json(200, {"ok": True, "spin": True})
            return
        cmd = str(payload.get("cmd", "")).strip()
        if not cmd:
            self._send_json(400, {"ok": False, "error": "missing cmd"})
            return
        step = payload.get("step")
        if cmd == "goto_imu":
            angle = payload.get("angle")
            if angle is None:
                self._send_json(400, {"ok": False, "error": "missing angle"})
                return
            CONTROL.post_cmd(f"goto_imu:{float(angle)}")
            self._send_json(200, {"ok": True, "cmd": cmd, "angle": float(angle)})
            return
        CONTROL.post_cmd(cmd, step=float(step) if step is not None else None)
        self._send_json(200, {"ok": True, "cmd": cmd})

    def do_GET(self) -> None:
        if serve_static(self, self.path):
            return
        if self.path in ("/", "/index.html"):
            body = HTML_PAGE.encode("utf-8")
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


def start_server(host: str, port: int) -> ThreadingHTTPServer:
    server = ThreadingHTTPServer((host, port), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    return server
