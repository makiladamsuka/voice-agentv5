#!/usr/bin/env python3
"""Interactive arm jogger — hold keys for continuous motion.

Right arm (WASD):  W/S = A0 raise up/down,  A/D = A2 arm left/right
Left arm (IJKL):   I/K = A1 raise up/down,  J/L = A3 arm left/right

Requires ``firmware/head_servo_hands`` flashed.

  cd voice-agentv5 && python tests/test_servo_arms_manual.py

  +/-   jog step size (hold key to repeat at ~30 Hz)
  H     home all arms
  Ctrl+C  home all arms and quit
  0-3   mark current angle as home for that arm
  Y     5-step limit capture (raise low → max sweep → max raise → min/max sweep)
  C     toggle calibrate mode (no software clamp — find true mechanical stops)
  P     print pose
  Q     quit
"""

from __future__ import annotations

import argparse
import json
import select
import sys
import termios
import tty
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

import _bootstrap  # noqa: F401

from arduino_servo import ArduinoServoLink
from test_servo_manual import load_servo_cfg

ARM_LIMITS = (
    (47.0, 124.0),   # A0 right shoulder raise
    (0.0, 65.0),     # A1 left shoulder raise (low=65, high=0)
    (44.0, 78.0),    # A2 right arm sweep
    (70.0, 102.0),   # A3 left arm sweep
)
ARM_HOMES = (47.0, 65.0, 64.0, 87.0)  # raise low + min sweep (captured)
CAL_LIMITS = (0.0, 180.0)  # wide envelope while calibrating mechanical stops
CAPTURED_LIMITS_PATH = Path(__file__).with_name("captured_arm_limits.json")
STEP_CHOICES = (1.0, 2.0, 5.0, 10.0, 15.0)
HOLD_RELEASE_SEC = 0.25
POLL_SEC = 0.033  # ~30 Hz while key held
JOG_HZ = 30.0

# Guided calibration: each Y captures all four servos at that mechanical pose.
# Limits are derived: raise min/max from steps 1 & 3; sweep min/max from steps 1,2,4,5.
CAPTURE_STEPS: tuple[tuple[str, str], ...] = (
    (
        "raise_low_min_sweep",
        "① RAISE LOW + MIN SWEEP — rest/home pose on both arms, then press Y",
    ),
    (
        "max_sweep_at_low",
        "② MAX SWEEP — sweep fully out (keep raise LOW), then press Y",
    ),
    (
        "max_raise",
        "③ MAX RAISE — raise to highest on both arms, then press Y",
    ),
    (
        "min_sweep_at_high",
        "④ MIN SWEEP — at MAX raise, sweep to minimum, then press Y",
    ),
    (
        "max_sweep_at_high",
        "⑤ MAX SWEEP — at MAX raise, sweep to maximum, then press Y",
    ),
)

# One direction per axis — opposite keys cancel; newest key wins.
_AXIS_PAIRS = (
    ("a0_down", "a0_up", 0),
    ("a2_left", "a2_right", 2),
    ("a1_down", "a1_up", 1),
    ("a3_left", "a3_right", 3),  # L = sweep outward (+), J = inward (-)
)
_OPPOSITE: dict[str, str] = {}
for _neg, _pos, _ in _AXIS_PAIRS:
    _OPPOSITE[_neg] = _pos
    _OPPOSITE[_pos] = _neg

ARROW_UP = "\x1b[A"
ARROW_DOWN = "\x1b[B"
ARROW_LEFT = "\x1b[D"
ARROW_RIGHT = "\x1b[C"


def _clamp_arm(deg: float, idx: int, *, calibrate: bool) -> float:
    lo, hi = CAL_LIMITS if calibrate else ARM_LIMITS[idx]
    return max(lo, min(hi, deg))


