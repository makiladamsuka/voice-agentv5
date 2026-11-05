#!/usr/bin/env python3
"""
Pi Camera XRGB Web Browser Stream Test
======================================
Captures raw video frames from the Raspberry Pi Camera using Picamera2 in
native XRGB8888 format, processes the buffer, and streams/posts the live feed
to any web browser over HTTP (MJPEG + interactive dashboard).

Features:
  - Picamera2 configured directly for native "XRGB8888" capture
  - Converts XRGB buffer (ARM little-endian BGRA) to clean video frames
  - Built-in multi-threaded HTTP server (no external framework required)
  - Interactive Web Dashboard at http://<pi-ip>:<port>/
  - Live MJPEG video stream at /stream
  - Single JPEG snapshot at /snapshot
  - REST POST endpoints (/api/snapshot, /api/settings) to test POSTing from/to browser
  - Graceful fallback for non-Pi / development environments (OpenCV / PIL / synthetic pattern)
  - Real-time HUD showing resolution, FPS, format (XRGB8888), and timestamp

How to run on the robot:
    python tests/test_xrgb_camera_web.py --port 8000

How to view in browser:
    Open http://<robot-ip>:8000/ on your computer or phone browser.
"""

from __future__ import annotations

import _bootstrap  # noqa: F401
import argparse
import io
import json
import os
import socket
import socketserver
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, HTTPServer
from typing import Any, Dict, Optional, Tuple

import numpy as np

# ── Headless-safe OpenCV detection ────────────────────────────────────────────
_has_display = bool(os.environ.get("DISPLAY") or os.environ.get("WAYLAND_DISPLAY"))
if not _has_display:
    os.environ.setdefault("OPENCV_VIDEOIO_PRIORITY_MSMF", "0")
    os.environ["QT_QPA_PLATFORM"] = "offscreen"

_cv2_available = False
try:
    import cv2  # type: ignore

    if hasattr(cv2, "imencode") and hasattr(cv2, "cvtColor"):
        _cv2_available = True
except Exception:
    _cv2_available = False

try:
    from PIL import Image, ImageDraw, ImageFont  # type: ignore

    _pil_available = True
except Exception:
    _pil_available = False


# ── Robust Frame Encoding Helpers ─────────────────────────────────────────────
def xrgb_to_bgr(frame_xrgb: np.ndarray) -> np.ndarray:
    """Converts Picamera2 XRGB8888 buffer to standard BGR image.

    On ARM little-endian (Raspberry Pi), XRGB8888 is stored in memory as
    channel order [B, G, R, X]. Dropping or slicing the 4th pad byte yields BGR.
    """
    if _cv2_available and hasattr(cv2, "COLOR_BGRA2BGR"):
        return cv2.cvtColor(frame_xrgb, cv2.COLOR_BGRA2BGR)
    # Fast zero-copy / slice fallback:
    return frame_xrgb[:, :, :3].copy()


def encode_jpeg(frame_bgr: np.ndarray, quality: int = 80) -> bytes:
    """Encodes a BGR numpy array to JPEG bytes using cv2 or PIL."""
    if _cv2_available:
        try:
            encode_params = [int(cv2.IMWRITE_JPEG_QUALITY), quality]
            success, jpeg = cv2.imencode(".jpg", frame_bgr, encode_params)
            if success:
                return jpeg.tobytes()
        except Exception:
            pass

    if _pil_available:
        # Convert BGR to RGB for Pillow
        rgb = frame_bgr[:, :, ::-1] if frame_bgr.ndim == 3 and frame_bgr.shape[2] == 3 else frame_bgr
        pil_img = Image.fromarray(rgb)
        buf = io.BytesIO()
        pil_img.save(buf, format="JPEG", quality=quality)
        return buf.getvalue()

    raise RuntimeError("Neither OpenCV nor Pillow is available for JPEG encoding.")


# ── Global Server State ───────────────────────────────────────────────────────
latest_frame_bgr: Optional[np.ndarray] = None
latest_frame_time: float = 0.0
frame_counter: int = 0
frame_lock = threading.Lock()

