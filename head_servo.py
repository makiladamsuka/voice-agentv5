#!/usr/bin/env python3
"""
Interactive WASD head servo test for IMU calibration poses.

  cd voice-agentv5 && python head_servo.py
  python head_servo.py --imu          # show BMI160 roll/pitch while moving
  python head_servo.py --step 1       # degrees per key press (default 1)

Keys (one step per press):
  W/S = tilt up/down        A/D = pan left/right
  C   = jump to center      U = upright preset (pan/tilt center)
  H   = horizontal preset   I = IMU pose check once
  Q   = home head and quit
"""

from __future__ import annotations

import argparse
import select
import sys
import termios
import tty
import time
from pathlib import Path

try:
    import yaml
except ImportError:
    yaml = None

from arduino_servo import ArduinoServoLink
from elastic_head_motion import clamp

APP_DIR = Path(__file__).resolve().parent
CONFIG_PATH = APP_DIR / "config.yaml"

LOOP_HZ = 50.0
DEBUG_HZ = 10.0
STEP_DEBOUNCE_SEC = 0.12


def _load_servo_cfg() -> dict:
    if yaml is None or not CONFIG_PATH.exists():
        return {}
    with open(CONFIG_PATH, encoding="utf-8") as f:
        data = yaml.safe_load(f) or {}
    return data.get("servo") or {}


def _limits(cfg: dict) -> tuple[float, float, float, float, float, float, float, float]:
    pan_min = float(cfg.get("pan_min", 40.0))
    pan_max = float(cfg.get("pan_max", 120.0))
    tilt_min = float(cfg.get("tilt_min", 100.0))
    tilt_max = float(cfg.get("tilt_max", 120.0))
    pan_center = float(cfg.get("pan_center", (pan_min + pan_max) * 0.5))
    tilt_center = float(cfg.get("tilt_center", (tilt_min + tilt_max) * 0.5))
    pan_sign = float(cfg.get("pan_sign", 1.0))
    tilt_sign = float(cfg.get("tilt_sign", 1.0))
    return pan_min, pan_max, tilt_min, tilt_max, pan_center, tilt_center, pan_sign, tilt_sign


def _get_key() -> str | None:
    if select.select([sys.stdin], [], [], 0)[0]:
        return sys.stdin.read(1)
    return None


def _drain_keys() -> list[str]:
    keys: list[str] = []
    while True:
        key = _get_key()
        if key is None:
            break
        keys.append(key)
    return keys


def _imu_status_line() -> str:
    try:
        from imu_sensor import ImuReader

        reader = getattr(_imu_status_line, "_reader", None)
        if reader is None:
            imu_cfg = {}
            if yaml and CONFIG_PATH.exists():
                with open(CONFIG_PATH, encoding="utf-8") as f:
                    imu_cfg = (yaml.safe_load(f) or {}).get("imu") or {}
            reader = ImuReader(
                bus=int(imu_cfg.get("i2c_bus", 1)),
                address=int(imu_cfg.get("address", 0x69)),
                sample_hz=float(imu_cfg.get("sample_hz", 100.0)),
                roll_pitch_alpha=float(imu_cfg.get("roll_pitch_alpha", 0.02)),
                axis_remap=tuple(int(v) for v in (imu_cfg.get("axis_remap") or [-3, 2, 1])),
                roll_offset_deg=float(imu_cfg.get("roll_offset_deg", 0.0)),
                pitch_offset_deg=float(imu_cfg.get("pitch_offset_deg", 0.0)),
            )
            reader.start()
            _imu_status_line._reader = reader  # type: ignore[attr-defined]
        sample = reader.latest()
        if sample is None:
            return "  imu: warming up"
        return (
            f"  imu roll {sample.roll_deg:+5.1f}° pitch {sample.pitch_deg:+5.1f}° "
            f"gyro {sample.gyro_z_dps:+5.1f} dps"
        )
    except Exception as exc:
        return f"  imu: off ({exc})"


def _stop_imu_reader() -> None:
    reader = getattr(_imu_status_line, "_reader", None)
    if reader is not None:
        reader.stop()
        _imu_status_line._reader = None  # type: ignore[attr-defined]


def _run_mount_check() -> None:
    import subprocess

    script = APP_DIR / "tests" / "test_imu.py"
    if script.exists():
        subprocess.run([sys.executable, str(script), "--check-mount"], check=False)
    else:
        print("tests/test_imu.py not found")


def _apply_step(
    link: ArduinoServoLink,
    pan: float,
    tilt: float,
    *,
    d_pan: float,
    d_tilt: float,
    pan_min: float,
    pan_max: float,
    tilt_min: float,
    tilt_max: float,
) -> tuple[float, float]:
    pan = clamp(pan + d_pan, pan_min, pan_max)
    tilt = clamp(tilt + d_tilt, tilt_min, tilt_max)
    link.write_angles(pan, tilt, force=True)
    return pan, tilt


