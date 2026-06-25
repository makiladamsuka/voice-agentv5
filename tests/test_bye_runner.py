#!/usr/bin/env python3
"""
Bye Wave Runner — Real-time robot behaviour test.

Extends the hand-detection infrastructure to trigger a physical "bye wave" arm
sequence whenever a person waves at the robot.  Detection works anywhere in the
camera frame (no height boundary).  When a waving pattern of at least 4
reversals/swings and a sweep amplitude of at least 40 pixels is measured, the
robot picks one of the pre-defined bye animations (``bye1``, ``bye2``, or
``bye3``) from the ``"animations"`` section of ``tests/arm_pose_presets.json``
and plays back its frames in order, then enters a 10-second cooldown before it
can trigger again.

All visual HUD, MJPEG streaming, and skeleton-overlay features from
``test_hand_detection.py`` are retained.

How to run::

    python tests/test_bye_runner.py --port 8000

How to watch::

    Open a web browser on any computer on the same network and navigate to:
    http://<pi-ip-address>:8000/
"""

from __future__ import annotations

import _bootstrap  # noqa: F401
import argparse
import collections
from http.server import BaseHTTPRequestHandler, HTTPServer
import io
import json
import os
import pathlib
import random
import socket
import socketserver
import sys
import threading
import time

# ── Headless-safe OpenCV import ───────────────────────────────────────────────
# On a Raspberry Pi (or any machine without an active X/Wayland session) there
# is no display server, so OpenCV's Qt backend will abort when cv2.imshow() is
# called.  Detect this early and force a no-window mode so the script runs
# purely via the HTTP MJPEG stream.
_has_display = bool(
    os.environ.get("DISPLAY") or os.environ.get("WAYLAND_DISPLAY")
)
if not _has_display:
    # Prevent OpenCV from even trying to load Qt/xcb
    os.environ.setdefault("OPENCV_VIDEOIO_PRIORITY_MSMF", "0")
    os.environ["QT_QPA_PLATFORM"] = "offscreen"

import cv2  # noqa: E402  (must come after env-var setup)

from lib.hand_detector import HandDetector, HandDetection, draw_skeleton, draw_motion_trail

# ── Streaming Server State ────────────────────────────────────────────────────
latest_frame = None
frame_lock = threading.Lock()


# ── Camera Feed ───────────────────────────────────────────────────────────────

class CameraFeed:
    """Manages the video source using Picamera2."""
    
    def __init__(self):
        from picamera2 import Picamera2  # type: ignore
        print("[INFO] Camera Interface: Picamera2")
        self.picam2 = Picamera2()
        config = self.picam2.create_video_configuration(
            main={"format": "XRGB8888", "size": (640, 480)},
            buffer_count=2,
        )
        self.picam2.configure(config)

    def start(self) -> None:
        self.picam2.start()

    def read(self) -> tuple[bool, cv2.Mat | None]:
        try:
            frame_xrgb = self.picam2.capture_array()
            # XRGB8888 is stored as B,G,R,X in memory on ARM little-endian.
            # COLOR_BGRA2BGR drops the X pad byte, giving a clean BGR frame.
            frame_bgr = cv2.cvtColor(frame_xrgb, cv2.COLOR_BGRA2BGR)
            return True, frame_bgr
        except Exception as e:
            print(f"[ERROR] Picamera2 frame capture failed: {e}")
            return False, None

    def stop(self) -> None:
        try:
            self.picam2.stop()
        except Exception:
            pass


# ── Threading HTTP Server ─────────────────────────────────────────────────────

class ThreadingHTTPServer(socketserver.ThreadingMixIn, HTTPServer):
    """HTTP server that handles each request in a separate daemon thread."""
    daemon_threads = True


# ── WavingDetector subclass ───────────────────────────────────────────────────

from typing import Callable
from tests.test_hand_detection import WavingDetector as _ParentWavingDetector


