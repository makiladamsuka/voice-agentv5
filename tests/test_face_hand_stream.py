"""Standalone face + hand detection test with live web tuning dashboard.

Runs the camera with face detection (YuNet) and hand detection (MediaPipe),
streams annotated MJPEG to a web server accessible from any browser.

Features:
  - Live camera quality / resolution tuning
  - Servo deadzone adjustments (pan/tilt)
  - Face & hand detection toggles and thresholds
  - MJPEG stream with detection overlays (bounding boxes, landmarks, crosshairs)
  - JSON API for current detection state

Usage:
    python tests/test_face_hand_stream.py                  # defaults
    python tests/test_face_hand_stream.py --port 9090      # custom port
    python tests/test_face_hand_stream.py --config config.kiosk.yaml

Then open http://<pi-ip>:<port>/ in any browser.
"""

from __future__ import annotations

import argparse
import io
import json
import math
import os
import sys
import socketserver
import threading
import time
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path

# ── Bootstrap: add project root to path ──────────────────────────────────────
APP_DIR = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(APP_DIR))

import cv2
import numpy as np

try:
    import yaml
except ImportError:
    yaml = None

try:
    from PIL import Image
except ImportError:
    Image = None

from lib.hand_detector import HandDetector, draw_skeleton
from hardware.arduino_servo import ArduinoServoLink

# ── Config loading ───────────────────────────────────────────────────────────

def _load_yaml(path: Path) -> dict:
    if yaml is None or not path.exists():
        return {}
    with open(path, encoding="utf-8") as f:
        return yaml.safe_load(f) or {}


def _cfg(data, *keys, default=None):
    cur = data
    for key in keys:
        if not isinstance(cur, dict) or key not in cur:
            return default
        cur = cur[key]
    return cur


# ── Tunable parameters (shared across threads) ──────────────────────────────

class TuneState:
    """Thread-safe mutable tuning state, adjustable via web UI."""

    def __init__(self, cfg: dict):
        self._lock = threading.Lock()
        cam = _cfg(cfg, "camera", default={}) or {}
        stream = _cfg(cfg, "stream", default={}) or {}
        servo = _cfg(cfg, "servo", default={}) or {}

        # Camera
        self.main_res_w = int(_cfg(cam, "main_res", default=[1920, 1080])[0])
        self.main_res_h = int(_cfg(cam, "main_res", default=[1920, 1080])[1])
        self.detect_res_w = int(_cfg(cam, "detect_res", default=[1280, 720])[0])
        self.detect_res_h = int(_cfg(cam, "detect_res", default=[1280, 720])[1])
        self.confidence_threshold = float(_cfg(cam, "confidence_threshold", default=0.6))
        self.nms_threshold = float(_cfg(cam, "nms_threshold", default=0.3))
        self.rotate_180 = bool(_cfg(cam, "rotate_180", default=False))
        self.swap_rb = bool(_cfg(cam, "stream_swap_rb", default=True))

        # Stream
        self.stream_fps = int(_cfg(stream, "fps", default=8))
        self.jpeg_quality = int(_cfg(stream, "jpeg_quality", default=70))
        self.vision_fps = int(_cfg(stream, "vision_fps", default=10))
        self.stream_res_w = int(_cfg(stream, "res", default=[640, 360])[0])
        self.stream_res_h = int(_cfg(stream, "res", default=[640, 360])[1])

        # Servo deadzones
        self.deadzone_x = float(servo.get("deadzone_x", 0.05))
        self.deadzone_y = float(servo.get("deadzone_y", 0.06))
        self.pan_center = float(servo.get("pan_center", 100.0))
        self.tilt_center = float(servo.get("tilt_center", 110.0))
        self.pan_sign = float(servo.get("pan_sign", -1.0))
        self.tilt_sign = float(servo.get("tilt_sign", 1.0))
        self.pan_min = float(servo.get("pan_min", 20.0))
        self.pan_max = float(servo.get("pan_max", 160.0))
        self.tilt_min = float(servo.get("tilt_min", 40.0))
        self.tilt_max = float(servo.get("tilt_max", 150.0))

        # Servo tuning parameters
        self.servo_enabled = False
        self.pan_p_gain = 4.0
        self.tilt_p_gain = 3.0

        # Detection toggles
        self.face_detection_enabled = True
        self.hand_detection_enabled = True

        # Stats (read-only from detection thread)
        self.face_detected = False
        self.face_norm_x = 0.0
        self.face_norm_y = 0.0
        self.face_count = 0
        self.hand_detected = False
        self.hand_norm_x = 0.0
        self.hand_norm_y = 0.0
        self.hand_side = ""
        self.track_kind = "none"
        self.fps_actual = 0.0
        self.detect_ms = 0.0
        
        # Read-only from servo thread
        self.current_pan = self.pan_center
        self.current_tilt = self.tilt_center

    def get(self, key: str):
        with self._lock:
            return getattr(self, key, None)

    def set(self, key: str, value):
        with self._lock:
            if hasattr(self, key):
                setattr(self, key, value)

    def snapshot(self) -> dict:
        with self._lock:
            return {k: v for k, v in self.__dict__.items() if not k.startswith("_")}

    def update(self, values: dict):
        with self._lock:
            for k, v in values.items():
                if hasattr(self, k) and not k.startswith("_"):
                    old = getattr(self, k)
                    if isinstance(old, bool):
                        setattr(self, k, bool(v) if not isinstance(v, str) else v.lower() in ("true", "1", "yes"))
                    elif isinstance(old, int):
                        setattr(self, k, int(float(v)))
                    elif isinstance(old, float):
                        setattr(self, k, float(v))