def run_interactive(
    link: ArduinoServoLink,
    *,
    loop_delay: float,
    show_imu: bool,
    step_deg: float,
    horizontal_tilt: float | None,
    upright_pan: float | None,
    upright_tilt: float | None,
) -> tuple[float, float]:
    cfg = _load_servo_cfg()
    pan_min, pan_max, tilt_min, tilt_max, pan_center, tilt_center, pan_sign, tilt_sign = _limits(cfg)
    pan_home = upright_pan if upright_pan is not None else pan_center
    tilt_home = upright_tilt if upright_tilt is not None else tilt_center
    tilt_horizontal = clamp(
        horizontal_tilt if horizontal_tilt is not None else tilt_max,
        tilt_min,
        tilt_max,
    )

    pan = pan_home
    tilt = tilt_home
    last_step_ts = 0.0
    last_debug_ts = 0.0
    running = True

    link.write_angles(pan, tilt, force=True)

    print("--- Head servo WASD (v5) — step mode ---")
    print(f"  W/S = tilt ±{step_deg:.1f}°/press   A/D = pan ±{step_deg:.1f}°/press")
    print(f"  U = upright  P{pan_home:.0f} T{tilt_home:.0f}  (IMU level-calibrate pose)")
    print(f"  H = horizontal  P{pan_center:.0f} T{tilt_horizontal:.0f}  (face toward ground)")
    print("  C = jump to center   I = IMU pose check   Q = quit")
    if show_imu:
        print("  IMU roll/pitch below — U for upright (~0°), H for face-down (~+90° pitch)")
    print(f"  Limits pan [{pan_min:.0f},{pan_max:.0f}]  tilt [{tilt_min:.0f},{tilt_max:.0f}]")

    old_settings = termios.tcgetattr(sys.stdin)
    try:
        tty.setcbreak(sys.stdin.fileno())
        while running:
            now = time.time()
            moved = False
            for key in _drain_keys():
                k = key.lower()
                if k == "q":
                    running = False
                    break
                if k == "i":
                    print()
                    _run_mount_check()
                    continue
                if k == "c":
                    pan, tilt = pan_home, tilt_home
                    link.write_angles(pan, tilt, force=True)
                    print(f"\n→ Center P{pan:.1f} T{tilt:.1f}")
                    moved = True
                    continue
                if k == "u":
                    pan, tilt = pan_home, tilt_home
                    link.write_angles(pan, tilt, force=True)
                    print(f"\n→ Upright P{pan:.1f} T{tilt:.1f}")
                    moved = True
                    continue
                if k == "h":
                    pan, tilt = pan_center, tilt_horizontal
                    link.write_angles(pan, tilt, force=True)
                    print(f"\n→ Horizontal P{pan:.1f} T{tilt:.1f}")
                    moved = True
                    continue
                if k not in "wasd":
                    continue
                if now - last_step_ts < STEP_DEBOUNCE_SEC:
                    continue
                d_pan = 0.0
                d_tilt = 0.0
                if k == "a":
                    d_pan = -step_deg * pan_sign
                elif k == "d":
                    d_pan = step_deg * pan_sign
                elif k == "w":
                    d_tilt = step_deg * tilt_sign
                elif k == "s":
                    d_tilt = -step_deg * tilt_sign
                pan, tilt = _apply_step(
                    link, pan, tilt,
                    d_pan=d_pan, d_tilt=d_tilt,
                    pan_min=pan_min, pan_max=pan_max,
                    tilt_min=tilt_min, tilt_max=tilt_max,
                )
                last_step_ts = now
                moved = True

            if moved or now - last_debug_ts >= 1.0 / DEBUG_HZ:
                imu_txt = _imu_status_line() if show_imu else ""
                sys.stdout.write(
                    f"\r  P {pan:5.1f}  T {tilt:5.1f}  (±{step_deg:.1f}°/key){imu_txt}   "
                )
                sys.stdout.flush()
                last_debug_ts = now

            time.sleep(loop_delay)
    finally:
        termios.tcsetattr(sys.stdin, termios.TCSADRAIN, old_settings)

    link.write_angles(pan_home, tilt_home, force=True)
    return pan_home, tilt_home


def main() -> int:
    cfg = _load_servo_cfg()
    parser = argparse.ArgumentParser(description="WASD head servo test for IMU calibration poses")
    parser.add_argument("--port", default=str(cfg.get("port", "")), help="ESP32 serial port")
    parser.add_argument("--baud", type=int, default=int(cfg.get("baud", 115200)))
    parser.add_argument("--imu", action="store_true", help="Show live IMU roll/pitch")
    parser.add_argument("--step", type=float, default=1.0, help="Degrees per WASD key press (default 1)")
    parser.add_argument(
        "--horizontal-tilt",
        type=float,
        default=None,
        help="Tilt deg for H key (default: tilt_max from config)",
    )
    parser.add_argument("--upright-pan", type=float, default=None, help="Pan for U/C home (default: pan_center)")
    parser.add_argument("--upright-tilt", type=float, default=None, help="Tilt for U/C home (default: tilt_center)")
    args = parser.parse_args()

    link = ArduinoServoLink(port=args.port, baud=args.baud)
    if not link.connect():
        print("Failed to connect. Check USB, dialout group, and firmware READY.")
        return 1

    loop_delay = 1.0 / LOOP_HZ
    try:
        run_interactive(
            link,
            loop_delay=loop_delay,
            show_imu=args.imu,
            step_deg=max(0.1, abs(args.step)),
            horizontal_tilt=args.horizontal_tilt,
            upright_pan=args.upright_pan,
            upright_tilt=args.upright_tilt,
        )
    except KeyboardInterrupt:
        print("\nInterrupted.")
    finally:
        _stop_imu_reader()
        pan_c = float(cfg.get("pan_center", 78.0))
        tilt_c = float(cfg.get("tilt_center", 112.0))
        link.close(home_pan=pan_c, home_tilt=tilt_c)
        print("Done.")

    return 0


if __name__ == "__main__":
    sys.exit(main())
