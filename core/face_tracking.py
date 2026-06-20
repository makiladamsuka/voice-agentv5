"""Face Tracking module for the greeting robot.

Runs the camera, YuNet face detector, and optional YOLOv8 body detector.
Writes vision results to the Blackboard so other modules can react.

Writer fields:
    face_detected, face_norm_x, face_norm_y, face_roll_deg,
    face_count, face_area_ratio, face_candidates,
    body_detected, track_kind, stream_frame
"""

import math
import random
import threading
import time
from pathlib import Path

import cv2
import numpy as np

try:
    import yaml
except ImportError:
    yaml = None

from core.blackboard import Blackboard

APP_DIR = Path(__file__).resolve().parent.parent
DEFAULT_CONFIG_PATH = APP_DIR / "config.yaml"

# ── Multi-face attention constants ───────────────────────────────────────────
ATTENTION_HOLD_MIN_SEC = 3.2
ATTENTION_HOLD_MAX_SEC = 6.5
MULTI_FACE_DEBOUNCE_SEC = 0.85
MULTI_FACE_CENTER_CHANCE = 0.38
MULTI_FACE_ALTERNATE_CHANCE = 0.34

FACE_ROLL_MAX_DEG = 10.0
FACE_ROLL_MULT = 0.75
FAR_FACE_AREA_RATIO = 0.018
FAR_SQUINT_CHANCE = 0.08
FAR_SQUINT_MIN_SEC = 0.22
FAR_SQUINT_MAX_SEC = 0.55


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


class MultiFaceAttention:
    def __init__(self):
        self.mode = "largest"
        self.index = 0
        self.hold_until = 0.0
        self.stable_since = 0.0

    def _next_hold(self, now):
        self.hold_until = now + random.uniform(ATTENTION_HOLD_MIN_SEC, ATTENTION_HOLD_MAX_SEC)

    def select(self, faces, now):
        ranked = sorted(faces, key=lambda f: f[2] * f[3], reverse=True)
        count = len(ranked)
        if count <= 1:
            self.mode = "largest"
            self.index = 0
            self.stable_since = 0.0
            self._next_hold(now)
            return ranked[0], "face", 0

        if self.stable_since <= 0.0:
            self.stable_since = now

        if now >= self.hold_until and now - self.stable_since >= MULTI_FACE_DEBOUNCE_SEC:
            r = random.random()
            if r < MULTI_FACE_CENTER_CHANCE:
                self.mode = "center"
                self.index = 0
            elif r < MULTI_FACE_CENTER_CHANCE + MULTI_FACE_ALTERNATE_CHANCE:
                self.mode = "alternate"
                self.index = random.randrange(1, count)
            else:
                self.mode = "largest"
                self.index = 0
            self._next_hold(now)

        if self.mode == "center" and count >= 2:
            return (ranked[0], ranked[1]), "center", -1

        self.index = min(max(0, self.index), count - 1)
        kind = "multi" if self.index > 0 else "face"
        return ranked[self.index], kind, self.index