# ── Detection thread ─────────────────────────────────────────────────────────

class DetectionThread(threading.Thread):
    """Captures frames, runs YuNet + MediaPipe, updates TuneState."""

    daemon = True

    def __init__(self, tune: TuneState, cfg: dict):
        super().__init__(name="DetectionThread")
        self.tune = tune
        self.cfg = cfg
        self.latest_frame = None  # annotated JPEG bytes
        self._frame_lock = threading.Lock()
        cam_cfg = _cfg(cfg, "camera", default={}) or {}
        self._face_model_path = str(APP_DIR / cam_cfg.get("face_model_path", "face_detection_yunet_2023mar.onnx"))

    def get_frame(self) -> bytes | None:
        with self._frame_lock:
            return self.latest_frame

    def run(self):
        # ── Init camera ──────────────────────────────────────────────────
        cam = self._init_camera()
        if cam is None:
            print("[DetectionThread] Camera unavailable — exiting.")
            return

        # ── Init face detector ───────────────────────────────────────────
        detector = self._init_face_detector()

        # ── Init hand detector ───────────────────────────────────────────
        hand_detector = HandDetector(max_num_hands=2)

        print("[DetectionThread] Started — capturing and detecting.")

        fps_counter = 0
        fps_timer = time.perf_counter()

        while True:
            t0 = time.perf_counter()
            vision_fps = max(1, self.tune.get("vision_fps"))

            try:
                frame_full = cam.capture_array()
            except Exception as e:
                print(f"[DetectionThread] Capture error: {e}")
                time.sleep(0.1)
                continue

            if self.tune.get("rotate_180"):
                frame_full = cv2.rotate(frame_full, cv2.ROTATE_180)

            detect_w = self.tune.get("detect_res_w")
            detect_h = self.tune.get("detect_res_h")
            detect_res = (detect_w, detect_h)
            frame = cv2.resize(frame_full, detect_res, interpolation=cv2.INTER_LINEAR)

            face_detected = False
            face_norm_x = 0.0
            face_norm_y = 0.0
            face_count = 0
            face_candidates = []
            hand_detected = False
            hand_norm_x = 0.0
            hand_norm_y = 0.0
            hand_side = ""
            track_kind = "none"

            # ── Face detection ───────────────────────────────────────────
            if self.tune.get("face_detection_enabled") and detector is not None:
                detector.setInputSize(detect_res)
                _, faces = detector.detect(frame)
                if faces is not None and len(faces) > 0:
                    valid = [f for f in faces if float(f[2]) > 4 and float(f[3]) > 4]
                    if valid:
                        face_count = len(valid)
                        ranked = sorted(valid, key=lambda f: float(f[2]) * float(f[3]), reverse=True)
                        best = ranked[0]
                        fx, fy, fw, fh = [float(v) for v in best[0:4]]
                        cx = (fx + fw * 0.5) / detect_w
                        cy = (fy + fh * 0.5) / detect_h
                        face_norm_x = cx * 2.0 - 1.0
                        face_norm_y = cy * 2.0 - 1.0
                        face_detected = True
                        track_kind = "face"

                        # Draw all face boxes
                        for idx, f in enumerate(ranked):
                            bx, by, bw, bh = int(f[0]), int(f[1]), int(f[2]), int(f[3])
                            color = (0, 255, 255) if idx == 0 else (0, 160, 0)
                            cv2.rectangle(frame, (bx, by), (bx + bw, by + bh), color, 2)
                            label = f"face{idx} ({float(f[14]):.2f})" if len(f) > 14 else f"face{idx}"
                            cv2.putText(frame, label, (bx, max(12, by - 4)),
                                        cv2.FONT_HERSHEY_SIMPLEX, 0.45, color, 1)

                            # Draw landmarks if available
                            if len(f) >= 14:
                                for li in range(4, 14, 2):
                                    lx, ly = int(f[li]), int(f[li + 1])
                                    cv2.circle(frame, (lx, ly), 3, (255, 0, 255), -1)

            # ── Hand detection ───────────────────────────────────────────
            if self.tune.get("hand_detection_enabled") and hand_detector is not None:
                hands = hand_detector.process(frame, mirrored=False)
                if hands:
                    best_hand = max(hands, key=lambda h: h.confidence)
                    if best_hand.pixel_landmarks:
                        xs = [lm[0] for lm in best_hand.pixel_landmarks]
                        ys = [lm[1] for lm in best_hand.pixel_landmarks]
                        hand_cx = (max(xs) + min(xs)) / 2.0
                        hand_cy = (max(ys) + min(ys)) / 2.0
                        hand_norm_x = (hand_cx / detect_w) * 2.0 - 1.0
                        hand_norm_y = (hand_cy / detect_h) * 2.0 - 1.0
                        hand_detected = True
                        hand_side = best_hand.physical_side
                        if not face_detected:
                            track_kind = "hand"
                    for hand in hands:
                        draw_skeleton(frame, hand, is_active=(hand.physical_side == hand_side))

            # ── Deadzone visualization ───────────────────────────────────
            dz_x = self.tune.get("deadzone_x")
            dz_y = self.tune.get("deadzone_y")
            h_frame, w_frame = frame.shape[:2]
            cx_px = w_frame // 2
            cy_px = h_frame // 2
            dz_half_w = int(dz_x * w_frame * 0.5)
            dz_half_h = int(dz_y * h_frame * 0.5)
            # Draw deadzone rectangle (red dashed-like)
            cv2.rectangle(frame,
                          (cx_px - dz_half_w, cy_px - dz_half_h),
                          (cx_px + dz_half_w, cy_px + dz_half_h),
                          (0, 0, 255), 1)
            # Draw center crosshair
            cv2.line(frame, (cx_px - 15, cy_px), (cx_px + 15, cy_px), (255, 255, 255), 1)
            cv2.line(frame, (cx_px, cy_px - 15), (cx_px, cy_px + 15), (255, 255, 255), 1)

            # Draw face target dot
            if face_detected:
                fx_px = int((face_norm_x + 1.0) * 0.5 * w_frame)
                fy_px = int((face_norm_y + 1.0) * 0.5 * h_frame)
                cv2.circle(frame, (fx_px, fy_px), 8, (0, 255, 255), 2)
                in_dz = abs(face_norm_x) < dz_x and abs(face_norm_y) < dz_y
                status = "IN DEADZONE" if in_dz else "TRACKING"
                cv2.putText(frame, status, (fx_px + 12, fy_px),
                            cv2.FONT_HERSHEY_SIMPLEX, 0.45,
                            (0, 255, 0) if in_dz else (0, 255, 255), 1)

            # ── HUD text ─────────────────────────────────────────────────
            detect_ms = (time.perf_counter() - t0) * 1000.0
            cv2.putText(frame, f"TRACK: {track_kind.upper()}", (10, 25),
                        cv2.FONT_HERSHEY_SIMPLEX, 0.65, (0, 255, 0), 2)
            cv2.putText(frame, f"Faces: {face_count}  Hands: {'Y' if hand_detected else 'N'}",
                        (10, 50), cv2.FONT_HERSHEY_SIMPLEX, 0.5, (200, 200, 200), 1)
            cv2.putText(frame, f"Detect: {detect_ms:.0f}ms  FPS: {self.tune.get('fps_actual'):.1f}",
                        (10, 72), cv2.FONT_HERSHEY_SIMPLEX, 0.45, (180, 180, 180), 1)
            cv2.putText(frame, f"DetRes: {detect_w}x{detect_h}  DZ: {dz_x:.3f}/{dz_y:.3f}",
                        (10, 92), cv2.FONT_HERSHEY_SIMPLEX, 0.4, (150, 150, 150), 1)

            # ── Resize for stream ────────────────────────────────────────
            stream_w = self.tune.get("stream_res_w")
            stream_h = self.tune.get("stream_res_h")
            stream_frame = cv2.resize(frame, (stream_w, stream_h), interpolation=cv2.INTER_LINEAR)
            if self.tune.get("swap_rb"):
                stream_frame = cv2.cvtColor(stream_frame, cv2.COLOR_BGR2RGB)

            # Encode JPEG
            quality = self.tune.get("jpeg_quality")
            if Image is not None:
                img = Image.fromarray(stream_frame)
                buf = io.BytesIO()
                img.save(buf, format="JPEG", quality=quality)
                jpg = buf.getvalue()
            else:
                _, jpg = cv2.imencode(".jpg", stream_frame, [cv2.IMWRITE_JPEG_QUALITY, quality])
                jpg = jpg.tobytes()

            with self._frame_lock:
                self.latest_frame = jpg

            # ── Update tune state stats ──────────────────────────────────
            fps_counter += 1
            elapsed = time.perf_counter() - fps_timer
            if elapsed >= 1.0:
                self.tune.set("fps_actual", fps_counter / elapsed)
                fps_counter = 0
                fps_timer = time.perf_counter()

            self.tune.update({
                "face_detected": face_detected,
                "face_norm_x": face_norm_x,
                "face_norm_y": face_norm_y,
                "face_count": face_count,
                "hand_detected": hand_detected,
                "hand_norm_x": hand_norm_x,
                "hand_norm_y": hand_norm_y,
                "hand_side": hand_side,
                "track_kind": track_kind,
                "detect_ms": detect_ms,
            })

            # ── Frame rate throttle ──────────────────────────────────────
            frame_time = 1.0 / max(1, vision_fps)
            spent = time.perf_counter() - t0
            if spent < frame_time:
                time.sleep(frame_time - spent)

    # ── Camera init ──────────────────────────────────────────────────────

    def _init_camera(self):
        try:
            import logging
            logging.getLogger("picamera2").setLevel(logging.WARNING)
            from picamera2 import Picamera2
            cam = Picamera2()
            main_res = (self.tune.get("main_res_w"), self.tune.get("main_res_h"))
            cfg = cam.create_video_configuration(
                main={"format": "RGB888", "size": main_res},
                buffer_count=2,
            )
            cam.configure(cfg)
            cam.start()
            print(f"[DetectionThread] Camera started: {main_res}")
            return cam
        except Exception as e:
            print(f"[DetectionThread] Camera init failed: {e}")
            return None

    def _init_face_detector(self):
        if not Path(self._face_model_path).exists():
            print(f"[DetectionThread] Face model not found: {self._face_model_path}")
            return None
        try:
            detect_res = (self.tune.get("detect_res_w"), self.tune.get("detect_res_h"))
            d = cv2.FaceDetectorYN.create(
                model=self._face_model_path,
                config="",
                input_size=detect_res,
                score_threshold=self.tune.get("confidence_threshold"),
                nms_threshold=self.tune.get("nms_threshold"),
                top_k=5000,
                backend_id=cv2.dnn.DNN_BACKEND_OPENCV,
                target_id=cv2.dnn.DNN_TARGET_CPU,
            )
            print("[DetectionThread] YuNet face detector initialized.")
            return d
        except Exception as e:
            print(f"[DetectionThread] Face detector init failed: {e}")
            return None