def _sync_arms_from_link(link: ArduinoServoLink, arms: list[float]) -> None:
    for i, attr in enumerate(("_last_a0", "_last_a1", "_last_a2", "_last_a3")):
        sent = getattr(link, attr, None)
        if sent is not None:
            arms[i] = sent


def _fmt_arm_tuple(vals: tuple[float, ...] | list[float]) -> str:
    return ", ".join(f"{v:.1f}" for v in vals)


def _print_limit_snippets(
    mins: tuple[float, ...],
    maxs: tuple[float, ...],
    *,
    homes: tuple[float, ...],
) -> None:
    print("\n--- paste into firmware / tests ---", flush=True)
    print(
        f"ARM_HOMES = ({homes[0]:.1f}, {homes[1]:.1f}, {homes[2]:.1f}, {homes[3]:.1f})",
        flush=True,
    )
    print(
        f"ARM_LIMITS = (\n"
        f"    ({mins[0]:.1f}, {maxs[0]:.1f}),   # A0 right shoulder raise\n"
        f"    ({mins[1]:.1f}, {maxs[1]:.1f}),   # A1 left shoulder raise\n"
        f"    ({mins[2]:.1f}, {maxs[2]:.1f}),   # A2 right arm sweep\n"
        f"    ({mins[3]:.1f}, {maxs[3]:.1f}),   # A3 left arm sweep\n"
        f")",
        flush=True,
    )
    print(
        f"const float ARM_MIN_DEG[ARM_CH_COUNT] = "
        f"{{{mins[0]:.1f}f, {mins[1]:.1f}f, {mins[2]:.1f}f, {mins[3]:.1f}f}};",
        flush=True,
    )
    print(
        f"const float ARM_MAX_DEG[ARM_CH_COUNT] = "
        f"{{{maxs[0]:.1f}f, {maxs[1]:.1f}f, {maxs[2]:.1f}f, {maxs[3]:.1f}f}};",
        flush=True,
    )
    print(
        f"const float ARM_HOME_DEG[ARM_CH_COUNT] = "
        f"{{{homes[0]:.1f}f, {homes[1]:.1f}f, {homes[2]:.1f}f, {homes[3]:.1f}f}};",
        flush=True,
    )
    print(
        "_DEG_RANGE arms:\n"
        f'    "arm_0": ({mins[0]:.1f}, {maxs[0]:.1f}),\n'
        f'    "arm_1": ({mins[1]:.1f}, {maxs[1]:.1f}),\n'
        f'    "arm_2": ({mins[2]:.1f}, {maxs[2]:.1f}),\n'
        f'    "arm_3": ({mins[3]:.1f}, {maxs[3]:.1f}),',
        flush=True,
    )
    print("---", flush=True)


def _derive_limits_from_captures(
    poses: dict[str, tuple[float, float, float, float]],
) -> tuple[tuple[float, float, float, float], tuple[float, float, float, float], tuple[float, float, float, float]]:
    """Return (homes, mins, maxs) from the five named calibration poses."""
    low = poses["raise_low_min_sweep"]
    max_sw_low = poses["max_sweep_at_low"]
    high_raise = poses["max_raise"]
    min_sw_high = poses["min_sweep_at_high"]
    max_sw_high = poses["max_sweep_at_high"]

    homes = low
    mins = (
        min(low[0], high_raise[0]),
        min(low[1], high_raise[1]),
        min(low[2], max_sw_low[2], min_sw_high[2], max_sw_high[2]),
        min(low[3], max_sw_low[3], min_sw_high[3], max_sw_high[3]),
    )
    maxs = (
        max(low[0], high_raise[0]),
        max(low[1], high_raise[1]),
        max(low[2], max_sw_low[2], min_sw_high[2], max_sw_high[2]),
        max(low[3], max_sw_low[3], min_sw_high[3], max_sw_high[3]),
    )
    return homes, mins, maxs


