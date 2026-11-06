"""FaceGreetingMonitor — trigger a random hello when someone appears in frame.

No face enrollment. Writes face_greeting_seq + face_greeting_text to the
Blackboard; VoiceService speaks it when a LiveKit session is active.
"""

from __future__ import annotations

import time
from pathlib import Path

try:
    import yaml
except ImportError:
    yaml = None

from core.blackboard import Blackboard
from voice.greetings import generate_dwell_stall_prompt, generate_random_face_greeting

APP_DIR = Path(__file__).resolve().parent.parent
DEFAULT_CONFIG_PATH = APP_DIR / "config.yaml"


def _load_yaml(path: Path) -> dict:
    if yaml is None or not path.exists():
        return {}
    with open(path, encoding="utf-8") as f:
        return yaml.safe_load(f) or {}


class FaceGreetingMonitor:
    """Watch face_detected / ToF proximity and queue a greeting on each new appearance."""

    def __init__(self, bb: Blackboard, config_path: Path = DEFAULT_CONFIG_PATH) -> None:
        self.bb = bb
        self.config_path = config_path
        self._last_cfg_mtime = 0.0
        self._face_since: float | None = None
        self._greeted_this_visit = False
        self._greeted_face_count = 0
        self._crowd_growth_greeted = False
        self._last_greet_ts = 0.0
        self._last_activity_ts = 0.0
        self._last_dwell_prompt_ts = 0.0
        self._dwell_prompts_this_visit = 0
        self._seq = 0
        self._reload_config()

    def _reload_config(self) -> None:
        try:
            mtime = self.config_path.stat().st_mtime
            if mtime > self._last_cfg_mtime:
                cfg = _load_yaml(self.config_path)
                fg = (cfg.get("face_greeting") or {}) if cfg else {}
                self.enabled = bool(fg.get("enabled", True))
                self.cooldown_sec = float(fg.get("cooldown_sec", 60.0))
                self.hold_sec = float(fg.get("hold_sec", 0.45))
                self.min_face_area_ratio = float(fg.get("min_face_area_ratio", 0.008))
                self.dwell_prompt_enabled = bool(fg.get("dwell_prompt_enabled", True))
                self.dwell_prompt_sec = float(fg.get("dwell_prompt_sec", 14.0))
                self.dwell_cooldown_sec = float(fg.get("dwell_cooldown_sec", 35.0))
                self.max_dwell_prompts_per_visit = int(fg.get("max_dwell_prompts_per_visit", 2))
                self._last_cfg_mtime = mtime
        except Exception:
            pass

    def run(self) -> None:
        loop_delay = 0.1
        print("[FaceGreeting] Monitoring thread started.")

        while self.bb.read("running")["running"]:
            self._reload_config()
            if not self.enabled:
                time.sleep(loop_delay)
                continue

            now = time.time()
            state = self.bb.read(
                "face_detected",
                "face_area_ratio",
                "face_count",
                "body_detected",
                "prox_zone_count",
                "tof_surrounded",
                "agent_speaking",
                "user_speaking",
                "voice_session_active",
                "screen_touch_ts",
            )
            cam_visible = (
                (state["face_detected"] and float(state["face_area_ratio"]) >= self.min_face_area_ratio)
                or state["body_detected"]
            )
            prox_zones = int(state.get("prox_zone_count", 0) or 0)
            tof_surrounded = bool(state.get("tof_surrounded", False))
            cam_faces = int(state.get("face_count", 0) or 0) if cam_visible else 0
            # Fuse camera face count with ToF occupied zone count
            effective_count = max(1, cam_faces, prox_zones)
            if tof_surrounded and (cam_faces >= 2 or prox_zones >= 3):
                effective_count = max(effective_count, 3)

            screen_touch_ts = float(state.get("screen_touch_ts") or 0.0)
            if screen_touch_ts > self._last_activity_ts:
                self._last_activity_ts = screen_touch_ts
            if state["agent_speaking"] or state["user_speaking"]:
                self._last_activity_ts = now

            person_visible = cam_visible or (prox_zones >= 1)
            required_hold = self.hold_sec if cam_visible else max(self.hold_sec, 2.0)

            if person_visible:
                if self._face_since is None:
                    self._face_since = now
                    self._last_activity_ts = now
                    self._dwell_prompts_this_visit = 0
                elif (
                    not self._greeted_this_visit
                    and (now - self._face_since) >= required_hold
                    and (now - self._last_greet_ts) >= self.cooldown_sec
                    and not state["agent_speaking"]
                    and not state["user_speaking"]
                ):
                    text = generate_random_face_greeting(face_count=effective_count)
                    self._seq = int(self.bb.read("face_greeting_seq").get("face_greeting_seq", 0)) + 1
                    self.bb.write(face_greeting_seq=self._seq, face_greeting_text=text)
                    self._last_greet_ts = now
                    self._last_activity_ts = now
                    self._greeted_this_visit = True
                    self._greeted_face_count = effective_count
                    self._crowd_growth_greeted = effective_count >= 3
                    print(f"[FaceGreeting] Queued (count={effective_count}, cam={cam_faces}, tof={prox_zones}): {text!r}")
                elif (
                    self._greeted_this_visit
                    and not self._crowd_growth_greeted
                    and self._greeted_face_count <= 2
                    and effective_count >= 3
                    and (now - self._last_greet_ts) >= 12.0
                    and not state["agent_speaking"]
                    and not state["user_speaking"]
                ):
                    text = generate_random_face_greeting(
                        face_count=effective_count,
                        is_crowd_growth=True,
                    )
                    self._seq = int(self.bb.read("face_greeting_seq").get("face_greeting_seq", 0)) + 1
                    self.bb.write(face_greeting_seq=self._seq, face_greeting_text=text)
                    self._last_greet_ts = now
                    self._last_activity_ts = now
                    self._crowd_growth_greeted = True
                    print(f"[FaceGreeting] Crowd join-in queued (count={effective_count}, cam={cam_faces}, tof={prox_zones}): {text!r}")
                elif (
                    self.dwell_prompt_enabled
                    and self._greeted_this_visit
                    and self._dwell_prompts_this_visit < self.max_dwell_prompts_per_visit
                    and (now - self._last_activity_ts) >= self.dwell_prompt_sec
                    and (now - self._last_dwell_prompt_ts) >= self.dwell_cooldown_sec
                    and not state["agent_speaking"]
                    and not state["user_speaking"]
                ):
                    text = generate_dwell_stall_prompt()
                    self._seq = int(self.bb.read("face_greeting_seq").get("face_greeting_seq", 0)) + 1
                    self.bb.write(face_greeting_seq=self._seq, face_greeting_text=text)
                    self._last_activity_ts = now
                    self._last_dwell_prompt_ts = now
                    self._dwell_prompts_this_visit += 1
                    print(f"[FaceGreeting] Proactive dwell prompt #{self._dwell_prompts_this_visit}: {text!r}")
            else:
                self._face_since = None
                self._greeted_this_visit = False
                self._greeted_face_count = 0
                self._crowd_growth_greeted = False
                self._dwell_prompts_this_visit = 0

            time.sleep(loop_delay)

        print("[FaceGreeting] Stopped.")
