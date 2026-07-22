#!/usr/bin/env python3
"""
Interactive robot test — WASD head, hold M/N for continuous base spin (voice-agentv4 style).

  cd voice-agentv5 && python tests/robottest.py --port /dev/ttyUSB0

  W/S  tilt up / down
  A/D  pan left / right
  M    hold = spin base left, release = stop
  N    hold = spin base right, release = stop
  C    center head (pan/tilt from config.yaml)
  Z    zero base encoder (current pose = 0°)
  ?    print status
  Q    quit

Requires ESP32 firmware with L/R/X spin commands. Reflash:
  arduino-cli upload -p /dev/ttyUSB0 --fqbn esp32:esp32:esp32 firmware/head_servo

Stop start_robot.py and other serial users first.
"""

from __future__ import annotations

import argparse
import re
import select
import sys
import termios
import tty
import time
from pathlib import Path

import _bootstrap  # noqa: F401

from arduino_servo import ArduinoServoLink
from base_motor_utils import apply_base_calibration_to_nano, load_move_timeout

CONFIG_PATH = Path(__file__).resolve().parents[1] / "config.yaml"

HOLD_RELEASE_SEC = 0.12
POLL_SEC = 0.03

ARROW_LEFT = "\x1b[D"
ARROW_RIGHT = "\x1b[C"
ARROW_UP = "\x1b[A"
ARROW_DOWN = "\x1b[B"


def _load_servo_section() -> dict:
    text = CONFIG_PATH.read_text(encoding="utf-8")
    section: dict = {}
    in_servo = False
    for raw in text.splitlines():
        if not raw.strip() or raw.lstrip().startswith("#"):
            continue
        if not raw.startswith(" ") and raw.rstrip().endswith(":"):
            in_servo = raw.strip() == "servo:"
            continue
        if not in_servo:
            continue
        if not raw.startswith(" "):
            break
        m = re.match(r"^\s+(\w+):\s*([^#]+)", raw)
        if m:
            val = m.group(2).strip()
            try:
                section[m.group(1)] = float(val) if "." in val else int(val)
            except ValueError:
                section[m.group(1)] = val.strip("'\"")
    return section


def load_servo_limits() -> tuple[float, float, float, float, float, float]:
    sv = _load_servo_section()
    pan_min = float(sv.get("pan_min", 25.0))
    pan_max = float(sv.get("pan_max", 150.0))
    tilt_min = float(sv.get("tilt_min", 100.0))
    tilt_max = float(sv.get("tilt_max", 150.0))
    pan_center = float(sv.get("pan_center", (pan_min + pan_max) * 0.5))
    tilt_center = float(sv.get("tilt_center", (tilt_min + tilt_max) * 0.5))
    return pan_min, pan_max, tilt_min, tilt_max, pan_center, tilt_center


def clamp(v: float, lo: float, hi: float) -> float:
    return max(lo, min(hi, v))


def print_help(head_step: float) -> None:
    print(
        "\nrobottest — keyboard control (v4-style base spin)\n"
        f"  W/S     tilt ±{head_step:.0f}°   A/D     pan ±{head_step:.0f}°\n"
        "  M       hold = spin base left     N       hold = spin base right\n"
        "  C       center head    Z  zero base    ?  status    Q  quit\n"
    )


