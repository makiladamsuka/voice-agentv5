#!/usr/bin/env python3
"""Standalone Bottango animation playback.

Reads AnimationCommands.json directly and maps raw sC bezier curves to servo
degrees using the correct limits below.  Zero dependency on botango_loader or
voice-agentv4.  The only non-stdlib import is arduino_servo.

SERVO LIMITS & HOME
  head_tilt : home = 110, min = 100, max = 120
  head_pan  : home =  85, min =  40, max = 120
  arms      : home positions defined by ARM_HOMES, range 0-180
"""

from __future__ import annotations

import argparse
import json
import os
import time
from pathlib import Path

from arduino_servo1 import ArduinoServoLink

# ─── Servo targets ────────────────────────────────────────────────────────────
PAN_HOME  = 85.0
TILT_HOME = 110.0
PAN_MIN,  PAN_MAX  = 40.0,  120.0
TILT_MIN, TILT_MAX = 100.0, 120.0
ARM_HOMES = {"arm_0": 0.0, "arm_1": 180.0, "arm_2": 90.0, "arm_3": 90.0}

# Degree range used when converting raw Bottango 0-1 movement → degrees.
_DEG_RANGE = {
    "head_pan":  (40.0, 130.0),   
    "head_tilt": (100.0, 120.0),  
    "arm_0":     (0.0,  180.0),
    "arm_1":     (0.0,  180.0),
    "arm_2":     (0.0,  180.0),
    "arm_3":     (0.0,  180.0),
}

# Bottango effector_id → servo name
EFFECTOR_MAP = {
    "644": "head_pan",
    "645": "head_tilt",
    "640": "arm_0",
    "642": "arm_1",
    "648": "arm_2",
    "649": "arm_3",
}

BOTANGO_SCALE = 8192.0  # raw position units


# ─── AnimationCommands.json search paths ──────────────────────────────────────
def _find_anim_commands(explicit: str) -> Path:
    if explicit:
        p = Path(explicit).expanduser().resolve()
        if p.is_dir():
            p = p / "AnimationCommands.json"
        if p.is_file():
            return p
        raise FileNotFoundError(f"Not found or is not a file: {p}")
        
    # Safely extract environment variable without defaulting to "."
    env_val = os.environ.get("BOTANGO_JSON", "").strip()
    env_path = Path(env_val) if env_val else None

    candidates = [
        Path(__file__).parent / "animation" / "AnimationCommands.json",
        env_path,
        Path("/home/nema/Documents/voice-agentv4/backend/animations/AnimationCommands.json"),
        Path("/home/nema/Documents/voice-agentv5/tests/animation/AnimationCommands.json"),
        Path(__file__).parent / "AnimationCommands.json"
    ]
    
    for p in candidates:
        if p and p.is_file():
            return p
            
    raise FileNotFoundError(
        "AnimationCommands.json not found. Pass --json <path> or set BOTANGO_JSON."
    )


# ─── Bezier curve from sC line ─────────────────────────────────────────────────
class _Curve:
    """Single Bottango cubic-bezier segment for one effector."""
    __slots__ = ("servo", "start_ms", "end_ms", "p0", "p1", "p2", "p3")

    def __init__(self, servo: str, start_ms: float, duration_ms: float,
                 start_y: float, start_cy: float,
                 end_y: float, end_cx: float) -> None:
        self.servo    = servo
        self.start_ms = start_ms
        self.end_ms   = start_ms + duration_ms
        self.p0 = start_y
        self.p3 = end_y
        self.p1 = start_y + start_cy
        self.p2 = end_y   + end_cx

    def evaluate(self, t_ms: float) -> float:
        if t_ms <= self.start_ms:
            return self.p0
        if t_ms >= self.end_ms:
            return self.p3
        t = (t_ms - self.start_ms) / (self.end_ms - self.start_ms)
        mt = 1.0 - t
        return mt**3*self.p0 + 3*mt**2*t*self.p1 + 3*mt*t**2*self.p2 + t**3*self.p3

    def to_deg(self, movement: float) -> float:
        lo, hi = _DEG_RANGE.get(self.servo, (0.0, 180.0))
        return lo + movement * (hi - lo)


