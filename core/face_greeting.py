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
from voice.greetings import generate_random_face_greeting

APP_DIR = Path(__file__).resolve().parent.parent
DEFAULT_CONFIG_PATH = APP_DIR / "config.yaml"


def _load_yaml(path: Path) -> dict:
    if yaml is None or not path.exists():
        return {}
    with open(path, encoding="utf-8") as f:
        return yaml.safe_load(f) or {}


class FaceGreetingMonitor:
    """Watch face_detected and queue a greeting on each new appearance."""

    def __init__(self, bb: Blackboard, config_path: Path = DEFAULT_CONFIG_PATH) -> None:
        self.bb = bb
        self.config_path = config_path
        self._last_cfg_mtime = 0.0
        self._face_since: float | None = None
        self._greeted_this_visit = False
        self._last_greet_ts = 0.0
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
                "body_detected",
                "agent_speaking",
                "user_speaking",
                "voice_session_active",
            )
            person_visible = (
                (state["face_detected"] and float(state["face_area_ratio"]) >= self.min_face_area_ratio)
                or state["body_detected"]
            )

            if person_visible:
                if self._face_since is None:
                    self._face_since = now
                elif (
                    not state.get("voice_session_active", False)
                    and not self._greeted_this_visit
                    and (now - self._face_since) >= self.hold_sec
                    and (now - self._last_greet_ts) >= self.cooldown_sec
                    and not state["agent_speaking"]
                    and not state["user_speaking"]
                ):
                    text = generate_random_face_greeting()
                    self._seq += 1
                    self.bb.write(face_greeting_seq=self._seq, face_greeting_text=text)
                    self._last_greet_ts = now
                    self._greeted_this_visit = True
                    print(f"[FaceGreeting] Queued: {text!r}")
            else:
                self._face_since = None
                self._greeted_this_visit = False

            time.sleep(loop_delay)

        print("[FaceGreeting] Stopped.")
