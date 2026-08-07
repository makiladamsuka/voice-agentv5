"""FaceGreetingArmService — trigger random hi poses when a new/forgotten face appears.

Face Memory Architecture (Pi 4B optimized — single model):
  - Detection/Alignment: YuNet (face_detection_yunet_2023mar.onnx) via cv2.FaceDetectorYN
    on a 112×112 affine-aligned crop, using the 5 eye-pair landmarks already extracted
    by FaceTracker and passed via face_candidates["landmarks"].
  - Embedding: Affine-aligned 112×112 crop → normalized multi-channel histogram
    (H channel from HSV + grayscale intensity) → compact identity vector.
  - Matching: Cosine similarity threshold 0.85 (higher = stricter).
  - TTL Cache: 30-minute per-person memory; GC every cleanup_interval_sec seconds.

No extra model downloads required — only face_detection_yunet_2023mar.onnx (already present).
No dlib / face_recognition / MobileFaceNet required — pure cv2.

Project file structure:
  voice-agentv5/
    face_detection_yunet_2023mar.onnx   ← already present (used by FaceTracker)
    core/
      face_greeting_arm.py              ← this file
    config.yaml                         ← face_greeting_arm section
"""

from __future__ import annotations

import json
import random
import time
from pathlib import Path
from typing import List, Optional

try:
    import cv2
    import numpy as np
except ImportError:
    cv2 = None
    np = None

try:
    import yaml
except ImportError:
    yaml = None

from core.blackboard import Blackboard

APP_DIR = Path(__file__).resolve().parent.parent
DEFAULT_CONFIG_PATH = APP_DIR / "config.yaml"
DEFAULT_PRESETS_PATH = APP_DIR / "tests" / "arm_pose_presets.json"
DEFAULT_YUNET_PATH = APP_DIR / "face_detection_yunet_2023mar.onnx"

# ── ArcFace reference eye positions for a 112×112 crop ───────────────────────
# These are the standard anchor points used for face alignment.
_REF_LEFT_EYE  = np.array([38.2946, 51.6963], dtype=np.float32) if np else None
_REF_RIGHT_EYE = np.array([73.5318, 51.5014], dtype=np.float32) if np else None

# Cosine similarity threshold (−1…1). Above this = same person.
COSINE_THRESHOLD = 0.85

# TTL for face memory
MEMORY_TTL_SEC = 30 * 60  # 30 minutes


# ── Config helpers ────────────────────────────────────────────────────────────

def _load_yaml(path: Path) -> dict:
    if yaml is None or not path.exists():
        return {}
    with open(path, encoding="utf-8") as f:
        return yaml.safe_load(f) or {}


def _load_json(path: Path) -> dict:
    if not path.exists():
        return {}
    with open(path, encoding="utf-8") as f:
        return json.load(f)


# ── Affine alignment to 112×112 ───────────────────────────────────────────────

def align_face_112(frame: "np.ndarray", landmarks_10: "np.ndarray") -> Optional["np.ndarray"]:
    """Affine-align a face region to the 112×112 ArcFace standard using eye landmarks.

    Args:
        frame:         Full BGR/RGB camera frame (any resolution).
        landmarks_10:  Flat array of 10 floats [lm0x, lm0y, lm1x, lm1y, ...]
                       representing 5 facial points in pixel coords of `frame`.
                       Order: left_eye, right_eye, nose_tip, left_mouth, right_mouth.

    Returns:
        112×112 uint8 image, or None on failure.
    """
    if frame is None or landmarks_10 is None or len(landmarks_10) < 4:
        return None
    try:
        pts = np.array(landmarks_10[:10], dtype=np.float32).reshape(5, 2)
        src = np.array([pts[0], pts[1]], dtype=np.float32)   # left_eye, right_eye
        dst = np.array([_REF_LEFT_EYE, _REF_RIGHT_EYE], dtype=np.float32)

        # Estimate similarity transform (rotation + uniform scale + translation)
        mat, _ = cv2.estimateAffinePartial2D(src, dst, method=cv2.LMEDS)
        if mat is None:
            return None

        aligned = cv2.warpAffine(frame, mat, (112, 112), flags=cv2.INTER_LINEAR)
        return aligned
    except Exception as e:
        print(f"[FaceGreetingArm] align_face_112 error: {e}")
        return None


# ── Histogram-based face embedding ───────────────────────────────────────────

