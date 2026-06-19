#!/usr/bin/env python3
"""BMI160 bring-up, level calibration, and gyro vs encoder spin validation."""

from __future__ import annotations

import argparse
import subprocess
import sys
import time
from pathlib import Path

import _bootstrap  # noqa: F401

from arduino_servo import ArduinoServoLink
from base_motor_utils import apply_config_cpd_to_nano
from imu_sensor import Bmi160, ImuAttitudeFilter, ImuReader, startup_level_calibrate

CONFIG_PATH = Path(__file__).resolve().parents[1] / "config.yaml"

try:
    import yaml
except ImportError:
    yaml = None


def _parse_scalar(value: str):
    value = value.split("#", 1)[0].strip()
    if not value:
        return ""
    if value.startswith("[") and value.endswith("]"):
        return [_parse_scalar(item) for item in value[1:-1].split(",") if item.strip()]
    lowered = value.lower()
    if lowered in ("true", "false"):
        return lowered == "true"
    try:
        if lowered.startswith("0x"):
            return int(lowered, 16)
        if any(ch in value for ch in (".", "e", "E")):
            return float(value)
        return int(value)
    except ValueError:
        return value.strip("'\"")


def _load_simple_config() -> dict:
    data = {}
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


def _load_imu_cfg() -> dict:
    if not CONFIG_PATH.exists():
        return {}
    try:
        if yaml is None:
            return _load_simple_config().get("imu") or {}
        with open(CONFIG_PATH, encoding="utf-8") as f:
            return (yaml.safe_load(f) or {}).get("imu") or {}
    except Exception:
        return {}


def _load_imu_defaults() -> tuple[int, int]:
    imu = _load_imu_cfg()
    return int(imu.get("i2c_bus", 1)), int(imu.get("address", 0x69))


def _load_imu_offsets() -> tuple[float, float]:
    imu = _load_imu_cfg()
    return float(imu.get("roll_offset_deg", 0.0)), float(imu.get("pitch_offset_deg", 0.0))


def _write_imu_offsets_to_config(roll: float, pitch: float) -> None:
    import re

    text = CONFIG_PATH.read_text(encoding="utf-8")
    text = re.sub(
        r"^(\s*roll_offset_deg:\s*)([^\n#]+)(.*)$",
        rf"\g<1>{roll:.2f}\3",
        text,
        count=1,
        flags=re.MULTILINE,
    )
    text = re.sub(
        r"^(\s*pitch_offset_deg:\s*)([^\n#]+)(.*)$",
        rf"\g<1>{pitch:.2f}\3",
        text,
        count=1,
        flags=re.MULTILINE,
    )
    CONFIG_PATH.write_text(text, encoding="utf-8")


def _load_axis_remap() -> tuple[int, ...]:
    values = _load_imu_cfg().get("axis_remap")
    if values:
        return tuple(int(v) for v in values)
    return (-3, 2, 1)


def run_i2c_detect(bus: int) -> int:
    print(f"Running i2cdetect -y {bus} ...")
    try:
        result = subprocess.run(
            ["i2cdetect", "-y", str(bus)],
            check=False,
            capture_output=True,
            text=True,
        )
    except FileNotFoundError:
        print("i2cdetect not found — enable I2C via raspi-config and install i2c-tools.")
        return 1
    print(result.stdout)
    if result.returncode != 0:
        print(result.stderr)
    return result.returncode


def run_raw_stream(bus: int, address: int, seconds: float, hz: float) -> int:
    roll_off, pitch_off = _load_imu_offsets()
    dev = Bmi160(bus=bus, address=address)
    filt = ImuAttitudeFilter(
        axis_remap=_load_axis_remap(),
        roll_offset_deg=roll_off,
        pitch_offset_deg=pitch_off,
    )
    dev.open()
    print(f"Raw BMI160 stream for {seconds:.1f}s at ~{hz:.0f} Hz (Ctrl+C to stop early)")
    print("Tilt or rotate the sensor — roll/pitch/gyro should change.")
    deadline = time.time() + seconds
    interval = 1.0 / max(1.0, hz)
    prev = time.perf_counter()
    try:
        while time.time() < deadline:
            ax, ay, az, gx, gy, gz = dev.read_raw()
            now = time.perf_counter()
            sample = filt.update(ax, ay, az, gx, gy, gz, now - prev)
            prev = now
            print(
                f"\r roll {sample.roll_deg:+6.2f} pitch {sample.pitch_deg:+6.2f} "
                f"gyro(yaw) {sample.gyro_z_dps:+7.1f} dps  "
                f"acc({sample.accel_x_g:+.2f},{sample.accel_y_g:+.2f},{sample.accel_z_g:+.2f})g   ",
                end="",
                flush=True,
            )
            time.sleep(max(0.0, interval - (time.perf_counter() - now)))
    except KeyboardInterrupt:
        print()
    finally:
        dev.close()
    print()
    return 0