class WavingDetector(_ParentWavingDetector):
    """
    Subclass of the parent WavingDetector that removes the height-gate entirely
    and adds a ``wave_callback`` for bye-wave event dispatch.

    Changes vs. parent:
    - No ``above_limit`` key in hand_states (no height boundary).
    - Default ``cooldown_sec`` raised to 10.0 s.
    - Accepts a ``wave_callback(side: str)`` callable.
    - ``cooldown_until`` is initialised to 0.0 here; it is set externally by
      ``ByeSequenceRunner.on_complete`` after the animation finishes.
    """

    def __init__(
        self,
        history_len: int = 25,
        dead_zone_px: int = 10,
        cooldown_sec: float = 10.0,
        wave_callback: Callable[[str], None] | None = None,
    ) -> None:
        # Initialise parent fields (history_len, dead_zone_px, cooldown_sec,
        # cooldown_until, announcement_end_time, announcement_hand, hand_states).
        super().__init__(
            history_len=history_len,
            dead_zone_px=dead_zone_px,
            cooldown_sec=cooldown_sec,
        )

        # Store the bye-wave callback.
        self.wave_callback = wave_callback

        # Ensure cooldown_until is reset (parent already sets it to 0.0, but
        # be explicit for clarity).
        self.cooldown_until = 0.0

        # Remove the ``above_limit`` key from each side's state dict — this
        # subclass has no height boundary.
        for state in self.hand_states.values():
            state.pop("above_limit", None)

    def process_detection(self, detections: list[HandDetection], now: float) -> list[str]:
        """Updates internal deques and returns list of sides currently waving.

        Overrides parent to remove the ``limit_y`` height-gate entirely.  Every
        frontside palm contributes to the history regardless of y-coordinate.
        Trigger criterion is horizontal-axis only: rev_x >= 4 AND amp_x >= 40 px.
        ``cooldown_until`` is NOT set here — it is set externally by
        ``ByeSequenceRunner.on_complete`` after the animation finishes.
        """
        # Timeout unobserved hands (older than 0.4 seconds)
        for side in ["Left", "Right"]:
            if now - self.hand_states[side]["last_seen"] > 0.4:
                self.hand_states[side]["x_history"].clear()
                self.hand_states[side]["y_history"].clear()
                self.hand_states[side]["is_waving"] = False
                self.hand_states[side]["reversals"] = 0
                self.hand_states[side]["amplitude"] = 0.0
                self.hand_states[side]["intensity"] = 0.0

        waving_hands: list[str] = []

        for hand in detections:
            side = hand.physical_side
            state = self.hand_states[side]
            state["last_seen"] = now

            palm_x, palm_y = hand.palm_center

            # Accumulate history for every frontside palm — no height gate
            if hand.is_frontside:
                state["x_history"].append(palm_x)
                state["y_history"].append(palm_y)
            else:
                state["x_history"].clear()
                state["y_history"].clear()

            # Horizontal-axis reversal only
            rev_x, amp_x = self.detect_reversals(list(state["x_history"]))

            best_rev = rev_x
            best_amp = amp_x

            state["reversals"] = best_rev
            state["amplitude"] = best_amp

            # is_waving: horizontal criterion only, no height gate
            is_waving = rev_x >= 4 and amp_x >= 40.0
            state["is_waving"] = is_waving

            # Intensity gauge (0.0 – 1.0), lower normaliser (4 swings / 160 px)
            state["intensity"] = (
                min(1.0, best_amp / 160.0) * min(1.0, best_rev / 4.0)
            )

            # Trigger bye-wave event: 4 swings, 40 px sweep, not in cooldown
            if rev_x >= 4 and amp_x >= 40.0 and now > self.cooldown_until:
                # Update announcement state (mirrored from parent pattern)
                self.announcement_end_time = now + 3.0
                self.announcement_hand = side
                # Dispatch callback — cooldown_until is set by ByeSequenceRunner.on_complete
                if self.wave_callback is not None:
                    self.wave_callback(side)

            if is_waving:
                waving_hands.append(side)

        return waving_hands


# ── Bye Sequence Runner ───────────────────────────────────────────────────────

