"""Compact single-screen debug dashboard (camera sidebar + ToF map)."""

from __future__ import annotations

from core.tof_dashboard_html import _mjs_cache_bust

DEBUG_DASHBOARD_HTML = """<!DOCTYPE html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>Robot debug</title>
  <style>
    :root {
      --bg: #0a0e14;
      --card: #141b26;
      --border: #243044;
      --text: #e8edf4;
      --muted: #7d8fa8;
      --na: #4a5568;
      --accent: #38bdf8;
      --track: #22c55e;
      --warn: #f59e0b;
    }
    * { box-sizing: border-box; margin: 0; padding: 0; }
    html, body {
      height: 100%;
      overflow: hidden;
      font-family: "Segoe UI", system-ui, sans-serif;
      background: var(--bg);
      color: var(--text);
    }
    .shell {
      display: grid;
      grid-template-rows: auto 1fr;
      height: 100vh;
      padding: 0.45rem 0.55rem;
      gap: 0.45rem;
    }
    .topbar {
      display: flex;
      flex-wrap: wrap;
      align-items: center;
      gap: 0.45rem 0.75rem;
      padding: 0.35rem 0.5rem;
      background: var(--card);
      border: 1px solid var(--border);
      border-radius: 8px;
      font-size: 0.78rem;
    }
    .topbar h1 {
      font-size: 0.95rem;
      font-weight: 600;
      margin-right: 0.25rem;
    }
    .badge {
      display: inline-block;
      padding: 0.15rem 0.45rem;
      border-radius: 999px;
      font-size: 0.68rem;
      font-weight: 600;
    }
    .badge.ok { background: #14532d; color: #86efac; }
    .badge.warn { background: #713f12; color: #fde68a; }
    .badge.err { background: #7f1d1d; color: #fca5a5; }
    .mode-pill {
      padding: 0.2rem 0.55rem;
      border-radius: 6px;
      background: #1e3a5f;
      border: 1px solid #2563eb;
      color: #93c5fd;
      font-weight: 700;
      font-size: 0.72rem;
      letter-spacing: 0.04em;
      text-transform: uppercase;
    }
    .mode-pill.track { background: #14532d; border-color: #22c55e; color: #86efac; }
    .mode-pill.wander { background: #422006; border-color: #f59e0b; color: #fde68a; }
    .mode-pill.idle { background: #1e293b; border-color: #475569; color: #cbd5e1; }
    .meta { color: var(--muted); font-size: 0.68rem; }
    .meta strong { color: var(--text); font-weight: 600; }
    .main {
      display: grid;
      grid-template-columns: 1fr min(240px, 26vw);
      gap: 0.45rem;
      min-height: 0;
    }
    .map-col {
      display: flex;
      flex-direction: column;
      min-height: 0;
      background: var(--card);
      border: 1px solid var(--border);
      border-radius: 8px;
      padding: 0.35rem 0.45rem 0.4rem;
    }
    .map-col h2 {
      font-size: 0.65rem;
      text-transform: uppercase;
      letter-spacing: 0.1em;
      color: var(--muted);
      margin-bottom: 0.25rem;
    }
    #view3d {
      position: relative;
      flex: 1;
      min-height: 0;
      border-radius: 8px;
      overflow: hidden;
      border: 1px solid rgba(56,189,248,0.12);
      background: #080a0f;
    }
    #view3d canvas { display: block; width: 100% !important; height: 100% !important; }
    #viz-loading {
      position: absolute; inset: 0; display: flex; align-items: center; justify-content: center;
      color: var(--accent); font-size: 0.72rem; letter-spacing: 0.12em; text-transform: uppercase;
      z-index: 5; pointer-events: none;
    }
    .hud-overlay {
      position: absolute; top: 6px; left: 8px; right: 8px;
      display: flex; justify-content: space-between; align-items: flex-start;
      pointer-events: none; z-index: 10;
    }
    .hud-tag {
      background: rgba(8,10,15,0.8); backdrop-filter: blur(6px);
      border: 1px solid rgba(56,189,248,0.1); border-radius: 5px;
      padding: 3px 7px; font-family: ui-monospace, monospace;
      font-size: 0.58rem; color: var(--muted); line-height: 1.35;
    }
    .hud-tag .val { color: #e2e8f0; font-weight: 600; }
    .hud-orient {
      left: 50%; transform: translateX(-50%); top: auto; bottom: 4px;
      text-align: center; max-width: 95%;
      font-size: 0.52rem !important;
    }
    .map-foot {
      display: flex;
      flex-wrap: wrap;
      align-items: center;
      gap: 0.35rem 0.65rem;
      margin-top: 0.3rem;
      font-size: 0.58rem;
      color: var(--muted);
    }
    .map-foot i {
      display: inline-block; width: 7px; height: 7px; border-radius: 50%;
      margin-right: 2px;
    }
    .object-readout {
      font-size: 0.62rem;
      font-family: ui-monospace, monospace;
      color: var(--text);
      flex: 1;
      min-width: 8rem;
      white-space: nowrap;
      overflow: hidden;
      text-overflow: ellipsis;
    }
    .readout-human { color: #38bdf8; }
    .readout-obstacle { color: #f97316; }
    .readout-uncertain { color: #6b7280; }
    .side-col {
      display: flex;
      flex-direction: column;
      gap: 0.4rem;
      min-height: 0;
    }
    .camera-box {
      background: #000;
      border: 1px solid var(--border);
      border-radius: 8px;
      overflow: hidden;
      flex: 0 0 auto;
    }
    .camera-box img {
      display: block;
      width: 100%;
      height: auto;
      max-height: 28vh;
      object-fit: cover;
    }
    .camera-label {
      font-size: 0.6rem;
      text-transform: uppercase;
      letter-spacing: 0.08em;
      color: var(--muted);
      padding: 0.25rem 0.4rem 0;
    }
    .sensors-compact {
      flex: 1;
      min-height: 0;
      display: flex;
      flex-direction: column;
      gap: 0.3rem;
      background: var(--card);
      border: 1px solid var(--border);
      border-radius: 8px;
      padding: 0.35rem;
      overflow: hidden;
    }
    .sensors-compact h2 {
      font-size: 0.6rem;
      text-transform: uppercase;
      letter-spacing: 0.08em;
      color: var(--muted);
    }
    .sensor-row {
      display: grid;
      grid-template-columns: 1fr 1fr 1fr;
      gap: 0.25rem;
      flex: 1;
      min-height: 0;
    }
    .sensor-cell {
      background: #0d1117;
      border: 1px solid var(--border);
      border-radius: 6px;
      padding: 0.25rem 0.3rem;
      display: flex;
      flex-direction: column;
      min-height: 0;
    }
    .sensor-cell .zone {
      font-size: 0.55rem;
      font-weight: 700;
      letter-spacing: 0.06em;
      margin-bottom: 0.1rem;
    }
    .sensor-cell .dist {
      font-size: 0.95rem;
      font-weight: 700;
      font-variant-numeric: tabular-nums;
      line-height: 1.1;
    }
    .sensor-cell .dist.na { color: var(--na); font-size: 0.75rem; }
    .sensor-cell .unit { font-size: 0.55rem; color: var(--muted); font-weight: 500; }
    .bar-wrap {
      height: 5px;
      background: #0a0c10;
      border-radius: 3px;
      margin: 0.15rem 0;
      overflow: hidden;
    }
    .bar-fill { height: 100%; border-radius: 2px; min-width: 1px; transition: width 0.12s; }
    .sensor-cell .vel {
      font-size: 0.5rem;
      color: var(--muted);
      line-height: 1.2;
      overflow: hidden;
      text-overflow: ellipsis;
      white-space: nowrap;
    }
    canvas.spark {
      width: 100%;
      height: 22px;
      display: block;
      margin-top: auto;
      border-radius: 3px;
      background: #0a0c10;
    }
    .side-stats {
      font-size: 0.58rem;
      color: var(--muted);
      line-height: 1.45;
      padding-top: 0.15rem;
      border-top: 1px solid var(--border);
    }
    .side-stats span { color: var(--text); font-weight: 600; }
  </style>
</head>
<body>
  <div class="shell">
    <header class="topbar">
      <h1>Robot debug</h1>
      <span id="mode-pill" class="mode-pill idle">—</span>
      <span id="status" class="badge warn">…</span>
      <span class="meta" id="face-meta">face —</span>
      <span class="meta" id="prox-meta"></span>
      <span class="meta" id="port"></span>
      <span class="meta" id="samples"></span>
      <span class="meta" id="cpu-meta"></span>
    </header>

    <div class="main">
      <section class="map-col">
        <h2>Proximity map</h2>
        <div id="view3d">
          <div id="viz-loading">Loading map…</div>
          <div class="hud-overlay">
            <div class="hud-tag" id="hud-left">SCAN <span class="val">…</span></div>
            <div class="hud-tag" id="hud-right">TRACKS <span class="val">0</span></div>
            <div class="hud-tag hud-orient" id="hud-orient">ORIENT —</div>
          </div>
        </div>
        <div class="map-foot">
          <span><i style="background:#38bdf8"></i>person</span>
          <span><i style="background:#f97316"></i>obstacle</span>
          <span><i style="background:#111"></i>heading</span>
          <span><i style="background:#94a3b8"></i>HOME</span>
          <div class="object-readout" id="object-readout">Scanning…</div>
        </div>
      </section>

      <aside class="side-col">
        <div class="camera-box">
          <div class="camera-label">Camera</div>
          <img src="/stream" alt="Camera stream">
        </div>
        <div class="sensors-compact">
          <h2>ToF sensors</h2>
          <div class="sensor-row" id="sensors"></div>
          <div class="side-stats" id="side-stats">dropouts —</div>
        </div>
      </aside>
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
  <script type="module" src="/static/tof_viz_3d.mjs?v=__MJS_V__"></script>

  <script>
    const sensorNames = ["LEFT", "CENTER", "RIGHT"];
    const colors = ["#3b82f6", "#a855f7", "#22c55e"];

    function velShort(v) {
      if (v === null || v === undefined) return "—";
      if (v < -45) return "in";
      if (v > 45) return "out";
      return "still";
    }

    function barColor(mm, maxMm) {
      if (mm < 0) return "#4a5568";
      const t = Math.min(mm / maxMm, 1);
      if (t < 0.15) return "#ef4444";
      if (t < 0.4) return "#f59e0b";
      return "#22c55e";
    }

    function initSensors() {
      document.getElementById("sensors").innerHTML = sensorNames.map((name, i) => `
        <div class="sensor-cell" id="cell-${i}">
          <div class="zone" style="color:${colors[i]}">${name}</div>
          <div class="dist na" id="dist-${i}">open</div>
          <div class="bar-wrap"><div class="bar-fill" id="bar-${i}"></div></div>
          <div class="vel" id="vel-${i}">—</div>
          <canvas class="spark" id="spark-${i}" width="120" height="22"></canvas>
        </div>
      `).join("");
    }

    function drawSpark(canvas, data, color, maxMm) {
      const ctx = canvas.getContext("2d");
      const w = canvas.width, h = canvas.height;
      ctx.clearRect(0, 0, w, h);
      if (!data.length) return;
      ctx.strokeStyle = color;
      ctx.lineWidth = 1.5;
      ctx.beginPath();
      data.forEach((mm, idx) => {
        const x = (idx / Math.max(data.length - 1, 1)) * (w - 2) + 1;
        const y = mm < 0 ? h - 1 : h - 1 - (Math.min(mm, maxMm) / maxMm) * (h - 2);
        if (idx === 0) ctx.moveTo(x, y); else ctx.lineTo(x, y);
      });
      ctx.stroke();
    }

    function hideVizLoading() {
      const el = document.getElementById("viz-loading");
      if (el) el.style.display = "none";
    }

    function updateModePill(data) {
      const el = document.getElementById("mode-pill");
      const label = data.mode_label || data.servo_mode || "idle";
      el.textContent = label;
      el.className = "mode-pill";
      const mode = (data.servo_mode || "").toLowerCase();
      if (data.servo_forward_return_active) el.classList.add("wander");
      else if (mode === "track") el.classList.add("track");
      else if (mode === "wander") el.classList.add("wander");
      else el.classList.add("idle");
    }

    function render(data) {
      const st = document.getElementById("status");
      const port = document.getElementById("port");
      const samples = document.getElementById("samples");

      if (data.error) {
        st.textContent = "error";
        st.className = "badge err";
        port.textContent = data.error;
      } else if (data.connected) {
        st.textContent = `${data.ok_count}/3`;
        st.className = data.ok_count >= 2 ? "badge ok" : "badge warn";
        port.textContent = data.port || "";
      } else {
        st.textContent = "…";
        st.className = "badge warn";
      }
      samples.textContent = `n=${data.sample_count || 0}`;

      updateModePill(data);

      const faceEl = document.getElementById("face-meta");
      if (data.face_detected) {
        faceEl.innerHTML = `face <strong>${data.track_kind || "yes"}</strong>`;
      } else if (data.body_detected) {
        faceEl.innerHTML = "body <strong>seen</strong>";
      } else {
        faceEl.textContent = "face —";
      }

      const proxEl = document.getElementById("prox-meta");
      if (data.prox_approach_active && data.prox_approach_zone) {
        proxEl.textContent = `PROX ${data.prox_approach_zone}`;
      } else {
        proxEl.textContent = "";
      }

      const cpuEl = document.getElementById("cpu-meta");
      const cpuParts = [];
      if (data.cpu_load_pct != null) {
        cpuParts.push(`CPU ${Math.round(data.cpu_load_pct)}%`);
      }
      if (data.cpu_temp_c != null) {
        cpuParts.push(`${data.cpu_temp_c.toFixed(0)}°C`);
      }
      cpuEl.textContent = cpuParts.join(" · ");

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
          dist.className = "dist na";
          bar.style.width = "0%";
          velEl.textContent = "—";
        } else {
          dist.innerHTML = `${mm}<span class="unit">mm</span>`;
          dist.className = "dist";
          bar.style.width = Math.min(100, (mm / data.max_mm) * 100) + "%";
          bar.style.background = barColor(mm, data.max_mm);
          velEl.textContent = vel != null ? `${vel >= 0 ? "+" : ""}${vel} ${velShort(vel)}` : "…";
        }
        drawSpark(spark, data.history[i] || [], colors[i], data.max_mm);
      });

      document.getElementById("side-stats").innerHTML =
        `yaw enc <span>${Math.round(data.from_home_enc_deg || 0)}°</span> · ` +
        `imu <span>${Math.round(data.from_home_imu_deg || 0)}°</span> · ` +
        `drop <span>${(data.dropouts || [0,0,0]).join("/")}</span>`;

      hideVizLoading();
      if (window.updateScene3d) window.updateScene3d(data);
    }

    initSensors();
    setTimeout(() => {
      if (!window.updateScene3d) {
        const el = document.getElementById("viz-loading");
        if (el && el.style.display !== "none") {
          el.style.color = "#fca5a5";
          el.textContent = "Map load failed — Ctrl+Shift+R";
        }
      }
    }, 8000);

    async function poll() {
      try {
        const r = await fetch("/api/state", { cache: "no-store" });
        if (!r.ok) return;
        render(await r.json());
      } catch (e) { /* retry */ }
      setTimeout(poll, __POLL_MS__);
    }
    poll();
  </script>
</body>
</html>
"""


def build_debug_dashboard_html(*, poll_ms: int = 30, include_camera_stream: bool = True) -> str:
    html = DEBUG_DASHBOARD_HTML.replace(
        "setTimeout(poll, __POLL_MS__);", f"setTimeout(poll, {int(poll_ms)});"
    )
    html = html.replace("__MJS_V__", str(_mjs_cache_bust()))
    if not include_camera_stream:
        html = html.replace(
            'grid-template-columns: 1fr min(240px, 26vw);',
            "grid-template-columns: 1fr;",
        )
        html = html.replace(
            """      <aside class="side-col">
        <div class="camera-box">
          <div class="camera-label">Camera</div>
          <img src="/stream" alt="Camera stream">
        </div>
        <div class="sensors-compact">""",
            """      <aside class="side-col">
        <div class="sensors-compact">""",
        )
    return html
