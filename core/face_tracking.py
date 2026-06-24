"""FaceTracker: camera + YuNet face detection + YOLO body detection.

Writes to BB:
    face_detected, face_norm_x, face_norm_y, face_roll_deg,
    face_area_ratio, face_count, face_candidates,
    body_detected, track_kind,
    stream_frame,
    person_snapshots, last_seen_world_yaw

Reads from BB:
    running, base_world_yaw_deg, base_encoder_deg, servo_pan, servo_tilt
"""

from __future__ import annotations

import math
import random
import time
from pathlib import Path
from typing import Optional

import cv2
import numpy as np

try:
    import yaml
except ImportError:
    yaml = None

from core.blackboard import Blackboard
from lib.person_memory import PersonMemory, angular_error_deg, wrap_degrees
from lib.motion_memory import MotionMemory, MotionMemoryItem

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


# ── Multi-face attention selector ─────────────────────────────────────────────

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


# ── FaceTracker ───────────────────────────────────────────────────────────────

class FaceTracker:
    """Camera + face/body detection — publishes vision fields to the Blackboard."""

    def __init__(self, bb: Blackboard, config_path: Path = DEFAULT_CONFIG_PATH) -> None:
        self.bb = bb
        cfg = _load_yaml(config_path)
        cam = _cfg(cfg, "camera", default={}) or {}
        stream = _cfg(cfg, "stream", default={}) or {}
        pm = _cfg(cfg, "person_memory", default={}) or {}
        lss = _cfg(cfg, "last_seen_search", default={}) or {}
        prox = _cfg(cfg, "proximity", default={}) or {}
        inv = _cfg(prox, "investigate", default={}) or {}

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

        # Person memory
        self.pm_enabled = bool(pm.get("enabled", True))
        self.pm_timeout = float(pm.get("timeout_sec", 20.0))
        self.pm_merge = float(pm.get("merge_angle_deg", 12.0))
        self.pm_hfov = float(pm.get("camera_hfov_deg", 62.0))
        self.pm_max = int(pm.get("max_items", 6))
        self.pm_face_conf = float(pm.get("face_confidence", 1.0))
        self.pm_body_conf = float(pm.get("body_confidence", 0.65))

        # Last-seen-at-edge search
        self.lss_enabled = bool(lss.get("enabled", True))
        self.lss_edge_norm = float(lss.get("edge_norm", 0.40))

        self.prox_min_confidence = int(prox.get("min_confidence", 3))
        self.prox_investigate_enabled = bool(inv.get("enabled", True))
        self.prox_motion_fade_sec = float(inv.get("motion_fade_sec", 5.0))
        self.prox_verified_ttl_sec = float(inv.get("verified_ttl_sec", 5.0))
        self.prox_zone_yaw_deg = float(inv.get("zone_yaw_deg", float(prox.get("turn_step_deg", 35.0))))
        self.prox_revisit_max_age_sec = float(inv.get("revisit_max_age_sec", 5.0))

        # Internals
        self._attention = MultiFaceAttention()
        self._squint_until = 0.0
        self._frame_index = 0
        self._body_norm_x = 0.0
        self._body_norm_y = 0.0
        self._body_last_ts = 0.0
        self._body_box: tuple[float, float, float, float] | None = None

        self._person_memory: Optional[PersonMemory] = None
        if self.pm_enabled:
            self._person_memory = PersonMemory(
                timeout_sec=self.pm_timeout,
                merge_angle_deg=self.pm_merge,
                camera_hfov_deg=self.pm_hfov,
                max_items=self.pm_max,
                prox_verify_timeout_sec=self.prox_verified_ttl_sec,
            )
        self._motion_memory: Optional[MotionMemory] = None
        if self.prox_investigate_enabled:
            self._motion_memory = MotionMemory(
                fade_sec=self.prox_motion_fade_sec,
                zone_yaw_deg=self.prox_zone_yaw_deg,
            )
        self._last_recorded_prox_ts = 0.0
        self._last_scan_complete_ts = 0.0

    # ─────────────────────────────────────────────────────────────────────────

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
            print(f"[FaceTracker] Camera started: {self.main_res} → detect {self.detect_res}")
            return cam
        except Exception as e:
            print(f"[FaceTracker] Camera init failed: {e}")
            return None

    def _init_detector(self):
        if not Path(self.face_model).exists():
            print(f"[FaceTracker] Face model not found: {self.face_model}")
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
            print("[FaceTracker] YuNet face detector initialized.")
            return d
        except Exception as e:
            print(f"[FaceTracker] Face detector init failed: {e}")
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
            print("[FaceTracker] YOLO body detector initialized.")
            return pd
        except Exception as e:
            print(f"[FaceTracker] Body detector disabled: {e}")
            return None

    # ── Geometry helpers ──────────────────────────────────────────────────────

    @staticmethod
    def _face_box(face):
        return [float(v) for v in face[0:4]]

    def _face_center_norm(self, face):
        """Normalized offset of detection bbox center from camera frame center.

        +norm_x = face right of center, -norm_x = face left (matches pan_right/pan_left).
        """
        fx, fy, fw, fh = self._face_box(face)
        cx = (fx + fw * 0.5) / self.detect_res[0]
        cy = (fy + fh * 0.5) / self.detect_res[1]
        return cx * 2.0 - 1.0, cy * 2.0 - 1.0

    @staticmethod
    def _face_area_ratio(face, detect_res):
        _, _, fw, fh = face[0], face[1], face[2], face[3]
        return (float(fw) * float(fh)) / (detect_res[0] * detect_res[1])

    @staticmethod
    def _roll_from_face(face):
        if len(face) < 15:
            return 0.0
        lx, ly = float(face[4]), float(face[5])
        rx, ry = float(face[6]), float(face[7])
        dx, dy = rx - lx, ry - ly
        if abs(dx) < 1e-6:
            return 0.0
        raw = math.degrees(math.atan2(dy, dx))
        return max(-FACE_ROLL_MAX_DEG, min(FACE_ROLL_MAX_DEG, raw * FACE_ROLL_MULT))

    # ── Person memory update ──────────────────────────────────────────────────

    def _update_memory(self, norm_x: float, norm_y: float, kind: str, now: float, confidence: float) -> None:
        if self._person_memory is None:
            return
        state = self.bb.read("base_world_yaw_deg", "servo_pan")
        world_yaw = state["base_world_yaw_deg"]
        pan = state["servo_pan"]
        # Approximate mechanical pan offset: centre of servo range ≈ 0°
        pan_mech = pan - 80.0  # rough estimate; servo_loop publishes exact value
        self._person_memory.observe(
            norm_x=norm_x,
            norm_y=norm_y,
            base_world_yaw_deg=world_yaw,
            pan_mech_deg=pan_mech,
            kind=kind,
            confidence=confidence,
            now=now,
        )

    def _record_prox_motion(self, now: float) -> MotionMemoryItem | None:
        if self._motion_memory is None:
            return None
        state = self.bb.read(
            "prox_approach_active", "prox_approach_zone", "prox_approach_distance",
            "prox_approach_confidence", "prox_approach_ts", "base_world_yaw_deg",
        )
        ts = float(state.get("prox_approach_ts", 0.0) or 0.0)
        if not state.get("prox_approach_active") or ts <= 0.0:
            return None
        if ts == self._last_recorded_prox_ts:
            return None
        if int(state.get("prox_approach_confidence", 0)) < self.prox_min_confidence:
            return None
        zone = str(state.get("prox_approach_zone", ""))
        if zone not in ("L", "C", "R"):
            return None
        item = self._motion_memory.observe_from_prox(
            zone=zone,
            base_world_yaw_deg=float(state["base_world_yaw_deg"]),
            distance_mm=int(state.get("prox_approach_distance", 0)),
            now=now,
        )
        self._last_recorded_prox_ts = ts
        self.bb.write(prox_investigate_motion_id=item.id)
        return item

    def _handle_prox_verify(
        self,
        now: float,
        *,
        face_detected: bool,
        body_detected: bool,
        face_norm_x: float,
        face_norm_y: float,
    ) -> float | None:
        if self._person_memory is None:
            return None
        state = self.bb.read(
            "prox_investigate_active", "prox_investigate_phase",
            "prox_investigate_yaw", "prox_investigate_motion_id",
            "base_world_yaw_deg", "servo_pan", "prox_scan_complete_ts",
        )
        scan_ts = float(state.get("prox_scan_complete_ts", 0.0) or 0.0)
        if scan_ts > 0.0 and scan_ts != self._last_scan_complete_ts:
            self._last_scan_complete_ts = scan_ts
            if self._motion_memory is not None:
                motion_id = int(state.get("prox_investigate_motion_id", 0) or 0)
                if motion_id > 0:
                    self._motion_memory.start_fade(motion_id, now=now)
                else:
                    self._motion_memory.start_fade(now=now)

        if not state.get("prox_investigate_active"):
            verified = self._person_memory.best_prox_verified(
                current_world_yaw_deg=float(state["base_world_yaw_deg"]),
                now=now,
                max_age_sec=self.prox_revisit_max_age_sec,
            )
            return verified.world_yaw_deg if verified is not None else None

        if state.get("prox_investigate_phase") not in ("scan", "turn", "done"):
            return None
        if not face_detected and not body_detected:
            return None

        inv_yaw = float(state.get("prox_investigate_yaw", 0.0))
        kind = "face" if face_detected else "body"
        if face_detected:
            self._person_memory.observe(
                norm_x=face_norm_x,
                norm_y=face_norm_y,
                base_world_yaw_deg=float(state["base_world_yaw_deg"]),
                pan_mech_deg=float(state["servo_pan"]) - 80.0,
                kind=kind,
                confidence=self.pm_face_conf if face_detected else self.pm_body_conf,
                source="prox_verify",
                now=now,
            )
        else:
            self._person_memory.observe_at_yaw(
                world_yaw_deg=inv_yaw,
                kind=kind,
                source="prox_verify",
                confidence=self.pm_body_conf,
                now=now,
            )
        if self._motion_memory is not None:
            self._motion_memory.mark_verified(inv_yaw, now=now)
        self.bb.write(
            prox_investigate_active=False,
            prox_investigate_phase="",
            prox_search_active=False,
        )
        return inv_yaw

    # ── Main loop ─────────────────────────────────────────────────────────────

    def run(self) -> None:
        cam = self._init_camera()
        detector = self._init_detector()
        body_detector = self._init_body()

        if cam is None or detector is None:
            print("[FaceTracker] Cannot run: camera or detector unavailable.")
            return

        interval = 1.0 / max(1, self.vision_fps)
        next_tick = time.perf_counter()

        while self.bb.read("running")["running"]:
            now_pc = time.perf_counter()
            if now_pc < next_tick:
                time.sleep(max(0.001, next_tick - now_pc))
            next_tick = time.perf_counter() + interval

            now = time.time()
            try:
                frame_full = cam.capture_array()
            except Exception:
                time.sleep(0.05)
                continue

            if self.rotate_180:
                frame_full = cv2.rotate(frame_full, cv2.ROTATE_180)

            # Resize to detection resolution
            frame = cv2.resize(frame_full, self.detect_res, interpolation=cv2.INTER_LINEAR)
            detector.setInputSize(self.detect_res)

            _, faces = detector.detect(frame)
            self._frame_index += 1

            face_detected = False
            face_norm_x = 0.0
            face_norm_y = 0.0
            face_roll = 0.0
            face_area = 0.0
            face_count = 0
            face_candidates = []
            body_detected = False
            track_kind = "none"
            active_face_index = -1

            # ── Body detection (lower frame rate) ──────────────────────────
            run_body = (
                body_detector is not None
                and (faces is None or len(faces) == 0)
                and (self._frame_index % self.body_stride == 0)
            )
            if run_body:
                try:
                    body_bgr = cv2.cvtColor(frame, cv2.COLOR_RGB2BGR)
                    det = body_detector.detect_largest(body_bgr)
                    if det is not None:
                        bx, by, bw, bh = det.x, det.y, det.w, det.h
                        cx_raw = (bx + bw * 0.5) / self.detect_res[0]
                        cy_raw = (by + bh * self.body_aim_y) / self.detect_res[1]
                        new_bx = cx_raw * 2.0 - 1.0
                        new_by = cy_raw * 2.0 - 1.0
                        self._body_norm_x += (new_bx - self._body_norm_x) * self.body_alpha
                        self._body_norm_y += (new_by - self._body_norm_y) * self.body_alpha
                        self._body_last_ts = now
                        self._body_box = (bx, by, bw, bh)
                except Exception:
                    pass

            body_fresh = (now - self._body_last_ts) < self.body_cache_sec
            if body_fresh and (faces is None or len(faces) == 0):
                body_detected = True
                face_norm_x = self._body_norm_x
                face_norm_y = self._body_norm_y
                track_kind = "body"
                self._update_memory(face_norm_x, face_norm_y, "body", now, confidence=0.65)
            elif not body_fresh:
                self._body_box = None

            # ── Face detection ──────────────────────────────────────────────
            if faces is not None and len(faces) > 0:
                valid = [f for f in faces if float(f[2]) > 4 and float(f[3]) > 4]
                if valid:
                    face_count = len(valid)
                    ranked = sorted(valid, key=lambda f: float(f[2]) * float(f[3]), reverse=True)
                    face_candidates = []
                    for f in ranked:
                        fx, fy, fw, fh = self._face_box(f)
                        nx, ny = self._face_center_norm(f)
                        face_candidates.append(
                            {
                                "norm_x": nx,
                                "norm_y": ny,
                                "area_ratio": self._face_area_ratio(f, self.detect_res),
                                "x": fx,
                                "y": fy,
                                "w": fw,
                                "h": fh,
                            }
                        )

                    selected_face, kind, active_face_index = self._attention.select(valid, now)

                    if kind == "center" and isinstance(selected_face, tuple):
                        f1, f2 = selected_face
                        cx1, cy1 = self._face_center_norm(f1)
                        cx2, cy2 = self._face_center_norm(f2)
                        face_norm_x = (cx1 + cx2) * 0.5
                        face_norm_y = (cy1 + cy2) * 0.5
                        face_area = (
                            self._face_area_ratio(f1, self.detect_res)
                            + self._face_area_ratio(f2, self.detect_res)
                        ) * 0.5
                        face_roll = 0.0
                        track_kind = "center"
                    else:
                        face_norm_x, face_norm_y = self._face_center_norm(selected_face)
                        face_area = self._face_area_ratio(selected_face, self.detect_res)
                        face_roll = self._roll_from_face(selected_face)
                        track_kind = kind

                    face_detected = True
                    self._update_memory(face_norm_x, face_norm_y, "face", now, confidence=self.pm_face_conf)

            # ── Proximity motion + verify ───────────────────────────────────
            self._record_prox_motion(now)
            verified_yaw = self._handle_prox_verify(
                now,
                face_detected=face_detected,
                body_detected=body_detected,
                face_norm_x=face_norm_x,
                face_norm_y=face_norm_y,
            )

            # ── Last-seen-at-edge tracking ──────────────────────────────────
            last_seen_yaw = None
            if (
                self.lss_enabled
                and not face_detected
                and not body_detected
                and self._person_memory is not None
            ):
                state = self.bb.read("base_world_yaw_deg")
                world_yaw = state["base_world_yaw_deg"]
                best = self._person_memory.best_for_current_view(current_world_yaw_deg=world_yaw, now=now)
                if best is not None:
                    last_seen_yaw = best.world_yaw_deg

            # ── Publish to Blackboard ────────────────────────────────────────
            snapshots = self._person_memory.snapshots(now) if self._person_memory else []
            motion_snapshots = self._motion_memory.snapshots(now) if self._motion_memory else []
            if verified_yaw is None and self._person_memory is not None:
                best_verified = self._person_memory.best_prox_verified(
                    current_world_yaw_deg=self.bb.read("base_world_yaw_deg")["base_world_yaw_deg"],
                    now=now,
                    max_age_sec=self.prox_revisit_max_age_sec,
                )
                verified_yaw = best_verified.world_yaw_deg if best_verified is not None else None
            self.bb.write(
                face_detected=face_detected,
                face_norm_x=face_norm_x,
                face_norm_y=face_norm_y,
                face_roll_deg=face_roll,
                face_area_ratio=face_area,
                face_count=face_count,
                face_candidates=face_candidates,
                body_detected=body_detected,
                track_kind=track_kind,
                person_snapshots=snapshots,
                motion_snapshots=motion_snapshots,
                last_seen_world_yaw=last_seen_yaw,
                prox_verified_priority_yaw=verified_yaw,
            )

            # ── Publish stream frame ─────────────────────────────────────────
            if self.stream_enabled:
                try:
                    stream_frame = cv2.resize(frame, self.stream_res, interpolation=cv2.INTER_LINEAR)
                    if self.swap_rb:
                        stream_frame = cv2.cvtColor(stream_frame, cv2.COLOR_BGR2RGB)

                    scale_x = self.stream_res[0] / self.detect_res[0]
                    scale_y = self.stream_res[1] / self.detect_res[1]

                    if face_count > 0:
                        ranked = sorted(
                            face_candidates,
                            key=lambda c: c.get("area_ratio", 0.0),
                            reverse=True,
                        )
                        for idx, cand in enumerate(ranked):
                            bx_s = int(cand["x"] * scale_x)
                            by_s = int(cand["y"] * scale_y)
                            bw_s = int(cand["w"] * scale_x)
                            bh_s = int(cand["h"] * scale_y)
                            active = idx == active_face_index or (
                                active_face_index < 0 and idx == 0
                            )
                            color = (0, 255, 255) if active else (0, 160, 0)
                            cv2.rectangle(
                                stream_frame,
                                (bx_s, by_s),
                                (bx_s + bw_s, by_s + bh_s),
                                color,
                                2,
                            )
                            cv2.putText(
                                stream_frame,
                                f"face{idx}",
                                (bx_s, max(12, by_s - 4)),
                                cv2.FONT_HERSHEY_SIMPLEX,
                                0.4,
                                color,
                                1,
                            )

                    if track_kind == "center" and face_detected:
                        cx_s = int((face_norm_x + 1.0) * 0.5 * self.stream_res[0])
                        cy_s = int((face_norm_y + 1.0) * 0.5 * self.stream_res[1])
                        cv2.circle(stream_frame, (cx_s, cy_s), 7, (0, 255, 255), 2)
                        cv2.putText(
                            stream_frame,
                            "center",
                            (cx_s + 8, cy_s),
                            cv2.FONT_HERSHEY_SIMPLEX,
                            0.4,
                            (0, 255, 255),
                            1,
                        )
                    elif face_detected:
                        cx_s = int((face_norm_x + 1.0) * 0.5 * self.stream_res[0])
                        cy_s = int((face_norm_y + 1.0) * 0.5 * self.stream_res[1])
                        cv2.circle(stream_frame, (cx_s, cy_s), 6, (0, 255, 255), 2)

                    if body_detected and self._body_box is not None:
                        bx, by, bw, bh = self._body_box
                        bx_s = int(bx * scale_x)
                        by_s = int(by * scale_y)
                        bw_s = int(bw * scale_x)
                        bh_s = int(bh * scale_y)
                        aim_x_s = int((face_norm_x + 1.0) * 0.5 * self.stream_res[0])
                        aim_y_s = int((face_norm_y + 1.0) * 0.5 * self.stream_res[1])
                        cv2.rectangle(
                            stream_frame,
                            (bx_s, by_s),
                            (bx_s + bw_s, by_s + bh_s),
                            (255, 120, 0),
                            2,
                        )
                        cv2.circle(stream_frame, (aim_x_s, aim_y_s), 6, (255, 120, 0), 2)
                        cv2.putText(
                            stream_frame,
                            "body",
                            (bx_s, max(12, by_s - 4)),
                            cv2.FONT_HERSHEY_SIMPLEX,
                            0.4,
                            (255, 120, 0),
                            1,
                        )

                    self.bb.write(stream_frame=stream_frame)
                except Exception:
                    pass

        print("[FaceTracker] Stopped.")
        try:
            cam.stop()
        except Exception:
            pass
