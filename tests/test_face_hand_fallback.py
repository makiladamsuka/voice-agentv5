#!/usr/bin/env python3
"""
Test Face -> Hand Fallback Tracking.
Validates the scenario where a user's hand is near the camera, occluding their face.
When the face is undetected, the robot tracks the hand instead.

Features a built-in HTTP MJPEG streaming server to allow remote monitoring on a local network.

How to run:
    python tests/test_face_hand_fallback.py --port 8000

How to watch:
    Open a web browser and navigate to: http://<pi-ip-address>:8000/
"""

from __future__ import annotations

import _bootstrap  # noqa: F401
import argparse
from http.server import BaseHTTPRequestHandler, HTTPServer
import os
import socket
import socketserver
import threading
import time
import json
from pathlib import Path

# ── Headless-safe OpenCV import ───────────────────────────────────────────────
_has_display = bool(
    os.environ.get("DISPLAY") or os.environ.get("WAYLAND_DISPLAY")
)
if not _has_display:
    os.environ.setdefault("OPENCV_VIDEOIO_PRIORITY_MSMF", "0")
    os.environ["QT_QPA_PLATFORM"] = "offscreen"

import math
import cv2
import numpy as np

from lib.hand_detector import HandDetector, draw_skeleton
from lib.elastic_head_motion import clamp, smooth_toward
from hardware.arduino_servo import ArduinoServoLink

# ── Smoothing constants (ported from core/servo_loop.py) ──────────────────────
# EMA input filter alphas — separate per axis; tilt is gentler to avoid bobbing.
FACE_ALPHA_X = 0.22        # core: servo.face_alpha_x
FACE_ALPHA_Y = 0.06        # core: servo.face_alpha_y
HAND_ALPHA_X = 0.25        # slightly more responsive than face (hands jitter more)
HAND_ALPHA_Y = 0.08
BLOB_ALPHA_X = 0.30        # very fast tracking for massive skin blobs
BLOB_ALPHA_Y = 0.10

# Deadzone — ignore errors this small to prevent micro-oscillation near center.
DEADZONE_X = 0.04          # core: servo.deadzone_x
DEADZONE_Y = 0.05          # core: servo.deadzone_y

# Per-tick step clamp — prevents servo snaps on detection dropout/reappear.
PAN_MAX_STEP_DEG  = 1.5    # core: 1.2° (slightly more generous for hand tracking)
TILT_MAX_STEP_DEG = 2.0    # core: 1.8°

# Exponential smooth rates — lower = smoother but more sluggish.
FACE_SMOOTH_HZ = 4.5       # matches core's pan_track_smooth_hz
HAND_SMOOTH_HZ = 3.5       # extra smoothing for noisier hand detections
BLOB_SMOOTH_HZ = 3.0       # absorbs noisy centroid wobbles of shapeless blobs

# Camera FOV half-angles (degrees) used to convert normalized error to servo degrees.
FOV_HALF_X_DEG = 30.0
FOV_HALF_Y_DEG = 20.0