def format_status(link: ArduinoServoLink, pan: float, tilt: float, spin: int) -> str:
    st = link.query_status()
    spin_s = {0: "stop", -1: "left", 1: "right"}.get(spin, "?")
    if st is None:
        return f"head P{pan:.0f} T{tilt:.0f}  base spin {spin_s}"
    return (
        f"head P{pan:.0f} T{tilt:.0f}  |  "
        f"base {st.degrees:.1f}° POS {st.encoder_count}  spin {spin_s}  BUSY {int(st.busy)}"
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


def desired_base_spin(last_m: float, last_n: float, now: float) -> int:
    m_held = (now - last_m) < HOLD_RELEASE_SEC
    n_held = (now - last_n) < HOLD_RELEASE_SEC
    if n_held and (not m_held or last_n >= last_m):
        return 1
    if m_held:
        return -1
    return 0


def apply_base_spin(link: ArduinoServoLink, spin: int, prev: int) -> int:
    if spin == prev:
        return prev
    if spin == 0:
        link.write_base_stop()
    elif spin == -1:
        link.write_base_spin_left()
    elif spin == 1:
        link.write_base_spin_right()
    return spin


def handle_discrete_key(
    key: str,
    link: ArduinoServoLink,
    pan: float,
    tilt: float,
    pan_min: float,
    pan_max: float,
    tilt_min: float,
    tilt_max: float,
    pan_center: float,
    tilt_center: float,
    head_step: float,
    spin: int,
) -> tuple[float, float, int]:
    key_l = key.lower() if len(key) == 1 else key

    if key_l in ("q", "\x03"):
        link.write_base_stop()
        print("\nQuit.")
        return pan, tilt, -2

    if key_l == "?":
        print(format_status(link, pan, tilt, spin))
        return pan, tilt, spin

    if key_l == "c":
        pan = pan_center
        tilt = tilt_center
        link.write_angles(pan, tilt, force=True)
        print(f"Head centered  {format_status(link, pan, tilt, spin)}")
        return pan, tilt, spin

    if key_l == "z":
        link.write_base_stop()
        spin = 0
        link.zero_base()
        print(f"Base zeroed    {format_status(link, pan, tilt, spin)}")
        return pan, tilt, spin

    if key_l == "w":
        tilt = clamp(tilt + head_step, tilt_min, tilt_max)
        link.write_angles(pan, tilt)
        return pan, tilt, spin

    if key_l == "s":
        tilt = clamp(tilt - head_step, tilt_min, tilt_max)
        link.write_angles(pan, tilt)
        return pan, tilt, spin

    if key_l == "a":
        pan = clamp(pan - head_step, pan_min, pan_max)
        link.write_angles(pan, tilt)
        return pan, tilt, spin

    if key_l == "d":
        pan = clamp(pan + head_step, pan_min, pan_max)
        link.write_angles(pan, tilt)
        return pan, tilt, spin

    return pan, tilt, spin


def verify_spin_firmware(link: ArduinoServoLink) -> bool:
    """Confirm L/R spin commands exist (v4-style firmware)."""
    if link._ser is None:
        return False
    link._drain_rx()
    link._ser.write(b"L\n")
    link._ser.flush()
    deadline = time.time() + 1.0
    while time.time() < deadline:
        line = link._ser.readline().decode("utf-8", errors="ignore").strip()
        if not line:
            continue
        if line == "OK L":
            link.write_base_stop()
            time.sleep(0.1)
            return True
        if line.startswith("ERR"):
            print(
                f"Firmware rejected spin command ({line}).\n"
                "  Reflash: arduino-cli upload -p /dev/ttyUSB0 "
                "--fqbn esp32:esp32:esp32 firmware/head_servo"
            )
            return False
    print("No OK L from firmware — reflash head_servo.ino with L/R spin support.")
    return False


def run_interactive(link: ArduinoServoLink, *, head_step: float) -> None:
    pan_min, pan_max, tilt_min, tilt_max, pan_center, tilt_center = load_servo_limits()
    pan = pan_center
    tilt = tilt_center
    link.write_angles(pan, tilt, force=True)
    link.write_base_stop()

    print_help(head_step)
    print(format_status(link, pan, tilt, 0))

    last_m = 0.0
    last_n = 0.0
    active_spin = 0
    status_every = 0.0

    with RawKeyReader() as keys:
        while True:
            now = time.time()
            for key in keys.drain_keys():
                if key in (ARROW_LEFT, ARROW_RIGHT, ARROW_UP, ARROW_DOWN):
                    continue
                key_l = key.lower() if len(key) == 1 else key
                if key_l == "m":
                    last_m = now
                elif key_l == "n":
                    last_n = now
                else:
                    pan, tilt, code = handle_discrete_key(
                        key,
                        link,
                        pan,
                        tilt,
                        pan_min,
                        pan_max,
                        tilt_min,
                        tilt_max,
                        pan_center,
                        tilt_center,
                        head_step,
                        active_spin,
                    )
                    if code == -2:
                        return

            want = desired_base_spin(last_m, last_n, now)
            active_spin = apply_base_spin(link, want, active_spin)

            if active_spin != 0 and now - status_every > 0.5:
                print(f"\r{format_status(link, pan, tilt, active_spin)}   ", end="", flush=True)
                status_every = now
            elif active_spin == 0 and status_every != 0:
                print()
                status_every = 0

            time.sleep(POLL_SEC)


def main() -> int:
    parser = argparse.ArgumentParser(description="WASD head + hold M/N base spin (v4-style)")
    parser.add_argument("--port", default="", help="Serial port (default auto)")
    parser.add_argument("--baud", type=int, default=115200)
    parser.add_argument("--head-step", type=float, default=5.0, help="Degrees per WASD step")
    parser.add_argument(
        "--no-config-cpd",
        action="store_true",
        help="Skip loading base calibration from config.yaml",
    )
    args = parser.parse_args()

    if not sys.stdin.isatty():
        print("robottest needs an interactive terminal (SSH -t or local console).")
        return 1

    link = ArduinoServoLink(port=args.port, baud=args.baud)
    link.base_move_timeout_sec = load_move_timeout()

    if not link.connect():
        print("Failed to connect. Check USB and stop other serial users.")
        return 1

    try:
        if not args.no_config_cpd:
            apply_base_calibration_to_nano(link)

        if not verify_spin_firmware(link):
            return 1

        print("Tip: hold M/N for manual base spin; use Z to zero encoder at current forward.")
        run_interactive(link, head_step=args.head_step)
    except KeyboardInterrupt:
        print("\nInterrupted.")
    finally:
        link.write_base_stop()
        link.close(skip_home=True)

    return 0


if __name__ == "__main__":
    sys.exit(main())