class ByeSequenceRunner:
    """Runs a bye-wave arm animation on a dedicated background daemon thread.

    Only one animation may be active at a time; surplus trigger calls during an
    active animation are silently discarded (re-entrancy guard via ``_lock``).

    Parameters
    ----------
    bb:
        A ``Blackboard`` instance used to publish arm poses, or ``None`` when
        arm motion is disabled (e.g. Blackboard unavailable at startup).
    presets_path:
        ``pathlib.Path`` pointing to ``tests/arm_pose_presets.json``.  The file
        is loaded fresh on each ``trigger()`` call so in-place edits take effect
        without restarting the process.
    on_complete:
        Optional zero-argument callable invoked after the last animation frame
        completes.  The main script uses this to set
        ``WavingDetector.cooldown_until``.
    """

    def __init__(
        self,
        bb,
        presets_path: pathlib.Path,
        on_complete=None,
    ) -> None:
        self._lock: threading.Lock = threading.Lock()
        self._thread: threading.Thread | None = None
        self._running: bool = False
        self._bb = bb
        self._presets_path: pathlib.Path = presets_path
        self._on_complete = on_complete

    # ------------------------------------------------------------------
    # Public interface
    # ------------------------------------------------------------------

    @property
    def is_running(self) -> bool:
        """``True`` while a bye animation is executing on the background thread."""
        return self._running

    def trigger(self, side: str) -> None:
        """Start the bye animation unless one is already running.

        Steps:
        1. Acquire ``_lock``; if ``_running``, return immediately (discard event).
        2. Load presets JSON; extract ``"animations"`` dict; pick one of
           ``["bye1","bye2","bye3"]`` at random.
        3. On any file / parse / key error: print to stderr, release lock, return.
        4. Set ``_running = True``; spawn daemon thread targeting
           ``_run_animation(frames)``; release lock.
        """
        with self._lock:
            if self._running:
                return

            # Step 2 – load and validate the presets file
            try:
                data = json.loads(self._presets_path.read_text())
                animations = data["animations"]
                key = random.choice(["bye1", "bye2", "bye3"])
                frames = animations[key]["frames"]
                if not frames:
                    raise ValueError(f"Animation '{key}' has an empty frames list")
            except FileNotFoundError as exc:
                print(
                    f"[ERROR] ByeSequenceRunner: presets file not found: {exc}",
                    file=sys.stderr,
                )
                return
            except json.JSONDecodeError as exc:
                print(
                    f"[ERROR] ByeSequenceRunner: JSON parse error in presets: {exc}",
                    file=sys.stderr,
                )
                return
            except KeyError as exc:
                print(
                    f"[ERROR] ByeSequenceRunner: missing key in presets: {exc}",
                    file=sys.stderr,
                )
                return
            except Exception as exc:
                print(
                    f"[ERROR] ByeSequenceRunner: failed to load presets: {exc}",
                    file=sys.stderr,
                )
                return

            # Step 4 – set running flag and spawn daemon thread
            self._running = True
            self._thread = threading.Thread(
                target=self._run_animation,
                args=(frames,),
                daemon=True,
            )
            self._thread.start()
            # Lock is released here when the ``with`` block exits

    def _run_animation(self, frames: list[dict]) -> None:
        """Execute the animation on the background thread.

        For each frame dict, writes arm pose to the Blackboard (if available)
        then sleeps 0.25 s.  After the last frame, calls ``_on_complete`` if
        set, then clears the ``_running`` flag under ``_lock``.
        """
        for f in frames:
            if self._bb is not None:
                self._bb.write(
                    arm_a0=f["a0"],
                    arm_a1=f["a1"],
                    arm_a2=f["a2"],
                    arm_a3=f["a3"],
                )
            time.sleep(0.25)

        if self._on_complete is not None:
            self._on_complete()

        with self._lock:
            self._running = False

    def join(self, timeout: float = 2.0) -> None:
        """Block until the running animation finishes or *timeout* seconds elapse.

        Safe to call even when no animation has been started yet.
        """
        if self._thread is not None:
            self._thread.join(timeout=timeout)


# ── MJPEG Request Handler ─────────────────────────────────────────────────────

