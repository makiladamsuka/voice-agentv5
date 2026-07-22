#!/usr/bin/env python3
"""
ToF Sensor Test Monitor — run on the Pi while tof_test.ino is flashed.

Usage:
    python3 tests/test_tof_sensors.py              # auto-detect port
    python3 tests/test_tof_sensors.py /dev/ttyUSB0  # specific port

What it does:
    1. Connects to ESP32 serial
    2. Parses TOF readings
    3. Shows a live dashboard with distance bars + velocity
    4. Validates sensor health (out-of-range, dropouts)
"""

from __future__ import annotations

import re
import sys
import time

try:
    import serial
except ImportError:
    print("Install pyserial: pip install pyserial")
    sys.exit(1)

DEFAULT_PORTS = ("/dev/ttyUSB0", "/dev/ttyUSB1", "/dev/ttyACM0")
BAUD = 115200

_TOF_RE = re.compile(
    r"TOF\s+L=(-?\d+)\s+C=(-?\d+)\s+R=(-?\d+)"
    r"\s+VL=(-?\d+)\s+VC=(-?\d+)\s+VR=(-?\d+)"
)


def find_port(hint: str = "") -> str:
    import os
    candidates = [p for p in DEFAULT_PORTS if os.path.exists(p)]
    if not candidates:
        print(f"No serial port found. Tried: {', '.join(DEFAULT_PORTS)}")
        sys.exit(1)
    if hint and os.path.exists(hint):
        return hint
    if hint:
        print(f"Note: {hint} not found, using auto-detect...")
    return candidates[-1]  # USB re-enumeration often lands on higher ttyUSBn


def distance_bar(mm: int, max_mm: int = 2000, width: int = 30) -> str:
    if mm < 0:
        return "?" * width
    filled = int(min(mm / max_mm, 1.0) * width)
    return "#" * filled + "." * (width - filled)


def velocity_arrow(v: int) -> str:
    if v < -100:
        return "<<< APPROACH"
    elif v < -30:
        return "<<  approaching"
    elif v < -10:
        return "<   drifting closer"
    elif v > 100:
        return ">>>  LEAVING"
    elif v > 30:
        return ">>  departing"
    elif v > 10:
        return ">   drifting away"
    else:
        return ".   still"


def main():
    port = find_port(sys.argv[1] if len(sys.argv) > 1 else "")
    print(f"Connecting to {port}@{BAUD}...")

    ser = serial.Serial(port, BAUD, timeout=0.5)
    time.sleep(2)  # wait for ESP32 boot

    # Drain boot messages
    boot_lines = []
    deadline = time.time() + 3.0
    while time.time() < deadline:
        line = ser.readline().decode("utf-8", errors="ignore").strip()
        if line:
            boot_lines.append(line)
            print(f"  {line}")
        if "Streaming readings" in line:
            break

    if not boot_lines:
        print("No response from ESP32. Check connection.")
        ser.close()
        sys.exit(1)

    print()
    print("=" * 70)
    print("  ToF Sensor Live Monitor  (Ctrl+C to exit)")
    print("=" * 70)
    print()

    labels = ["LEFT ", "CENTER", "RIGHT"]
    sample_count = 0
    errors = [0, 0, 0]

    try:
        while True:
            line = ser.readline().decode("utf-8", errors="ignore").strip()
            if not line:
                continue

            m = _TOF_RE.search(line)
            if not m:
                if line:
                    print(f"  [{line}]")
                continue

            sample_count += 1
            mm = [int(m.group(1)), int(m.group(2)), int(m.group(3))]
            vel = [int(m.group(4)), int(m.group(5)), int(m.group(6))]

            for i in range(3):
                if mm[i] < 0:
                    errors[i] += 1
                bar = distance_bar(mm[i])
                v_label = velocity_arrow(vel[i])
                mm_str = f"{mm[i]:5d}mm" if mm[i] >= 0 else "  N/A  "
                vel_str = f"{vel[i]:+5d}mm/s" if mm[i] >= 0 else "        "
                print(
                    f"  {labels[i]:7s} {bar} {mm_str} {vel_str}  {v_label}"
                )

            ok_count = sum(1 for d in mm if d >= 0)
            print(
                f"  --- samples: {sample_count}  |  "
                f"sensors OK: {ok_count}/3  |  "
                f"dropouts: L={errors[0]} C={errors[1]} R={errors[2]}"
            )
            print()

    except KeyboardInterrupt:
        print()
        print("-" * 70)
        print(f"Total samples: {sample_count}")
        for i in range(3):
            err_pct = (errors[i] / max(1, sample_count)) * 100
            status = "GOOD" if err_pct < 5 else "WARNING" if err_pct < 20 else "BAD"
            print(f"  {labels[i]:7s}  dropouts: {errors[i]:4d} ({err_pct:.1f}%)  [{status}]")
        print("-" * 70)
    finally:
        ser.close()


if __name__ == "__main__":
    main()
