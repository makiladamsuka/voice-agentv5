"""ToF/yaw 3D dashboard HTML (shared by debug dashboard + tof_viz_server)."""

from __future__ import annotations

from pathlib import Path

APP_DIR = Path(__file__).resolve().parent.parent
STATIC_DIR = APP_DIR / "static"

CAMERA_STREAM_HTML = (
    '<div style="margin-bottom: 10px; border: 1px solid #2a3142; background: #000;">'
    '<img src="/stream" style="width: 100%; height: auto; display: block;" '
    'alt="Camera stream"></div>'
)

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
      z-index: 5; pointer-events: none;
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
    .hud-orient {
      left: 50%; transform: translateX(-50%); top: auto; bottom: 0.55rem;
      text-align: center; min-width: 18rem;
    }
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
        <div class="hud-tag hud-orient" id="hud-orient">ORIENT —</div>
      </div>
    </div>
    <div class="viz-legend">
      <span><i style="color:#38bdf8;background:#38bdf8"></i>person (moving)</span>
      <span><i style="color:#f97316;background:#f97316"></i>obstacle (static)</span>
      <span><i style="color:#6b7280;background:#6b7280"></i>uncertain</span>
      <span><i style="color:#111111;background:#111111"></i>black line = base heading (IMU)</span>
      <span><i style="color:#94a3b8;background:#94a3b8"></i>grey cone = HOME forward</span>
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
  <script type="module" src="/static/tof_viz_3d.mjs?v=__MJS_V__"></script>

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

    function hideVizLoading() {
      const el = document.getElementById("viz-loading");
      if (el) el.style.display = "none";
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
      hideVizLoading();
      if (window.updateScene3d) window.updateScene3d(data);
    }

    initCards();
    setTimeout(() => {
      if (!window.updateScene3d) {
        const el = document.getElementById("viz-loading");
        if (el && el.style.display !== "none") {
          el.style.color = "#fca5a5";
          el.textContent = "3D module failed to load — hard refresh (Ctrl+Shift+R)";
        }
      }
    }, 8000);
    async function poll() {
      try {
        const r = await fetch("/api/state", { cache: "no-store" });
        if (!r.ok) return;
        const data = await r.json();
        render(data);
      } catch (e) { /* retry */ }
      setTimeout(poll, __POLL_MS__);
    }
    poll();
  </script>
</body>
</html>
"""

def _mjs_cache_bust() -> int:
    mjs = STATIC_DIR / "tof_viz_3d.mjs"
    try:
        return int(mjs.stat().st_mtime)
    except OSError:
        return 1


def build_tof_dashboard_html(
    *,
    include_camera_stream: bool = False,
    poll_ms: int = 30,
    title: str = "ToF Live — VL53L0X",
) -> str:
    html = HTML_PAGE.replace("<title>ToF Live — VL53L0X</title>", f"<title>{title}</title>")
    html = html.replace("<h1>ToF Sensor Live</h1>", f"<h1>{title}</h1>")
    if include_camera_stream:
        html = html.replace("<body>", f"<body>\n  {CAMERA_STREAM_HTML}", 1)
    html = html.replace("setTimeout(poll, __POLL_MS__);", f"setTimeout(poll, {int(poll_ms)});")
    return html.replace("__MJS_V__", str(_mjs_cache_bust()))