# ── Servo thread ─────────────────────────────────────────────────────────────

class ServoThread(threading.Thread):
    """Drives the Arduino servo link based on current track coordinates."""
    
    daemon = True
    
    def __init__(self, tune: TuneState, link: ArduinoServoLink | None):
        super().__init__(name="ServoThread")
        self.tune = tune
        self.link = link
        self.pan = self.tune.get("pan_center")
        self.tilt = self.tune.get("tilt_center")
        
    def run(self):
        if self.link is None or not self.link.connected:
            print("[ServoThread] No servo link. Simulation mode only.")
            
        print(f"[ServoThread] Started. Initial center: pan={self.pan:.1f}, tilt={self.tilt:.1f}")
        
        while True:
            time.sleep(0.04)  # ~25 Hz update rate
            
            if not self.tune.get("servo_enabled"):
                continue
                
            track_kind = self.tune.get("track_kind")
            if track_kind == "none":
                continue
                
            if track_kind == "face":
                norm_x = self.tune.get("face_norm_x")
                norm_y = self.tune.get("face_norm_y")
            elif track_kind == "hand":
                norm_x = self.tune.get("hand_norm_x")
                norm_y = self.tune.get("hand_norm_y")
            else:
                continue
                
            dz_x = self.tune.get("deadzone_x")
            dz_y = self.tune.get("deadzone_y")
            
            error_x = 0.0
            error_y = 0.0
            
            if abs(norm_x) > dz_x:
                error_x = norm_x - (dz_x if norm_x > 0 else -dz_x)
            if abs(norm_y) > dz_y:
                error_y = norm_y - (dz_y if norm_y > 0 else -dz_y)
                
            # Basic P controller
            p_gain = self.tune.get("pan_p_gain")
            t_gain = self.tune.get("tilt_p_gain")
            
            delta_pan = error_x * p_gain * self.tune.get("pan_sign")
            delta_tilt = error_y * t_gain * self.tune.get("tilt_sign")
            
            self.pan += delta_pan
            self.tilt += delta_tilt
            
            # Clamp limits
            self.pan = max(self.tune.get("pan_min"), min(self.tune.get("pan_max"), self.pan))
            self.tilt = max(self.tune.get("tilt_min"), min(self.tune.get("tilt_max"), self.tilt))
            
            self.tune.set("current_pan", self.pan)
            self.tune.set("current_tilt", self.tilt)
            
            if self.link is not None and self.link.connected:
                # Use force=False so it only sends if it changed significantly
                self.link.write_angles(self.pan, self.tilt, force=False)