# ─── AnimationClip ─────────────────────────────────────────────────────────────
class _Clip:
    def __init__(self, name: str, duration_ms: float,
                 curves: list[_Curve]) -> None:
        self.clip_id     = name
        self.duration_ms = duration_ms
        self._curves     = curves

    def sample(self, t_ms: float) -> dict[str, float] | None:
        if t_ms > self.duration_ms:
            return None
        active: dict[str, _Curve] = {}
        for c in self._curves:
            if c.start_ms <= t_ms <= c.end_ms:
                prev = active.get(c.servo)
                if prev is None or c.start_ms >= prev.start_ms:
                    active[c.servo] = c
        if not active:
            return None
        return {servo: c.to_deg(c.evaluate(t_ms)) for servo, c in active.items()}


# ─── Parser ────────────────────────────────────────────────────────────────────
def _parse_sc_line(line: str) -> _Curve | None:
    line = line.strip()
    if not line.startswith("sC,"):
        return None
    parts = line.split(",")
    if len(parts) < 10:
        return None
    servo = EFFECTOR_MAP.get(parts[1])
    if servo is None:
        return None
    try:
        start_ms    = float(parts[2])
        duration_ms = float(parts[3])
        start_y     = float(parts[4]) / BOTANGO_SCALE
        start_cy    = float(parts[6]) / BOTANGO_SCALE
        end_y       = float(parts[7]) / BOTANGO_SCALE
        end_cx      = float(parts[8]) / BOTANGO_SCALE
    except (ValueError, IndexError):
        return None
    return _Curve(servo, start_ms, duration_ms, start_y, start_cy, end_y, end_cx)


def load_anim_commands(path: Path) -> list[_Clip]:
    if not path.is_file():
        raise FileNotFoundError(f"Expected a valid file path, but received: {path}")
        
    data = json.loads(path.read_text(encoding="utf-8"))
    if isinstance(data, list):
        controllers = data
    else:
        controllers = [data]

    clips: list[_Clip] = []
    for ctrl in controllers:
        for anim in ctrl.get("Animations", []):
            name     = anim.get("Animation Name", "unknown")
            cmd_text = anim.get("Animation Commands", "")

            curves: list[_Curve] = []
            for line in cmd_text.splitlines():
                c = _parse_sc_line(line)
                if c is not None:
                    curves.append(c)

            if not curves:
                continue

            duration_ms = max(c.end_ms for c in curves)
            clips.append(_Clip(name, duration_ms, curves))
    return clips


# ─── Helpers ───────────────────────────────────────────────────────────────────
def _clamp(v: float, lo: float, hi: float) -> float:
    return max(lo, min(hi, v))


def _print_clips(clips: list[_Clip]) -> None:
    print("Available clips:")
    for i, c in enumerate(clips, 1):
        print(f"  {i:>2}. {c.clip_id} ({c.duration_ms:.0f} ms)")


def _resolve(raw: str, clips: list[_Clip]) -> _Clip | None:
    raw = raw.strip()
    if raw.isdigit():
        idx = int(raw)
        if 1 <= idx <= len(clips):
            return clips[idx - 1]
    for c in clips:
        if c.clip_id == raw:
            return c
    return None


# ─── Playback ──────────────────────────────────────────────────────────────────
def _play_clip(clip: _Clip, seconds: float, hz: float,
               *, link: ArduinoServoLink | None = None) -> None:
    print(f"\nPlaying '{clip.clip_id}' for up to {seconds:.1f}s...")
    interval = 1.0 / max(1.0, hz)
    deadline = time.time() + max(0.1, seconds)
    t0       = time.time()

    while time.time() < deadline:
        t_ms   = (time.time() - t0) * 1000.0
        sample = clip.sample(t_ms)

        if sample is None:
            print("Clip ended.")
            break

        pan_target  = _clamp(sample.get("head_pan",  PAN_HOME),  PAN_MIN,  PAN_MAX)
        tilt_target = _clamp(sample.get("head_tilt", TILT_HOME), TILT_MIN, TILT_MAX)
        a0 = _clamp(sample.get("arm_0", ARM_HOMES["arm_0"]), 0.0, 180.0)
        a1 = _clamp(sample.get("arm_1", ARM_HOMES["arm_1"]), 0.0, 180.0)
        a2 = _clamp(sample.get("arm_2", ARM_HOMES["arm_2"]), 0.0, 180.0)
        a3 = _clamp(sample.get("arm_3", ARM_HOMES["arm_3"]), 0.0, 180.0)

        print(
            f"arm_0={a0:.1f}, arm_1={a1:.1f}, arm_2={a2:.1f}, arm_3={a3:.1f}, "
            f"head_pan={pan_target:.1f}, head_tilt={tilt_target:.1f}"
        )

        if link is not None:
            link.write_angles_and_arms(pan_target, tilt_target, a0, a1, a2, a3)

        time.sleep(interval)

    if link is not None:
        print(
            f"Returning to HOME: PAN={PAN_HOME:.1f}, TILT={TILT_HOME:.1f}, "
            f"A0={ARM_HOMES['arm_0']:.1f}, A1={ARM_HOMES['arm_1']:.1f}, "
            f"A2={ARM_HOMES['arm_2']:.1f}, A3={ARM_HOMES['arm_3']:.1f}"
        )
        link.write_angles_and_arms(
            PAN_HOME, TILT_HOME,
            ARM_HOMES["arm_0"], ARM_HOMES["arm_1"],
            ARM_HOMES["arm_2"], ARM_HOMES["arm_3"],
            force=True, wait_ack=True,
        )


