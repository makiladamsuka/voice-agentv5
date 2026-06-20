"""EmotionEngine: reactive emotion state machine.

All emotion logic is contained here. Adding or changing an emotion trigger
requires editing only this file.

Reads from BB:
    face_detected, face_area_ratio, face_count, track_kind,
    servo_mode, servo_pan, manual_emotion, running

Writes to BB:
    emotion, emotion_intensity
"""

from __future__ import annotations

import random
import time
from pathlib import Path

try:
    import yaml
except ImportError:
    yaml = None

from core.blackboard import Blackboard

APP_DIR = Path(__file__).resolve().parent.parent
DEFAULT_CONFIG_PATH = APP_DIR / "config.yaml"

# ── Constants ──────────────────────────────────────────────────────────────────
NO_FACE_GRACE_SEC = 0.9
NO_PERSON_HOLD_MIN_SEC = 2.8
NO_PERSON_HOLD_MAX_SEC = 5.2
PERSON_HOLD_MIN_SEC = 1.1
PERSON_HOLD_MAX_SEC = 2.8
DIRECTION_TRIGGER_NORM_X = 0.22
DIRECTION_HOLD_MIN_SEC = 0.6
DIRECTION_HOLD_MAX_SEC = 1.2
DIRECTION_COOLDOWN_SEC = 0.9
CLOSE_FACE_AREA_RATIO = 0.05
FAR_FACE_AREA_RATIO = 0.018
NEAR_EXIT_RATIO = CLOSE_FACE_AREA_RATIO * 0.82
FAR_EXIT_RATIO = FAR_FACE_AREA_RATIO * 1.25
EMOTION_CHANGE_COOLDOWN = 0.75
EMOTION_LOG = True


def _load_yaml(path: Path) -> dict:
    if yaml is None or not path.exists():
        return {}
    with open(path, encoding="utf-8") as f:
        return yaml.safe_load(f) or {}


def _classify_distance(area: float, prev_zone: str) -> str:
    if prev_zone == "near" and area >= NEAR_EXIT_RATIO:
        return "near"
    if prev_zone == "far" and area <= FAR_EXIT_RATIO:
        return "far"
    if area >= CLOSE_FACE_AREA_RATIO:
        return "near"
    if area < FAR_FACE_AREA_RATIO:
        return "far"
    return "mid"


def _weighted_pick(weights: dict, fallback: str = "idle") -> str:
    total = sum(w for w in weights.values() if w > 0)
    if total <= 0:
        return fallback
    r = random.uniform(0.0, total)
    acc = 0.0
    for name, w in weights.items():
        if w <= 0:
            continue
        acc += w
        if r <= acc:
            return name
    return fallback


def _choose_no_person(toggle: list) -> str:
    base = toggle[0]
    toggle[0] = "idle" if base == "sleepy" else "sleepy"
    r = random.random()
    if r < 0.08:
        return "sad"
    if r < 0.15:
        return "idle"
    return base


def _choose_person(zone: str, track_kind: str) -> str:
    if zone == "near":
        return _weighted_pick({"happy": 0.45, "excited": 0.22, "warm": 0.20, "surprised": 0.13})
    if zone == "far":
        return _weighted_pick({"curious": 0.40, "attentive": 0.30, "calm": 0.20, "uncertain": 0.10})
    if track_kind == "multi":
        return _weighted_pick({"engaged": 0.35, "amused": 0.30, "playful": 0.25, "happy": 0.10})
    return _weighted_pick({"attentive": 0.35, "engaged": 0.28, "curious": 0.22, "happy": 0.15})


# ── EmotionEngine ──────────────────────────────────────────────────────────────

class EmotionEngine:
    """Reactive emotion state machine — writes emotion + emotion_intensity to BB."""

    def __init__(self, bb: Blackboard, config_path: Path = DEFAULT_CONFIG_PATH) -> None:
        self.bb = bb

        self._current = "idle"
        self._last_change_ts = 0.0
        self._next_change_ts = time.time() + random.uniform(1.6, 3.0)
        self._distance_zone = "mid"
        self._no_person_toggle = ["sleepy"]
        self._direction_cooldown = 0.0
        self._last_seen_ts = 0.0
        self._loop_hz = 24.0

    def _set(self, name: str, intensity: float = 1.0) -> None:
        now = time.time()
        if name == self._current:
            return
        if (now - self._last_change_ts) < EMOTION_CHANGE_COOLDOWN:
            return
        self._current = name
        self._last_change_ts = now
        self.bb.write(emotion=name, emotion_intensity=intensity)
        if EMOTION_LOG:
            print(f"[emotion {time.strftime('%H:%M:%S')}] {name}")

    def run(self) -> None:
        loop_delay = 1.0 / self._loop_hz
        self.bb.write(emotion="idle", emotion_intensity=1.0)

        while self.bb.read("running")["running"]:
            now = time.time()

            # Manual override wins unconditionally
            state = self.bb.read(
                "manual_emotion", "face_detected", "face_area_ratio",
                "face_count", "track_kind", "servo_mode",
            )
            manual = state["manual_emotion"]
            if manual is not None:
                self._set(manual)
                time.sleep(loop_delay)
                continue

            face = state["face_detected"]
            area = state["face_area_ratio"]
            count = state["face_count"]
            kind = state["track_kind"]
            mode = state["servo_mode"]

            if face:
                self._last_seen_ts = now

            # Compute distance zone
            if face:
                self._distance_zone = _classify_distance(area, self._distance_zone)

            person_present = face or (now - self._last_seen_ts) < NO_FACE_GRACE_SEC

            if now >= self._next_change_ts:
                if not person_present:
                    emotion = _choose_no_person(self._no_person_toggle)
                    hold = random.uniform(NO_PERSON_HOLD_MIN_SEC, NO_PERSON_HOLD_MAX_SEC)
                else:
                    emotion = _choose_person(self._distance_zone, kind)
                    hold = random.uniform(PERSON_HOLD_MIN_SEC, PERSON_HOLD_MAX_SEC)
                self._set(emotion)
                self._next_change_ts = now + hold

            time.sleep(loop_delay)

        print("[EmotionEngine] Stopped.")