# User configurable runtime settings
app_settings = {
    "mirror": False,
    "show_hud": True,
    "quality": 80,
}


# ── Camera Feed Interface ────────────────────────────────────────────────────
class PiCameraXRGBFeed:
    """Handles camera capture using Picamera2 with XRGB8888 format,

    with automatic fallback for development on non-Pi platforms.
    """

    def __init__(self, width: int = 640, height: int = 480, fps: int = 30) -> None:
        self.width = width
        self.height = height
        self.fps = fps
        self.use_picam = False
        self.picam2 = None
        self.cap = None
        self.backend_name = "Unknown"

        # 1. Attempt to initialize Picamera2 (First priority on robot)
        try:
            from picamera2 import Picamera2  # type: ignore

            print("[INFO] Initializing Picamera2 with XRGB8888 configuration...")
            self.picam2 = Picamera2()
            config = self.picam2.create_video_configuration(
                main={"format": "XRGB8888", "size": (self.width, self.height)},
                buffer_count=2,
                controls={"FrameRate": self.fps},
            )
            self.picam2.configure(config)
            self.use_picam = True
            self.backend_name = "Picamera2 (XRGB8888)"
            print(f"[INFO] Picamera2 successfully configured ({self.width}x{self.height} @ {self.fps}fps, format=XRGB8888)")
        except (ImportError, Exception) as e:
            print(f"[WARN] Picamera2 not available ({e}).")

            # 2. Attempt OpenCV VideoCapture fallback
            if _cv2_available and hasattr(cv2, "VideoCapture"):
                try:
                    self.cap = cv2.VideoCapture(0)
                    if self.cap.isOpened():
                        self.cap.set(cv2.CAP_PROP_FRAME_WIDTH, self.width)
                        self.cap.set(cv2.CAP_PROP_FRAME_HEIGHT, self.height)
                        self.cap.set(cv2.CAP_PROP_FPS, self.fps)
                        self.backend_name = "OpenCV VideoCapture"
                        print("[INFO] Using OpenCV webcam fallback.")
                except Exception:
                    self.cap = None

            # 3. Synthetic test pattern generator (Always works anywhere)
            if not self.use_picam and (self.cap is None or not self.cap.isOpened()):
                self.backend_name = "Synthetic XRGB Generator"
                print("[INFO] Using Synthetic XRGB test pattern generator.")

    def start(self) -> None:
        if self.use_picam and self.picam2 is not None:
            self.picam2.start()
            print("[INFO] Picamera2 video capture started.")

    def read(self) -> Tuple[bool, Optional[np.ndarray], Dict[str, Any]]:
        """Captures a frame and returns (success, bgr_image, metadata).

        In Picamera2 with format='XRGB8888', the buffer returned by capture_array()
        is represented in memory on ARM little-endian as B-G-R-X (4 channels).
        We convert it via xrgb_to_bgr to a standard 3-channel BGR frame.
        """
        meta = {
            "backend": self.backend_name,
            "format": "XRGB8888" if self.use_picam else "Simulated",
            "channels": 4 if self.use_picam else 3,
            "timestamp": time.time(),
        }

        if self.use_picam and self.picam2 is not None:
            try:
                # Raw capture array in XRGB8888 format (height, width, 4)
                frame_xrgb = self.picam2.capture_array()
                frame_bgr = xrgb_to_bgr(frame_xrgb)
                return True, frame_bgr, meta
            except Exception as ex:
                print(f"[ERROR] Picamera2 capture failed: {ex}")
                return False, None, meta

        elif self.cap is not None and self.cap.isOpened():
            ret, frame = self.cap.read()
            if ret and frame is not None:
                return True, frame, meta
            return False, None, meta

        else:
            # Generate simulated XRGB buffer
            synth_xrgb = np.zeros((self.height, self.width, 4), dtype=np.uint8)
            t = time.time()
            cx = int((np.sin(t * 2) * 0.35 + 0.5) * self.width)
            cy = int((np.cos(t * 2) * 0.35 + 0.5) * self.height)

            # Background color
            synth_xrgb[:, :, 0] = 40   # Blue
            synth_xrgb[:, :, 1] = 25   # Green
            synth_xrgb[:, :, 2] = 20   # Red
            synth_xrgb[:, :, 3] = 255  # Pad

            # Moving box/circle representation
            r = 35
            y1 = max(0, cy - r)
            y2 = min(self.height, cy + r)
            x1 = max(0, cx - r)
            x2 = min(self.width, cx + r)
            synth_xrgb[y1:y2, x1:x2, 0] = 0    # B
            synth_xrgb[y1:y2, x1:x2, 1] = 200  # G
            synth_xrgb[y1:y2, x1:x2, 2] = 255  # R

            frame_bgr = xrgb_to_bgr(synth_xrgb)
            time.sleep(1.0 / self.fps)
            return True, frame_bgr, meta

    def stop(self) -> None:
        if self.use_picam and self.picam2 is not None:
            try:
                self.picam2.stop()
            except Exception:
                pass
        if self.cap is not None and self.cap.isOpened():
            try:
                self.cap.release()
            except Exception:
                pass


