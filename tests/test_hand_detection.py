#!/usr/bin/env python3
"""
Test Hand Detection with Picamera2 and OpenCV Fallback.
Provides real-time hand skeleton tracking, motion trails, waving gesture detection,
and a premium glassmorphic HUD.

Features a built-in HTTP MJPEG streaming server to allow remote monitoring on a local network.

How to run:
    python tests/test_hand_detection.py --port 8000 --camera auto

How to watch:
    Open a web browser on any computer on the same network and navigate to:
    http://<pi-ip-address>:8000/
"""

from __future__ import annotations

import _bootstrap  # noqa: F401
import argparse
import collections
import cv2
from http.server import BaseHTTPRequestHandler, HTTPServer
import io
import os
import socket
import socketserver
import sys
import threading
import time

from lib.hand_detector import HandDetector, HandDetection, draw_skeleton, draw_motion_trail

# Streaming Server State
latest_frame = None
frame_lock = threading.Lock()

class ThreadingHTTPServer(socketserver.ThreadingMixIn, HTTPServer):
    daemon_threads = True

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
                            <p>1. Raise your hand above the orange boundary line (top 45% of screen).</p>
                            <p>2. Wave side-to-side to build up the intensity gauge.</p>
                            <p>3. Complete 8 swings to trigger a wave event.</p>
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

class CameraFeed:
    """Manages the video source using Picamera2."""
    
    def __init__(self):
        from picamera2 import Picamera2  # type: ignore
        print("[INFO] Camera Interface: Picamera2")
        self.picam2 = Picamera2()
        config = self.picam2.create_video_configuration(
            main={"format": "RGB888", "size": (640, 480)},
            buffer_count=2,
        )
        self.picam2.configure(config)

    def start(self) -> None:
        self.picam2.start()

    def read(self) -> tuple[bool, cv2.Mat | None]:
        try:
            frame_rgb = self.picam2.capture_array()
            # Convert RGB (Picamera2) to BGR (OpenCV standards)
            frame_bgr = cv2.cvtColor(frame_rgb, cv2.COLOR_RGB2BGR)
            return True, frame_bgr
        except Exception as e:
            print(f"[ERROR] Picamera2 frame capture failed: {e}")
            return False, None

    def stop(self) -> None:
        try:
            self.picam2.stop()
        except Exception:
            pass

class WavingDetector:
    """Detects waving hand patterns and maintains state histories."""
    
    def __init__(self, history_len: int = 25, dead_zone_px: int = 10, cooldown_sec: float = 5.0):
        self.history_len = history_len
        self.dead_zone_px = dead_zone_px
        self.cooldown_sec = cooldown_sec
        
        self.cooldown_until = 0.0
        self.announcement_end_time = 0.0
        self.announcement_hand = ""
        
        self.hand_states = {
            "Left": {
                "x_history": collections.deque(maxlen=self.history_len),
                "y_history": collections.deque(maxlen=self.history_len),
                "last_seen": 0.0,
                "is_waving": False,
                "above_limit": False,
                "reversals": 0,
                "amplitude": 0.0,
                "intensity": 0.0
            },
            "Right": {
                "x_history": collections.deque(maxlen=self.history_len),
                "y_history": collections.deque(maxlen=self.history_len),
                "last_seen": 0.0,
                "is_waving": False,
                "above_limit": False,
                "reversals": 0,
                "amplitude": 0.0,
                "intensity": 0.0
            }
        }

    def detect_reversals(self, history: list[int]) -> tuple[int, float]:
        """Analyzes coordinates history for peak-to-peak swing counts (reversals) and amplitude."""
        if len(history) < 6:
            return 0, 0.0

        reversals = 0
        anchor = history[0]
        direction = 0  # +1 = increasing, -1 = decreasing
        peaks = [history[0]]

        for val in history[1:]:
            diff = val - anchor
            if abs(diff) < self.dead_zone_px:
                continue
                
            new_dir = 1 if diff > 0 else -1
            if direction != 0 and new_dir != direction:
                reversals += 1
                peaks.append(anchor)
            direction = new_dir
            anchor = val

        amplitude = max(peaks) - min(peaks) if peaks else 0.0
        return reversals, amplitude

    def process_detection(self, detections: list[HandDetection], limit_y: int, now: float) -> list[str]:
        """Updates internal deques and returns list of sides currently waving."""
        # Timeout unobserved hands (older than 0.4 seconds)
        for side in ["Left", "Right"]:
            if now - self.hand_states[side]["last_seen"] > 0.4:
                self.hand_states[side]["x_history"].clear()
                self.hand_states[side]["y_history"].clear()
                self.hand_states[side]["is_waving"] = False
                self.hand_states[side]["above_limit"] = False
                self.hand_states[side]["reversals"] = 0
                self.hand_states[side]["amplitude"] = 0.0
                self.hand_states[side]["intensity"] = 0.0

        waving_hands = []

        for hand in detections:
            side = hand.physical_side
            state = self.hand_states[side]
            state["last_seen"] = now
            
            # Palm center & height boundary check
            palm_x, palm_y = hand.palm_center
            is_above_limit = palm_y < limit_y
            state["above_limit"] = is_above_limit

            # Process movement history if palm faces frontside
            if hand.is_frontside:
                state["x_history"].append(palm_x)
                state["y_history"].append(palm_y)
            else:
                state["x_history"].clear()
                state["y_history"].clear()

            # Wave direction detection
            rev_x, amp_x = self.detect_reversals(list(state["x_history"]))
            rev_y, amp_y = self.detect_reversals(list(state["y_history"]))
            
            best_rev = max(rev_x, rev_y)
            best_amp = max(amp_x, amp_y)
            
            state["reversals"] = best_rev
            state["amplitude"] = best_amp

            # Criteria for waving: Minimum swings, sweep size, and height boundary
            is_waving = (best_rev >= 3 and best_amp >= 40.0 and is_above_limit)
            state["is_waving"] = is_waving

            # Calculate wave intensity gauge (0.0 to 1.0)
            if is_above_limit:
                state["intensity"] = min(1.0, best_amp / 160.0) * min(1.0, best_rev / 5.0)
            else:
                state["intensity"] = 0.0

            # Trigger Wave Gesture Event (e.g. 8 swings)
            if best_rev >= 8 and is_above_limit and now > self.cooldown_until:
                self.cooldown_until = now + self.cooldown_sec
                self.announcement_end_time = now + 3.0
                self.announcement_hand = side
                print(f"[EVENT] 👋 {side.upper()} HAND WAVE TRIGGERED! Swings: {best_rev}, Intensity: {state['intensity']:.2f}")

            if is_waving:
                waving_hands.append(side)

        return waving_hands

