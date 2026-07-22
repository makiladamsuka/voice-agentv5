"""AnimationEngine — timeline-based synchronized hardware control.

"Pepper-like" keyframe choreography for arms, head pan/tilt, and eye colour.
When a timeline is active, reactive AI services (FaceTracker, EmotionEngine,
TalkGestureService) yield control via the animation_override Blackboard flag.

optimize/cpu2 addition.

Timeline JSON Format
--------------------
Animations are stored in a directory (default: <app_root>/animations/).
Each JSON file is one animation::

    {
        "animation_name": "excited_greeting",
        "duration_ms": 2500,
        "tracks": {
            "arms": [
                {"time_ms": 0,    "a0": 90,  "a1": 20,  "a2": 90,  "a3": 20},
                {"time_ms": 1000, "a0": 160, "a1": 90,  "a2": 160, "a3": 90,  "easing": "easeOutQuad"},
                {"time_ms": 2500, "a0": 90,  "a1": 20,  "a2": 90,  "a3": 20,  "easing": "easeInOutSine"}
            ],
            "head": [
                {"time_ms": 0,    "pan": 100, "tilt": 110},
                {"time_ms": 800,  "pan": 100, "tilt": 140, "easing": "easeOutQuad"},
                {"time_ms": 2500, "pan": 100, "tilt": 110, "easing": "easeInOutSine"}
            ],
            "eyes": [
                {"time_ms": 0,    "theme": "default"},
                {"time_ms": 800,  "theme": "rainbow"},
                {"time_ms": 2000, "theme": "default"}
            ]
        }
    }

Supported easing functions (applied to the segment *entering* a keyframe):
    linear, easeInQuad, easeOutQuad, easeInOutSine, easeInOutCubic

Trigger via Blackboard::

    bb.write(play_animation="excited_greeting")

AnimationEngine polls play_animation at ~10 Hz and starts the timeline
immediately, setting animation_override=True for the duration.
"""

from __future__ import annotations

import json
import math
import time
from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from core.blackboard import Blackboard

APP_DIR = Path(__file__).resolve().parent.parent
DEFAULT_ANIM_DIR = APP_DIR / "animations"

# ── Easing functions ──────────────────────────────────────────────────────────

def _ease(t: float, name: str) -> float:
    """Map normalised time t ∈ [0, 1] through an easing curve."""
    t = max(0.0, min(1.0, t))
    if name == "linear":
        return t
    elif name == "easeInQuad":
        return t * t
    elif name == "easeOutQuad":
        return t * (2.0 - t)
    elif name == "easeInOutSine":
        return -(math.cos(math.pi * t) - 1.0) / 2.0
    elif name == "easeInOutCubic":
        if t < 0.5:
            return 4.0 * t * t * t
        else:
            p = -2.0 * t + 2.0
            return 1.0 - (p * p * p) / 2.0
    return t  # fallback: linear


def _interp(a: float, b: float, t: float, easing: str = "linear") -> float:
    return a + (b - a) * _ease(t, easing)


# ── Timeline parsing helpers ──────────────────────────────────────────────────

def _find_segment(keyframes: list[dict], elapsed_ms: float):
    """Return (kf_a, kf_b) bounding keyframes for elapsed_ms, or (last, last)."""
    if not keyframes:
        return None, None
    if elapsed_ms <= keyframes[0]["time_ms"]:
        return keyframes[0], keyframes[0]
    for i in range(len(keyframes) - 1):
        if keyframes[i]["time_ms"] <= elapsed_ms <= keyframes[i + 1]["time_ms"]:
            return keyframes[i], keyframes[i + 1]
    return keyframes[-1], keyframes[-1]


def _interp_arms(kf_a: dict, kf_b: dict, t: float, easing: str) -> dict:
    return {
        "a0": _interp(kf_a["a0"], kf_b["a0"], t, easing),
        "a1": _interp(kf_a["a1"], kf_b["a1"], t, easing),
        "a2": _interp(kf_a["a2"], kf_b["a2"], t, easing),
        "a3": _interp(kf_a["a3"], kf_b["a3"], t, easing),
    }