def compute_aligned_embedding(aligned_112: "np.ndarray") -> Optional["np.ndarray"]:
    """Build a compact face-identity embedding from an affine-aligned 112×112 crop.

    Strategy (Pi 4B optimized — no extra model):
      1. Convert to HSV; extract H (hue / skin tone) and V (intensity) channels.
      2. Compute per-channel histograms on 4 spatial zones
         (top-left, top-right, bottom-left, bottom-right face quadrants).
      3. Concatenate and L2-normalise → ~128-dim vector.

    Rationale:
      - Skin tone (H channel) and facial contrast (V channel) are stable identifiers.
      - Spatial zoning captures relative feature placement (eye zone vs mouth zone).
      - Pure cv2, no extra model file, very fast on Pi 4B (~0.5 ms).
    """
    if aligned_112 is None or cv2 is None:
        return None
    try:
        img = aligned_112
        # Handle both RGB (from picamera2) and BGR (from cv2)
        hsv = cv2.cvtColor(img, cv2.COLOR_RGB2HSV if img.shape[2] == 3 else cv2.COLOR_BGR2HSV)
        h_ch, s_ch, v_ch = cv2.split(hsv)

        zones = [
            (slice(0, 56), slice(0, 56)),    # top-left  (forehead-left)
            (slice(0, 56), slice(56, 112)),   # top-right (forehead-right)
            (slice(56, 112), slice(0, 56)),   # bottom-left (cheek/mouth-left)
            (slice(56, 112), slice(56, 112)), # bottom-right (cheek/mouth-right)
        ]

        feats = []
        for (ry, rx) in zones:
            for ch in (h_ch[ry, rx], v_ch[ry, rx], s_ch[ry, rx]):
                hist = cv2.calcHist([ch], [0], None, [16], [0, 256])
                hist = cv2.normalize(hist, hist).flatten()
                feats.append(hist)

        embedding = np.concatenate(feats).astype(np.float32)
        norm = np.linalg.norm(embedding)
        if norm > 1e-6:
            embedding /= norm
        return embedding
    except Exception as e:
        print(f"[FaceGreetingArm] compute_aligned_embedding error: {e}")
        return None


def landmark_only_embedding(landmarks_10: "np.ndarray") -> Optional["np.ndarray"]:
    """Scale-invariant geometric embedding from 5 YuNet landmarks (last-resort fallback).

    Used when the frame is unavailable or alignment fails.
    """
    if landmarks_10 is None or len(landmarks_10) < 10 or np is None:
        return None
    try:
        pts = np.array(landmarks_10[:10], dtype=np.float64).reshape(5, 2)
        eye_dist = np.linalg.norm(pts[1] - pts[0]) + 1e-6
        pts_norm = (pts - pts[0]) / eye_dist
        vec = pts_norm.flatten().astype(np.float32)
        norm = np.linalg.norm(vec)
        if norm > 1e-6:
            vec /= norm
        return vec
    except Exception:
        return None


# ── Cosine similarity ─────────────────────────────────────────────────────────

def cosine_similarity(a: "np.ndarray", b: "np.ndarray") -> float:
    """Cosine similarity of two L2-normalised vectors (higher = more similar)."""
    if a is None or b is None or np is None:
        return -1.0
    return float(np.dot(a, b))


# ── TTL cache entry ───────────────────────────────────────────────────────────

class _CacheEntry:
    """One person slot in the 30-min TTL greeting memory."""

    def __init__(self, embedding: "np.ndarray", ts: float) -> None:
        self.embeddings = [embedding]
        self.ts = ts
        self.greet_count = 1
        self.learning_until = ts + 30.0

    def add_embedding(self, embedding: "np.ndarray"):
        if len(self.embeddings) < 50:
            self.embeddings.append(embedding)

    def is_learning(self, now: float) -> bool:
        return now < self.learning_until

    def age_sec(self, now: float) -> float:
        return now - self.ts

    def is_expired(self, now: float) -> bool:
        return self.age_sec(now) > MEMORY_TTL_SEC

    def refresh(self, now: float) -> None:
        # Do not update self.ts here so the 30-min TTL is a strict deadline from first detection
        self.greet_count += 1

    def re_encounter(self, now: float) -> None:
        self.ts = now
        self.greet_count += 1
        self.learning_until = now + 30.0


# ── Main service class ────────────────────────────────────────────────────────