def draw_hud(
    frame: cv2.Mat,
    detector_states: dict,
    limit_y: int,
    announcement_hand: str,
    announcement_end_time: float,
    cooldown_until: float,
    fps: float,
    now: float
) -> cv2.Mat:
    """Renders the top panel, status tags, countdowns, and waving events."""
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

    # B. Height Boundary Line
    col_line = (255, 106, 0) if any(s["above_limit"] for s in detector_states.values()) else (60, 60, 80)
    cv2.line(frame, (0, limit_y), (w, limit_y), col_line, 1, cv2.LINE_AA)
    cv2.putText(frame, "HEIGHT LIMIT FOR WAVING DETECTOR", (w - 280, limit_y - 8), cv2.FONT_HERSHEY_SIMPLEX, 0.35, col_line, 1, cv2.LINE_AA)

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
        lbl = "WAVE HAND ABOVE LINE"
        col = (0, 255, 150)
        if now < cooldown_until:
            lbl = "COOLDOWN... ⏳"
            col = (0, 165, 255)
        elif left_state["above_limit"]:
            if left_state["reversals"] >= 8:
                lbl = "TRIGGERED! 👋"
                col = (255, 255, 0)
            elif left_state["reversals"] >= 3:
                lbl = f"SWINGS: {left_state['reversals']}/8"
                col = (0, 255, 255)
        
        cv2.putText(frame, lbl, (25, h - 88), cv2.FONT_HERSHEY_DUPLEX, 0.45, col, 1, cv2.LINE_AA)
        cv2.putText(frame, f"Above limit: {'YES' if left_state['above_limit'] else 'NO'}", (25, h - 68), cv2.FONT_HERSHEY_SIMPLEX, 0.4, (0, 255, 150) if left_state['above_limit'] else (150, 150, 170), 1, cv2.LINE_AA)
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
        lbl = "WAVE HAND ABOVE LINE"
        col = (0, 255, 150)
        if now < cooldown_until:
            lbl = "COOLDOWN... ⏳"
            col = (0, 165, 255)
        elif right_state["above_limit"]:
            if right_state["reversals"] >= 8:
                lbl = "TRIGGERED! 👋"
                col = (255, 255, 0)
            elif right_state["reversals"] >= 3:
                lbl = f"SWINGS: {right_state['reversals']}/8"
                col = (0, 255, 255)
        
        cv2.putText(frame, lbl, (w - 235, h - 88), cv2.FONT_HERSHEY_DUPLEX, 0.45, col, 1, cv2.LINE_AA)
        cv2.putText(frame, f"Above limit: {'YES' if right_state['above_limit'] else 'NO'}", (w - 235, h - 68), cv2.FONT_HERSHEY_SIMPLEX, 0.4, (0, 255, 150) if right_state['above_limit'] else (150, 150, 170), 1, cv2.LINE_AA)
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

        msg = f"👋 WAVE DETECTED! ({announcement_hand.upper()})"
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

