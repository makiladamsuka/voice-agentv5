#!/usr/bin/env python3
"""Interactive pan/tilt jogger (WASD) to find servo min/max/center limits."""

from __future__ import annotations

import argparse
import re
import sys
import termios
import tty
from pathlib import Path
from typing import Optional

import _bootstrap  # noqa: F401

from arduino_servo import ArduinoServoLink

CONFIG_PATH = Path(__file__).resolve().parents[1] / "config.yaml"

try:
    import yaml
except ImportError:
    yaml = None

STEP_CHOICES = (0.25, 0.5, 1.0, 2.0, 5.0)


def _parse_scalar(value: str):
    value = value.split("#", 1)[0].strip()
    if not value:
        return ""
    lowered = value.lower()
    if lowered in ("true", "false"):
        return lowered == "true"
    try:
        if any(ch in value for ch in (".", "e", "E")):
            return float(value)
        return int(value)
    except ValueError:
        return value.strip("'\"")


def _load_simple_config() -> dict:
    data: dict = {}
    current = None
    for raw_line in CONFIG_PATH.read_text(encoding="utf-8").splitlines():
        if not raw_line.strip() or raw_line.lstrip().startswith("#"):
            continue
        if not raw_line.startswith(" ") and raw_line.rstrip().endswith(":"):
            current = raw_line.strip()[:-1]
            data[current] = {}
            continue
        if current is None or ":" not in raw_line:
            continue
        key, value = raw_line.split(":", 1)
        data[current][key.strip()] = _parse_scalar(value)
    return data


def load_servo_cfg() -> dict:
    if not CONFIG_PATH.exists():
        return {}
    try:
        if yaml is None:
            return _load_simple_config().get("servo") or {}
        with open(CONFIG_PATH, encoding="utf-8") as f:
            return (yaml.safe_load(f) or {}).get("servo") or {}
    except Exception:
        return {}


def _write_servo_limits_to_config(
    *,
    pan_min: Optional[float],
    pan_max: Optional[float],
    tilt_min: Optional[float],
    tilt_max: Optional[float],
    pan_center: Optional[float],
    tilt_center: Optional[float],
) -> None:
    text = CONFIG_PATH.read_text(encoding="utf-8")
    updates = {
        "pan_min": pan_min,
        "pan_max": pan_max,
        "tilt_min": tilt_min,
        "tilt_max": tilt_max,
        "pan_center": pan_center,
        "tilt_center": tilt_center,
    }
    for key, value in updates.items():
        if value is None:
            continue
        pattern = rf"^(\s*{re.escape(key)}:\s*)([^\n#]+)(.*)$"
        repl = rf"\g<1>{value:.1f}\g<3>"
        new_text, n = re.subn(pattern, repl, text, count=1, flags=re.MULTILINE)
        if n:
            text = new_text
        else:
            raise RuntimeError(f"could not find servo.{key} in {CONFIG_PATH}")
    CONFIG_PATH.write_text(text, encoding="utf-8")


def _print_pose(pan: float, tilt: float) -> None:
    print(f"pan={pan:.1f}  tilt={tilt:.1f}", flush=True)


def _print_yaml_snippet(marks: dict[str, Optional[float]]) -> None:
    print("\n# paste into config.yaml servo:")
    for key in ("pan_min", "pan_max", "tilt_min", "tilt_max", "pan_center", "tilt_center"):
        value = marks.get(key)
        if value is not None:
            print(f"  {key}: {value:.1f}")


def _read_key() -> str:
    ch = sys.stdin.read(1)
    if ch != "\x1b":
        return ch
    rest = sys.stdin.read(2)
    return ch + rest


def _map_key(key: str) -> Optional[str]:
    if key in ("w", "W", "\x1b[A"):
        return "tilt_up"
    if key in ("s", "S", "\x1b[B"):
        return "tilt_down"
    if key in ("a", "A", "\x1b[D"):
        return "pan_left"
    if key in ("d", "D", "\x1b[C"):
        return "pan_right"
    if key in ("+", "="):
        return "step_up"
    if key in ("-", "_"):
        return "step_down"
    if key in ("h", "H"):
        return "home"
    if key == "1":
        return "mark_pan_min"
    if key == "2":
        return "mark_pan_max"
    if key == "3":
        return "mark_tilt_min"
    if key == "4":
        return "mark_tilt_max"
    if key in ("c", "C"):
        return "mark_center"
    if key in ("p", "P"):
        return "print_yaml"
    if key in ("y", "Y"):
        return "write_config"
    if key in ("q", "Q", "\x03"):
        return "quit"
    return None