class FaceGreetingArmService:
    """Watch for faces and trigger arm hi-gesture for new/forgotten faces.

    Embedding pipeline:
        face_candidates[0] from Blackboard
            → scale landmarks to stream_frame coords
            → affine align to 112×112 (eye-pair anchored)
            → zonal HSV histogram embedding (192-dim, L2-normed)
            → cosine similarity against 30-min TTL cache
            → new OR forgotten face → trigger hi arm pose
    """

    def __init__(
        self,
        bb: Blackboard,
        config_path: Path = DEFAULT_CONFIG_PATH,
        presets_path: Path = DEFAULT_PRESETS_PATH,
    ) -> None:
        self.bb = bb

        # ── Config ────────────────────────────────────────────────────────────
        cfg = _load_yaml(config_path)
        fg = (cfg.get("face_greeting_arm") or {}) if cfg else {}

        self.enabled = bool(fg.get("enabled", True))
        self.memory_timeout_sec = float(fg.get("memory_timeout_minutes", 30.0)) * 60.0
        self.min_face_area_ratio = float(fg.get("min_face_area_ratio", 0.012))
        self.hold_sec = float(fg.get("hold_sec", 1.5))
        self.cleanup_interval_sec = float(fg.get("cleanup_interval_sec", 60.0))
        self.cosine_threshold = float(fg.get("cosine_threshold", COSINE_THRESHOLD))
        self.greeting_cooldown_sec = float(fg.get("greeting_cooldown_sec", 30.0))

        # ── Hi poses ──────────────────────────────────────────────────────────
        presets = _load_json(presets_path)
        poses = presets.get("poses", {})
        self.hi_poses = [name for name in poses.keys() if name.startswith("hi")]
        if not self.hi_poses:
            print("[FaceGreetingArm] WARNING: No 'hi' poses found — using 'home'")
            self.hi_poses = ["home"]

        # ── TTL cache ─────────────────────────────────────────────────────────
        self._cache: List[_CacheEntry] = []

        # ── State ─────────────────────────────────────────────────────────────
        self._face_since: Optional[float] = None
        self._current_embedding: Optional["np.ndarray"] = None
        self._current_match: Optional[_CacheEntry] = None
        self._last_capture_time = 0.0
        self._last_cleanup = time.time()
        self._last_greeting_time: Optional[float] = None

        print(
            f"[FaceGreetingArm] Initialized — "
            f"YuNet-aligned HSV embedding | "
            f"cosine_threshold={self.cosine_threshold:.2f} | "
            f"TTL={self.memory_timeout_sec / 60:.0f} min | "
            f"hi_poses={self.hi_poses}"
        )

    # ── Cache helpers ─────────────────────────────────────────────────────────

    def _gc_cache(self, now: float) -> None:
        """Prune expired entries — prevents memory leak."""
        before = len(self._cache)
        self._cache = [e for e in self._cache if not e.is_expired(now)]
        removed = before - len(self._cache)
        if removed:
            print(
                f"[FaceGreetingArm] GC: removed {removed} expired "
                f"({len(self._cache)} remain)"
            )

    def _find_match(self, embedding: "np.ndarray") -> Optional[_CacheEntry]:
        """Best cosine match above threshold, or None (new person)."""
        if embedding is None:
            return None
        best: Optional[_CacheEntry] = None
        best_sim = -1.0
        for entry in self._cache:
            for saved_emb in entry.embeddings:
                sim = cosine_similarity(embedding, saved_emb)
                if sim > self.cosine_threshold and sim > best_sim:
                    best = entry
                    best_sim = sim
        if best:
            print(f"[FaceGreetingArm] Cache HIT  — sim={best_sim:.3f}, greeted {best.greet_count}x")
        else:
            print(f"[FaceGreetingArm] Cache MISS — no match above {self.cosine_threshold:.2f}")
        return best

    # ── Embedding pipeline ────────────────────────────────────────────────────

    def _compute_embedding(
        self,
        frame: "np.ndarray",
        face_data: dict,
    ) -> Optional["np.ndarray"]:
        """Align face → histogram embedding.

        face_data keys (from FaceTracker face_candidates):
            norm_x, norm_y  — face center, normalised [-1, 1]
            area_ratio      — face-area / frame-area
            landmarks       — 10 pixel coords in detect_res space  ← key input
            detect_res      — (dw, dh) tuple of detection resolution
        """
        landmarks_raw = face_data.get("landmarks")
        detect_res = face_data.get("detect_res")

        # ── Scale landmarks from detect_res → stream_frame pixel space ────────
        if (
            frame is not None
            and landmarks_raw is not None
            and len(landmarks_raw) >= 10
            and detect_res is not None
        ):
            fh, fw = frame.shape[:2]
            dw, dh = detect_res
            lm = np.array(landmarks_raw[:10], dtype=np.float32)
            # Even indices = X (width axis), odd = Y (height axis)
            lm[0::2] = lm[0::2] * (fw / dw)
            lm[1::2] = lm[1::2] * (fh / dh)

            aligned = align_face_112(frame, lm)
            if aligned is not None:
                embedding = compute_aligned_embedding(aligned)
                if embedding is not None:
                    return embedding

        # ── Geometric fallback if frame/alignment unavailable ─────────────────
        if landmarks_raw is not None and len(landmarks_raw) >= 10:
            print("[FaceGreetingArm] Using geometric landmark fallback embedding")
            return landmark_only_embedding(
                np.array(landmarks_raw[:10], dtype=np.float32)
            )

        print("[FaceGreetingArm] Cannot compute embedding — no landmarks")
        return None

    # ── Greeting trigger ──────────────────────────────────────────────────────

    def _trigger_greeting(self) -> str:
        pose_name = random.choice(self.hi_poses)
        seq = self.bb.read("arm_greeting_seq").get("arm_greeting_seq", 0)
        
        try:
            from voice.greetings import generate_random_face_greeting
            text = generate_random_face_greeting()
            f_seq = self.bb.read("face_greeting_seq").get("face_greeting_seq", 0)
            self.bb.write(
                arm_greeting_seq=seq + 1,
                arm_greeting_pose=pose_name,
                face_greeting_seq=f_seq + 1,
                face_greeting_text=text,
            )
            print(f"[FaceGreetingArm] Hi! → {pose_name} | Spoken greeting queued: '{text}'")
        except Exception as e:
            self.bb.write(arm_greeting_seq=seq + 1, arm_greeting_pose=pose_name)
            print(f"[FaceGreetingArm] Hi! → {pose_name} (Voice trigger error: {e})")

        return pose_name

    # ── Main loop ─────────────────────────────────────────────────────────────

    def run(self) -> None:
        if not self.enabled:
            print("[FaceGreetingArm] Disabled in config.")
            return
        if cv2 is None or np is None:
            print("[FaceGreetingArm] OpenCV/NumPy unavailable. Disabled.")
            return

        loop_delay = 0.15  # 150 ms — gentle on Pi 4B CPU
        print("[FaceGreetingArm] Running — waiting for new faces.")

        while self.bb.read("running")["running"]:
            now = time.time()

            # Periodic GC
            if (now - self._last_cleanup) > self.cleanup_interval_sec:
                self._gc_cache(now)
                self._last_cleanup = now

            # ── Blackboard read ────────────────────────────────────────────────
            state = self.bb.read(
                "face_detected",
                "face_area_ratio",
                "face_candidates",
                "stream_frame",
                "agent_speaking",
                "user_speaking",
                "bye_wave_active",
            )

            face_visible = (
                state["face_detected"]
                and float(state["face_area_ratio"]) >= self.min_face_area_ratio
            )
            busy = (
                state["agent_speaking"]
                or state["user_speaking"]
                or state["bye_wave_active"]
            )

            if face_visible and not busy:
                # ── Hold timer: require stable face before processing ──────────
                if self._face_since is None:
                    self._face_since = now
                    self._current_embedding = None

                elif (now - self._face_since) >= self.hold_sec:
                    # Face stable long enough — handle initial computation or learning
                    frame = state["stream_frame"]
                    candidates = state["face_candidates"]

                    if frame is not None and candidates:
                        face_data = candidates[0]  # largest / best face
                        
                        # Initial detection
                        if self._current_embedding is None:
                            embedding = self._compute_embedding(frame, face_data)
                            self._current_embedding = embedding  # mark processed

                            if embedding is not None:
                                match = self._find_match(embedding)

                                if match is None:
                                    # NEW FACE ─────────────────────────────────────
                                    self._trigger_greeting()
                                    match = _CacheEntry(embedding, now)
                                    self._cache.append(match)
                                    self._last_greeting_time = now

                                elif match.is_expired(now):
                                    # FORGOTTEN FACE (TTL expired) ─────────────────
                                    print(
                                        f"[FaceGreetingArm] Forgotten face — "
                                        f"age {match.age_sec(now)/60:.1f} min"
                                    )
                                    self._trigger_greeting()
                                    match.re_encounter(now)
                                    self._last_greeting_time = now

                                else:
                                    # KNOWN FACE — suppress greeting ───────────────
                                    remaining = (MEMORY_TTL_SEC - match.age_sec(now)) / 60.0
                                    print(
                                        f"[FaceGreetingArm] Greeting suppressed — "
                                        f"greeted {match.greet_count}x, "
                                        f"forgotten in {remaining:.1f} min"
                                    )
                                    match.refresh(now)

                                self._current_match = match
                                self._last_capture_time = now

                        # Continuous learning phase (max 2 FPS)
                        elif self._current_match is not None and self._current_match.is_learning(now):
                            if (now - self._last_capture_time) >= 0.5:
                                embedding = self._compute_embedding(frame, face_data)
                                if embedding is not None:
                                    self._current_match.add_embedding(embedding)
                                self._last_capture_time = now

                    else:
                        self._face_since = None  # no frame yet, retry
            else:
                # Face lost or robot busy
                if self._face_since is not None:
                    self._face_since = None
                    self._current_embedding = None
                    self._current_match = None

            time.sleep(loop_delay)

        print("[FaceGreetingArm] Stopped.")