def main():
    parser = argparse.ArgumentParser(description="Test Hand Detection and Stream client")
    parser.add_argument("--host", default="0.0.0.0", help="HTTP Server Host (default 0.0.0.0)")
    parser.add_argument("--port", type=int, default=8000, help="HTTP Server Port (default 8000)")
    parser.add_argument("--mirror", type=bool, default=True, help="Mirror camera image horizontally")
    parser.add_argument("--height-limit", type=float, default=0.45, help="Y ratio (0.0 to 1.0) above which waving is active")
    parser.add_argument("--max-hands", type=int, default=2, help="Max hand instances to track")
    parser.add_argument("--no-window", action="store_true", help="Disable cv2.imshow GUI window (useful for headless servers)")
    args = parser.parse_args()

    # 1. Start Stream Server
    print(f"[SERVER] Starting HTTP stream server on http://{args.host}:{args.port}/")
    server = ThreadingHTTPServer((args.host, args.port), MJPEGHandler)
    server_thread = threading.Thread(target=server.serve_forever, daemon=True)
    server_thread.start()

    # Resolve local hostname/IP address
    local_ip = "localhost"
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.connect(("8.8.8.8", 80))
        local_ip = s.getsockname()[0]
        s.close()
    except Exception:
        pass

    print("\n=======================================================")
    print("      [VISION ENGINE] MediaPipe Hand Detection Active")
    print("=======================================================")
    print(f"  • Local Watch URL:  http://localhost:{args.port}/")
    print(f"  • Network Watch URL: http://{local_ip}:{args.port}/")
    print(f"  • Active Camera:    Picamera2")
    print(f"  • Height restriction: Y < {args.height_limit * 100:.1f}% of frame height")
    print("  • Press Ctrl+C in console to stop safely.")
    print("=======================================================\n")

    # 2. Init Camera Feed
    camera = CameraFeed()
    camera.start()

    # 3. Init Hand Detector
    detector = HandDetector(max_num_hands=args.max_hands)
    waving_detector = WavingDetector()

    prev_time = time.time()
    fps = 0.0

    try:
        while True:
            ret, frame = camera.read()
            if not ret or frame is None:
                time.sleep(0.01)
                continue

            # Mirror frame for intuitive interaction if configured
            if args.mirror:
                frame = cv2.flip(frame, 1)

            h, w, _ = frame.shape
            limit_y = int(args.height_limit * h)
            now = time.time()

            # Process hand detection (mirrored flag is handled inside camera flip + coordinates correction)
            # Pass mirrored=False since we already flipped the frame visually!
            detections = detector.process(frame, mirrored=False)

            # Draw skeletons for detected hands
            for hand in detections:
                is_hand_waving = waving_detector.hand_states[hand.physical_side]["is_waving"]
                draw_skeleton(frame, hand, is_active=is_hand_waving)
                
                # Draw motion trails
                state = waving_detector.hand_states[hand.physical_side]
                draw_motion_trail(frame, list(state["x_history"]), list(state["y_history"]), is_active=is_hand_waving)

            # Process waving rules
            waving_detector.process_detection(detections, limit_y, now)

            # Calculate FPS
            fps_now = time.time()
            fps = 1.0 / max(fps_now - prev_time, 1e-6)
            prev_time = fps_now

            # Render HUD overlays
            frame = draw_hud(
                frame,
                waving_detector.hand_states,
                limit_y,
                waving_detector.announcement_hand,
                waving_detector.announcement_end_time,
                waving_detector.cooldown_until,
                fps,
                now
            )

            # Publish frame to MJPEG HTTP stream
            global latest_frame
            with frame_lock:
                latest_frame = frame.copy()

            # Optional local GUI window (skipped if head-less/--no-window passed)
            if not args.no_window:
                cv2.imshow("AI Hand Detector - Visual Test client", frame)
                if cv2.waitKey(1) & 0xFF in (27, ord('q'), ord('Q')):
                    break

    except KeyboardInterrupt:
        print("\n[INFO] Stopped by user command (Ctrl+C).")
    finally:
        print("\nShutting down stream, camera and detector...")
        camera.stop()
        detector.close()
        server.shutdown()
        cv2.destroyAllWindows()
        print("[SUCCESS] Cleanup completed. Offline.")

if __name__ == "__main__":
    main()