# ── Web dashboard HTML ───────────────────────────────────────────────────────

DASHBOARD_HTML = """<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>Face + Hand Detection Tuner</title>
<style>
  * { margin: 0; padding: 0; box-sizing: border-box; }
  body {
    font-family: 'Segoe UI', Tahoma, Geneva, Verdana, sans-serif;
    background: #0a0a0f;
    color: #e0e0e0;
    min-height: 100vh;
  }
  .header {
    background: linear-gradient(135deg, #1a1a2e 0%, #16213e 100%);
    padding: 12px 24px;
    border-bottom: 2px solid #0f3460;
    display: flex;
    align-items: center;
    justify-content: space-between;
  }
  .header h1 {
    font-size: 1.3em;
    background: linear-gradient(90deg, #e94560, #0f3460);
    -webkit-background-clip: text;
    -webkit-text-fill-color: transparent;
  }
  .status-bar {
    display: flex; gap: 16px; font-size: 0.85em;
  }
  .status-bar .pill {
    padding: 4px 12px;
    border-radius: 12px;
    font-weight: 600;
  }
  .pill.face-on  { background: #0f3460; color: #4fc3f7; }
  .pill.face-off { background: #333; color: #666; }
  .pill.hand-on  { background: #1b5e20; color: #66bb6a; }
  .pill.hand-off { background: #333; color: #666; }

  .main {
    display: grid;
    grid-template-columns: 1fr 380px;
    gap: 16px;
    padding: 16px;
    max-width: 1400px;
    margin: 0 auto;
  }
  @media (max-width: 900px) {
    .main { grid-template-columns: 1fr; }
  }

  .stream-box {
    background: #111;
    border-radius: 12px;
    overflow: hidden;
    border: 1px solid #222;
  }
  .stream-box img {
    width: 100%;
    height: auto;
    display: block;
  }

  .panel {
    display: flex;
    flex-direction: column;
    gap: 12px;
  }
  .card {
    background: #14141f;
    border: 1px solid #222;
    border-radius: 10px;
    padding: 16px;
  }
  .card h3 {
    font-size: 0.9em;
    color: #e94560;
    margin-bottom: 10px;
    text-transform: uppercase;
    letter-spacing: 1px;
  }
  .slider-row {
    display: flex;
    align-items: center;
    gap: 8px;
    margin-bottom: 8px;
  }
  .slider-row label {
    flex: 0 0 140px;
    font-size: 0.82em;
    color: #aaa;
  }
  .slider-row input[type=range] {
    flex: 1;
    accent-color: #e94560;
  }
  .slider-row .val {
    flex: 0 0 60px;
    text-align: right;
    font-size: 0.82em;
    color: #4fc3f7;
    font-family: monospace;
  }
  .toggle-row {
    display: flex;
    align-items: center;
    gap: 10px;
    margin-bottom: 8px;
  }
  .toggle-row label { font-size: 0.85em; color: #ccc; }
  .toggle-row input[type=checkbox] {
    width: 18px; height: 18px; accent-color: #e94560;
  }

  .stats-grid {
    display: grid;
    grid-template-columns: 1fr 1fr;
    gap: 6px;
  }
  .stat {
    background: #1a1a2e;
    padding: 8px 10px;
    border-radius: 6px;
    font-size: 0.8em;
  }
  .stat .label { color: #888; }
  .stat .value { color: #4fc3f7; font-family: monospace; font-weight: 600; }

  .btn {
    padding: 8px 16px;
    border: none;
    border-radius: 6px;
    cursor: pointer;
    font-weight: 600;
    font-size: 0.85em;
    transition: all 0.2s;
  }
  .btn-apply {
    background: linear-gradient(135deg, #e94560, #c0392b);
    color: #fff;
  }
  .btn-apply:hover { filter: brightness(1.2); }
  .btn-reset {
    background: #333;
    color: #aaa;
  }
  .btn-reset:hover { background: #444; }
</style>
</head>
<body>
<div class="header">
  <h1>🎯 Face + Hand Detection Tuner</h1>
  <div class="status-bar">
    <span id="pill-face" class="pill face-off">FACE: --</span>
    <span id="pill-hand" class="pill hand-off">HAND: --</span>
    <span id="pill-fps" class="pill" style="background:#222;color:#aaa">FPS: --</span>
  </div>
</div>

<div class="main">
  <div class="stream-box">
    <img id="stream" src="/stream" alt="Camera Stream">
  </div>

  <div class="panel">
    <!-- Detection Toggles -->
    <div class="card">
      <h3>Detection</h3>
      <div class="toggle-row">
        <input type="checkbox" id="servo_enabled">
        <label for="servo_enabled">Enable Hardware Servos (Warning: Moves Robot)</label>
      </div>
      <div class="toggle-row">
        <input type="checkbox" id="face_detection_enabled" checked>
        <label for="face_detection_enabled">Face Detection (YuNet)</label>
      </div>
      <div class="toggle-row">
        <input type="checkbox" id="hand_detection_enabled" checked>
        <label for="hand_detection_enabled">Hand Detection (MediaPipe)</label>
      </div>
    </div>

    <!-- Camera Quality -->
    <div class="card">
      <h3>Camera Quality</h3>
      <div class="slider-row">
        <label>Detect Width</label>
        <input type="range" id="detect_res_w" min="160" max="1920" step="160">
        <span class="val" id="v_detect_res_w"></span>
      </div>
      <div class="slider-row">
        <label>Detect Height</label>
        <input type="range" id="detect_res_h" min="90" max="1080" step="90">
        <span class="val" id="v_detect_res_h"></span>
      </div>
      <div class="slider-row">
        <label>JPEG Quality</label>
        <input type="range" id="jpeg_quality" min="10" max="100" step="5">
        <span class="val" id="v_jpeg_quality"></span>
      </div>
      <div class="slider-row">
        <label>Vision FPS</label>
        <input type="range" id="vision_fps" min="1" max="30" step="1">
        <span class="val" id="v_vision_fps"></span>
      </div>
      <div class="slider-row">
        <label>Stream Width</label>
        <input type="range" id="stream_res_w" min="160" max="1280" step="160">
        <span class="val" id="v_stream_res_w"></span>
      </div>
      <div class="slider-row">
        <label>Stream Height</label>
        <input type="range" id="stream_res_h" min="90" max="720" step="90">
        <span class="val" id="v_stream_res_h"></span>
      </div>
      <div class="slider-row">
        <label>Confidence</label>
        <input type="range" id="confidence_threshold" min="0.1" max="0.95" step="0.05">
        <span class="val" id="v_confidence_threshold"></span>
      </div>
    </div>

    <!-- Servo Deadzones -->
    <div class="card">
      <h3>Servo Deadzones</h3>
      <div class="slider-row">
        <label>Deadzone X</label>
        <input type="range" id="deadzone_x" min="0" max="0.2" step="0.005">
        <span class="val" id="v_deadzone_x"></span>
      </div>
      <div class="slider-row">
        <label>Deadzone Y</label>
        <input type="range" id="deadzone_y" min="0" max="0.2" step="0.005">
        <span class="val" id="v_deadzone_y"></span>
      </div>
      <div class="slider-row">
        <label>Pan Gain (Speed)</label>
        <input type="range" id="pan_p_gain" min="0.1" max="10.0" step="0.1">
        <span class="val" id="v_pan_p_gain"></span>
      </div>
      <div class="slider-row">
        <label>Tilt Gain (Speed)</label>
        <input type="range" id="tilt_p_gain" min="0.1" max="10.0" step="0.1">
        <span class="val" id="v_tilt_p_gain"></span>
      </div>
    </div>

    <!-- Live Stats -->
    <div class="card">
      <h3>Live Stats</h3>
      <div class="stats-grid">
        <div class="stat"><span class="label">Face X:</span> <span class="value" id="s_face_x">--</span></div>
        <div class="stat"><span class="label">Face Y:</span> <span class="value" id="s_face_y">--</span></div>
        <div class="stat"><span class="label">Faces:</span> <span class="value" id="s_faces">--</span></div>
        <div class="stat"><span class="label">Track:</span> <span class="value" id="s_track">--</span></div>
        <div class="stat"><span class="label">Hand:</span> <span class="value" id="s_hand">--</span></div>
        <div class="stat"><span class="label">Detect ms:</span> <span class="value" id="s_detect">--</span></div>
        <div class="stat" style="grid-column: span 2"><span class="label">Servo Cmd:</span> <span class="value" id="s_servo">--</span></div>
      </div>
    </div>
  </div>
</div>

<script>
const SLIDERS = [
  'detect_res_w', 'detect_res_h', 'jpeg_quality', 'vision_fps',
  'stream_res_w', 'stream_res_h', 'confidence_threshold',
  'deadzone_x', 'deadzone_y', 'pan_p_gain', 'tilt_p_gain'
];
const TOGGLES = ['face_detection_enabled', 'hand_detection_enabled', 'servo_enabled'];

// Init: fetch state and populate UI
async function init() {
  const resp = await fetch('/api/state');
  const state = await resp.json();
  for (const id of SLIDERS) {
    const el = document.getElementById(id);
    if (el && state[id] !== undefined) {
      el.value = state[id];
      document.getElementById('v_' + id).textContent = state[id];
    }
  }
  for (const id of TOGGLES) {
    const el = document.getElementById(id);
    if (el && state[id] !== undefined) el.checked = state[id];
  }
}

// Send changes on slider input
for (const id of SLIDERS) {
  const el = document.getElementById(id);
  if (!el) continue;
  el.addEventListener('input', () => {
    document.getElementById('v_' + id).textContent = el.value;
  });
  el.addEventListener('change', async () => {
    await fetch('/api/tune', {
      method: 'POST',
      headers: {'Content-Type': 'application/json'},
      body: JSON.stringify({[id]: el.value})
    });
  });
}

for (const id of TOGGLES) {
  const el = document.getElementById(id);
  if (!el) continue;
  el.addEventListener('change', async () => {
    await fetch('/api/tune', {
      method: 'POST',
      headers: {'Content-Type': 'application/json'},
      body: JSON.stringify({[id]: el.checked})
    });
  });
}

// Poll live stats
setInterval(async () => {
  try {
    const resp = await fetch('/api/state');
    const s = await resp.json();
    document.getElementById('s_face_x').textContent = s.face_detected ? s.face_norm_x.toFixed(3) : '--';
    document.getElementById('s_face_y').textContent = s.face_detected ? s.face_norm_y.toFixed(3) : '--';
    document.getElementById('s_faces').textContent = s.face_count;
    document.getElementById('s_track').textContent = s.track_kind;
    document.getElementById('s_hand').textContent = s.hand_detected ? s.hand_side : 'No';
    document.getElementById('s_detect').textContent = s.detect_ms.toFixed(1);
    
    if (s.servo_enabled) {
      document.getElementById('s_servo').textContent = `Pan ${s.current_pan.toFixed(1)} / Tilt ${s.current_tilt.toFixed(1)}`;
      document.getElementById('s_servo').style.color = '#ff6b6b';
    } else {
      document.getElementById('s_servo').textContent = 'DISABLED';
      document.getElementById('s_servo').style.color = '#666';
    }

    const pf = document.getElementById('pill-face');
    pf.className = 'pill ' + (s.face_detected ? 'face-on' : 'face-off');
    pf.textContent = 'FACE: ' + (s.face_detected ? s.face_count : 'NO');

    const ph = document.getElementById('pill-hand');
    ph.className = 'pill ' + (s.hand_detected ? 'hand-on' : 'hand-off');
    ph.textContent = 'HAND: ' + (s.hand_detected ? s.hand_side : 'NO');

    document.getElementById('pill-fps').textContent = 'FPS: ' + s.fps_actual.toFixed(1);
  } catch(e) {}
}, 500);

init();
</script>
</body>
</html>
"""

