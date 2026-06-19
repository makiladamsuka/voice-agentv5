#!/usr/bin/env python3
"""Stress-test ToF polling while continuously writing servo angles."""

from __future__ import annotations

import argparse
import math
import sys
import time

import _bootstrap  # noqa: F401

from arduino_servo import ArduinoServoLink


def main() -> int:
    parser = argparse.ArgumentParser(description="Stress test ToF + servo serial contention")
    parser.add_argument("--port", default="", help="Serial port (default: auto)")
    parser.add_argument("--baud", type=int, default=115200)
    parser.add_argument("--seconds", type=float, default=30.0)
    parser.add_argument("--servo-hz", type=float, default=20.0)
    parser.add_argument("--tof-hz", type=float, default=6.0)
    parser.add_argument("--pan-amp-deg", type=float, default=14.0)
    parser.add_argument("--tilt-amp-deg", type=float, default=6.0)
    args = parser.parse_args()

    link = ArduinoServoLink(port=args.port, baud=args.baud)
    if not link.connect():
        print("Could not connect to ESP32.")
        return 1

    pan_center = 80.0
    tilt_center = 110.0
    start = time.time()
    next_servo = start
    next_tof = start
    servo_interval = 1.0 / max(2.0, args.servo_hz)
    tof_interval = 1.0 / max(0.5, args.tof_hz)

    tof_ok = 0
    tof_miss = 0
    servo_ok = 0
    servo_fail = 0

    try:
        while time.time() - start < max(1.0, args.seconds):
            now = time.time()

            if now >= next_servo:
                t = now - start
                pan = pan_center + (math.sin(t * 1.7) * args.pan_amp_deg)
                tilt = tilt_center + (math.cos(t * 1.3) * args.tilt_amp_deg)
                if link.write_angles(pan, tilt):
                    servo_ok += 1
                else:
                    servo_fail += 1
                next_servo += servo_interval

            if now >= next_tof:
                snap = link.poll_tof(timeout=min(0.5, tof_interval * 0.9))
                if snap is None:
                    tof_miss += 1
                else:
                    tof_ok += 1
                next_tof += tof_interval

            time.sleep(0.001)
    except KeyboardInterrupt:
        print("\nInterrupted.")
    finally:
        link.close(home_pan=pan_center, home_tilt=tilt_center)

    elapsed = max(0.001, time.time() - start)
    print("\n=== contention stats ===")
    print(f"elapsed_sec={elapsed:.1f}")
    print(f"servo_ok={servo_ok} servo_fail={servo_fail}")
    print(f"tof_ok={tof_ok} tof_miss={tof_miss}")
    print(f"tof_success_rate={(tof_ok / max(1, tof_ok + tof_miss)) * 100.0:.1f}%")
    return 0


if __name__ == "__main__":
    sys.exit(main())