def _interp_head(kf_a: dict, kf_b: dict, t: float, easing: str) -> dict:
    result = {}
    if "pan" in kf_a:
        result["pan"] = _interp(kf_a["pan"], kf_b.get("pan", kf_a["pan"]), t, easing)
    if "tilt" in kf_a:
        result["tilt"] = _interp(kf_a["tilt"], kf_b.get("tilt", kf_a["tilt"]), t, easing)
    return result


def _active_eye_theme(keyframes: list[dict], elapsed_ms: float) -> str | None:
    """Return the most recent eye theme at elapsed_ms (step function, no tween)."""
    active = None
    for kf in keyframes:
        if kf["time_ms"] <= elapsed_ms:
            active = kf.get("theme")
        else:
            break
    return active


# ── AnimationEngine ───────────────────────────────────────────────────────────

class AnimationEngine:
    """Plays keyframe animation timelines over the Blackboard.

    Runs in its own daemon thread spawned by start_robot.py.
    Polls bb.play_animation at ~10 Hz; runs the interpolation loop at 25 Hz.

    Parameters
    ----------
    bb:
        Shared Blackboard instance.
    anim_dir:
        Directory to search for .json animation files.
    playback_hz:
        Frequency (Hz) at which the interpolation loop writes to the Blackboard.
        25 Hz = 40 ms per frame — sufficient for smooth servo motion.
    poll_hz:
        Frequency (Hz) at which the idle loop checks for a new play_animation trigger.
    """

    def __init__(
        self,
        bb: "Blackboard",
        anim_dir: Path = DEFAULT_ANIM_DIR,
        playback_hz: float = 25.0,
        poll_hz: float = 10.0,
    ) -> None:
        self.bb = bb
        self.anim_dir = Path(anim_dir)
        self._playback_hz = max(5.0, playback_hz)
        self._poll_hz = max(1.0, poll_hz)
        self._cache: dict[str, dict] = {}  # name -> parsed timeline dict
        self._ensure_anim_dir()

    def _ensure_anim_dir(self) -> None:
        """Create animations/ directory and write a sample animation if empty."""
        self.anim_dir.mkdir(exist_ok=True)
        sample = self.anim_dir / "hello_wave.json"
        if not sample.exists():
            sample.write_text(json.dumps({
                "animation_name": "hello_wave",
                "duration_ms": 2000,
                "tracks": {
                    "arms": [
                        {"time_ms": 0,    "a0": 90, "a1": 20, "a2": 90, "a3": 20},
                        {"time_ms": 700,  "a0": 150, "a1": 80, "a2": 90, "a3": 20,
                         "easing": "easeOutQuad"},
                        {"time_ms": 1200, "a0": 130, "a1": 90, "a2": 90, "a3": 20,
                         "easing": "easeInOutSine"},
                        {"time_ms": 2000, "a0": 90, "a1": 20, "a2": 90, "a3": 20,
                         "easing": "easeInOutCubic"},
                    ],
                    "head": [
                        {"time_ms": 0,    "pan": 100, "tilt": 110},
                        {"time_ms": 500,  "pan": 100, "tilt": 130, "easing": "easeOutQuad"},
                        {"time_ms": 2000, "pan": 100, "tilt": 110, "easing": "easeInOutSine"},
                    ],
                    "eyes": [
                        {"time_ms": 0,    "theme": "default"},
                        {"time_ms": 400,  "theme": "rainbow"},
                        {"time_ms": 1700, "theme": "default"},
                    ],
                },
            }, indent=2))
            print(f"[AnimationEngine] Created sample animation: {sample}")

    def _load(self, name: str) -> dict | None:
        """Load and cache a timeline by name (filename without .json)."""
        if name in self._cache:
            return self._cache[name]
        path = self.anim_dir / f"{name}.json"
        if not path.exists():
            print(f"[AnimationEngine] Animation not found: {path}")
            return None
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
            self._cache[name] = data
            return data
        except Exception as exc:
            print(f"[AnimationEngine] Failed to parse {path}: {exc}")
            return None

    def _play(self, name: str) -> None:
        """Execute one full animation timeline synchronously (blocks the thread)."""
        timeline = self._load(name)
        if timeline is None:
            return

        tracks = timeline.get("tracks", {})
        duration_ms = float(timeline.get("duration_ms", 2000))
        arm_kfs = sorted(tracks.get("arms", []), key=lambda k: k["time_ms"])
        head_kfs = sorted(tracks.get("head", []), key=lambda k: k["time_ms"])
        eye_kfs = sorted(tracks.get("eyes", []), key=lambda k: k["time_ms"])

        dt = 1.0 / self._playback_hz
        print(f"[AnimationEngine] Playing '{name}' ({duration_ms:.0f} ms @ {self._playback_hz:.0f} Hz)")

        # Seize control
        self.bb.write(animation_override=True, play_animation="")

        try:
            start = time.perf_counter()
            last_eye_theme: str | None = None

            while self.bb.read("running")["running"]:
                elapsed_ms = (time.perf_counter() - start) * 1000.0
                if elapsed_ms >= duration_ms:
                    break

                writes: dict = {}

                # ── Arms ──────────────────────────────────────────────────
                if arm_kfs:
                    kf_a, kf_b = _find_segment(arm_kfs, elapsed_ms)
                    if kf_a is not None and kf_b is not None and kf_a is not kf_b:
                        seg_len = kf_b["time_ms"] - kf_a["time_ms"]
                        t = (elapsed_ms - kf_a["time_ms"]) / max(1.0, seg_len)
                        easing = kf_b.get("easing", "easeInOutSine")
                        arm = _interp_arms(kf_a, kf_b, t, easing)
                    else:
                        arm = {k: kf_b[k] for k in ("a0", "a1", "a2", "a3") if k in kf_b}
                    writes.update(
                        arm_a0=arm.get("a0", 90.0),
                        arm_a1=arm.get("a1", 20.0),
                        arm_a2=arm.get("a2", 90.0),
                        arm_a3=arm.get("a3", 20.0),
                    )

                # ── Head ──────────────────────────────────────────────────
                if head_kfs:
                    kf_a, kf_b = _find_segment(head_kfs, elapsed_ms)
                    if kf_a is not None and kf_b is not None and kf_a is not kf_b:
                        seg_len = kf_b["time_ms"] - kf_a["time_ms"]
                        t = (elapsed_ms - kf_a["time_ms"]) / max(1.0, seg_len)
                        easing = kf_b.get("easing", "easeInOutSine")
                        head = _interp_head(kf_a, kf_b, t, easing)
                    else:
                        head = {k: kf_b[k] for k in ("pan", "tilt") if k in kf_b}
                    if "pan" in head:
                        writes["servo_pan"] = head["pan"]
                    if "tilt" in head:
                        writes["servo_tilt"] = head["tilt"]

                # ── Eyes ──────────────────────────────────────────────────
                if eye_kfs:
                    theme = _active_eye_theme(eye_kfs, elapsed_ms)
                    if theme and theme != last_eye_theme:
                        try:
                            from core.eye_themes import resolve_eye_color
                            writes["eye_color"] = resolve_eye_color(theme)
                        except Exception:
                            pass
                        last_eye_theme = theme

                if writes:
                    self.bb.write(**writes)

                time.sleep(dt)

        except Exception as exc:
            print(f"[AnimationEngine] Playback error in '{name}': {exc}")
        finally:
            # Always release control — reactive AI resumes immediately
            self.bb.write(animation_override=False)
            print(f"[AnimationEngine] '{name}' finished.")

    def run(self) -> None:
        """Idle loop: wait for play_animation trigger, then execute the timeline."""
        print(f"[AnimationEngine] Ready. Drop .json timelines in: {self.anim_dir}")
        poll_delay = 1.0 / self._poll_hz

        while self.bb.read("running")["running"]:
            state = self.bb.read("play_animation")
            name = str(state.get("play_animation", "") or "").strip()
            if name:
                self._play(name)
            else:
                time.sleep(poll_delay)

        print("[AnimationEngine] Stopped.")