# ── HTTP handler ─────────────────────────────────────────────────────────────

class StreamHandler(BaseHTTPRequestHandler):
    tune: TuneState
    detection_thread: DetectionThread

    def log_message(self, format, *args):
        return  # suppress logs

    def do_GET(self):
        if self.path in ("/", "/index.html"):
            body = DASHBOARD_HTML.encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
            return

        if self.path == "/stream":
            self.send_response(200)
            self.send_header("Age", "0")
            self.send_header("Cache-Control", "no-cache, private")
            self.send_header("Pragma", "no-cache")
            self.send_header("Content-Type", "multipart/x-mixed-replace; boundary=frame")
            self.end_headers()
            try:
                while True:
                    jpg = self.detection_thread.get_frame()
                    if jpg is None:
                        time.sleep(0.05)
                        continue
                    self.wfile.write(b"--frame\r\n")
                    self.wfile.write(b"Content-Type: image/jpeg\r\n")
                    self.wfile.write(f"Content-Length: {len(jpg)}\r\n\r\n".encode("utf-8"))
                    self.wfile.write(jpg)
                    self.wfile.write(b"\r\n")
                    fps = max(1, self.tune.get("stream_fps"))
                    time.sleep(1.0 / fps)
            except (BrokenPipeError, ConnectionResetError):
                return
            return

        if self.path == "/api/state":
            snap = self.tune.snapshot()
            body = json.dumps(snap).encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Cache-Control", "no-store")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
            return

        self.send_error(404)

    def do_POST(self):
        if self.path == "/api/tune":
            length = int(self.headers.get("Content-Length", 0))
            raw = self.rfile.read(length) if length > 0 else b"{}"
            try:
                payload = json.loads(raw.decode("utf-8"))
            except json.JSONDecodeError:
                self.send_error(400)
                return
            self.tune.update(payload)
            snap = self.tune.snapshot()
            body = json.dumps({"ok": True, "state": snap}).encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
            return
        self.send_error(404)


