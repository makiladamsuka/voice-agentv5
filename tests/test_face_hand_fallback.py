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
from pathlib import Path

# ── Headless-safe OpenCV import ───────────────────────────────────────────────
_has_display = bool(
    os.environ.get("DISPLAY") or os.environ.get("WAYLAND_DISPLAY")
)
if not _has_display:
    os.environ.setdefault("OPENCV_VIDEOIO_PRIORITY_MSMF", "0")
    os.environ["QT_QPA_PLATFORM"] = "offscreen"

import cv2
import numpy as np

from lib.hand_detector import HandDetector, draw_skeleton

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

def draw_hud(frame: cv2.Mat, target: tuple[int, int] | None, target_type: str, fps: float) -> cv2.Mat:
    """Draws HUD and target crosshair."""
    h, w, _ = frame.shape
    
    # Top info bar
    cv2.rectangle(frame, (0, 0), (w, 40), (15, 15, 20), -1)
    cv2.putText(frame, f"FPS: {fps:.1f}", (10, 25), cv2.FONT_HERSHEY_DUPLEX, 0.6, (0, 255, 120), 1)
    cv2.putText(frame, f"TARGET: {target_type}", (150, 25), cv2.FONT_HERSHEY_DUPLEX, 0.6, (255, 106, 0), 1)

    # Crosshair
    if target is not None:
        tx, ty = target
        color = (0, 255, 0) if target_type == "FACE" else (0, 255, 255)
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

    prev_time = time.time()
    fps = 0.0

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
            if faces is not None and len(faces) > 0:
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

            # Calculate FPS
            now = time.time()
            fps = 1.0 / max(now - prev_time, 1e-6)
            prev_time = now

            # Draw HUD
            frame = draw_hud(frame, target, target_type, fps)

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
        server.shutdown()
        if not args.no_window:
            cv2.destroyAllWindows()
        print("[SUCCESS] Cleanup completed.")

if __name__ == "__main__":
    main()