class MJPEGHandler(BaseHTTPRequestHandler):
    """HTTP Request Handler that serves a control landing page and MJPEG stream."""

    def do_GET(self):
        global latest_frame
        if self.path == "/":
            self.send_response(200)
            self.send_header("Content-Type", "text/html")
            self.end_headers()

            # Retrieve host IP address to make links clickable/clear
            host_ip = "localhost"
            try:
                s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
                # Doesn't need to connect, just gets local interface bound IP
                s.connect(("8.8.8.8", 80))
                host_ip = s.getsockname()[0]
                s.close()
            except Exception:
                pass

            port = self.server.server_address[1]
            html = f"""
            <!DOCTYPE html>
            <html>
            <head>
                <meta charset="utf-8">
                <title>AI Robot Hand Tracker Live Feed</title>
                <style>
                    body {{
                        background-color: #0f101a;
                        color: #f1f5f9;
                        font-family: 'Outfit', 'Inter', -apple-system, sans-serif;
                        text-align: center;
                        margin: 0;
                        padding: 40px 20px;
                    }}
                    .container {{
                        max-width: 840px;
                        margin: 0 auto;
                        background: #161826;
                        border-radius: 16px;
                        padding: 30px;
                        box-shadow: 0 10px 30px rgba(0, 0, 0, 0.6);
                        border: 1px solid #272d42;
                    }}
                    h1 {{
                        color: #ff6a00;
                        margin: 0 0 10px 0;
                        font-size: 2.2rem;
                        letter-spacing: 1px;
                        text-shadow: 0 0 20px rgba(255, 106, 0, 0.35);
                    }}
                    .subtitle {{
                        color: #94a3b8;
                        font-size: 1rem;
                        margin-bottom: 25px;
                    }}
                    .stream-box {{
                        position: relative;
                        display: inline-block;
                        border-radius: 12px;
                        overflow: hidden;
                        border: 2px solid #ff6a00;
                        box-shadow: 0 0 25px rgba(255, 106, 0, 0.2);
                        background: #000;
                    }}
                    img {{
                        display: block;
                        max-width: 100%;
                        height: auto;
                    }}
                    .badge {{
                        position: absolute;
                        top: 15px;
                        left: 15px;
                        background: #ef4444;
                        color: #fff;
                        padding: 5px 12px;
                        border-radius: 20px;
                        font-size: 0.75rem;
                        font-weight: bold;
                        letter-spacing: 1px;
                        animation: pulse 1.5s infinite;
                    }}
                    @keyframes pulse {{
                        0% {{ opacity: 0.6; }}
                        50% {{ opacity: 1; }}
                        100% {{ opacity: 0.6; }}
                    }}
                    .info-grid {{
                        display: grid;
                        grid-template-columns: 1fr 1fr;
                        gap: 20px;
                        margin-top: 30px;
                        text-align: left;
                    }}
                    .card {{
                        background: #1e2235;
                        padding: 20px;
                        border-radius: 10px;
                        border: 1px solid #2f3754;
                    }}
                    .card h3 {{
                        color: #00e5ff;
                        margin: 0 0 10px 0;
                        font-size: 1.1rem;
                    }}
                    .card p {{
                        margin: 5px 0;
                        font-size: 0.9rem;
                        color: #cbd5e1;
                        line-height: 1.5;
                    }}
                    code {{
                        background: #0a0b12;
                        padding: 3px 6px;
                        border-radius: 4px;
                        color: #ff6a00;
                        font-family: monospace;
                    }}
                </style>
            </head>
            <body>
                <div class="container">
                    <h1>AI ROBOT VISION FEED</h1>
                    <div class="subtitle">Real-time Hand Detection & Gesture Recognition</div>

                    <div class="stream-box">
                        <img src="/stream" alt="Robot Camera Feed" />
                        <div class="badge">REC LIVE</div>
                    </div>

                    <div class="info-grid">
                        <div class="card">
                            <h3>Camera Stream Info</h3>
                            <p>Direct Stream: <a href="/stream" style="color:#00e5ff;"><code>http://{host_ip}:{port}/stream</code></a></p>
                            <p>Resolution: 640 x 480 px</p>
                            <p>Frame Format: Motion JPEG (MJPEG)</p>
                        </div>
                        <div class="card">
                            <h3>Waving Instructions</h3>
                            <p>1. No height limit — wave anywhere in the frame.</p>
                            <p>2. Wave side-to-side to build up the intensity gauge.</p>
                            <p>3. 4 swings needed to trigger the bye event.</p>
                        </div>
                    </div>
                </div>
            </body>
            </html>
            """
            self.wfile.write(html.encode("utf-8"))
            return

        elif self.path == "/stream":
            self.send_response(200)
            self.send_header("Age", "0")
            self.send_header("Cache-Control", "no-cache, private")
            self.send_header("Pragma", "no-cache")
            self.send_header("Content-Type", "multipart/x-mixed-replace; boundary=frame")
            self.end_headers()
            try:
                while True:
                    with frame_lock:
                        frame = None if latest_frame is None else latest_frame.copy()
                    if frame is None:
                        time.sleep(0.03)
                        continue

                    _, encoded_img = cv2.imencode('.jpg', frame)
                    jpg = encoded_img.tobytes()

                    self.wfile.write(b"--frame\r\n")
                    self.wfile.write(b"Content-Type: image/jpeg\r\n")
                    self.wfile.write(f"Content-Length: {len(jpg)}\r\n\r\n".encode("utf-8"))
                    self.wfile.write(jpg)
                    self.wfile.write(b"\r\n")
                    # Yield CPU time (target ~25-30 FPS streaming limit)
                    time.sleep(0.04)
            except (BrokenPipeError, ConnectionResetError):
                return
            except Exception:
                return
        else:
            self.send_error(404)

    def log_message(self, format, *args):
        # Suppress logging network requests to CLI
        pass


