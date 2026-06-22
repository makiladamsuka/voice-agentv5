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
  P     print pose
  Q     quit
"""

from __future__ import annotations

import argparse
import select
import sys
import termios
import tty
import time
from typing import Optional

import _bootstrap  # noqa: F401

from arduino_servo import ArduinoServoLink
from test_servo_manual import load_servo_cfg

ARM_MIN = 0.0
ARM_MAX = 180.0
ARM_HOMES = (0.0, 180.0, 90.0, 90.0)  # A0..A3
STEP_CHOICES = (1.0, 2.0, 5.0, 10.0, 15.0)
HOLD_RELEASE_SEC = 0.15
POLL_SEC = 0.033  # ~30 Hz while key held
JOG_HZ = 30.0

ARROW_UP = "\x1b[A"
ARROW_DOWN = "\x1b[B"
ARROW_LEFT = "\x1b[D"
ARROW_RIGHT = "\x1b[C"


def _clamp_arm(deg: float) -> float:
    return max(ARM_MIN, min(ARM_MAX, deg))


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


def _apply_held(
    arms: list[float],
    held: dict[str, float],
    now: float,
    step: float,
) -> bool:
    moved = False
    if now - held.get("a0_up", 0.0) < HOLD_RELEASE_SEC:
        arms[0] = _clamp_arm(arms[0] + step)
        moved = True
    if now - held.get("a0_down", 0.0) < HOLD_RELEASE_SEC:
        arms[0] = _clamp_arm(arms[0] - step)
        moved = True
    if now - held.get("a2_left", 0.0) < HOLD_RELEASE_SEC:
        arms[2] = _clamp_arm(arms[2] - step)
        moved = True
    if now - held.get("a2_right", 0.0) < HOLD_RELEASE_SEC:
        arms[2] = _clamp_arm(arms[2] + step)
        moved = True
    if now - held.get("a1_up", 0.0) < HOLD_RELEASE_SEC:
        arms[1] = _clamp_arm(arms[1] + step)
        moved = True
    if now - held.get("a1_down", 0.0) < HOLD_RELEASE_SEC:
        arms[1] = _clamp_arm(arms[1] - step)
        moved = True
    if now - held.get("a3_left", 0.0) < HOLD_RELEASE_SEC:
        arms[3] = _clamp_arm(arms[3] - step)
        moved = True
    if now - held.get("a3_right", 0.0) < HOLD_RELEASE_SEC:
        arms[3] = _clamp_arm(arms[3] + step)
        moved = True
    return moved


def main() -> int:
    servo = load_servo_cfg()
    parser = argparse.ArgumentParser(description="WASD/IJKL manual arm jogger")
    parser.add_argument("--port", default=str(servo.get("port") or ""))
    parser.add_argument("--baud", type=int, default=int(servo.get("baud", 115200)))
    parser.add_argument("--step", type=float, default=5.0, help="Degrees per jog tick while held")
    parser.add_argument("--no-home", action="store_true", help="Do not move arms home on exit")
    args = parser.parse_args()

    link = ArduinoServoLink(port=args.port, baud=args.baud)
    link.configure_servo_stream(send_hz=JOG_HZ, min_deg=0.02, quantum_deg=0.1)

    if not link.connect():
        print("Could not connect to ESP32.")
        return 1

    if not link.has_arm_firmware():
        print(
            "ERROR: ESP32 does not report arm firmware (V -> HOME A0=...).\n"
            "Flash firmware/head_servo_hands/head_servo_hands.ino.",
            flush=True,
        )
        link.close(skip_home=True)
        return 1

    arms = list(ARM_HOMES)
    homes = ARM_HOMES
    step_idx = STEP_CHOICES.index(args.step) if args.step in STEP_CHOICES else 2
    held: dict[str, float] = {}
    last_jog_ts = 0.0
    last_print_ts = 0.0
    quit_requested = False

    print(
        "Right arm WASD: W/S=A0 big raise, A/D=A2 small sweep\n"
        "Left arm  IJKL: I/K=A1 big raise, J/L=A3 small sweep\n"
        "Hold keys for continuous motion. +/-=step  H=home  Ctrl+C=home+quit  Q=quit"
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
                    held[action] = now

                step = STEP_CHOICES[step_idx]
                if now - last_jog_ts >= jog_interval:
                    before = list(arms)
                    if _apply_held(arms, held, now, step):
                        link.write_arms(*arms, force=True)
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