class FaceTracker:
    def __init__(self, bb: Blackboard, config_path: Path = DEFAULT_CONFIG_PATH):
        self.bb = bb
        cfg = _load_yaml(config_path)
        cam = _cfg(cfg, "camera", default={}) or {}
        stream = _cfg(cfg, "stream", default={}) or {}

        self.face_model = str(APP_DIR / _cfg(cam, "face_model_path", default="face_detection_yunet_2023mar.onnx"))
        self.body_model = str(APP_DIR / _cfg(cam, "body_model_path", default="yolov8n.onnx"))
        self.body_enabled = bool(_cfg(cam, "body_enabled", default=True))
        self.body_conf = float(_cfg(cam, "body_confidence_threshold", default=0.35))
        self.body_nms = float(_cfg(cam, "body_nms_threshold", default=0.45))
        self.body_input = int(_cfg(cam, "body_input_size", default=640))
        self.body_stride = int(_cfg(cam, "body_detect_stride", default=3))
        self.body_alpha = float(_cfg(cam, "body_track_servo_alpha", default=0.30))
        self.body_aim_y = float(_cfg(cam, "body_aim_y_ratio", default=0.22))
        self.body_cache_sec = float(_cfg(cam, "body_cache_sec", default=0.75))
        self.main_res = tuple(_cfg(cam, "main_res", default=[1920, 1080]))
        self.detect_res = tuple(_cfg(cam, "detect_res", default=[1280, 720]))
        self.stream_res = tuple(_cfg(stream, "res", default=[320, 180]))
        self.confidence = float(_cfg(cam, "confidence_threshold", default=0.6))
        self.nms = float(_cfg(cam, "nms_threshold", default=0.3))
        self.rotate_180 = bool(_cfg(cam, "rotate_180", default=False))
        self.swap_rb = bool(_cfg(cam, "stream_swap_rb", default=True))
        self.stream_enabled = bool(_cfg(stream, "enabled", default=True))
        self.vision_fps = int(_cfg(stream, "vision_fps", default=10))

        # internal helpers
        self._attention = MultiFaceAttention()
        self._squint_until = 0.0
        self._frame_index = 0

    def _init_camera(self):
        try:
            from picamera2 import Picamera2
            cam = Picamera2()
            cfg = cam.create_video_configuration(
                main={"format": "RGB888", "size": self.main_res},
                raw={"size": (3280, 2464)},
                buffer_count=1,
            )
            cam.configure(cfg)
            cam.set_controls({"ScalerCrop": (0, 0, 3280, 2464)})
            cam.start()
            print(f"Camera started: {self.main_res} → detect {self.detect_res}")
            return cam
        except Exception as e:
            print(f"Camera init failed: {e}")
            return None

    def _init_detector(self):
        if not Path(self.face_model).exists():
            print(f"Face model not found: {self.face_model}")
            return None
        try:
            d = cv2.FaceDetectorYN.create(
                model=self.face_model,
                config="",
                input_size=self.detect_res,
                score_threshold=self.confidence,
                nms_threshold=self.nms,
                top_k=5000,
                backend_id=cv2.dnn.DNN_BACKEND_OPENCV,
                target_id=cv2.dnn.DNN_TARGET_CPU,
            )
            print("YuNet face detector initialized.")
            return d
        except Exception as e:
            print(f"Face detector init failed: {e}")
            return None

    def _init_body(self):
        if not self.body_enabled:
            return None
        try:
            from lib.person_detector import PersonDetector
            pd = PersonDetector(
                self.body_model,
                confidence_threshold=self.body_conf,
                nms_threshold=self.body_nms,
                input_size=self.body_input,
            )
            print("YOLO body detector initialized.")
            return pd
        except Exception as e:
            print(f"Body detector disabled: {e}")
            return None

    @staticmethod
    def _face_box(face):
        return [float(v) for v in face[0:4]]

    def _face_center_norm(self, face):
        fx, fy, fw, fh = self._face_box(face)
        cx = (fx + fw * 0.5) / self.detect_res[0]
        cy = (fy + fh * 0.5) / self.detect_res[1]
        return -((cx - 0.5) * 2.0), (cy - 0.5) * 2.0

    @staticmethod
    def _roll_from_face(face):
        re_x, re_y = face[4], face[5]
        le_x, le_y = face[6], face[7]
        dx = re_x - le_x
        dy = re_y - le_y
        if dx == 0:
            return 0.0
        angle_deg = math.degrees(math.atan2(dy, dx))
        return max(-FACE_ROLL_MAX_DEG, min(FACE_ROLL_MAX_DEG, -angle_deg * FACE_ROLL_MULT))

    def run(self):
        print("FaceTracker started.")
        cam = self._init_camera()
        detector = self._init_detector()
        body_det = self._init_body()

        if cam is None or detector is None:
            print("FaceTracker: no camera or detector — vision unavailable.")
            return

        interval = 1.0 / max(1.0, float(self.vision_fps))
        next_tick = time.perf_counter()
        cached_body = None
        cached_body_ts = 0.0

        while self.bb.read("running")["running"]:
            try:
                large = cam.capture_array()
                frame = cv2.resize(large, self.detect_res)
                if self.rotate_180:
                    frame = cv2.rotate(frame, cv2.ROTATE_180)

                now = time.time()
                stream_frame = None
                if self.stream_enabled:
                    stream_frame = cv2.resize(frame, self.stream_res)
                    if self.swap_rb:
                        stream_frame = cv2.cvtColor(stream_frame, cv2.COLOR_BGR2RGB)
                    scale_x = self.stream_res[0] / self.detect_res[0]
                    scale_y = self.stream_res[1] / self.detect_res[1]

                detector.setInputSize((frame.shape[1], frame.shape[0]))
                faces = detector.detect(frame)

                face_detected = False
                face_norm_x = 0.0
                face_norm_y = 0.0
                face_roll_deg = 0.0
                face_count = 0
                face_area_ratio = 0.0
                face_candidates = []
                body_detected = False
                track_kind = "none"

                if faces[1] is not None:
                    detected = list(faces[1])
                    face_count = len(detected)
                    face_candidates = [tuple(self._face_box(f)) for f in detected]
                    active, kind, active_idx = self._attention.select(detected, now)
                    track_kind = kind

                    if kind == "center":
                        f1, f2 = active
                        nx1, ny1 = self._face_center_norm(f1)
                        nx2, ny2 = self._face_center_norm(f2)
                        face_norm_x = (nx1 + nx2) * 0.5
                        face_norm_y = (ny1 + ny2) * 0.5
                        fx1, fy1, fw1, fh1 = self._face_box(f1)
                        fx2, fy2, fw2, fh2 = self._face_box(f2)
                        fx = min(fx1, fx2); fy = min(fy1, fy2)
                        fw = max(fx1+fw1, fx2+fw2)-fx; fh = max(fy1+fh1, fy2+fh2)-fy
                        face_roll_deg = (self._roll_from_face(f1) + self._roll_from_face(f2)) * 0.5
                    else:
                        fx, fy, fw, fh = self._face_box(active)
                        face_norm_x, face_norm_y = self._face_center_norm(active)
                        face_roll_deg = self._roll_from_face(active)

                    face_area_ratio = (fw * fh) / float(self.detect_res[0] * self.detect_res[1])
                    face_detected = True

                    # squint far-face hint: not written to BB here, emitted via face_area_ratio
                else:
                    # Try body detector
                    if body_det is not None and self._frame_index % max(1, self.body_stride) == 0:
                        try:
                            body_bgr = cv2.cvtColor(frame, cv2.COLOR_RGB2BGR)
                            cached_body = body_det.detect_largest(body_bgr)
                            cached_body_ts = now if cached_body is not None else 0.0
                        except Exception as e:
                            cached_body = None
                            print(f"Body detect error: {e}")

                    if cached_body is not None and now - cached_body_ts <= self.body_cache_sec:
                        body_cx = cached_body.cx / self.detect_res[0]
                        body_ay = cached_body.aim_y(self.body_aim_y) / self.detect_res[1]
                        face_norm_x = -((body_cx - 0.5) * 2.0)
                        face_norm_y = (body_ay - 0.5) * 2.0
                        face_detected = True
                        body_detected = True
                        track_kind = "body"

                self.bb.write(
                    face_detected=face_detected,
                    face_norm_x=face_norm_x,
                    face_norm_y=face_norm_y,
                    face_roll_deg=face_roll_deg,
                    face_count=face_count,
                    face_area_ratio=face_area_ratio,
                    face_candidates=face_candidates,
                    body_detected=body_detected,
                    track_kind=track_kind,
                    stream_frame=stream_frame,
                )
                self._frame_index += 1

            except Exception as e:
                print(f"FaceTracker error: {e}")

            next_tick += interval
            sleep_time = next_tick - time.perf_counter()
            if sleep_time > 0:
                time.sleep(sleep_time)
            else:
                next_tick = time.perf_counter()