def run_mount_check(bus: int, address: int) -> int:
    """One-shot pose hint: face-down vs upright for axis_remap validation."""
    roll_off, pitch_off = _load_imu_offsets()
    dev = Bmi160(bus=bus, address=address)
    filt = ImuAttitudeFilter(
        axis_remap=_load_axis_remap(),
        roll_offset_deg=roll_off,
        pitch_offset_deg=pitch_off,
    )
    dev.open()
    try:
        ax, ay, az, gx, gy, gz = dev.read_raw()
        sample = filt.update(ax, ay, az, gx, gy, gz, 0.01)
        print("Sensor frame (chip XYZ):")
        print(f"  acc({ax:+.3f},{ay:+.3f},{az:+.3f})g  gyro({gx:+.1f},{gy:+.1f},{gz:+.1f})dps")
        print("Head frame after axis_remap:")
        print(
            f"  roll {sample.roll_deg:+.1f}°  pitch {sample.pitch_deg:+.1f}°  "
            f"yaw-rate {sample.gyro_z_dps:+.1f} dps"
        )
        print(
            f"  acc forward {sample.accel_x_g:+.2f}g  left {sample.accel_y_g:+.2f}g  "
            f"up {sample.accel_z_g:+.2f}g"
        )
        pitch = sample.pitch_deg
        fwd = sample.accel_x_g
        up = sample.accel_z_g
        if pitch > 60.0 and fwd < -0.75:
            print("\nPose: FACE DOWN — matches expected axis_remap (pitch ~+90°, forward ≈ -1g).")
            print("For level-calibrate, hold the head UPRIGHT in normal tracking pose instead.")
            return 0
        if abs(sample.roll_deg) < 20.0 and abs(pitch) < 20.0 and up > 0.75:
            print("\nPose: UPRIGHT LEVEL — good for: python tests/test_imu.py --level-calibrate")
            return 0
        if abs(sample.roll_deg) < 20.0 and abs(pitch) < 20.0 and up < -0.75:
            print("\nPose: UPRIGHT LEVEL (up axis inverted) — consider flipping up axis in axis_remap.")
            return 1
        print("\nPose: tilted or rotating — hold still upright or face-down to validate mount.")
        return 0
    finally:
        dev.close()


def run_level_calibrate(bus: int, address: int, seconds: float, *, write_config: bool) -> int:
    imu = _load_imu_cfg()
    reader = ImuReader(
        bus=bus,
        address=address,
        sample_hz=float(imu.get("sample_hz", 100.0)),
        roll_pitch_alpha=float(imu.get("roll_pitch_alpha", 0.02)),
        axis_remap=_load_axis_remap(),
        roll_offset_deg=0.0,
        pitch_offset_deg=0.0,
    )
    reader.start()
    print("Keep head at mechanical center (upright) and hold still ...")
    time.sleep(0.5)
    try:
        roll_off, pitch_off, _, used = startup_level_calibrate(
            reader,
            duration_sec=seconds,
            warmup_sec=0.3,
            max_gyro_dps=float(imu.get("auto_level_gyro_max_dps", 8.0)),
            min_samples=40,
        )
    except ValueError as exc:
        print(f"Level calibrate failed: {exc}")
        return 1
    finally:
        reader.stop()
    print(
        f"Level offsets: roll {roll_off:+.3f}°, pitch {pitch_off:+.3f}° ({used} still samples)"
    )
    if write_config:
        _write_imu_offsets_to_config(roll_off, pitch_off)
        print(f"Wrote offsets to {CONFIG_PATH}")
    else:
        print("Add to config.yaml imu.roll_offset_deg / pitch_offset_deg, or re-run with --write-config")
    return 0