# ─── Main ──────────────────────────────────────────────────────────────────────
def main() -> int:
    parser = argparse.ArgumentParser(
        description="Standalone Bottango animation player — only needs arduino_servo.py."
    )
    parser.add_argument("--json",    default="", help="Path to AnimationCommands.json.")
    parser.add_argument("--clip",    default="", help="Clip name or number to play.")
    parser.add_argument("--seconds", type=float, default=5.0, help="Max play time (s).")
    parser.add_argument("--hz",      type=float, default=10.0, help="Sample rate (Hz).")
    parser.add_argument("--list",    action="store_true", help="List clips and exit.")
    parser.add_argument("--all",     action="store_true", help="Play all clips.")
    parser.add_argument("--port",    default="", help="ESP32 serial port (auto if empty).")
    parser.add_argument("--baud",    type=int, default=115200)
    parser.add_argument("--dry-run", action="store_true", help="Print only, no servo output.")
    args = parser.parse_args()

    try:
        json_path = _find_anim_commands(args.json)
        clips     = load_anim_commands(json_path)
    except FileNotFoundError as e:
        print(f"Error: {e}")
        return 1

    if not clips:
        print(f"No clips found in {json_path}")
        return 1

    print(f"Loaded {len(clips)} clips from {json_path}")
    _print_clips(clips)

    if args.list:
        return 0

    link: ArduinoServoLink | None = None
    if not args.dry_run:
        link = ArduinoServoLink(port=args.port, baud=args.baud)
        if link.connect():
            link.write_angles_and_arms(
                PAN_HOME, TILT_HOME,
                ARM_HOMES["arm_0"], ARM_HOMES["arm_1"],
                ARM_HOMES["arm_2"], ARM_HOMES["arm_3"],
                force=True,
            )
            print(f"Robot ready. HOME: PAN={PAN_HOME:.1f}, TILT={TILT_HOME:.1f}")
        else:
            print("Robot connection failed; continuing in print-only mode.")
            link = None

    try:
        if args.all:
            for clip in clips:
                _play_clip(clip, args.seconds, args.hz, link=link)
            return 0

        chosen = _resolve(args.clip, clips) if args.clip else None
        if chosen is not None:
            _play_clip(chosen, args.seconds, args.hz, link=link)
            return 0

        while True:
            raw = input("\nSelect clip by number or name (q to quit): ").strip()
            if raw.lower() in {"q", "quit", "exit"}:
                break
            chosen = _resolve(raw, clips)
            if chosen is None:
                print("Invalid choice. Try again.")
                continue
            _play_clip(chosen, args.seconds, args.hz, link=link)

    except KeyboardInterrupt:
        print("\nInterrupted.")
    finally:
        if link is not None:
            print(
                f"\nPerfect stop → HOME "
                f"(PAN={PAN_HOME:.1f}, TILT={TILT_HOME:.1f}, "
                f"A0={ARM_HOMES['arm_0']:.1f}, A1={ARM_HOMES['arm_1']:.1f}, "
                f"A2={ARM_HOMES['arm_2']:.1f}, A3={ARM_HOMES['arm_3']:.1f})"
            )
            link.close(
                home_pan=PAN_HOME, home_tilt=TILT_HOME,
                home_arm0=ARM_HOMES["arm_0"], home_arm1=ARM_HOMES["arm_1"],
                home_arm2=ARM_HOMES["arm_2"], home_arm3=ARM_HOMES["arm_3"],
            )

    return 0


if __name__ == "__main__":
    raise SystemExit(main())