class ThreadingHTTPServer(socketserver.ThreadingMixIn, HTTPServer):
    daemon_threads = True
    allow_reuse_address = True


# ── Main ─────────────────────────────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser(description="Face + Hand detection test with web tuner")
    parser.add_argument("--config", type=str, default=None,
                        help="Config YAML path (default: config.yaml)")
    parser.add_argument("--port", type=int, default=9090,
                        help="Web server port (default: 9090)")
    parser.add_argument("--host", type=str, default="0.0.0.0",
                        help="Bind host (default: 0.0.0.0 = all interfaces)")
    parser.add_argument("--serial-port", type=str, default=None,
                        help="ESP32 serial port (default: auto)")
    parser.add_argument("--baud", type=int, default=None,
                        help="ESP32 baud rate (default: from config)")
    args = parser.parse_args()

    config_path = Path(args.config) if args.config else APP_DIR / "config.yaml"
    if not config_path.is_absolute():
        config_path = APP_DIR / config_path
    cfg = _load_yaml(config_path)
    
    servo_cfg = cfg.get("servo", {}) or {}
    baud = args.baud if args.baud is not None else int(servo_cfg.get("baud", 115200))
    serial_port = args.serial_port or servo_cfg.get("port") or ""

    print(f"[Main] Config: {config_path}")
    print(f"[Main] Server: http://0.0.0.0:{args.port}/")
    print(f"[Main] Access from any device on your network at http://<pi-ip>:{args.port}/")
    
    print(f"[Main] Connecting to Arduino on {serial_port or 'auto'} @ {baud}...")
    link = ArduinoServoLink(port=serial_port, baud=baud)
    if not link.connect():
        print("[Main] WARNING: Arduino connect failed. Servos will NOT move.")
        link = None
    else:
        print("[Main] Arduino connected!")

    tune = TuneState(cfg)
    det = DetectionThread(tune, cfg)
    det.start()
    
    servo_thread = ServoThread(tune, link)
    servo_thread.start()

    handler = type("BoundStreamHandler", (StreamHandler,), {
        "tune": tune,
        "detection_thread": det,
    })

    server = ThreadingHTTPServer((args.host, args.port), handler)
    print(f"[Main] Web server started on port {args.port}")

    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\n[Main] Shutting down...")
        server.shutdown()
        server.server_close()


if __name__ == "__main__":
    main()
