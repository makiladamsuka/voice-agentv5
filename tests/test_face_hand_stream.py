"""Standalone face + hand detection test with live web tuning dashboard.

Runs the camera with face detection (YuNet) and hand detection (MediaPipe),
streams annotated MJPEG to a web server accessible from any browser.

Features:
  - WASD keyboard controls for manual head pan/tilt
  - JK keyboard controls for base rotation
  - Face & hand detection with live toggles
  - Servo deadzone adjustments (pan/tilt)
  - Hi/Bye/Talk arm animations with speed control
  - Text command box to trigger hi/bye/talk
  - MJPEG stream with detection overlays
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
import random
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
from lib.elastic_head_motion import HeadMotionParams, tick_toward
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


# ── Arm pose presets ─────────────────────────────────────────────────────────

def _load_presets(path: Path) -> dict:
    if not path.exists():
        return {}
    with open(path, encoding="utf-8") as f:
        return json.load(f)

ARM_PRESETS_PATH = APP_DIR / "tests" / "arm_pose_presets.json"


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

        # Servo
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

        # Servo tuning
        self.servo_enabled = False
        self.pan_p_gain = 4.0
        self.tilt_p_gain = 3.0

        # Detection toggles
        self.face_detection_enabled = True
        self.hand_detection_enabled = True
        self.face_tracking_enabled = True
        self.hand_tracking_enabled = True

        # Gesture toggles
        self.hi_gesture_enabled = True
        self.bye_gesture_enabled = True
        self.talk_gesture_enabled = True

        # Animation speeds (seconds per frame or multipliers)
        self.hi_speed_v = float(_cfg(cfg, "hi_gesture", "vertical_speed", default=1.0))
        self.hi_speed_h = float(_cfg(cfg, "hi_gesture", "horizontal_speed", default=1.5))
        self.bye_speed_v = float(_cfg(cfg, "bye_gesture", "vertical_speed", default=1.0))
        self.bye_speed_h = float(_cfg(cfg, "bye_gesture", "horizontal_speed", default=1.5))
        self.talk_speed_v = float(_cfg(cfg, "talk_gesture", "vertical_speed", default=1.0))
        self.talk_speed_h = float(_cfg(cfg, "talk_gesture", "horizontal_speed", default=1.5))
        self.base_rotate_deg = 15.0

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

        # Servo state
        self.current_pan = self.pan_center
        self.current_tilt = self.tilt_center

        # Manual control deltas (set by keyboard, consumed by servo thread)
        self.manual_pan_delta = 0.0
        self.manual_tilt_delta = 0.0
        self.manual_arm_up = False
        self.manual_arm_down = False
        self.manual_arm_left = False
        self.manual_arm_right = False
        self.manual_mode = False
        self.manual_step = 2.0

        # Animation state
        self.animation_active = ""
        self.animation_log = ""

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
                    elif isinstance(old, str):
                        setattr(self, k, str(v))


# ── Detection thread ─────────────────────────────────────────────────────────

class DetectionThread(threading.Thread):
    """Captures frames, runs YuNet + MediaPipe, updates TuneState."""

    daemon = True

    def __init__(self, tune: TuneState, cfg: dict):
        super().__init__(name="DetectionThread")
        self.tune = tune
        self.cfg = cfg
        self.latest_frame = None
        self._frame_lock = threading.Lock()
        cam_cfg = _cfg(cfg, "camera", default={}) or {}
        self._face_model_path = str(APP_DIR / cam_cfg.get("face_model_path", "face_detection_yunet_2023mar.onnx"))

    def get_frame(self) -> bytes | None:
        with self._frame_lock:
            return self.latest_frame

    def run(self):
        cam = self._init_camera()
        if cam is None:
            print("[DetectionThread] Camera unavailable — exiting.")
            return

        detector = self._init_face_detector()
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
            hand_detected = False
            hand_norm_x = 0.0
            hand_norm_y = 0.0
            hand_side = ""
            track_kind = "none"

            # ── Face detection ───────────────────────────────────────────
            if self.tune.get("face_detection_enabled") and detector is not None:
                detector.setInputSize(detect_res)
                detector.setScoreThreshold(self.tune.get("confidence_threshold"))
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
                        if self.tune.get("face_tracking_enabled"):
                            track_kind = "face"

                        # Draw all face boxes
                        for idx, f in enumerate(ranked):
                            bx, by, bw, bh = int(f[0]), int(f[1]), int(f[2]), int(f[3])
                            color = (0, 255, 255) if idx == 0 else (0, 160, 0)
                            cv2.rectangle(frame, (bx, by), (bx + bw, by + bh), color, 2)
                            label = f"face{idx} ({float(f[14]):.2f})" if len(f) > 14 else f"face{idx}"
                            cv2.putText(frame, label, (bx, max(12, by - 4)),
                                        cv2.FONT_HERSHEY_SIMPLEX, 0.45, color, 1)
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
                        if not face_detected and self.tune.get("hand_tracking_enabled"):
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
            cv2.rectangle(frame,
                          (cx_px - dz_half_w, cy_px - dz_half_h),
                          (cx_px + dz_half_w, cy_px + dz_half_h),
                          (0, 0, 255), 1)
            cv2.line(frame, (cx_px - 15, cy_px), (cx_px + 15, cy_px), (255, 255, 255), 1)
            cv2.line(frame, (cx_px, cy_px - 15), (cx_px, cy_px + 15), (255, 255, 255), 1)

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

            anim = self.tune.get("animation_active")
            if anim:
                cv2.putText(frame, f"ANIM: {anim.upper()}", (10, h_frame - 20),
                            cv2.FONT_HERSHEY_SIMPLEX, 0.7, (0, 200, 255), 2)

            manual = self.tune.get("manual_mode")
            if manual:
                cv2.putText(frame, "MANUAL (WASD)", (w_frame - 200, 25),
                            cv2.FONT_HERSHEY_SIMPLEX, 0.55, (255, 100, 100), 2)

            # ── Resize for stream ────────────────────────────────────────
            stream_w = self.tune.get("stream_res_w")
            stream_h = self.tune.get("stream_res_h")
            stream_frame = cv2.resize(frame, (stream_w, stream_h), interpolation=cv2.INTER_LINEAR)
            if self.tune.get("swap_rb"):
                stream_frame = cv2.cvtColor(stream_frame, cv2.COLOR_BGR2RGB)

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

            frame_time = 1.0 / max(1, vision_fps)
            spent = time.perf_counter() - t0
            if spent < frame_time:
                time.sleep(frame_time - spent)

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
        self.a0 = 47.0
        self.a1 = 65.0
        self.a2 = 54.0
        self.a3 = 76.0

    def run(self):
        if self.link is None or not self.link.connected:
            print("[ServoThread] No servo link. Simulation mode only.")

        print(f"[ServoThread] Started. Initial center: pan={self.pan:.1f}, tilt={self.tilt:.1f}")

        while True:
            time.sleep(0.04)  # ~25 Hz

            if not self.tune.get("servo_enabled"):
                continue

            # ── Manual WASD & IJKL control ────────────────────────────────
            pan_delta = self.tune.get("manual_pan_delta")
            tilt_delta = self.tune.get("manual_tilt_delta")
            arm_up = self.tune.get("manual_arm_up")
            arm_down = self.tune.get("manual_arm_down")
            arm_left = self.tune.get("manual_arm_left")
            arm_right = self.tune.get("manual_arm_right")
            manual_mode = self.tune.get("manual_mode")

            is_head_moving = abs(pan_delta) > 0.001 or abs(tilt_delta) > 0.001
            is_arm_moving = arm_up or arm_down or arm_left or arm_right

            if manual_mode and (is_head_moving or is_arm_moving):
                step = self.tune.get("manual_step")
                
                if is_head_moving:
                    self.pan += pan_delta * step
                    self.tilt += tilt_delta * step
                    self.pan = max(self.tune.get("pan_min"), min(self.tune.get("pan_max"), self.pan))
                    self.tilt = max(self.tune.get("tilt_min"), min(self.tune.get("tilt_max"), self.tilt))
                    self.tune.set("current_pan", self.pan)
                    self.tune.set("current_tilt", self.tilt)
                    
                if is_arm_moving:
                    if arm_up:
                        self.a0 += step
                        self.a1 += step
                    if arm_down:
                        self.a0 -= step
                        self.a1 -= step
                    if arm_left:
                        self.a2 -= step
                        self.a3 += step
                    if arm_right:
                        self.a2 += step
                        self.a3 -= step
                    self.a0 = max(0, min(180, self.a0))
                    self.a1 = max(0, min(180, self.a1))
                    self.a2 = max(0, min(180, self.a2))
                    self.a3 = max(0, min(180, self.a3))
                    
                if self.link is not None and self.link.connected:
                    if is_head_moving:
                        self.link.write_angles(self.pan, self.tilt, force=True)
                    if is_arm_moving:
                        self.link.write_arms(self.a0, self.a1, self.a2, self.a3, force=True)
                continue

            # ── Auto tracking ────────────────────────────────────────────
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

            p_gain = self.tune.get("pan_p_gain")
            t_gain = self.tune.get("tilt_p_gain")

            delta_pan = error_x * p_gain * self.tune.get("pan_sign")
            delta_tilt = error_y * t_gain * self.tune.get("tilt_sign")

            self.pan += delta_pan
            self.tilt += delta_tilt

            self.pan = max(self.tune.get("pan_min"), min(self.tune.get("pan_max"), self.pan))
            self.tilt = max(self.tune.get("tilt_min"), min(self.tune.get("tilt_max"), self.tilt))

            self.tune.set("current_pan", self.pan)
            self.tune.set("current_tilt", self.tilt)

            if self.link is not None and self.link.connected:
                self.link.write_angles(self.pan, self.tilt, force=False)


# ── Animation thread ─────────────────────────────────────────────────────────

class AnimationRunner:
    """Plays arm animations using ArduinoServoLink."""

    def __init__(self, tune: TuneState, link: ArduinoServoLink | None):
        self.tune = tune
        self.link = link
        self._presets = _load_presets(ARM_PRESETS_PATH)
        self._lock = threading.Lock()

    def play(self, command: str) -> str:
        """Play an animation by name. Returns status string."""
        if self.link is None or not self.link.connected:
            return "No servo link connected"

        with self._lock:
            if command == "hi":
                return self._play_hi()
            elif command == "bye":
                return self._play_bye()
            elif command == "talk":
                return self._play_talk()
            elif command == "home":
                return self._play_home()
            else:
                return f"Unknown command: {command}"

    def _play_elastic_sequence(self, sequence, speed_v, speed_h, home, hold_home_sec=0.0, pose_duration=0.0):
        # Init state
        arms = [home["a0"], home["a1"], home["a2"], home["a3"]]
        vels = [0.0, 0.0, 0.0, 0.0]
        
        base_vel = 120.0
        base_accel = 300.0
        base_decel = 350.0
        
        p_v = HeadMotionParams(
            max_vel_pos=base_vel * speed_v, max_vel_neg=base_vel * speed_v,
            accel=base_accel * speed_v, decel=base_decel * speed_v, track_gain=10.0
        )
        p_h = HeadMotionParams(
            max_vel_pos=base_vel * speed_h, max_vel_neg=base_vel * speed_h,
            accel=base_accel * speed_h, decel=base_decel * speed_h, track_gain=10.0
        )
        
        dt = 0.03
        for target_pose in sequence:
            targets = [target_pose["a0"], target_pose["a1"], target_pose["a2"], target_pose["a3"]]
            
            # Move elastically until target is reached (position error < 2.0)
            max_ticks = int(3.0 / dt)  # 3s max per pose to avoid infinite loops
            ticks_taken = 0
            for _ in range(max_ticks):
                moved = False
                for i, p in enumerate([p_v, p_v, p_h, p_h]):
                    arms[i], vels[i] = tick_toward(arms[i], vels[i], targets[i], dt, lo=0, hi=180, params=p)
                    # Only check position error, ignoring velocity to avoid oscillation stalls
                    if abs(arms[i] - targets[i]) > 2.0:
                        moved = True
                        
                self.link.write_arms(arms[0], arms[1], arms[2], arms[3], force=True)
                time.sleep(dt)
                ticks_taken += 1
                if not moved:
                    break

            # If pose_duration is set, hold the pose for the remaining time
            if pose_duration > 0.0:
                elapsed = ticks_taken * dt
                if elapsed < pose_duration:
                    remaining_ticks = int((pose_duration - elapsed) / dt)
                    for _ in range(remaining_ticks):
                        for i, p in enumerate([p_v, p_v, p_h, p_h]):
                            arms[i], vels[i] = tick_toward(arms[i], vels[i], targets[i], dt, lo=0, hi=180, params=p)
                        self.link.write_arms(arms[0], arms[1], arms[2], arms[3], force=True)
                        time.sleep(dt)

        if hold_home_sec > 0.0:
            hold_ticks = int(hold_home_sec / dt)
            for _ in range(hold_ticks):
                for i, p in enumerate([p_v, p_v, p_h, p_h]):
                    arms[i], vels[i] = tick_toward(arms[i], vels[i], targets[i], dt, lo=0, hi=180, params=p)
                self.link.write_arms(arms[0], arms[1], arms[2], arms[3], force=True)
                time.sleep(dt)

        # Return home elastically
        targets = [home["a0"], home["a1"], home["a2"], home["a3"]]
        max_ticks = int(3.0 / dt)
        for _ in range(max_ticks):
            moved = False
            for i, p in enumerate([p_v, p_v, p_h, p_h]):
                arms[i], vels[i] = tick_toward(arms[i], vels[i], targets[i], dt, lo=0, hi=180, params=p)
                if abs(arms[i] - targets[i]) > 2.0 or abs(vels[i]) > 5.0:
                    moved = True
                    
            self.link.write_arms(arms[0], arms[1], arms[2], arms[3], force=True)
            time.sleep(dt)
            if not moved:
                break

    def _play_hi(self):
        self.tune.set("animation_active", "hi")
        speed_v = self.tune.get("hi_speed_v")
        speed_h = self.tune.get("hi_speed_h")
        
        poses = self._presets.get("poses", {})
        hi_poses = [poses.get(f"hi{i}") for i in range(1, 5) if poses.get(f"hi{i}")]
        home = poses.get("home", {"a0": 47, "a1": 65, "a2": 54, "a3": 76})

        if not hi_poses:
            self.tune.set("animation_active", "")
            return "No hi poses found"

        # Optional base rotate (run in background so arms move concurrently)
        base_deg = self.tune.get("base_rotate_deg")
        if base_deg > 0:
            def _spin():
                try:
                    self.link.write_base_step_spin(base_deg, timeout_sec=3.0)
                except Exception:
                    pass
            threading.Thread(target=_spin, daemon=True).start()

        # Play a single randomly selected hi pose elastically with 1-3s hold before returning home
        chosen_hi = random.choice(hi_poses)
        hold_time = random.uniform(1.0, 3.0)
        self._play_elastic_sequence([chosen_hi], speed_v, speed_h, home, hold_home_sec=hold_time)

        # Rotate back (concurrent with returning home)
        if base_deg > 0:
            def _spin_back():
                try:
                    self.link.write_base_step_spin(-base_deg, timeout_sec=3.0)
                except Exception:
                    pass
            threading.Thread(target=_spin_back, daemon=True).start()

        self.tune.set("animation_active", "")
        return "Hi wave complete (smooth)"

    def _play_bye(self):
        self.tune.set("animation_active", "bye")
        speed_v = self.tune.get("bye_speed_v")
        speed_h = self.tune.get("bye_speed_h")
        anims = self._presets.get("animations", {})
        bye_names = [k for k in anims if k.startswith("bye")]
        if not bye_names:
            self.tune.set("animation_active", "")
            return "No bye animations found"

        chosen = random.choice(bye_names)
        frames = anims[chosen].get("frames", [])
        poses = self._presets.get("poses", {})
        home = poses.get("home", {"a0": 47, "a1": 65, "a2": 54, "a3": 76})

        # Optional base rotate (run in background so arms move concurrently)
        base_deg = self.tune.get("base_rotate_deg")
        if base_deg > 0:
            def _spin():
                try:
                    self.link.write_base_step_spin(-base_deg, timeout_sec=3.0)
                except Exception:
                    pass
            threading.Thread(target=_spin, daemon=True).start()

        self._play_elastic_sequence(frames, speed_v, speed_h, home, pose_duration=0.4)

        # Rotate back
        if base_deg > 0:
            def _spin_back():
                try:
                    self.link.write_base_step_spin(base_deg, timeout_sec=3.0)
                except Exception:
                    pass
            threading.Thread(target=_spin_back, daemon=True).start()

        self.tune.set("animation_active", "")
        return f"Bye wave ({chosen}) complete (elastic)"

    def _play_talk(self):
        self.tune.set("animation_active", "talk")
        speed_v = self.tune.get("talk_speed_v")
        speed_h = self.tune.get("talk_speed_h")
        
        poses = self._presets.get("poses", {})
        talk_poses = [poses.get(f"talk{i}") for i in range(1, 11) if poses.get(f"talk{i}")]
        home = poses.get("home", {"a0": 47, "a1": 65, "a2": 54, "a3": 76})

        if not talk_poses:
            self.tune.set("animation_active", "")
            return "No talk poses found"

        count = min(len(talk_poses), random.randint(4, 6))
        sequence = random.sample(talk_poses, count)
        
        self._play_elastic_sequence(sequence, speed_v, speed_h, home, pose_duration=0.5)

        self.tune.set("animation_active", "")
        return "Talk gesture complete (elastic)"

    def _play_home(self):
        self.tune.set("animation_active", "home")
        poses = self._presets.get("poses", {})
        home = poses.get("home", {"a0": 47, "a1": 65, "a2": 54, "a3": 76})
        self._play_elastic_sequence([], speed_v=1.0, speed_h=1.0, home=home, hold_home_sec=0.5)
        self.tune.set("animation_active", "")
        return "Returned to home pose (elastic)"


# ── Web dashboard HTML ───────────────────────────────────────────────────────

DASHBOARD_HTML = """<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>Robot Control Dashboard</title>
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
    padding: 10px 20px;
    border-bottom: 2px solid #0f3460;
    display: flex; align-items: center; justify-content: space-between; flex-wrap: wrap; gap: 8px;
  }
  .header h1 { font-size: 1.2em; background: linear-gradient(90deg, #e94560, #0f3460);
    -webkit-background-clip: text; -webkit-text-fill-color: transparent; }
  .status-bar { display: flex; gap: 10px; font-size: 0.8em; }
  .pill { padding: 3px 10px; border-radius: 10px; font-weight: 600; }
  .pill.on { background: #0f3460; color: #4fc3f7; }
  .pill.off { background: #333; color: #666; }
  .pill.anim { background: #b71c1c; color: #ff8a80; animation: pulse 0.5s infinite alternate; }
  @keyframes pulse { to { opacity: 0.6; } }

  .main { display: grid; grid-template-columns: 1fr 400px; gap: 12px; padding: 12px; max-width: 1500px; margin: 0 auto; }
  @media (max-width: 900px) { .main { grid-template-columns: 1fr; } }

  .stream-box { background: #111; border-radius: 10px; overflow: hidden; border: 1px solid #222; }
  .stream-box img { width: 100%; height: auto; display: block; }

  .panel { display: flex; flex-direction: column; gap: 10px; max-height: 90vh; overflow-y: auto; }
  .card { background: #14141f; border: 1px solid #222; border-radius: 8px; padding: 12px; }
  .card h3 { font-size: 0.85em; color: #e94560; margin-bottom: 8px; text-transform: uppercase; letter-spacing: 1px; }
  .slider-row { display: flex; align-items: center; gap: 6px; margin-bottom: 6px; }
  .slider-row label { flex: 0 0 120px; font-size: 0.78em; color: #aaa; }
  .slider-row input[type=range] { flex: 1; accent-color: #e94560; }
  .slider-row .val { flex: 0 0 50px; text-align: right; font-size: 0.78em; color: #4fc3f7; font-family: monospace; }
  .toggle-row { display: flex; align-items: center; gap: 8px; margin-bottom: 5px; }
  .toggle-row label { font-size: 0.8em; color: #ccc; }
  .toggle-row input[type=checkbox] { width: 16px; height: 16px; accent-color: #e94560; }

  .stats-grid { display: grid; grid-template-columns: 1fr 1fr; gap: 4px; }
  .stat { background: #1a1a2e; padding: 6px 8px; border-radius: 5px; font-size: 0.75em; }
  .stat .label { color: #888; }
  .stat .value { color: #4fc3f7; font-family: monospace; font-weight: 600; }

  .cmd-row { display: flex; gap: 6px; margin-top: 6px; }
  .cmd-input { flex: 1; background: #1a1a2e; border: 1px solid #333; color: #fff; padding: 8px 10px;
    border-radius: 6px; font-size: 0.85em; }
  .cmd-input:focus { outline: none; border-color: #e94560; }

  .btn { padding: 7px 14px; border: none; border-radius: 6px; cursor: pointer; font-weight: 600;
    font-size: 0.8em; transition: all 0.15s; }
  .btn-send { background: linear-gradient(135deg, #e94560, #c0392b); color: #fff; }
  .btn-send:hover { filter: brightness(1.2); }
  .btn-action { background: #1a5276; color: #85c1e9; }
  .btn-action:hover { background: #1f618d; }
  .btn-warn { background: #7b241c; color: #f5b7b1; }
  .btn-warn:hover { background: #922b21; }

  .btn-group { display: flex; gap: 5px; flex-wrap: wrap; margin-top: 6px; }

  .kbd { display: inline-block; padding: 2px 7px; background: #2c2c3e; border: 1px solid #444;
    border-radius: 4px; font-family: monospace; font-size: 0.8em; color: #4fc3f7; min-width: 24px; text-align: center; }
  .kbd.active { background: #e94560; color: #fff; border-color: #e94560; }

  .keys-grid { display: grid; grid-template-columns: repeat(3, 36px); gap: 3px; justify-content: center; margin: 6px 0; }
  .keys-grid .placeholder { visibility: hidden; }

  .log-box { background: #0a0a0f; border: 1px solid #222; border-radius: 6px; padding: 8px;
    font-family: monospace; font-size: 0.72em; color: #66bb6a; max-height: 80px; overflow-y: auto; }
</style>
</head>
<body>
<div class="header">
  <h1>🤖 Robot Control Dashboard</h1>
  <div class="status-bar">
    <span id="pill-face" class="pill off">FACE: --</span>
    <span id="pill-hand" class="pill off">HAND: --</span>
    <span id="pill-anim" class="pill off">ANIM: --</span>
    <span id="pill-fps" class="pill" style="background:#222;color:#aaa">FPS: --</span>
  </div>
</div>

<div class="main">
  <div class="stream-box">
    <img id="stream" src="/stream" alt="Camera Stream">
  </div>

  <div class="panel">
    <!-- Manual Controls -->
    <div class="card">
      <h3>🎮 Manual Controls</h3>
      <div class="toggle-row">
        <input type="checkbox" id="servo_enabled">
        <label for="servo_enabled">Enable Servos (⚠ Moves Robot)</label>
      </div>
      <div class="toggle-row">
        <input type="checkbox" id="launch_kiosk">
        <label for="launch_kiosk">Launch Kiosk Mode</label>
      </div>
      <div style="display:flex;gap:20px;align-items:flex-start;margin-top:8px">
        <div style="text-align:center">
          <div style="font-size:0.75em;color:#888;margin-bottom:4px">Head (WASD)</div>
          <div class="keys-grid">
            <div class="placeholder"></div><span class="kbd" id="k_w">W</span><div class="placeholder"></div>
            <span class="kbd" id="k_a">A</span><span class="kbd" id="k_s">S</span><span class="kbd" id="k_d">D</span>
          </div>
        </div>
        <div style="text-align:center">
          <div style="font-size:0.75em;color:#888;margin-bottom:4px">Arms (IJKL)</div>
          <div class="keys-grid">
            <div class="placeholder"></div><span class="kbd" id="k_i">I</span><div class="placeholder"></div>
            <span class="kbd" id="k_j">J</span><span class="kbd" id="k_k">K</span><span class="kbd" id="k_l">L</span>
          </div>
        </div>
        <div style="text-align:center">
          <div style="font-size:0.75em;color:#888;margin-bottom:4px">Base (1/2)</div>
          <div style="display:flex;gap:3px;justify-content:center;margin-top:3px">
            <span class="kbd" id="k_1">1 ←</span>
            <span class="kbd" id="k_2">2 →</span>
          </div>
        </div>
      </div>
      <div class="slider-row" style="margin-top:8px">
        <label>Step Size</label>
        <input type="range" id="manual_step" min="0.5" max="8.0" step="0.5">
        <span class="val" id="v_manual_step"></span>
      </div>
    </div>

    <!-- Detection Toggles -->
    <div class="card">
      <h3>👁 Detection & Tracking</h3>
      <div class="toggle-row"><input type="checkbox" id="face_detection_enabled" checked><label>Face Detection</label></div>
      <div class="toggle-row"><input type="checkbox" id="hand_detection_enabled" checked><label>Hand Detection</label></div>
      <div class="toggle-row"><input type="checkbox" id="face_tracking_enabled" checked><label>Face Tracking (servo follows face)</label></div>
      <div class="toggle-row"><input type="checkbox" id="hand_tracking_enabled" checked><label>Hand Tracking (servo follows hand)</label></div>
    </div>

    <!-- Gestures & Animations -->
    <div class="card">
      <h3>🤚 Gestures & Animations</h3>
      <div class="toggle-row"><input type="checkbox" id="hi_gesture_enabled" checked><label>Hi Wave</label></div>
      <div class="toggle-row"><input type="checkbox" id="bye_gesture_enabled" checked><label>Bye Wave</label></div>
      <div class="toggle-row"><input type="checkbox" id="talk_gesture_enabled" checked><label>Talk Gesture</label></div>

      <div class="slider-row"><label>Hi V-Speed</label><input type="range" id="hi_speed_v" min="0.1" max="4.0" step="0.1"><span class="val" id="v_hi_speed_v"></span></div>
      <div class="slider-row"><label>Hi H-Speed</label><input type="range" id="hi_speed_h" min="0.1" max="4.0" step="0.1"><span class="val" id="v_hi_speed_h"></span></div>
      <div class="slider-row"><label>Bye V-Speed</label><input type="range" id="bye_speed_v" min="0.1" max="4.0" step="0.1"><span class="val" id="v_bye_speed_v"></span></div>
      <div class="slider-row"><label>Bye H-Speed</label><input type="range" id="bye_speed_h" min="0.1" max="4.0" step="0.1"><span class="val" id="v_bye_speed_h"></span></div>
      <div class="slider-row"><label>Talk V-Speed</label><input type="range" id="talk_speed_v" min="0.1" max="4.0" step="0.1"><span class="val" id="v_talk_speed_v"></span></div>
      <div class="slider-row"><label>Talk H-Speed</label><input type="range" id="talk_speed_h" min="0.1" max="4.0" step="0.1"><span class="val" id="v_talk_speed_h"></span></div>
      <div class="slider-row"><label>Base Rotate °</label><input type="range" id="base_rotate_deg" min="0" max="45" step="1"><span class="val" id="v_base_rotate_deg"></span></div>

      <div class="btn-group">
        <button class="btn btn-action" onclick="sendCmd('hi')">👋 Hi</button>
        <button class="btn btn-action" onclick="sendCmd('bye')">🙋 Bye</button>
        <button class="btn btn-action" onclick="sendCmd('talk')">💬 Talk</button>
        <button class="btn btn-warn" onclick="sendCmd('home')">🏠 Home</button>
      </div>

      <div class="cmd-row" style="margin-top:8px">
        <input type="text" class="cmd-input" id="cmd-box" placeholder="Type command: hi, bye, talk, home..." 
               onkeydown="if(event.key==='Enter'){sendCmd(this.value);this.value=''}">
        <button class="btn btn-send" onclick="sendCmd(document.getElementById('cmd-box').value);document.getElementById('cmd-box').value=''">Send</button>
      </div>

      <div class="log-box" id="anim-log">Ready.</div>
    </div>

    <!-- Camera Quality -->
    <div class="card">
      <h3>📷 Camera</h3>
      <div class="slider-row"><label>Detect W</label><input type="range" id="detect_res_w" min="160" max="1920" step="160"><span class="val" id="v_detect_res_w"></span></div>
      <div class="slider-row"><label>Detect H</label><input type="range" id="detect_res_h" min="90" max="1080" step="90"><span class="val" id="v_detect_res_h"></span></div>
      <div class="slider-row"><label>JPEG Quality</label><input type="range" id="jpeg_quality" min="10" max="100" step="5"><span class="val" id="v_jpeg_quality"></span></div>
      <div class="slider-row"><label>Vision FPS</label><input type="range" id="vision_fps" min="1" max="30" step="1"><span class="val" id="v_vision_fps"></span></div>
      <div class="slider-row"><label>Confidence</label><input type="range" id="confidence_threshold" min="0.1" max="0.95" step="0.05"><span class="val" id="v_confidence_threshold"></span></div>
    </div>

    <!-- Servo Deadzones -->
    <div class="card">
      <h3>⚙ Servo Tuning</h3>
      <div class="slider-row"><label>Deadzone X</label><input type="range" id="deadzone_x" min="0" max="0.2" step="0.005"><span class="val" id="v_deadzone_x"></span></div>
      <div class="slider-row"><label>Deadzone Y</label><input type="range" id="deadzone_y" min="0" max="0.2" step="0.005"><span class="val" id="v_deadzone_y"></span></div>
      <div class="slider-row"><label>Pan Gain</label><input type="range" id="pan_p_gain" min="0.1" max="10.0" step="0.1"><span class="val" id="v_pan_p_gain"></span></div>
      <div class="slider-row"><label>Tilt Gain</label><input type="range" id="tilt_p_gain" min="0.1" max="10.0" step="0.1"><span class="val" id="v_tilt_p_gain"></span></div>
    </div>

    <!-- Live Stats -->
    <div class="card">
      <h3>📊 Live Stats</h3>
      <div class="stats-grid">
        <div class="stat"><span class="label">Face X:</span> <span class="value" id="s_face_x">--</span></div>
        <div class="stat"><span class="label">Face Y:</span> <span class="value" id="s_face_y">--</span></div>
        <div class="stat"><span class="label">Faces:</span> <span class="value" id="s_faces">--</span></div>
        <div class="stat"><span class="label">Track:</span> <span class="value" id="s_track">--</span></div>
        <div class="stat"><span class="label">Hand:</span> <span class="value" id="s_hand">--</span></div>
        <div class="stat"><span class="label">Detect:</span> <span class="value" id="s_detect">--</span></div>
        <div class="stat" style="grid-column:span 2"><span class="label">Servo:</span> <span class="value" id="s_servo">--</span></div>
      </div>
    </div>
  </div>
</div>

<script>
const SLIDERS = [
  'detect_res_w','detect_res_h','jpeg_quality','vision_fps','confidence_threshold',
  'deadzone_x','deadzone_y','pan_p_gain','tilt_p_gain','manual_step',
  'hi_speed_v','hi_speed_h','bye_speed_v','bye_speed_h',
  'talk_speed_v','talk_speed_h','base_rotate_deg'
];
const TOGGLES = [
  'face_detection_enabled','hand_detection_enabled','face_tracking_enabled','hand_tracking_enabled',
  'hi_gesture_enabled','bye_gesture_enabled','talk_gesture_enabled','servo_enabled','launch_kiosk'
];

// ── Init ────────────────────────────────────────────────────────────────────
async function init() {
  const resp = await fetch('/api/state');
  const state = await resp.json();
  for (const id of SLIDERS) {
    const el = document.getElementById(id);
    if (el && state[id] !== undefined) { el.value = state[id]; document.getElementById('v_'+id).textContent = state[id]; }
  }
  for (const id of TOGGLES) {
    const el = document.getElementById(id);
    if (el && state[id] !== undefined) el.checked = state[id];
  }
}

// ── Slider events ───────────────────────────────────────────────────────────
for (const id of SLIDERS) {
  const el = document.getElementById(id);
  if (!el) continue;
  el.addEventListener('input', () => { document.getElementById('v_'+id).textContent = el.value; });
  el.addEventListener('change', async () => {
    await fetch('/api/tune', { method:'POST', headers:{'Content-Type':'application/json'}, body:JSON.stringify({[id]:el.value}) });
  });
}
for (const id of TOGGLES) {
  const el = document.getElementById(id);
  if (!el) continue;
  el.addEventListener('change', async () => {
    await fetch('/api/tune', { method:'POST', headers:{'Content-Type':'application/json'}, body:JSON.stringify({[id]:el.checked}) });
  });
}

// ── Keyboard controls ───────────────────────────────────────────────────────
const keyState = {};
const keyMap = {w:'k_w', a:'k_a', s:'k_s', d:'k_d', i:'k_i', j:'k_j', k:'k_k', l:'k_l', '1':'k_1', '2':'k_2'};

document.addEventListener('keydown', e => {
  if (e.target.tagName === 'INPUT') return;  // don't hijack text input
  const key = e.key.toLowerCase();
  if (keyMap[key] && !keyState[key]) {
    keyState[key] = true;
    document.getElementById(keyMap[key])?.classList.add('active');
    sendKey(key, true);
  }
});

document.addEventListener('keyup', e => {
  const key = e.key.toLowerCase();
  if (keyMap[key]) {
    keyState[key] = false;
    document.getElementById(keyMap[key])?.classList.remove('active');
    sendKey(key, false);
  }
});

async function sendKey(key, pressed) {
  await fetch('/api/key', {
    method: 'POST',
    headers: {'Content-Type': 'application/json'},
    body: JSON.stringify({key: key, pressed: pressed})
  });
}

// ── Send command ────────────────────────────────────────────────────────────
async function sendCmd(cmd) {
  cmd = cmd.trim().toLowerCase();
  if (!cmd) return;
  const log = document.getElementById('anim-log');
  log.textContent += '\\n> ' + cmd + '...';
  log.scrollTop = log.scrollHeight;
  try {
    const resp = await fetch('/api/command', {
      method: 'POST', headers: {'Content-Type':'application/json'},
      body: JSON.stringify({cmd: cmd})
    });
    const data = await resp.json();
    log.textContent += '\\n  ' + (data.result || data.error || 'done');
    log.scrollTop = log.scrollHeight;
  } catch(e) { log.textContent += '\\n  ERROR: ' + e; }
}

// ── Poll stats ──────────────────────────────────────────────────────────────
setInterval(async () => {
  try {
    const resp = await fetch('/api/state');
    const s = await resp.json();
    document.getElementById('s_face_x').textContent = s.face_detected ? s.face_norm_x.toFixed(3) : '--';
    document.getElementById('s_face_y').textContent = s.face_detected ? s.face_norm_y.toFixed(3) : '--';
    document.getElementById('s_faces').textContent = s.face_count;
    document.getElementById('s_track').textContent = s.track_kind;
    document.getElementById('s_hand').textContent = s.hand_detected ? s.hand_side : 'No';
    document.getElementById('s_detect').textContent = s.detect_ms.toFixed(1) + 'ms';

    if (s.servo_enabled) {
      document.getElementById('s_servo').textContent = 'P:' + s.current_pan.toFixed(1) + ' T:' + s.current_tilt.toFixed(1);
      document.getElementById('s_servo').style.color = '#ff6b6b';
    } else {
      document.getElementById('s_servo').textContent = 'OFF';
      document.getElementById('s_servo').style.color = '#666';
    }
    
    // Sync launch kiosk checkbox state if we want to
    const chkKiosk = document.getElementById('launch_kiosk');
    if (chkKiosk && s.launch_kiosk !== undefined) {
      chkKiosk.checked = (s.launch_kiosk === 1);
    }

    const pf = document.getElementById('pill-face');
    pf.className = 'pill ' + (s.face_detected ? 'on' : 'off');
    pf.textContent = 'FACE: ' + (s.face_detected ? s.face_count : 'NO');

    const ph = document.getElementById('pill-hand');
    ph.className = 'pill ' + (s.hand_detected ? 'on' : 'off');
    ph.textContent = 'HAND: ' + (s.hand_detected ? s.hand_side : 'NO');

    const pa = document.getElementById('pill-anim');
    if (s.animation_active) {
      pa.className = 'pill anim';
      pa.textContent = 'ANIM: ' + s.animation_active.toUpperCase();
    } else {
      pa.className = 'pill off';
      pa.textContent = 'ANIM: --';
    }

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
    animation_runner: AnimationRunner
    link: ArduinoServoLink | None

    def log_message(self, format, *args):
        return

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
        length = int(self.headers.get("Content-Length", 0))
        raw = self.rfile.read(length) if length > 0 else b"{}"
        try:
            payload = json.loads(raw.decode("utf-8"))
        except json.JSONDecodeError:
            self._json(400, {"ok": False, "error": "bad json"})
            return

        if self.path == "/api/tune":
            if "launch_kiosk" in payload and payload["launch_kiosk"] == 1 and self.tune.get("launch_kiosk") != 1:
                import subprocess
                try:
                    subprocess.Popen(["bash", "script/launch-kiosk.sh"])
                except Exception as e:
                    print("Failed to launch kiosk script:", e)
            self.tune.update(payload)
            self._json(200, {"ok": True})
            return

        if self.path == "/api/key":
            key = payload.get("key", "")
            pressed = payload.get("pressed", False)
            self._handle_key(key, pressed)
            self._json(200, {"ok": True})
            return

        if self.path == "/api/command":
            cmd = str(payload.get("cmd", "")).strip().lower()
            if not cmd:
                self._json(400, {"ok": False, "error": "empty command"})
                return
            # Run animation in background thread
            def _run():
                result = self.animation_runner.play(cmd)
                self.tune.set("animation_log", result)
            threading.Thread(target=_run, daemon=True).start()
            self._json(200, {"ok": True, "result": f"Started: {cmd}"})
            return

        self.send_error(404)

    def _handle_key(self, key: str, pressed: bool):
        pan_d = 0.0
        tilt_d = 0.0

        if key == "a":
            pan_d = -1.0 if pressed else 0.0
        elif key == "d":
            pan_d = 1.0 if pressed else 0.0
        elif key == "w":
            tilt_d = -1.0 if pressed else 0.0
        elif key == "s":
            tilt_d = 1.0 if pressed else 0.0
        elif key in ("1", "2"):
            if pressed and self.link is not None and self.link.connected:
                deg = self.tune.get("base_rotate_deg") or 15.0
                if key == "1":
                    deg = -deg
                threading.Thread(
                    target=lambda: self.link.write_base_step_spin(deg, timeout_sec=5.0),
                    daemon=True
                ).start()
            return

        if key in ("a", "d"):
            self.tune.set("manual_pan_delta", pan_d)
        elif key in ("w", "s"):
            self.tune.set("manual_tilt_delta", tilt_d)
            
        elif key == "i":
            self.tune.set("manual_arm_up", pressed)
            self.tune.set("manual_mode", pressed or self.tune.get("manual_mode"))
        elif key == "k":
            self.tune.set("manual_arm_down", pressed)
            self.tune.set("manual_mode", pressed or self.tune.get("manual_mode"))
        elif key == "j":
            self.tune.set("manual_arm_left", pressed)
            self.tune.set("manual_mode", pressed or self.tune.get("manual_mode"))
        elif key == "l":
            self.tune.set("manual_arm_right", pressed)
            self.tune.set("manual_mode", pressed or self.tune.get("manual_mode"))

        # Set manual mode if any WASD/IJKL key held
        self.tune.set("manual_mode", pressed or any([
            self.tune.get("manual_arm_up"), self.tune.get("manual_arm_down"),
            self.tune.get("manual_arm_left"), self.tune.get("manual_arm_right"),
            self.tune.get("manual_pan_delta"), self.tune.get("manual_tilt_delta")
        ]))

    def _json(self, code: int, data: dict):
        body = json.dumps(data).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)


class ThreadingHTTPServer(socketserver.ThreadingMixIn, HTTPServer):
    daemon_threads = True
    allow_reuse_address = True


# ── Main ─────────────────────────────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser(description="Robot face/hand/servo test dashboard")
    parser.add_argument("--config", type=str, default=None, help="Config YAML path")
    parser.add_argument("--port", type=int, default=9090, help="Web server port (default: 9090)")
    parser.add_argument("--host", type=str, default="0.0.0.0", help="Bind host")
    parser.add_argument("--serial-port", type=str, default=None, help="ESP32 serial port (auto)")
    parser.add_argument("--baud", type=int, default=None, help="ESP32 baud rate")
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
    print(f"[Main] Access from any device: http://<pi-ip>:{args.port}/")

    print(f"[Main] Connecting to Arduino on {serial_port or 'auto'} @ {baud}...")
    link = ArduinoServoLink(port=serial_port, baud=baud)
    if not link.connect():
        print("[Main] WARNING: Arduino connect failed. Servos will NOT move.")
        link = None
    else:
        print("[Main] Arduino connected!")
        if link.has_arm_firmware():
            print("[Main] Arm firmware detected — gestures available.")
        else:
            print("[Main] No arm firmware — gestures will not work.")

    tune = TuneState(cfg)

    det = DetectionThread(tune, cfg)
    det.start()

    servo_thread = ServoThread(tune, link)
    servo_thread.start()

    anim = AnimationRunner(tune, link)

    handler = type("BoundStreamHandler", (StreamHandler,), {
        "tune": tune,
        "detection_thread": det,
        "animation_runner": anim,
        "link": link,
    })

    server = ThreadingHTTPServer((args.host, args.port), handler)
    print(f"[Main] Dashboard live on port {args.port}")
    print(f"[Main] Controls: WASD=head  IJKL=arms  1/2=base  Buttons=animations")

    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\n[Main] Shutting down...")
        if link is not None and link.connected:
            poses = _load_presets(ARM_PRESETS_PATH).get("poses", {})
            home = poses.get("home")
            if home:
                link.write_arms(home["a0"], home["a1"], home["a2"], home["a3"], force=True)
                time.sleep(0.3)
            link.home_smooth(tune.get("pan_center"), tune.get("tilt_center"))
        server.shutdown()
        server.server_close()


if __name__ == "__main__":
    main()