def _save_captured_limits(
    mins: tuple[float, ...],
    maxs: tuple[float, ...],
    *,
    homes: tuple[float, float, float, float],
    poses: dict[str, list[float]],
    path: Path = CAPTURED_LIMITS_PATH,
) -> None:
    payload = {
        "captured_at": datetime.now(timezone.utc).isoformat(),
        "poses": poses,
        "homes": [round(v, 1) for v in homes],
        "min": [round(v, 1) for v in mins],
        "max": [round(v, 1) for v in maxs],
        "limits": [
            {"arm": i, "min": round(mins[i], 1), "max": round(maxs[i], 1)}
            for i in range(4)
        ],
    }
    path.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")
    print(f"Saved → {path}", flush=True)


def _print_capture_prompt(step_idx: int) -> None:
    total = len(CAPTURE_STEPS)
    _name, prompt = CAPTURE_STEPS[step_idx]
    print(f"\nCapture {step_idx + 1}/{total}: {prompt}", flush=True)


def _print_captured_pose(step_idx: int, pose: tuple[float, ...]) -> None:
    name, _ = CAPTURE_STEPS[step_idx]
    print(
        f"  ✓ {name}: A0={pose[0]:.1f} A1={pose[1]:.1f} "
        f"A2={pose[2]:.1f} A3={pose[3]:.1f}",
        flush=True,
    )


class RawKeyReader:
    def __init__(self) -> None:
        self._fd = sys.stdin.fileno()
        self._old: list | None = None

    def __enter__(self) -> RawKeyReader:
        if sys.stdin.isatty():
            self._old = termios.tcgetattr(self._fd)
            tty.setcbreak(self._fd)
        return self

    def __exit__(self, *args: object) -> None:
        if self._old is not None:
            termios.tcsetattr(self._fd, termios.TCSADRAIN, self._old)

    def read_key(self, timeout: float = POLL_SEC) -> str | None:
        if not sys.stdin.isatty():
            return None
        ready, _, _ = select.select([sys.stdin], [], [], timeout)
        if not ready:
            return None
        ch = sys.stdin.read(1)
        if ch != "\x1b":
            return ch
        if not select.select([sys.stdin], [], [], 0.02)[0]:
            return ch
        seq = sys.stdin.read(2)
        if len(seq) == 2 and seq[0] == "[":
            code = {"A": ARROW_UP, "B": ARROW_DOWN, "C": ARROW_RIGHT, "D": ARROW_LEFT}.get(seq[1])
            if code:
                return code
        return ch

    def drain_keys(self) -> list[str]:
        keys: list[str] = []
        while True:
            k = self.read_key(0)
            if k is None:
                break
            keys.append(k)
        return keys


def _map_key(key: str) -> Optional[str]:
    # Right arm: A0 big raise (W/S), A2 small sweep (A/D)
    if key in ("w", "W", ARROW_UP):
        return "a0_up"
    if key in ("s", "S", ARROW_DOWN):
        return "a0_down"
    if key in ("a", "A", ARROW_LEFT):
        return "a2_left"
    if key in ("d", "D", ARROW_RIGHT):
        return "a2_right"
    # Left arm: A1 big raise (I/K), A3 small sweep (J/L)
    if key in ("i", "I"):
        return "a1_up"
    if key in ("k", "K"):
        return "a1_down"
    if key in ("j", "J"):
        return "a3_left"
    if key in ("l", "L"):
        return "a3_right"
    if key in ("+", "="):
        return "step_up"
    if key in ("-", "_"):
        return "step_down"
    if key in ("h", "H"):
        return "home"
    if key in ("0", "1", "2", "3"):
        return f"mark_home_{key}"
    if key in ("y", "Y"):
        return "capture_limits"
    if key in ("c", "C"):
        return "toggle_calibrate"
    if key in ("p", "P"):
        return "print_pose"
    if key in ("q", "Q", "\x03"):
        return "quit"
    return None