# ── HUD Gauge ─────────────────────────────────────────────────────────────────

def draw_hud_gauge(frame: cv2.Mat, top_left: tuple[int, int], width: int, score: float) -> None:
    """Draw a status progress bar for wave intensity."""
    x, y = top_left
    height = 10
    cv2.rectangle(frame, (x, y), (x + width, y + height), (40, 40, 50), -1, cv2.LINE_AA)
    
    fill = int(width * score)
    if fill > 0:
        color = (
            int(255 * score),
            int(255 * (1.0 - score * 0.2)),
            int(100 * (1.0 - score))
        )
        cv2.rectangle(frame, (x, y), (x + fill, y + height), color, -1, cv2.LINE_AA)
        
    cv2.rectangle(frame, (x, y), (x + width, y + height), (120, 120, 140), 1, cv2.LINE_AA)


# ── HUD Renderer ──────────────────────────────────────────────────────────────

def draw_hud(
    frame: cv2.Mat,
    detector_states: dict,
    announcement_hand: str,
    announcement_end_time: float,
    cooldown_until: float,
    fps: float,
    now: float,
) -> cv2.Mat:
    """Renders the top panel, status tags, countdowns, and waving events.

    Drop-in replacement for the parent's draw_hud with the following changes:
    - No ``limit_y`` parameter (height boundary entirely removed).
    - No orange height-boundary line draw call.
    - No "HEIGHT LIMIT FOR WAVING DETECTOR" label.
    - "WAVE ANYWHERE" replaces "WAVE HAND ABOVE LINE".
    - No "Above limit: YES/NO" text.
    - Swing progress label uses ``N/4`` instead of ``N/8``.
    - Announcement text: "👋 BYE WAVE! (Left)" / "👋 BYE WAVE! (Right)".
    """
    h, w, _ = frame.shape
    overlay = frame.copy()

    # A. Header Bar Glassmorphic backing
    cv2.rectangle(overlay, (0, 0), (w, 80), (15, 15, 20), -1)
    cv2.line(overlay, (0, 80), (w, 80), (255, 106, 0), 2, cv2.LINE_AA)
    cv2.addWeighted(overlay, 0.7, frame, 0.3, 0, frame)

    # Header Titles
    cv2.putText(frame, "AI HAND DETECTOR", (20, 32), cv2.FONT_HERSHEY_DUPLEX, 0.75, (255, 255, 255), 2, cv2.LINE_AA)
    cv2.putText(frame, "MediaPipe Hands Remote stream client", (20, 55), cv2.FONT_HERSHEY_SIMPLEX, 0.45, (160, 160, 180), 1, cv2.LINE_AA)
    cv2.putText(frame, f"FPS: {fps:.1f}", (w - 130, 45), cv2.FONT_HERSHEY_DUPLEX, 0.6, (0, 255, 120), 2, cv2.LINE_AA)

    # B. Height boundary line REMOVED — no limit_y in bye-wave runner

    # C. Hand Status Side Panels (Bottom Corners)
    hud_overlay = frame.copy()

    # Left Hand Box
    cv2.rectangle(hud_overlay, (15, h - 135), (245, h - 15), (20, 20, 30), -1)
    cv2.rectangle(hud_overlay, (15, h - 135), (245, h - 15), (255, 106, 0), 1, cv2.LINE_AA)

    # Right Hand Box
    cv2.rectangle(hud_overlay, (w - 245, h - 135), (w - 15, h - 15), (20, 20, 30), -1)
    cv2.rectangle(hud_overlay, (w - 245, h - 135), (w - 15, h - 15), (255, 106, 0), 1, cv2.LINE_AA)

    cv2.addWeighted(hud_overlay, 0.8, frame, 0.2, 0, frame)

    # Render Left Hand Details
    left_state = detector_states["Left"]
    left_seen = (now - left_state["last_seen"]) < 0.4
    cv2.putText(frame, "LEFT HAND", (25, h - 110), cv2.FONT_HERSHEY_DUPLEX, 0.55, (255, 255, 255), 2, cv2.LINE_AA)

    if left_seen:
        lbl = "WAVE ANYWHERE"
        col = (0, 255, 150)
        if now < cooldown_until:
            lbl = "COOLDOWN... ⏳"
            col = (0, 165, 255)
        elif left_state["reversals"] >= 4:
            lbl = "TRIGGERED! 👋"
            col = (255, 255, 0)
        elif left_state["reversals"] >= 3:
            lbl = f"SWINGS: {left_state['reversals']}/4"
            col = (0, 255, 255)

        cv2.putText(frame, lbl, (25, h - 88), cv2.FONT_HERSHEY_DUPLEX, 0.45, col, 1, cv2.LINE_AA)
        cv2.putText(frame, f"Swings: {left_state['reversals']} | Sweep: {left_state['amplitude']:.0f}px", (25, h - 48), cv2.FONT_HERSHEY_SIMPLEX, 0.4, (200, 200, 200), 1, cv2.LINE_AA)
        draw_hud_gauge(frame, (25, h - 30), 210, left_state["intensity"])
    else:
        cv2.putText(frame, "OUT OF FRAME", (25, h - 80), cv2.FONT_HERSHEY_SIMPLEX, 0.5, (100, 100, 110), 1, cv2.LINE_AA)
        draw_hud_gauge(frame, (25, h - 30), 210, 0.0)

    # Render Right Hand Details
    right_state = detector_states["Right"]
    right_seen = (now - right_state["last_seen"]) < 0.4
    cv2.putText(frame, "RIGHT HAND", (w - 235, h - 110), cv2.FONT_HERSHEY_DUPLEX, 0.55, (255, 255, 255), 2, cv2.LINE_AA)

    if right_seen:
        lbl = "WAVE ANYWHERE"
        col = (0, 255, 150)
        if now < cooldown_until:
            lbl = "COOLDOWN... ⏳"
            col = (0, 165, 255)
        elif right_state["reversals"] >= 4:
            lbl = "TRIGGERED! 👋"
            col = (255, 255, 0)
        elif right_state["reversals"] >= 3:
            lbl = f"SWINGS: {right_state['reversals']}/4"
            col = (0, 255, 255)

        cv2.putText(frame, lbl, (w - 235, h - 88), cv2.FONT_HERSHEY_DUPLEX, 0.45, col, 1, cv2.LINE_AA)
        cv2.putText(frame, f"Swings: {right_state['reversals']} | Sweep: {right_state['amplitude']:.0f}px", (w - 235, h - 48), cv2.FONT_HERSHEY_SIMPLEX, 0.4, (200, 200, 200), 1, cv2.LINE_AA)
        draw_hud_gauge(frame, (w - 235, h - 30), 210, right_state["intensity"])
    else:
        cv2.putText(frame, "OUT OF FRAME", (w - 235, h - 80), cv2.FONT_HERSHEY_SIMPLEX, 0.5, (100, 100, 110), 1, cv2.LINE_AA)
        draw_hud_gauge(frame, (w - 235, h - 30), 210, 0.0)

    # D. Giant Center Event Announcement
    if now < announcement_end_time:
        event_overlay = frame.copy()
        box_w, box_h = 420, 80
        bx = (w - box_w) // 2
        by = (h - box_h) // 2
        cv2.rectangle(event_overlay, (bx, by), (bx + box_w, by + box_h), (15, 10, 25), -1)
        cv2.rectangle(event_overlay, (bx, by), (bx + box_w, by + box_h), (255, 255, 0), 2, cv2.LINE_AA)
        cv2.addWeighted(event_overlay, 0.85, frame, 0.15, 0, frame)

        # Title-case side string: "👋 BYE WAVE! (Left)" or "👋 BYE WAVE! (Right)"
        msg = f"👋 BYE WAVE! ({announcement_hand.title()})"
        txt_size = cv2.getTextSize(msg, cv2.FONT_HERSHEY_DUPLEX, 0.8, 2)[0]
        tx = bx + (box_w - txt_size[0]) // 2
        ty = by + (box_h + txt_size[1]) // 2
        cv2.putText(frame, msg, (tx + 1, ty + 1), cv2.FONT_HERSHEY_DUPLEX, 0.8, (0, 0, 0), 3, cv2.LINE_AA)
        cv2.putText(frame, msg, (tx, ty), cv2.FONT_HERSHEY_DUPLEX, 0.8, (255, 255, 0), 2, cv2.LINE_AA)

    # E. Cooldown Countdown Pill
    rem = cooldown_until - now
    if rem > 0:
        pill_w, pill_h = 200, 30
        px = (w - pill_w) // 2
        py = 95
        pill_overlay = frame.copy()
        cv2.rectangle(pill_overlay, (px, py), (px + pill_w, py + pill_h), (15, 10, 10), -1)
        cv2.rectangle(pill_overlay, (px, py), (px + pill_w, py + pill_h), (255, 106, 0), 1, cv2.LINE_AA)
        cv2.addWeighted(pill_overlay, 0.8, frame, 0.2, 0, frame)
        cv2.putText(frame, f"⏳ COOLDOWN: {rem:.1f}s", (px + 20, py + 20), cv2.FONT_HERSHEY_DUPLEX, 0.45, (255, 106, 0), 1, cv2.LINE_AA)

    return frame


