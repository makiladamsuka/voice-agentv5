"""FaceTracker: camera + YuNet face detection + MediaPipe hand fallback.

Writes to BB:
    face_detected, face_norm_x, face_norm_y, face_roll_deg,
    face_area_ratio, face_count, face_candidates,
    body_detected, track_kind,
    hand_detected, hand_norm_x, hand_norm_y, hand_physical_side,
    skin_blob_detected, skin_blob_norm_x, skin_blob_norm_y,
    hand_gesture, hand_gesture_side, hand_gesture_seq,
    stream_frame,
    person_snapshots, last_seen_world_yaw

Reads from BB:
    running, base_world_yaw_deg, base_encoder_deg, servo_pan, servo_tilt
"""

from __future__ import annotations

import math
import random
import collections
import time
from pathlib import Path
from typing import Optional

import cv2
import numpy as np

# Limit OpenCV internal OpenMP threads to 2 cores to prevent CPU saturation on Pi
try:
    cv2.setNumThreads(2)
except Exception:
    pass

try:
    import yaml
except ImportError:
    yaml = None

from core.blackboard import Blackboard
from lib.hand_detector import HandDetector
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

# ── Hand / gesture constants ─────────────────────────────────────────────────
SKIN_BLOB_MIN_AREA_RATIO = 0.15   # blob must cover 15% of frame
HI_WAVE_COOLDOWN_SEC = 30.0
BYE_WAVE_NEAR_FACE_PX = 110       # hand-near-face trigger distance (pixels)


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
    """Camera + face detection — publishes vision fields to the Blackboard."""

    def __init__(self, bb: Blackboard, config_path: Path = DEFAULT_CONFIG_PATH) -> None:
        self.bb = bb
        cfg = _load_yaml(config_path)
        cam = _cfg(cfg, "camera", default={}) or {}
        stream = _cfg(cfg, "stream", default={}) or {}
        pm = _cfg(cfg, "person_memory", default={}) or {}
        lss = _cfg(cfg, "last_seen_search", default={}) or {}
        prox = _cfg(cfg, "proximity", default={}) or {}
        inv = _cfg(prox, "investigate", default={}) or {}

        servo = _cfg(cfg, "servo", default={}) or {}
        self.servo_pan_center = float(servo.get("pan_center", 100.0))
        self.servo_pan_sign = float(servo.get("pan_sign", -1.0))

        self.face_model = str(APP_DIR / _cfg(cam, "face_model_path", default="face_detection_yunet_2023mar.onnx"))
        self.main_res = tuple(_cfg(cam, "main_res", default=[1920, 1080]))
        self.detect_res = tuple(_cfg(cam, "detect_res", default=[1280, 720]))
        _fsf = cam.get("full_sensor_fov")
        if _fsf is None:
            # Full 3280×2464 ISP path only when main stream is high-res (widest FOV).
            self.full_sensor_fov = max(self.main_res) > 720
        else:
            self.full_sensor_fov = bool(_fsf)
        self.stream_res = tuple(_cfg(stream, "res", default=[320, 180]))
        self.confidence = float(_cfg(cam, "confidence_threshold", default=0.6))
        self.nms = float(_cfg(cam, "nms_threshold", default=0.3))
        self.rotate_180 = bool(_cfg(cam, "rotate_180", default=False))
        self.swap_rb = bool(_cfg(cam, "stream_swap_rb", default=True))
        self.stream_enabled = bool(_cfg(stream, "enabled", default=True))
        self.vision_fps = int(_cfg(stream, "vision_fps", default=10))
        self.vision_fps_voice = int(_cfg(stream, "vision_fps_voice", default=0))
        self.vision_pause_on_audio = bool(_cfg(stream, "vision_pause_on_audio", default=True))
        self.vision_pause_poll_hz = float(_cfg(stream, "vision_pause_poll_hz", default=2.0))

        # Person memory
        self.pm_enabled = bool(pm.get("enabled", True))
        self.pm_timeout = float(pm.get("timeout_sec", 20.0))
        self.pm_merge = float(pm.get("merge_angle_deg", 12.0))
        self.pm_hfov = float(pm.get("camera_hfov_deg", 62.0))
        self.pm_max = int(pm.get("max_items", 6))
        self.pm_face_conf = float(pm.get("face_confidence", 1.0))

        # Last-seen-at-edge search
        self.lss_enabled = bool(lss.get("enabled", True))
        self.lss_edge_norm = float(lss.get("edge_norm", 0.40))

        self.prox_min_confidence = int(prox.get("min_confidence", 3))
        self.prox_investigate_enabled = bool(inv.get("enabled", True))
        self.prox_motion_fade_sec = float(inv.get("motion_fade_sec", 5.0))
        self.prox_verified_ttl_sec = float(inv.get("verified_ttl_sec", 5.0))
        self.prox_zone_yaw_deg = float(inv.get("zone_yaw_deg", float(prox.get("turn_step_deg", 35.0))))
        self.prox_revisit_max_age_sec = float(inv.get("revisit_max_age_sec", 5.0))

        # Hand fallback config
        hand_cfg = _cfg(cfg, "hand_fallback", default={}) or {}
        # self._hand_fallback_enabled = bool(hand_cfg.get("enabled", True))
        self._hand_max_num = int(hand_cfg.get("max_hands", 1))
        self._hi_gesture_enabled = bool(hand_cfg.get("hi_gesture", True))
        self._bye_gesture_from_hand = bool(hand_cfg.get("bye_gesture", True))
        
        # Waving detection config
        self._wave_threshold_enabled = bool(hand_cfg.get("wave_threshold", True))
        self._wave_reversals_min = int(hand_cfg.get("wave_reversals_min", 4))
        self._wave_amplitude_min = float(hand_cfg.get("wave_amplitude_min", 40.0))
        self._wave_history_len = int(hand_cfg.get("wave_history_len", 25))
        self._wave_dead_zone_px = int(hand_cfg.get("wave_dead_zone_px", 10))


        # Internals
        self._attention = MultiFaceAttention()
        self._squint_until = 0.0

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
        self._was_vision_paused = False
        
        # Skin blob lock (prevents face detection when hand is too close for skeleton detection)
        # self._skin_blob_lock = False
        
        self._last_face_area = 0.0
        
        # Relative hand tracking
        self._hand_offset_x = 0.0
        self._hand_offset_y = 0.0
        self._was_hand_tracking = False

        # ROI Crop tracking
        self.roi_tracking_enabled = bool(_cfg(cam, "roi_tracking_enabled", default=True))
        self.roi_padding_factor = float(_cfg(cam, "roi_padding_factor", default=1.6))
        self.roi_full_scan_interval = int(_cfg(cam, "roi_full_scan_interval", default=20))

        self._roi_active = False
        self._roi_box = None  # (fx, fy, fw, fh) in detect_res space
        self._roi_scan_count = 0
        self._hardware_transformed = False

    def _vision_audio_busy(self, state: dict) -> bool:
        """True while user or agent audio is active — skip camera + YuNet."""
        if not self.vision_pause_on_audio:
            return False
        if not state.get("voice_session_active"):
            return False
        if state.get("user_speaking") or state.get("agent_speaking"):
            return True
        return state.get("conv_state", "idle") in ("listening", "speaking", "nodding")
    
    def _detect_reversals(self, history: list[int]) -> tuple[int, float]:
        """Analyzes coordinate history for peak-to-peak swing counts (reversals) and amplitude.
        
        Returns (reversals, amplitude) where reversals is the number of direction changes
        and amplitude is the pixel range of movement.
        """
        if len(history) < 6:
            return 0, 0.0
        
        reversals = 0
        anchor = history[0]
        direction = 0  # +1 = increasing, -1 = decreasing
        peaks = [history[0]]
        
        for val in history[1:]:
            diff = val - anchor
            if abs(diff) < self._wave_dead_zone_px:
                continue
            
            new_dir = 1 if diff > 0 else -1
            if direction != 0 and new_dir != direction:
                reversals += 1
                peaks.append(anchor)
            direction = new_dir
            anchor = val
        
        amplitude = max(peaks) - min(peaks) if peaks else 0.0
        return reversals, amplitude

    def _effective_vision_fps(self, state: dict) -> float:
        voice_active = bool(state.get("voice_session_active"))
        if voice_active and self.vision_fps_voice > 0:
            return float(self.vision_fps_voice)
        return float(max(1, self.vision_fps))

    # ─────────────────────────────────────────────────────────────────────────

    def _init_camera(self):
        try:
            import logging
            logging.getLogger("picamera2").setLevel(logging.WARNING)
            from picamera2 import Picamera2
            import logging
            logging.getLogger("picamera2").setLevel(logging.WARNING)
            cam = Picamera2()
            if self.full_sensor_fov:
                cfg = cam.create_video_configuration(
                    main={"format": "RGB888", "size": self.main_res},
                    raw={"size": (3280, 2464)},
                    buffer_count=1,
                )
                cam.configure(cfg)
                cam.set_controls({"ScalerCrop": (0, 0, 3280, 2464)})
                mode = "full-sensor 3280×2464"
            else:
                # Let libcamera pick a modest sensor mode — avoids ISP cost of full sensor.
                cfg = cam.create_video_configuration(
                    main={"format": "RGB888", "size": self.main_res},
                    buffer_count=2,
                )
                cam.configure(cfg)
                mode = "main-only"
            if self.rotate_180:
                try:
                    from libcamera import Transform
                    cam.set_transform(Transform(hflip=True, vflip=True))
                    self._hardware_transformed = True
                except Exception:
                    self._hardware_transformed = False
            else:
                self._hardware_transformed = False
            cam.start()
            print(
                f"[FaceTracker] Camera started ({mode}): "
                f"{self.main_res} → detect {self.detect_res}"
            )
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
        # Calculate mechanical pan offset properly using servo config
        pan_mech = (pan - self.servo_pan_center) * self.servo_pan_sign
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
        if not face_detected:
            return None

        inv_yaw = float(state.get("prox_investigate_yaw", 0.0))
        self._person_memory.observe(
            norm_x=face_norm_x,
            norm_y=face_norm_y,
            base_world_yaw_deg=float(state["base_world_yaw_deg"]),
            pan_mech_deg=(float(state["servo_pan"]) - self.servo_pan_center) * self.servo_pan_sign,
            kind="face",
            confidence=self.pm_face_conf,
            source="prox_verify",
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

        if cam is None or detector is None:
            print("[FaceTracker] Cannot run: camera or detector unavailable.")
            return

        hand_detector = None
        if self._bye_gesture_from_hand: # Fallback tracking uses the same HandDetector
            hand_detector = HandDetector(max_num_hands=self._hand_max_num)

        next_tick = time.perf_counter()

        while self.bb.read("running")["running"]:
            now_pc = time.perf_counter()
            if now_pc < next_tick:
                time.sleep(max(0.001, next_tick - now_pc))

            voice_state = self.bb.read(
                "voice_session_active",
                "user_speaking",
                "agent_speaking",
                "conv_state",
                "animation_override",
            )

            # optimize/cpu2: yield to AnimationEngine — skip camera capture
            if voice_state.get("animation_override", False):
                next_tick = time.perf_counter() + 0.1
                continue

            paused = self._vision_audio_busy(voice_state)
            if paused != self._was_vision_paused:
                label = "paused (voice audio — CPU saved)" if paused else "resumed"
                print(f"[FaceTracker] Vision {label}")
                self._was_vision_paused = paused

            if paused:
                poll_hz = max(0.5, self.vision_pause_poll_hz)
                next_tick = time.perf_counter() + (1.0 / poll_hz)
                continue

            # optimize/cpu2: hard-cap at 12 FPS max on Pi to free CPU for
            # hardware control and voice processing.
            fps = min(12, self._effective_vision_fps(voice_state))
            next_tick = time.perf_counter() + (1.0 / max(1.0, fps))

            now = time.time()
            try:
                frame_full = cam.capture_array()
            except Exception:
                time.sleep(0.05)
                continue

            if self.rotate_180 and not getattr(self, "_hardware_transformed", False):
                frame_full = cv2.rotate(frame_full, cv2.ROTATE_180)

            # Resize to detection resolution if needed
            if (frame_full.shape[1], frame_full.shape[0]) != self.detect_res:
                frame = cv2.resize(frame_full, self.detect_res, interpolation=cv2.INTER_LINEAR)
            else:
                frame = frame_full

            # ── ROI Crop Detection vs Full-Frame Detection ───────────────────
            faces = None
            use_roi = (
                self.roi_tracking_enabled
                and self._roi_active
                and self._roi_box is not None
                and self._roi_scan_count < self.roi_full_scan_interval
            )

            if use_roi:
                fx, fy, fw, fh = self._roi_box
                cx, cy = fx + fw * 0.5, fy + fh * 0.5
                crop_w = max(60.0, fw * self.roi_padding_factor)
                crop_h = max(60.0, fh * self.roi_padding_factor)

                rx1 = max(0, int(cx - crop_w * 0.5))
                ry1 = max(0, int(cy - crop_h * 0.5))
                rx2 = min(self.detect_res[0], int(cx + crop_w * 0.5))
                ry2 = min(self.detect_res[1], int(cy + crop_h * 0.5))

                rw = rx2 - rx1
                rh = ry2 - ry1

                if rw > 30 and rh > 30:
                    roi_patch = frame[ry1:ry2, rx1:rx2]
                    detector.setInputSize((rw, rh))
                    _, roi_faces = detector.detect(roi_patch)

                    if roi_faces is not None and len(roi_faces) > 0:
                        mapped_faces = []
                        for rf in roi_faces:
                            f_mapped = np.copy(rf)
                            f_mapped[0] += rx1
                            f_mapped[1] += ry1
                            if len(f_mapped) >= 14:
                                for li in range(4, 14, 2):
                                    f_mapped[li] += rx1
                                    f_mapped[li + 1] += ry1
                            mapped_faces.append(f_mapped)
                        faces = np.array(mapped_faces)
                        self._roi_scan_count += 1
                    else:
                        self._roi_active = False
                        self._roi_scan_count = 0
                else:
                    self._roi_active = False

            if faces is None:
                detector.setInputSize(self.detect_res)
                _, faces = detector.detect(frame)
                self._roi_scan_count = 0

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
            hands = None

            # ── Face detection ──────────────────────────────────────────────
            # Suppress face detection when skin blob lock is active (hand too close)
            # if not self._skin_blob_lock and faces is not None and len(faces) > 0:
            if faces is not None and len(faces) > 0:
                valid = [f for f in faces if float(f[2]) > 4 and float(f[3]) > 4]
                if valid:
                    face_count = len(valid)
                    ranked = sorted(valid, key=lambda f: float(f[2]) * float(f[3]), reverse=True)
                    face_candidates = []
                    for f in ranked:
                        fx, fy, fw, fh = self._face_box(f)
                        nx, ny = self._face_center_norm(f)
                        
                        # Extract landmarks from YuNet detection
                        # YuNet provides 5 landmarks: [4-5]=left_eye, [6-7]=right_eye, 
                        # [8-9]=nose_tip, [10-11]=left_mouth, [12-13]=right_mouth
                        landmarks = []
                        if len(f) >= 14:
                            # Extract x,y coordinates for 5 facial landmarks
                            for i in range(4, 14, 2):
                                landmarks.extend([float(f[i]), float(f[i+1])])
                        
                        face_candidates.append(
                            {
                                "norm_x": nx,
                                "norm_y": ny,
                                "area_ratio": self._face_area_ratio(f, self.detect_res),
                                "x": fx,
                                "y": fy,
                                "w": fw,
                                "h": fh,
                                "landmarks": landmarks if len(landmarks) == 10 else None,
                                "detect_res": self.detect_res,
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
                    self._last_face_area = face_area
                    self._update_memory(face_norm_x, face_norm_y, "face", now, confidence=self.pm_face_conf)

                    # Update ROI tracking box from active target face
                    primary_f = selected_face[0] if (kind == "center" and isinstance(selected_face, tuple)) else selected_face
                    self._roi_box = self._face_box(primary_f)
                    self._roi_active = True
                else:
                    self._roi_active = False
            else:
                self._roi_active = False

            # ── Hand fallback + gesture detection ───────────────────────────
            hand_detected = False
            hand_norm_x = 0.0
            hand_norm_y = 0.0
            hand_physical_side = ""
            skin_blob_detected = False
            skin_blob_norm_x = 0.0
            skin_blob_norm_y = 0.0
            hand_gesture = ""
            hand_gesture_side = ""

            # if self._hand_fallback_enabled and hand_detector is not None:
            if hand_detector is not None:
                hands = hand_detector.process(frame, mirrored=False)

                # ── Gesture recognition (runs even when face IS detected) ──
                if hands:
                    best_hand = max(hands, key=lambda h: h.confidence)
                    px, py = best_hand.palm_center
                    dw, dh = self.detect_res
                    side = best_hand.physical_side
                    
                    # Bye gesture: hand near detected face, frontside
                    if (
                        self._bye_gesture_from_hand
                        and face_detected
                        and best_hand.is_frontside
                        and not hand_gesture
                    ):
                        face_px_x = int((face_norm_x + 1.0) * 0.5 * dw)
                        face_px_y = int((face_norm_y + 1.0) * 0.5 * dh)
                        dist = ((px - face_px_x) ** 2 + (py - face_px_y) ** 2) ** 0.5
                        if dist < 300:  # Hand near face threshold
                            hand_gesture = "bye_wave"
                            hand_gesture_side = best_hand.physical_side

                    # Hi gesture: frontside hand away from face (with 30s cooldown)
                    hi_cooldown_until = float(self.bb.read("hi_wave_cooldown_until").get("hi_wave_cooldown_until", 0.0))
                    if (
                        self._hi_gesture_enabled
                        and best_hand.is_frontside
                        and not hand_gesture
                        and now >= hi_cooldown_until
                    ):
                        is_near_face = False
                        if face_detected:
                            face_px_x = int((face_norm_x + 1.0) * 0.5 * dw)
                            face_px_y = int((face_norm_y + 1.0) * 0.5 * dh)
                            dist = ((px - face_px_x) ** 2 + (py - face_px_y) ** 2) ** 0.5
                            if dist < 300:
                                is_near_face = True
                        if not is_near_face:
                            hand_gesture = "hi_wave"
                            hand_gesture_side = best_hand.physical_side
                            
                    # ── Face -> Hand Fallback Logic ──
                    # Calculate hand bounding box area based on landmarks
                    if best_hand.pixel_landmarks:
                        xs = [lm[0] for lm in best_hand.pixel_landmarks]
                        ys = [lm[1] for lm in best_hand.pixel_landmarks]
                        hand_w = max(xs) - min(xs)
                        hand_h = max(ys) - min(ys)
                        
                        dw, dh = self.detect_res
                        hand_area_ratio = (hand_w * hand_h) / (dw * dh)
                        
                        # Use palm_center (middle of hand palm) for precise camera centering
                        palm_px, palm_py = best_hand.palm_center

                        # 70% offset of the hand bounding box half-height
                        hand_box_offset_y = 0.10 * (hand_h * 0.5)
                        hand_box_offset_x = 0.10 * (hand_w * 0.5)
                        
                        raw_hand_norm_x = ((palm_px + hand_box_offset_x) / dw) * 2.0 - 1.0
                        raw_hand_norm_y = ((palm_py + hand_box_offset_y) / dh) * 2.0 - 1.0
                        
                        # Compare against current or last known face area
                        compare_area = face_area if face_detected else self._last_face_area
                        
                        if hand_area_ratio > 1.3 * compare_area:
                            if not self._was_hand_tracking:
                                # First frame of hand tracking: compute offset to prevent jerking
                                current_aim_x = face_norm_x if face_detected else 0.0
                                current_aim_y = face_norm_y if face_detected else 0.0
                                self._hand_offset_x = raw_hand_norm_x - current_aim_x
                                self._hand_offset_y = raw_hand_norm_y - current_aim_y
                                self._was_hand_tracking = True
                            else:
                                # Smoothly decay the initial offset so camera glides and centralizes directly on palm middle
                                self._hand_offset_x *= 0.80
                                self._hand_offset_y *= 0.80
                                
                            track_kind = "hand"
                            # Override face tracking so servo loop follows hand
                            face_detected = False
                            # Disable hi/bye gestures when fallback tracking is active
                            hand_gesture = ""
                            hand_gesture_side = ""
                        else:
                            self._was_hand_tracking = False
                            self._hand_offset_x = 0.0
                            self._hand_offset_y = 0.0
                            
                        # Always publish hand coordinates so they can be drawn on stream HUD
                        hand_detected = True
                        if track_kind == "hand":
                            hand_norm_x = raw_hand_norm_x - self._hand_offset_x
                            hand_norm_y = raw_hand_norm_y - self._hand_offset_y
                        else:
                            hand_norm_x = raw_hand_norm_x
                            hand_norm_y = raw_hand_norm_y
                            
                        hand_physical_side = best_hand.physical_side
                    else:
                        self._was_hand_tracking = False
                        self._hand_offset_x = 0.0
                        self._hand_offset_y = 0.0
                else:
                    self._was_hand_tracking = False
                    self._hand_offset_x = 0.0
                    self._hand_offset_y = 0.0

                # ── Skin blob fallback (when neither face nor hand) ────────
                # if not face_detected and not hand_detected:
                #     try:
                #         hsv = cv2.cvtColor(frame, cv2.COLOR_BGR2HSV)
                #         mask1 = cv2.inRange(
                #             hsv,
                #             np.array([0, 20, 40], dtype=np.uint8),
                #             np.array([25, 255, 255], dtype=np.uint8),
                #         )
                #         mask2 = cv2.inRange(
                #             hsv,
                #             np.array([155, 20, 40], dtype=np.uint8),
                #             np.array([180, 255, 255], dtype=np.uint8),
                #         )
                #         mask = cv2.bitwise_or(mask1, mask2)
                #         mask = cv2.morphologyEx(
                #             mask, cv2.MORPH_OPEN, np.ones((5, 5), np.uint8)
                #         )
                #         contours, _ = cv2.findContours(
                #             mask, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE
                #         )
                #         if contours:
                #             largest = max(contours, key=cv2.contourArea)
                #             area = cv2.contourArea(largest)
                #             dw, dh = self.detect_res
                #             if area > dw * dh * SKIN_BLOB_MIN_AREA_RATIO:
                #                 M = cv2.moments(largest)
                #                 if M["m00"] > 0:
                #                     cx = int(M["m10"] / M["m00"])
                #                     cy = int(M["m01"] / M["m00"])
                #                     skin_blob_norm_x = (cx / dw) * 2.0 - 1.0
                #                     skin_blob_norm_y = (cy / dh) * 2.0 - 1.0
                #                     skin_blob_detected = True
                #                     track_kind = "close_up"
                #                     # Activate skin blob lock to suppress face detection
                #                     # self._skin_blob_lock = True
                #             else:
                #                 # Blob too small, release lock if it was active
                #                 # if self._skin_blob_lock:
                #                 #     self._skin_blob_lock = False
                #                 pass
                #         else:
                #             # No contours found, release lock if it was active
                #             # if self._skin_blob_lock:
                #             #     self._skin_blob_lock = False
                #             pass
                #     except Exception:
                #         pass
                # else:
                #     # Face or hand detected, release skin blob lock
                #     # if self._skin_blob_lock:
                #     #     self._skin_blob_lock = False
                #     pass

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
            if self.lss_enabled and not face_detected and self._person_memory is not None:
                state = self.bb.read("base_world_yaw_deg")
                world_yaw = state["base_world_yaw_deg"]
                best = self._person_memory.best_for_current_view(current_world_yaw_deg=world_yaw, now=now)
                if best is not None:
                    last_seen_yaw = best.world_yaw_deg

            # ── Publish hand gesture (increment seq on new gesture) ────────
            gesture_seq_delta = {}
            if hand_gesture:
                prev_seq = int(self.bb.read("hand_gesture_seq")["hand_gesture_seq"])
                gesture_seq_delta = {"hand_gesture_seq": prev_seq + 1}
                if hand_gesture == "hi_wave":
                    gesture_seq_delta["hi_wave_cooldown_until"] = now + HI_WAVE_COOLDOWN_SEC

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
                hand_detected=hand_detected,
                hand_norm_x=hand_norm_x,
                hand_norm_y=hand_norm_y,
                hand_physical_side=hand_physical_side,
                # skin_blob_detected=skin_blob_detected,
                # skin_blob_norm_x=skin_blob_norm_x,
                # skin_blob_norm_y=skin_blob_norm_y,
                hand_gesture=hand_gesture,
                hand_gesture_side=hand_gesture_side,
                person_snapshots=snapshots,
                motion_snapshots=motion_snapshots,
                last_seen_world_yaw=last_seen_yaw,
                prox_verified_priority_yaw=verified_yaw,
                **gesture_seq_delta,
            )

            # ── Publish stream frame (only when a client is watching) ───────
            stream_viewers = int(self.bb.read("stream_viewers")["stream_viewers"])
            if self.stream_enabled and stream_viewers > 0:
                try:
                    # Draw debug annotations onto frame before resize
                    if hands:
                        from lib.hand_detector import draw_skeleton
                        for hand in hands:
                            is_active = (hand_gesture != "") or (track_kind == "hand" and hand.physical_side == hand_physical_side)
                            draw_skeleton(frame, hand, is_active=is_active)
                    
                    if skin_blob_detected:
                        cx = int((skin_blob_norm_x + 1.0) * 0.5 * self.detect_res[0])
                        cy = int((skin_blob_norm_y + 1.0) * 0.5 * self.detect_res[1])
                        cv2.circle(frame, (cx, cy), 15, (0, 0, 255), -1)
                        cv2.circle(frame, (cx, cy), 10, (0, 165, 255), -1)
                        cv2.putText(frame, "BLOB", (cx + 20, cy), cv2.FONT_HERSHEY_SIMPLEX, 0.5, (0, 165, 255), 2)

                    stream_frame = cv2.resize(frame, self.stream_res, interpolation=cv2.INTER_LINEAR)
                    if self.swap_rb:
                        stream_frame = cv2.cvtColor(stream_frame, cv2.COLOR_BGR2RGB)

                    scale_x = self.stream_res[0] / self.detect_res[0]
                    scale_y = self.stream_res[1] / self.detect_res[1]

                    if self._roi_active and self._roi_box is not None:
                        fx, fy, fw, fh = self._roi_box
                        cx, cy = fx + fw * 0.5, fy + fh * 0.5
                        crop_w = max(60.0, fw * self.roi_padding_factor)
                        crop_h = max(60.0, fh * self.roi_padding_factor)
                        rx1 = max(0, int((cx - crop_w * 0.5) * scale_x))
                        ry1 = max(0, int((cy - crop_h * 0.5) * scale_y))
                        rx2 = min(self.stream_res[0], int((cx + crop_w * 0.5) * scale_x))
                        ry2 = min(self.stream_res[1], int((cy + crop_h * 0.5) * scale_y))
                        cv2.rectangle(stream_frame, (rx1, ry1), (rx2, ry2), (255, 120, 0), 1)

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
                    elif track_kind == "hand":
                        cx_s = int((hand_norm_x + 1.0) * 0.5 * self.stream_res[0])
                        cy_s = int((hand_norm_y + 1.0) * 0.5 * self.stream_res[1])
                        cv2.circle(stream_frame, (cx_s, cy_s), 8, (0, 255, 255), 2)
                        cv2.circle(stream_frame, (cx_s, cy_s), 3, (0, 255, 255), -1)
                        cv2.putText(
                            stream_frame,
                            "HAND AIM",
                            (cx_s + 10, cy_s + 4),
                            cv2.FONT_HERSHEY_SIMPLEX,
                            0.4,
                            (0, 255, 255),
                            1,
                        )
                    elif face_detected:
                        cx_s = int((face_norm_x + 1.0) * 0.5 * self.stream_res[0])
                        cy_s = int((face_norm_y + 1.0) * 0.5 * self.stream_res[1])
                        cv2.circle(stream_frame, (cx_s, cy_s), 6, (0, 255, 255), 2)

                    self.bb.write(stream_frame=stream_frame)
                except Exception:
                    pass

        print("[FaceTracker] Stopped.")
        if hand_detector is not None:
            hand_detector.close()
        try:
            cam.stop()
        except Exception:
            pass