def _print_pose(arms: list[float], *, homes: tuple[float, float, float, float]) -> None:
    print(
        f"R raise A0={arms[0]:.1f}  sweep A2={arms[2]:.1f}  |  "
        f"L raise A1={arms[1]:.1f}  sweep A3={arms[3]:.1f}  |  "
        f"homes {homes[0]:.0f}/{homes[1]:.0f}/{homes[2]:.0f}/{homes[3]:.0f}",
        flush=True,
    )


def _axis_step(
    held: dict[str, float],
    now: float,
    neg_key: str,
    pos_key: str,
    step: float,
) -> float:
    neg_ts = held.get(neg_key, 0.0)
    pos_ts = held.get(pos_key, 0.0)
    neg_active = (now - neg_ts) < HOLD_RELEASE_SEC
    pos_active = (now - pos_ts) < HOLD_RELEASE_SEC
    if neg_active and pos_active:
        return step if pos_ts >= neg_ts else -step
    if pos_active:
        return step
    if neg_active:
        return -step
    return 0.0


def _apply_held(
    arms: list[float],
    held: dict[str, float],
    now: float,
    step: float,
    *,
    calibrate: bool,
) -> bool:
    moved = False
    for neg_key, pos_key, idx in _AXIS_PAIRS:
        delta = _axis_step(held, now, neg_key, pos_key, step)
        if delta == 0.0:
            continue
        arms[idx] = _clamp_arm(arms[idx] + delta, idx, calibrate=calibrate)
        moved = True
    return moved