def main() -> int:
    servo = load_servo_cfg()
    parser = argparse.ArgumentParser(description="WASD manual pan/tilt limit finder")
    parser.add_argument("--port", default=str(servo.get("port") or ""), help="ESP32 serial port")
    parser.add_argument("--baud", type=int, default=int(servo.get("baud", 115200)))
    parser.add_argument("--pan", type=float, default=float(servo.get("pan_center", 75.0)))
    parser.add_argument("--tilt", type=float, default=float(servo.get("tilt_center", 112.0)))
    parser.add_argument("--step", type=float, default=1.0, help="Initial jog step (servo cmd units)")
    parser.add_argument("--no-home", action="store_true", help="Do not move to center on exit")
    args = parser.parse_args()

    pan_center = float(servo.get("pan_center", args.pan))
    tilt_center = float(servo.get("tilt_center", args.tilt))

    link = ArduinoServoLink(port=args.port, baud=args.baud)
    if not link.connect():
        print("Could not connect to ESP32.")
        return 1

    pan = args.pan
    tilt = args.tilt
    step_idx = STEP_CHOICES.index(args.step) if args.step in STEP_CHOICES else 2
    marks: dict[str, Optional[float]] = {
        "pan_min": float(servo["pan_min"]) if "pan_min" in servo else None,
        "pan_max": float(servo["pan_max"]) if "pan_max" in servo else None,
        "tilt_min": float(servo["tilt_min"]) if "tilt_min" in servo else None,
        "tilt_max": float(servo["tilt_max"]) if "tilt_max" in servo else None,
        "pan_center": pan_center,
        "tilt_center": tilt_center,
    }

    print("WASD = move   +/- = step   H = center   1-4 = mark min/max   C = center   P/Y = print/write   Q = quit")
    link.write_angles(pan, tilt, force=True)
    _print_pose(pan, tilt)

    fd = sys.stdin.fileno()
    old_term = termios.tcgetattr(fd)
    try:
        tty.setcbreak(fd)
        while True:
            step = STEP_CHOICES[step_idx]
            action = _map_key(_read_key())
            if action is None:
                continue
            if action == "quit":
                break
            if action == "step_up":
                step_idx = min(step_idx + 1, len(STEP_CHOICES) - 1)
                print(f"step={STEP_CHOICES[step_idx]:.2f}", flush=True)
                continue
            if action == "step_down":
                step_idx = max(step_idx - 1, 0)
                print(f"step={STEP_CHOICES[step_idx]:.2f}", flush=True)
                continue
            moved = False
            if action == "home":
                pan = pan_center
                tilt = tilt_center
                moved = True
            elif action == "pan_left":
                pan -= step
                moved = True
            elif action == "pan_right":
                pan += step
                moved = True
            elif action == "tilt_up":
                tilt += step
                moved = True
            elif action == "tilt_down":
                tilt -= step
                moved = True
            elif action == "mark_pan_min":
                marks["pan_min"] = pan
                print(f"marked pan_min={pan:.1f}", flush=True)
            elif action == "mark_pan_max":
                marks["pan_max"] = pan
                print(f"marked pan_max={pan:.1f}", flush=True)
            elif action == "mark_tilt_min":
                marks["tilt_min"] = tilt
                print(f"marked tilt_min={tilt:.1f}", flush=True)
            elif action == "mark_tilt_max":
                marks["tilt_max"] = tilt
                print(f"marked tilt_max={tilt:.1f}", flush=True)
            elif action == "mark_center":
                marks["pan_center"] = pan
                marks["tilt_center"] = tilt
                pan_center = pan
                tilt_center = tilt
                print(f"marked center pan={pan:.1f} tilt={tilt:.1f}", flush=True)
            elif action == "print_yaml":
                _print_yaml_snippet(marks)
            elif action == "write_config":
                try:
                    _write_servo_limits_to_config(
                        pan_min=marks["pan_min"],
                        pan_max=marks["pan_max"],
                        tilt_min=marks["tilt_min"],
                        tilt_max=marks["tilt_max"],
                        pan_center=marks["pan_center"],
                        tilt_center=marks["tilt_center"],
                    )
                    print(f"Wrote marked limits to {CONFIG_PATH}", flush=True)
                except Exception as exc:
                    print(f"Write failed: {exc}", flush=True)
            if moved:
                link.write_angles(pan, tilt, force=True)
                _print_pose(pan, tilt)
    except KeyboardInterrupt:
        pass
    finally:
        termios.tcsetattr(fd, termios.TCSADRAIN, old_term)
        home_pan = marks["pan_center"] if marks["pan_center"] is not None else pan_center
        home_tilt = marks["tilt_center"] if marks["tilt_center"] is not None else tilt_center
        link.close(
            home_pan=home_pan,
            home_tilt=home_tilt,
            skip_home=args.no_home,
        )
        _print_yaml_snippet(marks)
        print("Done.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