# ── Entry Point ───────────────────────────────────────────────────────────────

def main() -> None:
    """Parse CLI arguments, validate them, and run the bye-wave runner.

    Remaining startup, camera loop, and shutdown steps are implemented in
    subsequent tasks (8.2 – 8.4).
    """
    parser = argparse.ArgumentParser(
        description="Bye Wave Runner — triggers a robot arm animation on wave detection.",
    )
    parser.add_argument(
        "--host",
        type=str,
        default="0.0.0.0",
        help="Host address for the MJPEG HTTP server (default: 0.0.0.0)",
    )
    parser.add_argument(
        "--port",
        type=int,
        default=8000,
        help="Port for the MJPEG HTTP server (default: 8000)",
    )
    parser.add_argument(
        "--mirror",
        type=bool,
        default=True,
        help="Mirror camera image horizontally (default: True)",
    )
    parser.add_argument(
        "--max-hands",
        type=int,
        default=2,
        dest="max_hands",
        help="Maximum number of hands to track (default: 2)",
    )
    parser.add_argument(
        "--no-window",
        action="store_true",
        help="Disable the local OpenCV display window",
    )
    parser.add_argument(
        "--cooldown",
        type=float,
        default=10.0,
        help="Cooldown in seconds after a bye wave fires (must be > 0, default: 10.0)",
    )

    args = parser.parse_args()

    # Validate cooldown immediately after parsing
    if args.cooldown <= 0.0:
        parser.error("--cooldown must be greater than 0.0")

    # Auto-disable window when there is no display server available
    if not _has_display:
        args.no_window = True
        print("[INFO] No display detected — running in headless mode (HTTP stream only).")

    # ── 8.2  Startup sequence ─────────────────────────────────────────────────

    # 1. Start MJPEG HTTP server
    print(f"[SERVER] Starting HTTP stream server on http://{args.host}:{args.port}/")
    server = ThreadingHTTPServer((args.host, args.port), MJPEGHandler)
    server_thread = threading.Thread(target=server.serve_forever, daemon=True)
    server_thread.start()

    # 2. Resolve local IP for the startup banner
    local_ip = "localhost"
    try:
        _s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        _s.connect(("8.8.8.8", 80))
        local_ip = _s.getsockname()[0]
        _s.close()
    except Exception:
        pass

    print("\n=======================================================")
    print("      [BYE RUNNER] MediaPipe Bye-Wave Detection Active")
    print("=======================================================")
    print(f"  • Local Watch URL:   http://localhost:{args.port}/")
    print(f"  • Network Watch URL: http://{local_ip}:{args.port}/")
    print(f"  • Active Camera:     Picamera2")
    print(f"  • Cooldown:          {args.cooldown:.1f}s")
    print(f"  • Trigger:           4 swings, 40 px sweep (no height limit)")
    print("  • Press Ctrl+C in console to stop safely.")
    print("=======================================================\n")

    # 3. Blackboard + arm controller (gracefully optional)
    bb = None
    home_pose = None
    try:
        from core.blackboard import Blackboard
        bb = Blackboard()
        # Load home pose from presets JSON directly (no full ArmController boot needed)
        _presets_path = pathlib.Path(__file__).resolve().parent / "arm_pose_presets.json"
        _presets_data = json.loads(_presets_path.read_text())
        _hp = _presets_data["poses"]["home"]
        home_pose = (_hp["a0"], _hp["a1"], _hp["a2"], _hp["a3"])
        # Publish home immediately so servos start in a known position
        bb.write(arm_a0=home_pose[0], arm_a1=home_pose[1],
                 arm_a2=home_pose[2], arm_a3=home_pose[3])
        print("[INFO] Blackboard initialised — arm motion enabled.")
    except Exception as exc:
        print(f"[WARNING] Arm/Blackboard unavailable ({exc}); arm motion disabled for this session.",
              file=sys.stderr)
        bb = None
        home_pose = None

    # 4. Presets path for ByeSequenceRunner
    presets_path = pathlib.Path(__file__).resolve().parent / "arm_pose_presets.json"

    # 5. Wire up WavingDetector → ByeSequenceRunner with a cooldown callback
    bye_runner = ByeSequenceRunner(
        bb=bb,
        presets_path=presets_path,
        on_complete=None,  # patched below after waving_detector is created
    )

    def _on_bye_complete() -> None:
        """Called by ByeSequenceRunner after animation finishes; sets cooldown."""
        waving_detector.cooldown_until = time.time() + args.cooldown
        print(f"[INFO] Bye sequence done — cooldown active for {args.cooldown:.1f}s.")

    bye_runner._on_complete = _on_bye_complete

    def _wave_callback(side: str) -> None:
        print(f"[EVENT] 👋 BYE WAVE triggered by {side.upper()} hand!")
        bye_runner.trigger(side)

    waving_detector = WavingDetector(
        cooldown_sec=args.cooldown,
        wave_callback=_wave_callback,
    )

    # 6. Camera feed and hand detector
    camera = CameraFeed()
    camera.start()
    detector = HandDetector(max_num_hands=args.max_hands)

    prev_time = time.time()
    fps = 0.0

    # ── 8.3  Main camera loop ──────────────────────────────────────────────────
    try:
        while True:
            ret, frame = camera.read()
            if not ret or frame is None:
                time.sleep(0.01)
                continue

            # Mirror frame for natural/intuitive interaction
            if args.mirror:
                frame = cv2.flip(frame, 1)

            now = time.time()

            # Hand detection (frame is already visually flipped; pass mirrored=False)
            detections = detector.process(frame, mirrored=False)

            # Draw skeleton and motion trail for each detected hand
            for hand in detections:
                is_waving = waving_detector.hand_states[hand.physical_side]["is_waving"]
                draw_skeleton(frame, hand, is_active=is_waving)
                state = waving_detector.hand_states[hand.physical_side]
                draw_motion_trail(
                    frame,
                    list(state["x_history"]),
                    list(state["y_history"]),
                    is_active=is_waving,
                )

            # Update waving state (no limit_y — bye runner has no height gate)
            waving_detector.process_detection(detections, now)

            # FPS calculation
            fps_now = time.time()
            fps = 1.0 / max(fps_now - prev_time, 1e-6)
            prev_time = fps_now

            # Render HUD overlays
            frame = draw_hud(
                frame,
                waving_detector.hand_states,
                waving_detector.announcement_hand,
                waving_detector.announcement_end_time,
                waving_detector.cooldown_until,
                fps,
                now,
            )

            # Publish annotated frame to MJPEG stream
            global latest_frame
            with frame_lock:
                latest_frame = frame.copy()

            # Optional local GUI window
            if not args.no_window:
                cv2.imshow("Bye Wave Runner", frame)
                if cv2.waitKey(1) & 0xFF in (27, ord("q"), ord("Q")):
                    break

    except KeyboardInterrupt:
        print("\n[INFO] Stopped by user (Ctrl+C).")

    # ── 8.4  Graceful shutdown ─────────────────────────────────────────────────
    finally:
        print("\n[INFO] Shutting down — please wait...")

        # (a) Wait for any in-progress bye animation (2-second timeout)
        bye_runner.join(timeout=2.0)

        # (b) Return arm to home pose
        if bb is not None and home_pose is not None:
            try:
                bb.write(arm_a0=home_pose[0], arm_a1=home_pose[1],
                         arm_a2=home_pose[2], arm_a3=home_pose[3])
                print("[INFO] Arm returned to home pose.")
            except Exception as exc:
                print(f"[WARNING] Could not publish home pose on shutdown: {exc}",
                      file=sys.stderr)

        # (c) Stop camera
        camera.stop()

        # (d) Close hand detector (releases MediaPipe resources)
        detector.close()

        # (e) Shut down HTTP server
        server.shutdown()

        # (f) Destroy any open OpenCV windows
        if not args.no_window:
            cv2.destroyAllWindows()

        print("[SUCCESS] Cleanup complete. Offline.")


if __name__ == "__main__":
    main()
