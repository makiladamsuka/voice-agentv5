"""EmotionEngine: reactive emotion state machine.

Uses SurroundingsEmotionController (ported from voice-agentv4) for distance,
directional, activity, and squint-driven expression selection.

Reads from BB:
    face_detected, face_area_ratio, face_norm_x, face_norm_y, face_count,
    track_kind, servo_mode, manual_emotion, running

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
from core.emotion_presets import EMOTION_INTENSITY, resolve_emotion_name
from core.surroundings_emotion import SurroundingsEmotionConfig, SurroundingsEmotionController

APP_DIR = Path(__file__).resolve().parent.parent
DEFAULT_CONFIG_PATH = APP_DIR / "config.yaml"

EMOTION_LOG = True


def _load_yaml(path: Path) -> dict:
    if yaml is None or not path.exists():
        return {}
    with open(path, encoding="utf-8") as f:
        return yaml.safe_load(f) or {}


def _cfg(cfg: dict, key: str, default=None):
    return (cfg.get(key) if cfg else None) or default


# ── EmotionEngine ──────────────────────────────────────────────────────────────

class EmotionEngine:
    """Reactive emotion state machine — writes emotion + emotion_intensity to BB."""

    def __init__(self, bb: Blackboard, config_path: Path = DEFAULT_CONFIG_PATH) -> None:
        self.bb = bb
        cfg = _load_yaml(config_path)
        se = _cfg(cfg, "surroundings_emotion", default={}) or {}
        ft = _cfg(cfg, "face_tracking", default={}) or {}
        vp = _cfg(cfg, "voice_profile", default={}) or {}

        self._controller = SurroundingsEmotionController(
            cfg=SurroundingsEmotionConfig(
                no_face_grace_sec=float(se.get("no_face_grace_sec", 0.9)),
                no_person_hold_min_sec=float(se.get("no_person_hold_min_sec", 2.8)),
                no_person_hold_max_sec=float(se.get("no_person_hold_max_sec", 5.2)),
                person_hold_min_sec=float(se.get("person_hold_min_sec", 1.1)),
                person_hold_max_sec=float(se.get("person_hold_max_sec", 2.8)),
                direction_trigger_norm_x=float(se.get("direction_trigger_norm_x", 0.22)),
                direction_hold_min_sec=float(se.get("direction_hold_min_sec", 0.6)),
                direction_hold_max_sec=float(se.get("direction_hold_max_sec", 1.2)),
                direction_cooldown_sec=float(se.get("direction_cooldown_sec", 0.9)),
                close_face_enter_ratio=float(se.get("close_face_enter_ratio", 0.05)),
                far_face_area_ratio=float(se.get("far_face_area_ratio", 0.018)),
                near_exit_ratio=float(se.get("near_exit_ratio", 0.041)),
                far_exit_ratio=float(se.get("far_exit_ratio", 0.0225)),
                emotion_history_len=int(se.get("emotion_history_len", 3)),
                no_face_sad_min_sec=float(se.get("no_face_sad_min_sec", 150.0)),
            ),
        )
        self._face_track_default = str(ft.get("face_track_default", "attentive"))
        self._far_squint_chance = float(ft.get("far_squint_chance", 0.08))

        self._current = "idle"
        self._prev_norm_x = 0.0
        self._prev_norm_y = 0.0
        self._loop_hz = 24.0
        self._voice_loop_hz = float(vp.get("emotion_loop_hz", 8.0))
        self._conv_emotion_last: str | None = None
        self._conv_emotion_hold_until = 0.0
        self._voice_glance_until = 0.0
        self._voice_glance_emotion = "idle"
        self._speak_glance_until = 0.0
        self._speak_glance_emotion = "idle"
        self._vad_start_ts = 0.0
        self._listening_glance_until = 0.0
        self._listening_glance_emotion = "idle"
        self._thinking_glance_until = 0.0
        self._thinking_glance_emotion = "thinking"
        self._waiting_start_ts = 0.0

    def _set(self, name: str, intensity_scale: float = 1.0) -> None:
        resolved = resolve_emotion_name(name)
        if resolved is None:
            return
        intensity = EMOTION_INTENSITY.get(resolved, 0.5) * max(0.0, min(1.0, intensity_scale))
        if resolved == self._current:
            self.bb.write(emotion_intensity=intensity)
            return
        self._current = resolved
        self.bb.write(emotion=resolved, emotion_intensity=intensity)
        if EMOTION_LOG:
            print(f"[emotion {time.strftime('%H:%M:%S')}] {resolved}")

    def run(self) -> None:
        self._set("idle")

        while self.bb.read("running")["running"]:
            voice_active = self.bb.read("voice_session_active")["voice_session_active"]
            loop_delay = 1.0 / (
                self._voice_loop_hz if voice_active else self._loop_hz
            )
            now = time.time()

            state = self.bb.read(
                "manual_emotion",
                "voice_session_active",
                "user_speaking",
                "conv_state",
                "conv_emotion",
                "prox_glance_active",
                "prox_glance_emotion",
                "face_detected",
                "face_area_ratio",
                "face_norm_x",
                "face_norm_y",
                "face_count",
                "track_kind",
                "servo_mode",
            )
            # ── optimize/cpu2: yield to AnimationEngine when active ──────────
            if state.get("animation_override", False):
                time.sleep(loop_delay)
                continue

            manual = state["manual_emotion"]
            if manual is not None:
                self._set(manual)
                time.sleep(loop_delay)
                continue

            voice_active = state.get("voice_session_active", False)
            user_speaking = state.get("user_speaking", False)
            conv_state = state.get("conv_state", "idle")
            conv_emotion = state.get("conv_emotion")

            if voice_active:
                # Priority 1: Server NLU Processing (Thinking)
                if conv_state == "thinking":
                    self._vad_start_ts = 0.0
                    self._waiting_start_ts = 0.0
                    if now >= self._thinking_glance_until:
                        # Rich cognitive cycle: thinking -> concentrating -> remembering
                        choices = ["thinking", "thinking", "concentrating", "remembering"]
                        self._thinking_glance_emotion = random.choice(choices)
                        self._thinking_glance_until = now + random.uniform(0.8, 1.8)
                    self._set(self._thinking_glance_emotion)
                    time.sleep(loop_delay)
                    continue
                else:
                    self._thinking_glance_until = 0.0

                # Priority 2: Robot Speaking (TTS Output)
                if conv_state == "speaking":
                    self._vad_start_ts = 0.0
                    self._waiting_start_ts = 0.0
                    base_emotion = conv_emotion or "engaged"

                    if self._speak_glance_until == 0.0:
                        # First frame of speech -> hold base sentiment emotion solidly for 2.2-3.5s
                        self._speak_glance_emotion = base_emotion
                        self._speak_glance_until = now + random.uniform(2.2, 3.5)
                    elif now >= self._speak_glance_until:
                        # 30% chance to do a brief micro-glance left/right after initial speech hold
                        if random.random() < 0.30:
                            is_happy = base_emotion in ("happy", "cheerful", "excited", "warm", "proud", "amused")
                            if is_happy:
                                choices = ["looking_left_cheerful", "looking_right_cheerful", "looking_left_happy", "looking_right_happy"]
                            else:
                                choices = ["looking_left_natural", "looking_right_natural"]
                            self._speak_glance_emotion = random.choice(choices)
                            self._speak_glance_until = now + random.uniform(0.7, 1.3)
                        else:
                            self._speak_glance_emotion = base_emotion
                            self._speak_glance_until = now + random.uniform(2.0, 3.8)

                    self._set(self._speak_glance_emotion)
                    time.sleep(loop_delay)
                    continue
                else:
                    self._speak_glance_until = 0.0

                # Priority 3: User Speaking (VAD Active)
                if user_speaking:
                    self._waiting_start_ts = 0.0
                    if self._vad_start_ts == 0.0:
                        self._vad_start_ts = now
                    elapsed_vad = now - self._vad_start_ts

                    # Step 1: Immediate reaction (0.0s - 0.5s: attentive, 0.5s - 1.2s: engaged)
                    if elapsed_vad <= 0.5:
                        self._set("attentive")
                    elif elapsed_vad <= 1.2:
                        self._set("engaged")
                    else:
                        # Step 2: Extended user speaking -> active listening cycle
                        if now >= self._listening_glance_until:
                            choices = [
                                "attentive", "engaged", "idle",
                                "looking_left_natural", "looking_right_natural",
                                "curious_intense"
                            ]
                            self._listening_glance_emotion = random.choice(choices)
                            self._listening_glance_until = now + random.uniform(1.2, 2.5)
                        self._set(self._listening_glance_emotion)

                    time.sleep(loop_delay)
                    continue

                # Priority 4: Mic active & waiting for next user question (Idle/Waiting)
                self._vad_start_ts = 0.0
                if self._waiting_start_ts == 0.0:
                    self._waiting_start_ts = now

                waiting_elapsed = now - self._waiting_start_ts
                if waiting_elapsed >= 15.0:
                    # Silence threshold reached -> robot looks sleepy/drowsy
                    if now >= self._voice_glance_until:
                        choices = ["sleepy", "sleepy", "bored"]
                        self._voice_glance_emotion = random.choice(choices)
                        self._voice_glance_until = now + random.uniform(3.0, 6.0)
                else:
                    if now >= self._voice_glance_until:
                        # Natural glance distribution during active waiting
                        choices = [
                            "idle", "idle", "idle", "idle",
                            "looking_left_natural", "looking_right_natural",
                            "content", "bored"
                        ]
                        self._voice_glance_emotion = random.choice(choices)
                        duration = random.uniform(1.8, 3.5) if "looking" in self._voice_glance_emotion else random.uniform(2.5, 5.0)
                        self._voice_glance_until = now + duration

                self._set(self._voice_glance_emotion)
                time.sleep(loop_delay)
                continue

            # Voice session inactive -> reset voice timers
            self._vad_start_ts = 0.0
            self._waiting_start_ts = 0.0
            self._voice_glance_until = 0.0
            self._speak_glance_until = 0.0
            self._thinking_glance_until = 0.0
            self._conv_emotion_last = None

            if state.get("prox_glance_active") and state.get("prox_glance_emotion"):
                self._set(str(state["prox_glance_emotion"]))
                time.sleep(loop_delay)
                continue

            face = state["face_detected"]
            area = float(state["face_area_ratio"])
            norm_x = float(state["face_norm_x"])
            norm_y = float(state["face_norm_y"])
            mode = state["servo_mode"]
            wander_mode = mode == "wander"

            dx = abs(norm_x - self._prev_norm_x)
            dy = abs(norm_y - self._prev_norm_y)
            activity = min(1.0, (dx + dy) / 2.0)
            self._prev_norm_x = norm_x
            self._prev_norm_y = norm_y

            zone = self._controller.classify_distance_zone(area) if face else self._controller.distance_zone
            squint_hint = 0.0
            if face and zone == "far" and random.random() < self._far_squint_chance:
                squint_hint = 1.0

            pick = self._controller.tick(
                now=now,
                face_detected=face,
                face_area_ratio=area,
                face_norm_x=norm_x,
                squint_hint=squint_hint,
                activity=activity,
                wander_mode=wander_mode,
            )
            if pick:
                self._set(pick)
            elif face:
                fallback = self._controller.current_emotion or self._face_track_default
                self._set(fallback)

            time.sleep(loop_delay)

        print("[EmotionEngine] Stopped.")