def main() -> int:
    servo = load_servo_cfg()
    parser = argparse.ArgumentParser(description="WASD/IJKL manual arm jogger")
    parser.add_argument("--port", default=str(servo.get("port") or ""))
    parser.add_argument("--baud", type=int, default=int(servo.get("baud", 115200)))
    parser.add_argument("--step", type=float, default=1.0, help="Degrees per jog tick while held")
    parser.add_argument("--no-home", action="store_true", help="Do not move arms home on exit")
    args = parser.parse_args()

    link = ArduinoServoLink(port=args.port, baud=args.baud)
    link.configure_servo_stream(send_hz=JOG_HZ, min_deg=0.02, quantum_deg=0.1)

    if not link.connect():
        print("Could not connect to ESP32.")
        return 1

    if not link.has_arm_firmware():
        banner = link.firmware_banner()
        if banner:
            for line in banner.splitlines():
                if line.startswith("FW "):
                    print(f"ESP32 reports: {line.strip()}", flush=True)
        hint = link.arm_firmware_hint()
        print(
            "ERROR: ESP32 does not report arm firmware (V -> HOME A0=...).",
            flush=True,
        )
        if hint:
            print(hint, flush=True)
        print(
            "Flash: arduino-cli upload -p /dev/ttyUSB1 --fqbn esp32:esp32:esp32 "
            "firmware/head_servo_hands",
            flush=True,
        )
        link.close(skip_home=True)
        return 1

    arms = list(ARM_HOMES)
    homes = ARM_HOMES
    step_idx = STEP_CHOICES.index(args.step) if args.step in STEP_CHOICES else 0
    held: dict[str, float] = {}
    last_jog_ts = 0.0
    last_print_ts = 0.0
    quit_requested = False
    calibrate = False
    capture_step: int | None = None  # None = idle; 0..4 = awaiting pose for that step
    capture_poses: dict[str, tuple[float, float, float, float]] = {}

    print(
        "Right arm WASD: W/S=A0 big raise, A/D=A2 small sweep\n"
        "Left arm  IJKL: I/K=A1 big raise, J/L=A3 small sweep\n"
        "Hold keys for continuous motion. +/-=step  H=home  C=calibrate\n"
        "Y=5-step limit capture  Ctrl+C=home+quit  Q=quit"
    )
    link.write_arms(*arms, force=True)
    _print_pose(arms, homes=homes)

    jog_interval = 1.0 / JOG_HZ

    try:
        with RawKeyReader() as keys:
            while not quit_requested:
                now = time.time()
                for key in keys.drain_keys():
                    action = _map_key(key)
                    if action is None:
                        continue
                    if action == "quit":
                        quit_requested = True
                        break
                    if action == "step_up":
                        step_idx = min(step_idx + 1, len(STEP_CHOICES) - 1)
                        print(f"step={STEP_CHOICES[step_idx]:.1f}", flush=True)
                        continue
                    if action == "step_down":
                        step_idx = max(step_idx - 1, 0)
                        print(f"step={STEP_CHOICES[step_idx]:.1f}", flush=True)
                        continue
                    if action == "print_pose":
                        _print_pose(arms, homes=homes)
                        continue
                    if action == "home":
                        arms = list(homes)
                        link.write_arms(*arms, force=True)
                        _print_pose(arms, homes=homes)
                        continue
                    if action.startswith("mark_home_"):
                        idx = int(action[-1])
                        homes_list = list(homes)
                        homes_list[idx] = arms[idx]
                        homes = tuple(homes_list)
                        print(f"marked arm_{idx} home={arms[idx]:.1f}", flush=True)
                        continue
                    if action == "toggle_calibrate":
                        calibrate = not calibrate
                        state = "ON (0–180°, no software clamp)" if calibrate else "OFF"
                        print(f"calibrate {state}", flush=True)
                        continue
                    if action == "capture_limits":
                        _sync_arms_from_link(link, arms)
                        pose = tuple(arms)
                        if capture_step is None:
                            capture_step = 0
                            capture_poses.clear()
                            calibrate = True
                            print("calibrate ON (0–180°, no software clamp)", flush=True)
                            _print_capture_prompt(0)
                            continue
                        name, _ = CAPTURE_STEPS[capture_step]
                        capture_poses[name] = pose
                        _print_captured_pose(capture_step, pose)
                        capture_step += 1
                        if capture_step < len(CAPTURE_STEPS):
                            _print_capture_prompt(capture_step)
                        else:
                            derived_homes, mins, maxs = _derive_limits_from_captures(
                                capture_poses
                            )
                            homes = derived_homes
                            print(
                                f"\nDONE — homes {_fmt_arm_tuple(derived_homes)}",
                                flush=True,
                            )
                            print(
                                f"       min  {_fmt_arm_tuple(mins)}  |  "
                                f"max  {_fmt_arm_tuple(maxs)}",
                                flush=True,
                            )
                            _save_captured_limits(
                                mins,
                                maxs,
                                homes=derived_homes,
                                poses={
                                    k: [round(v, 1) for v in vals]
                                    for k, vals in capture_poses.items()
                                },
                            )
                            _print_limit_snippets(mins, maxs, homes=derived_homes)
                            capture_step = None
                            capture_poses.clear()
                        continue
                    if action in _OPPOSITE:
                        held.pop(_OPPOSITE[action], None)
                    held[action] = now

                step = STEP_CHOICES[step_idx]
                if now - last_jog_ts >= jog_interval:
                    before = list(arms)
                    if _apply_held(arms, held, now, step, calibrate=calibrate):
                        link.write_arms(*arms, force=True)
                        _sync_arms_from_link(link, arms)
                        last_jog_ts = now
                        if now - last_print_ts > 0.25:
                            _print_pose(arms, homes=homes)
                            last_print_ts = now
                    elif arms != before:
                        link.write_arms(*arms, force=True)

                time.sleep(POLL_SEC)
    except KeyboardInterrupt:
        print("\nCtrl+C — homing arms...", flush=True)
        arms = list(homes)
        if not args.no_home:
            link.write_arms(*arms, force=True)
            time.sleep(0.15)
    finally:
        link.close(
            home_arm0=homes[0],
            home_arm1=homes[1],
            home_arm2=homes[2],
            home_arm3=homes[3],
            skip_home=args.no_home,
        )
    print("Done.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