# Servo limits
PAN_LO, PAN_HI   = 30.0, 150.0
TILT_LO, TILT_HI = 50.0, 130.0

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
            
            host_ip = "localhost"
            try:
                s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
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
                <title>Face-Hand Fallback Tracker</title>
                <style>
                    body {{ background-color: #0f101a; color: #f1f5f9; font-family: sans-serif; text-align: center; padding: 20px; }}
                    .container {{ max-width: 800px; margin: 0 auto; background: #161826; padding: 20px; border-radius: 12px; border: 1px solid #272d42; }}
                    h1 {{ color: #00e5ff; }}
                    .stream-box {{ border: 2px solid #00e5ff; display: inline-block; border-radius: 8px; overflow: hidden; }}
                    img {{ display: block; max-width: 100%; height: auto; }}
                </style>
            </head>
            <body>
                <div class="container">
                    <h1>Face-Hand Fallback Tracking Feed</h1>
                    <p>Occlude your face with your hand to test fallback tracking.</p>
                    <div class="stream-box">
                        <img src="/stream" alt="Camera Feed" />
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
                    time.sleep(0.04)
            except Exception:
                return
        else:
            self.send_error(404)

    def log_message(self, format, *args):
        pass

class CameraFeed:
    """Manages the video source using Picamera2."""
    def __init__(self):
        try:
            from picamera2 import Picamera2  # type: ignore
            print("[INFO] Camera Interface: Picamera2")
            self.picam2 = Picamera2()
            config = self.picam2.create_video_configuration(
                main={"format": "XRGB8888", "size": (640, 480)},
                buffer_count=2,
            )
            self.picam2.configure(config)
            self.use_picam = True
        except ImportError:
            print("[INFO] Camera Interface: OpenCV VideoCapture (fallback)")
            self.cap = cv2.VideoCapture(0)
            self.cap.set(cv2.CAP_PROP_FRAME_WIDTH, 640)
            self.cap.set(cv2.CAP_PROP_FRAME_HEIGHT, 480)
            self.use_picam = False

    def start(self) -> None:
        if self.use_picam:
            self.picam2.start()

    def read(self) -> tuple[bool, cv2.Mat | None]:
        if self.use_picam:
            try:
                frame_xrgb = self.picam2.capture_array()
                frame_bgr = cv2.cvtColor(frame_xrgb, cv2.COLOR_BGRA2BGR)
                return True, frame_bgr
            except Exception as e:
                print(f"[ERROR] Picamera2 frame capture failed: {e}")
                return False, None
        else:
            return self.cap.read()

    def stop(self) -> None:
        if self.use_picam:
            try:
                self.picam2.stop()
            except Exception:
                pass
        else:
            self.cap.release()

# ── Smoothing helpers (ported from core/servo_loop.py) ────────────────────────

def _apply_deadzone(value: float, deadzone: float) -> float:
    """Suppress small errors inside ±deadzone to prevent micro-oscillation."""
    if abs(value) <= deadzone:
        return 0.0
    sign = 1.0 if value > 0 else -1.0
    return sign * (abs(value) - deadzone) / (1.0 - deadzone)


def _smooth_toward_stepped(
    pos: float,
    target: float,
    dt: float,
    *,
    smooth_hz: float,
    lo: float,
    hi: float,
    max_step: float,
) -> float:
    """Exponential smooth with per-tick step cap — prevents servo snaps."""
    next_pos = smooth_toward(pos, target, dt, smooth_hz=smooth_hz, lo=lo, hi=hi)
    step = clamp(next_pos - pos, -max_step, max_step)
    return clamp(pos + step, lo, hi)


def draw_hud(
    frame: cv2.Mat,
    target: tuple[int, int] | None,
    target_type: str,
    fps: float,
    pan: float,
    tilt: float,
) -> cv2.Mat:
    """Draws HUD with target crosshair and servo angle readout."""
    h, w, _ = frame.shape
    
    # Top info bar
    cv2.rectangle(frame, (0, 0), (w, 40), (15, 15, 20), -1)
    cv2.putText(frame, f"FPS: {fps:.1f}", (10, 25), cv2.FONT_HERSHEY_DUPLEX, 0.6, (0, 255, 120), 1)
    cv2.putText(frame, f"TARGET: {target_type}", (150, 25), cv2.FONT_HERSHEY_DUPLEX, 0.6, (255, 106, 0), 1)
    cv2.putText(frame, f"PAN:{pan:.1f} TILT:{tilt:.1f}", (370, 25), cv2.FONT_HERSHEY_DUPLEX, 0.5, (180, 180, 220), 1)

    # Crosshair
    if target is not None:
        tx, ty = target
        if target_type == "FACE":
            color = (0, 255, 0)
        elif target_type == "HAND":
            color = (0, 255, 255)
        else:
            color = (255, 0, 255) # Magenta for CLOSE_UP
        length = 20
        cv2.line(frame, (tx - length, ty), (tx + length, ty), color, 2)
        cv2.line(frame, (tx, ty - length), (tx, ty + length), color, 2)
        cv2.circle(frame, (tx, ty), 10, color, 2)
        cv2.putText(frame, target_type, (tx + 15, ty - 15), cv2.FONT_HERSHEY_SIMPLEX, 0.6, color, 2)

    return frame

def main():
    parser = argparse.ArgumentParser(description="Test Face -> Hand Fallback Tracker")
    parser.add_argument("--host", default="0.0.0.0", help="HTTP Server Host")
    parser.add_argument("--port", type=int, default=8000, help="HTTP Server Port")
    parser.add_argument("--mirror", action="store_true", help="Mirror camera")
    parser.add_argument("--no-window", action="store_true", help="Disable GUI window")
    args = parser.parse_args()

    if not _has_display:
        args.no_window = True

    # Start MJPEG Stream Server
    print(f"[SERVER] Stream running at http://localhost:{args.port}/")
    server = ThreadingHTTPServer((args.host, args.port), MJPEGHandler)
    server_thread = threading.Thread(target=server.serve_forever, daemon=True)
    server_thread.start()

    # Start Camera
    camera = CameraFeed()
    camera.start()

    # Init Hand Detector
    hand_detector = HandDetector(max_num_hands=1)

    # Init Face Detector (YuNet)
    app_dir = Path(__file__).resolve().parent.parent
    face_model_path = str(app_dir / "face_detection_yunet_2023mar.onnx")
    
    if not Path(face_model_path).exists():
        print(f"[ERROR] Face model not found at {face_model_path}")
        return

    face_detector = cv2.FaceDetectorYN.create(
        model=face_model_path,
        config="",
        input_size=(640, 480),
        score_threshold=0.6,
        nms_threshold=0.3,
        top_k=50,
        backend_id=cv2.dnn.DNN_BACKEND_OPENCV,
        target_id=cv2.dnn.DNN_TARGET_CPU,
    )

    # Init Servo Link
    link = ArduinoServoLink()
    
    # Load Arm Homes
    arm_homes = [0.0, 180.0, 90.0, 90.0]
    limits_path = app_dir / "tests" / "captured_arm_limits.json"
    if limits_path.exists():
        try:
            with open(limits_path, 'r') as f:
                data = json.load(f)
                if "homes" in data:
                    arm_homes = data["homes"]
        except Exception as e:
            print(f"[ERROR] Failed to load arm limits: {e}")

    if link.connect():
        print("[INFO] Servo Link connected.")
        # Move arms to home position without touching head (pan/tilt)
        link.write_arms(*arm_homes, force=True)
    else:
        print("[WARN] Servo Link failed to connect. Running in software-only mode.")
        link = None

    prev_time = time.time()
    fps = 0.0
    pan = 90.0
    tilt = 90.0

    # ── EMA filter + velocity tracking state (matches core servo_loop) ────────
    filtered_norm_x = 0.0
    filtered_norm_y = 0.0
    prev_raw_norm_x = 0.0
    prev_raw_norm_y = 0.0
    prev_target_type = "NONE"

    print("\n[INFO] Starting loop. Press Ctrl+C to exit.")
    
    try:
        while True:
            ret, frame = camera.read()
            if not ret or frame is None:
                time.sleep(0.01)
                continue

            if args.mirror:
                frame = cv2.flip(frame, 1)

            h, w, _ = frame.shape
            
            # Ensure detector input size matches frame
            face_detector.setInputSize((w, h))

            target = None
            target_type = "NONE"

            # 1. Face Detection
            _, faces = face_detector.detect(frame)
            
            # 2. Hand Detection
            hands = hand_detector.process(frame, mirrored=False)

            # Draw Hand Skeletons
            for hand in hands:
                draw_skeleton(frame, hand, is_active=False)

            # Target Logic Selection
            target = None
            target_type = "NONE"

            # ── Skin Blob Lock (prevent false faces during close-up) ────────
            blob_active_this_frame = False
            if skin_blob_lock:
                try:
                    hsv = cv2.cvtColor(frame, cv2.COLOR_BGR2HSV)
                    mask1 = cv2.inRange(hsv, np.array([0, 20, 40], dtype=np.uint8), np.array([25, 255, 255], dtype=np.uint8))
                    mask2 = cv2.inRange(hsv, np.array([155, 20, 40], dtype=np.uint8), np.array([180, 255, 255], dtype=np.uint8))
                    mask = cv2.bitwise_or(mask1, mask2)
                    mask = cv2.morphologyEx(mask, cv2.MORPH_OPEN, np.ones((5, 5), np.uint8))
                    contours, _ = cv2.findContours(mask, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE)
                    
                    if contours:
                        largest_blob = max(contours, key=cv2.contourArea)
                        area = cv2.contourArea(largest_blob)
                        if area > (w * h * 0.15):
                            M = cv2.moments(largest_blob)
                            if M["m00"] > 0:
                                cx = int(M["m10"] / M["m00"])
                                cy = int(M["m01"] / M["m00"])
                                target = (cx, cy)
                                target_type = "CLOSE_UP"
                                blob_active_this_frame = True
                except Exception:
                    pass
                
                if not blob_active_this_frame:
                    skin_blob_lock = False

            if not skin_blob_lock and faces is not None and len(faces) > 0:
                valid_faces = [f for f in faces if float(f[2]) > 4 and float(f[3]) > 4]
                if valid_faces:
                    # Pick largest face
                    ranked_faces = sorted(valid_faces, key=lambda f: float(f[2]) * float(f[3]), reverse=True)
                    best_face = ranked_faces[0]
                    
                    fx, fy, fw, fh = map(int, best_face[0:4])
                    cx, cy = fx + fw // 2, fy + fh // 2
                    
                    # Draw Face Bounding Box
                    cv2.rectangle(frame, (fx, fy), (fx + fw, fy + fh), (0, 255, 0), 2)
                    
                    target = (cx, cy)
                    target_type = "FACE"
            
            # Fallback to Hand if Face not detected
            if target is None and len(hands) > 0:
                # Pick most confident hand
                best_hand = max(hands, key=lambda h: h.confidence)
                target = best_hand.palm_center
                target_type = "HAND"
                
            # 3. Fallback to Skin Blob if Hand not detected (hand too close/large)
            if target is None:
                hsv = cv2.cvtColor(frame, cv2.COLOR_BGR2HSV)
                # Broadened human skin color range in HSV (handling red wrap-around)
                # Lower red/orange hues
                mask1 = cv2.inRange(hsv, np.array([0, 20, 40], dtype=np.uint8), np.array([25, 255, 255], dtype=np.uint8))
                # Upper red hues
                mask2 = cv2.inRange(hsv, np.array([155, 20, 40], dtype=np.uint8), np.array([180, 255, 255], dtype=np.uint8))
                mask = cv2.bitwise_or(mask1, mask2)
                
                # Apply morphological opening to clean up isolated noise
                mask = cv2.morphologyEx(mask, cv2.MORPH_OPEN, np.ones((5, 5), np.uint8))
                
                contours, _ = cv2.findContours(mask, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE)
                if contours:
                    largest_blob = max(contours, key=cv2.contourArea)
                    area = cv2.contourArea(largest_blob)
                    # If blob covers at least 15% of the frame, it's likely a close-up block
                    if area > (w * h * 0.15):
                        M = cv2.moments(largest_blob)
                        if M["m00"] > 0:
                            cx = int(M["m10"] / M["m00"])
                            cy = int(M["m01"] / M["m00"])
                            target = (cx, cy)
                            target_type = "CLOSE_UP"
                            skin_blob_lock = True

            # 3. Servo Tracking Update — multi-layer smoothing pipeline
            #    (EMA filter → deadzone → velocity-adaptive alpha → smooth + step clamp)
            if target is not None:
                cx, cy = target
                dt = 1.0 / max(fps, 1.0)

                # ── Layer 1: Normalize to [-1, +1] ────────────────────────────
                raw_norm_x = (cx - (w / 2.0)) / (w / 2.0)
                raw_norm_y = (cy - (h / 2.0)) / (h / 2.0)

                # ── Layer 2: Velocity tracking (for adaptive alpha) ───────────
                vel_x = abs(raw_norm_x - prev_raw_norm_x) / max(dt, 0.001)
                vel_y = abs(raw_norm_y - prev_raw_norm_y) / max(dt, 0.001)
                prev_raw_norm_x = raw_norm_x
                prev_raw_norm_y = raw_norm_y

                # ── Layer 3: EMA input filter with velocity-adaptive alpha ────
                # Pick base alphas by target type.
                if target_type == "FACE":
                    alpha_x, alpha_y = FACE_ALPHA_X, FACE_ALPHA_Y
                    smooth_hz = FACE_SMOOTH_HZ
                elif target_type == "HAND":
                    alpha_x, alpha_y = HAND_ALPHA_X, HAND_ALPHA_Y
                    smooth_hz = HAND_SMOOTH_HZ
                else:
                    alpha_x, alpha_y = BLOB_ALPHA_X, BLOB_ALPHA_Y
                    smooth_hz = BLOB_SMOOTH_HZ

                # On target-type switch, snap filter to raw to avoid lag.
                if target_type != prev_target_type:
                    filtered_norm_x = raw_norm_x
                    filtered_norm_y = raw_norm_y
                    prev_target_type = target_type

                # Boost alpha when target moves fast (core servo_loop L710-L713).
                if vel_x > 2.0:
                    alpha_x = min(0.58, alpha_x + (vel_x - 2.0) * 0.05)
                if vel_y > 1.5:
                    alpha_y = min(0.40, alpha_y + (vel_y - 1.5) * 0.04)

                filtered_norm_x += (raw_norm_x - filtered_norm_x) * alpha_x
                filtered_norm_y += (raw_norm_y - filtered_norm_y) * alpha_y

                # ── Layer 4: Deadzone — suppress micro-corrections near center ─
                err_x = _apply_deadzone(filtered_norm_x, DEADZONE_X)
                err_y = _apply_deadzone(filtered_norm_y, DEADZONE_Y)

                # ── Layer 5: Convert to servo degree error ─────────────────────
                pan_error_deg = err_x * FOV_HALF_X_DEG
                tilt_error_deg = err_y * FOV_HALF_Y_DEG

                # Absolute targets
                if args.mirror:
                    target_pan = pan + pan_error_deg
                else:
                    target_pan = pan - pan_error_deg
                target_tilt = tilt - tilt_error_deg

                # ── Layer 6: Exponential smooth + per-tick step clamp ──────────
                pan = _smooth_toward_stepped(
                    pan, target_pan, dt,
                    smooth_hz=smooth_hz, lo=PAN_LO, hi=PAN_HI,
                    max_step=PAN_MAX_STEP_DEG,
                )
                tilt = _smooth_toward_stepped(
                    tilt, target_tilt, dt,
                    smooth_hz=smooth_hz, lo=TILT_LO, hi=TILT_HI,
                    max_step=TILT_MAX_STEP_DEG,
                )

                if link is not None:
                    link.write_angles(pan, tilt)
            else:
                prev_target_type = "NONE"

            # Calculate FPS
            now = time.time()
            fps = 1.0 / max(now - prev_time, 1e-6)
            prev_time = now

            # Draw HUD
            frame = draw_hud(frame, target, target_type, fps, pan, tilt)

            # Publish to Stream
            global latest_frame
            with frame_lock:
                latest_frame = frame.copy()

            # Display
            if not args.no_window:
                cv2.imshow("Face-Hand Fallback Test", frame)
                if cv2.waitKey(1) & 0xFF in (27, ord('q')):
                    break

    except KeyboardInterrupt:
        print("\n[INFO] Stopped by user.")
    finally:
        camera.stop()
        hand_detector.close()
        if link is not None:
            link.close()
        server.shutdown()
        if not args.no_window:
            cv2.destroyAllWindows()
        print("[SUCCESS] Cleanup completed.")

if __name__ == "__main__":
    main()