def run_spin_test(port: str, baud: int, degrees: float, verify: bool) -> int:
    if degrees <= 0:
        print("--degrees must be positive")
        return 1

    link = ArduinoServoLink(port=port, baud=baud)
    if not link.connect():
        print("ESP32 connect failed")
        return 1

    reader = ImuReader(
        sample_hz=float(_load_imu_cfg().get("sample_hz", 100.0)),
        axis_remap=_load_axis_remap(),
        roll_offset_deg=_load_imu_offsets()[0],
        pitch_offset_deg=_load_imu_offsets()[1],
    )
    reader.start()
    try:
        apply_config_cpd_to_nano(link)
        st0 = link.query_status()
        if st0 is None:
            print("No encoder status")
            return 1
        reader.filter.reset_yaw_integral()
        print(f"Spin test: B{degrees:+.1f}° — compare encoder delta vs gyro integral")
        if not link.write_base_relative(degrees, wait=verify):
            print("Base move failed")
            return 1

        deadline = time.time() + link.base_move_timeout_sec
        while time.time() < deadline:
            st = link.query_status()
            if st is not None and not st.busy:
                break
            time.sleep(0.05)

        st1 = link.query_status()
        enc_delta = (st1.degrees if st1 else st0.degrees) - st0.degrees
        gyro_delta = reader.filter.yaw_integral_deg()
        err = abs(enc_delta - gyro_delta)
        print(f"  encoder delta: {enc_delta:+.2f}°")
        print(f"  gyro integral: {gyro_delta:+.2f}°")
        print(f"  |error|:       {err:.2f}°")
        if err > max(8.0, abs(degrees) * 0.35):
            print("  WARNING: large mismatch — check axis_remap in config.yaml")
            return 1
        print("  OK — gyro and encoder agree within tolerance")
        return 0
    finally:
        reader.stop()
        link.close()


def main() -> int:
    default_bus, default_address = _load_imu_defaults()
    parser = argparse.ArgumentParser(description="BMI160 head IMU test tools")
    parser.add_argument("--bus", type=int, default=default_bus, help="I2C bus number")
    parser.add_argument(
        "--address",
        type=lambda x: int(x, 0),
        default=default_address,
        help="I2C address (SDO/ADDR to GND=0x68, VCC=0x69)",
    )
    parser.add_argument("--hz", type=float, default=20.0, help="Print rate for raw stream")
    parser.add_argument("--seconds", type=float, default=10.0, help="Duration for stream/calibrate")
    parser.add_argument("--detect", action="store_true", help="Run i2cdetect on the bus")
    parser.add_argument("--check-mount", action="store_true", help="One-shot face-down vs upright pose check")
    parser.add_argument("--raw", action="store_true", help="Print roll/pitch/gyro from raw reads")
    parser.add_argument("--level-calibrate", action="store_true", help="Average level offsets at P78 T112 center")
    parser.add_argument("--write-config", action="store_true", help="Write IMU offsets to config.yaml after --level-calibrate")
    parser.add_argument("--spin-test", action="store_true", help="Compare gyro integral vs encoder on B move")
    parser.add_argument("--degrees", type=float, default=10.0, help="Degrees for --spin-test")
    parser.add_argument("--port", default="", help="ESP32 serial port for --spin-test")
    parser.add_argument("--baud", type=int, default=115200)
    parser.add_argument("--verify", action="store_true", help="Wait for base ACK during --spin-test")
    args = parser.parse_args()

    if args.detect:
        return run_i2c_detect(args.bus)
    if args.check_mount:
        return run_mount_check(args.bus, args.address)
    if args.raw:
        return run_raw_stream(args.bus, args.address, args.seconds, args.hz)
    if args.level_calibrate:
        return run_level_calibrate(args.bus, args.address, args.seconds, write_config=args.write_config)
    if args.spin_test:
        return run_spin_test(args.port, args.baud, args.degrees, args.verify)

    parser.print_help()
    return 1


if __name__ == "__main__":
    sys.exit(main())