# ── HUD Drawing Helper ────────────────────────────────────────────────────────
def apply_hud(
    frame: np.ndarray,
    fps: float,
    frame_num: int,
    backend: str,
    mirror: bool,
) -> np.ndarray:
    """Draws telemetry and camera metadata directly on the frame."""
    h, w = frame.shape[:2]

    # Draw header bar
    frame[:32, :, :] = (frame[:32, :, :].astype(np.uint16) // 2 + 15).astype(np.uint8)

    t_str = time.strftime("%H:%M:%S")
    info_text = f"PI CAM XRGB | {backend} | {fps:.1f} FPS | #{frame_num:05d} {t_str}"

    if _cv2_available and hasattr(cv2, "putText"):
        cv2.putText(frame, info_text, (10, 22), cv2.FONT_HERSHEY_SIMPLEX, 0.48, (0, 230, 255), 1, cv2.LINE_AA)
        if mirror:
            cv2.putText(frame, "[MIRROR ON]", (10, h - 12), cv2.FONT_HERSHEY_SIMPLEX, 0.45, (0, 165, 255), 1, cv2.LINE_AA)
        cx, cy = w // 2, h // 2
        cv2.line(frame, (cx - 15, cy), (cx + 15, cy), (0, 255, 255), 1)
        cv2.line(frame, (cx, cy - 15), (cx, cy + 15), (0, 255, 255), 1)
    elif _pil_available:
        # Fallback text rendering via PIL
        rgb = frame[:, :, ::-1]
        pil_img = Image.fromarray(rgb)
        draw = ImageDraw.Draw(pil_img)
        draw.text((10, 8), info_text, fill=(255, 230, 0))
        if mirror:
            draw.text((10, h - 20), "[MIRROR ON]", fill=(255, 165, 0))
        # Draw center crosshair
        cx, cy = w // 2, h // 2
        draw.line([(cx - 15, cy), (cx + 15, cy)], fill=(0, 255, 255), width=1)
        draw.line([(cx, cy - 15), (cx, cy + 15)], fill=(0, 255, 255), width=1)
        frame = np.array(pil_img)[:, :, ::-1]

    return frame


# ── HTTP Server & Request Handler ─────────────────────────────────────────────
class ThreadingHTTPServer(socketserver.ThreadingMixIn, HTTPServer):
    daemon_threads = True


class XRGBCameraHTTPHandler(BaseHTTPRequestHandler):
    """Serves the web dashboard, MJPEG live stream, and handles POST requests."""

    def _get_host_ip(self) -> str:
        host_ip = "localhost"
        try:
            s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
            s.connect(("8.8.8.8", 80))
            host_ip = s.getsockname()[0]
            s.close()
        except Exception:
            pass
        return host_ip

    def do_GET(self) -> None:
        global latest_frame_bgr, latest_frame_time, frame_counter, app_settings

        # 1. Main Web Page Dashboard
        if self.path in ("/", "/index.html"):
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.end_headers()

            host_ip = self._get_host_ip()
            port = self.server.server_address[1]

            html = f"""<!DOCTYPE html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1.0">
  <title>Pi Camera XRGB Live Monitor</title>
  <style>
    :root {{
      --bg: #0b0d14;
      --card-bg: #141724;
      --border: #23283e;
      --accent: #00d2ff;
      --accent-hover: #00a8cc;
      --text: #e2e8f0;
      --subtext: #8892b0;
      --success: #10b981;
    }}
    * {{ box-sizing: border-box; margin: 0; padding: 0; }}
    body {{
      background: var(--bg);
      color: var(--text);
      font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, Oxygen, Ubuntu, Cantarell, sans-serif;
      display: flex;
      flex-direction: column;
      align-items: center;
      min-height: 100vh;
      padding: 24px 16px;
    }}
    .wrapper {{
      max-width: 900px;
      width: 100%;
      background: var(--card-bg);
      border: 1px solid var(--border);
      border-radius: 16px;
      padding: 24px;
      box-shadow: 0 12px 36px rgba(0,0,0,0.45);
    }}
    header {{
      display: flex;
      justify-content: space-between;
      align-items: center;
      margin-bottom: 20px;
      padding-bottom: 16px;
      border-bottom: 1px solid var(--border);
    }}
    .title-box h1 {{
      font-size: 1.5rem;
      color: var(--accent);
      letter-spacing: 0.5px;
    }}
    .title-box p {{
      color: var(--subtext);
      font-size: 0.85rem;
      margin-top: 4px;
    }}
    .badge {{
      background: rgba(16, 185, 129, 0.15);
      border: 1px solid var(--success);
      color: var(--success);
      padding: 4px 12px;
      border-radius: 20px;
      font-size: 0.75rem;
      font-weight: 600;
      display: inline-flex;
      align-items: center;
      gap: 6px;
    }}
    .badge::before {{
      content: "";
      width: 8px;
      height: 8px;
      background: var(--success);
      border-radius: 50%;
      animation: pulse 1.8s infinite;
    }}
    @keyframes pulse {{
      0%, 100% {{ transform: scale(0.9); opacity: 0.6; }}
      50% {{ transform: scale(1.2); opacity: 1; }}
    }}
    .video-viewport {{
      position: relative;
      background: #000;
      border-radius: 12px;
      overflow: hidden;
      border: 2px solid var(--border);
      display: flex;
      justify-content: center;
      align-items: center;
      min-height: 380px;
      box-shadow: inset 0 0 20px rgba(0,0,0,0.8);
    }}
    .video-viewport img {{
      width: 100%;
      height: auto;
      display: block;
      object-fit: contain;
    }}
    .controls {{
      margin-top: 20px;
      display: grid;
      grid-template-columns: repeat(auto-fit, minmax(180px, 1fr));
      gap: 12px;
    }}
    button, a.btn {{
      background: #1b2034;
      color: var(--text);
      border: 1px solid var(--border);
      border-radius: 8px;
      padding: 10px 16px;
      font-size: 0.88rem;
      font-weight: 600;
      cursor: pointer;
      text-decoration: none;
      display: inline-flex;
      align-items: center;
      justify-content: center;
      gap: 8px;
      transition: all 0.2s ease;
    }}
    button:hover, a.btn:hover {{
      background: var(--accent);
      color: #000;
      border-color: var(--accent);
    }}
    .info-panel {{
      margin-top: 20px;
      background: #0e111a;
      border: 1px solid var(--border);
      border-radius: 10px;
      padding: 16px;
      font-size: 0.85rem;
    }}
    .info-grid {{
      display: grid;
      grid-template-columns: repeat(auto-fit, minmax(200px, 1fr));
      gap: 12px;
      margin-top: 10px;
    }}
    .info-item span:first-child {{
      color: var(--subtext);
      display: block;
      margin-bottom: 2px;
      font-size: 0.78rem;
      text-transform: uppercase;
      letter-spacing: 0.5px;
    }}
    .info-item span:last-child {{
      color: var(--text);
      font-family: ui-monospace, Menlo, Monaco, monospace;
      font-weight: 600;
    }}
    .post-log {{
      margin-top: 16px;
      padding: 12px;
      background: #07090e;
      border: 1px solid #1c2136;
      border-radius: 8px;
      font-family: ui-monospace, monospace;
      font-size: 0.8rem;
      color: #7dd3fc;
      max-height: 120px;
      overflow-y: auto;
    }}
  </style>
</head>
<body>
  <div class="wrapper">
    <header>
      <div class="title-box">
        <h1>Pi Camera Live Stream</h1>
        <p>Native format: <strong>XRGB8888</strong> &bull; Raspberry Pi Robot Feed</p>
      </div>
      <div class="badge">STREAM ACTIVE</div>
    </header>

    <div class="video-viewport">
      <img id="liveStream" src="/stream" alt="Live Pi Camera Stream">
    </div>

    <div class="controls">
      <button onclick="sendPostSnapshot()">POST Snapshot Test</button>
      <button onclick="toggleSetting('mirror')">Toggle Mirror</button>
      <button onclick="toggleSetting('show_hud')">Toggle HUD</button>
      <a class="btn" href="/snapshot" target="_blank" download="pi_camera_snapshot.jpg">Download JPEG</a>
    </div>

    <div class="info-panel">
      <strong>Stream Endpoints & Network Metadata</strong>
      <div class="info-grid">
        <div class="info-item">
          <span>Web URL</span>
          <span>http://{host_ip}:{port}/</span>
        </div>
        <div class="info-item">
          <span>Direct MJPEG</span>
          <span>http://{host_ip}:{port}/stream</span>
        </div>
        <div class="info-item">
          <span>Direct Snapshot</span>
          <span>http://{host_ip}:{port}/snapshot</span>
        </div>
        <div class="info-item">
          <span>POST API</span>
          <span>POST /api/snapshot</span>
        </div>
      </div>

      <div style="margin-top: 14px;"><strong>POST Interaction Console:</strong></div>
      <div id="postLog" class="post-log">Ready. Click 'POST Snapshot Test' or submit a command.</div>
    </div>
  </div>

  <script>
    function logMessage(msg) {{
      const log = document.getElementById("postLog");
      const time = new Date().toLocaleTimeString();
      log.innerHTML = `[${{time}}] ${{msg}}<br>` + log.innerHTML;
    }}

    async function sendPostSnapshot() {{
      logMessage("Sending POST request to /api/snapshot...");
      try {{
        const res = await fetch("/api/snapshot", {{
          method: "POST",
          headers: {{ "Content-Type": "application/json" }},
          body: JSON.stringify({{ request_time: Date.now(), client: "Browser Dashboard" }})
        }});
        const data = await res.json();
        logMessage(`POST SUCCESS: Snapshot (Frame #${{data.frame_number}}, ${{data.width}}x${{data.height}}, ${{data.size_bytes}} bytes)`);
      }} catch (err) {{
        logMessage(`POST ERROR: ${{err.message}}`);
      }}
    }}

    async function toggleSetting(key) {{
      logMessage(`Sending POST to toggle setting '${{key}}'...`);
      try {{
        const res = await fetch("/api/settings", {{
          method: "POST",
          headers: {{ "Content-Type": "application/json" }},
          body: JSON.stringify({{ toggle: key }})
        }});
        const data = await res.json();
        logMessage(`POST SUCCESS: Settings updated: ${{JSON.stringify(data.settings)}}`);
      }} catch (err) {{
        logMessage(`POST ERROR: ${{err.message}}`);
      }}
    }}
  </script>
</body>
</html>"""
            self.wfile.write(html.encode("utf-8"))
            return

        # 2. Continuous MJPEG Stream
        elif self.path == "/stream":
            self.send_response(200)
            self.send_header("Age", "0")
            self.send_header("Cache-Control", "no-cache, private, no-store, must-revalidate")
            self.send_header("Pragma", "no-cache")
            self.send_header("Content-Type", "multipart/x-mixed-replace; boundary=frame")
            self.end_headers()

            try:
                while True:
                    with frame_lock:
                        frame = None if latest_frame_bgr is None else latest_frame_bgr.copy()

                    if frame is None:
                        time.sleep(0.02)
                        continue

                    # JPEG Encode
                    quality = app_settings.get("quality", 80)
                    try:
                        jpg_bytes = encode_jpeg(frame, quality=quality)
                    except Exception:
                        time.sleep(0.02)
                        continue

                    self.wfile.write(b"--frame\r\n")
                    self.wfile.write(b"Content-Type: image/jpeg\r\n")
                    self.wfile.write(f"Content-Length: {len(jpg_bytes)}\r\n\r\n".encode("utf-8"))
                    self.wfile.write(jpg_bytes)
                    self.wfile.write(b"\r\n")

                    # Target ~30 FPS output
                    time.sleep(0.033)
            except (BrokenPipeError, ConnectionResetError):
                return
            except Exception:
                return

        # 3. Single Snapshot Endpoint
        elif self.path == "/snapshot":
            with frame_lock:
                frame = None if latest_frame_bgr is None else latest_frame_bgr.copy()

            if frame is None:
                self.send_error(503, "Camera frame not available yet")
                return

            quality = app_settings.get("quality", 90)
            jpg_bytes = encode_jpeg(frame, quality=quality)

            self.send_response(200)
            self.send_header("Content-Type", "image/jpeg")
            self.send_header("Content-Length", str(len(jpg_bytes)))
            self.send_header("Cache-Control", "no-cache")
            self.end_headers()
            self.wfile.write(jpg_bytes)
            return

        else:
            self.send_error(404, "Not Found")

    def do_POST(self) -> None:
        """Handles POST requests from web browser or test automation scripts."""
        global latest_frame_bgr, frame_counter, app_settings

        content_length = int(self.headers.get("Content-Length", 0))
        body = self.rfile.read(content_length) if content_length > 0 else b"{}"

        try:
            req_data = json.loads(body.decode("utf-8")) if body else {}
        except Exception:
            req_data = {}

        # POST /api/snapshot: Triggers and returns snapshot metadata
        if self.path == "/api/snapshot":
            with frame_lock:
                frame = None if latest_frame_bgr is None else latest_frame_bgr.copy()
                curr_cnt = frame_counter

            if frame is None:
                self.send_response(503)
                self.send_header("Content-Type", "application/json")
                self.end_headers()
                self.wfile.write(json.dumps({"error": "No frame available"}).encode("utf-8"))
                return

            h, w = frame.shape[:2]
            jpg_bytes = encode_jpeg(frame, quality=85)

            resp_payload = {
                "status": "success",
                "message": "Snapshot captured from XRGB Pi Camera feed",
                "frame_number": curr_cnt,
                "width": w,
                "height": h,
                "size_bytes": len(jpg_bytes),
                "received_payload": req_data,
                "timestamp": time.time(),
            }

            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            self.wfile.write(json.dumps(resp_payload).encode("utf-8"))
            return

        # POST /api/settings: Update runtime camera/display settings
        elif self.path == "/api/settings":
            toggle_key = req_data.get("toggle")
            if toggle_key in app_settings and isinstance(app_settings[toggle_key], bool):
                app_settings[toggle_key] = not app_settings[toggle_key]

            for key, val in req_data.items():
                if key in app_settings and key != "toggle":
                    app_settings[key] = val

            resp_payload = {
                "status": "success",
                "settings": app_settings,
                "timestamp": time.time(),
            }

            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            self.wfile.write(json.dumps(resp_payload).encode("utf-8"))
            return

        else:
            self.send_error(404, "Endpoint not found")

    def log_message(self, format: str, *args: Any) -> None:
        """Suppress default HTTP request clutter in console."""
        pass


# ── Main Video Capture Loop ──────────────────────────────────────────────────
def capture_loop(camera: PiCameraXRGBFeed, stop_event: threading.Event) -> None:
    global latest_frame_bgr, latest_frame_time, frame_counter, app_settings

    fps_tracker = 0.0
    last_tick = time.time()

    while not stop_event.is_set():
        ret, frame, meta = camera.read()
        if not ret or frame is None:
            time.sleep(0.01)
            continue

        now = time.time()
        dt = now - last_tick
        last_tick = now
        if dt > 0:
            inst_fps = 1.0 / dt
            fps_tracker = 0.9 * fps_tracker + 0.1 * inst_fps if fps_tracker > 0 else inst_fps

        frame_counter += 1

        # Apply runtime settings (mirror, HUD)
        if app_settings.get("mirror", False):
            frame = np.ascontiguousarray(frame[:, ::-1, :])

        if app_settings.get("show_hud", True):
            frame = apply_hud(
                frame=frame,
                fps=fps_tracker,
                frame_num=frame_counter,
                backend=meta.get("backend", "XRGB"),
                mirror=app_settings.get("mirror", False),
            )

        with frame_lock:
            latest_frame_bgr = frame
            latest_frame_time = now


# ── Entry Point ───────────────────────────────────────────────────────────────
def main() -> None:
    parser = argparse.ArgumentParser(description="Pi Camera XRGB Web Browser Stream Test")
    parser.add_argument("--host", default="0.0.0.0", help="HTTP Server Host (default: 0.0.0.0)")
    parser.add_argument("--port", type=int, default=8000, help="HTTP Server Port (default: 8000)")
    parser.add_argument("--width", type=int, default=640, help="Frame width (default: 640)")
    parser.add_argument("--height", type=int, default=480, help="Frame height (default: 480)")
    parser.add_argument("--fps", type=int, default=30, help="Camera framerate (default: 30)")
    parser.add_argument("--mirror", action="store_true", help="Mirror/flip video horizontally")
    parser.add_argument("--no-hud", action="store_true", help="Disable on-screen HUD")
    args = parser.parse_args()

    app_settings["mirror"] = args.mirror
    app_settings["show_hud"] = not args.no_hud

    print("=" * 72)
    print("[TEST] PI CAMERA XRGB WEB BROWSER STREAM TEST")
    print("=" * 72)

    # 1. Initialize Camera
    camera = PiCameraXRGBFeed(width=args.width, height=args.height, fps=args.fps)
    camera.start()

    # 2. Start Capture Worker Thread
    stop_event = threading.Event()
    worker = threading.Thread(target=capture_loop, args=(camera, stop_event), daemon=True)
    worker.start()

    # 3. Start Web Server
    server = ThreadingHTTPServer((args.host, args.port), XRGBCameraHTTPHandler)
    server_thread = threading.Thread(target=server.serve_forever, daemon=True)
    server_thread.start()

    # Determine display host IP
    host_ip = "localhost"
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.connect(("8.8.8.8", 80))
        host_ip = s.getsockname()[0]
        s.close()
    except Exception:
        pass

    print("\n[READY] Server is LIVE!")
    print(f"  * Web Browser Dashboard: http://{host_ip}:{args.port}/")
    print(f"  * Localhost Dashboard:    http://localhost:{args.port}/")
    print(f"  * Direct Video Stream:    http://{host_ip}:{args.port}/stream")
    print(f"  * Snapshot (GET/POST):    http://{host_ip}:{args.port}/snapshot")
    print(f"  * Test POST API:          http://{host_ip}:{args.port}/api/snapshot")
    print("\nPress Ctrl+C to exit.\n" + "=" * 72)

    try:
        while True:
            time.sleep(1.0)
    except KeyboardInterrupt:
        print("\n[INFO] Stopping camera server...")
    finally:
        stop_event.set()
        camera.stop()
        server.shutdown()
        print("[INFO] Done.")


if __name__ == "__main__":
    main()
