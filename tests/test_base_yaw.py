#!/usr/bin/env python3
"""Smoke tests for startup-zero and base yaw sector behavior."""

from __future__ import annotations

import argparse
import time

import _bootstrap  # noqa: F401

from arduino_servo import ArduinoServoLink
from base_yaw_controller import BaseYawState


def run_zero(link: ArduinoServoLink) -> int:
    if not link.zero_base():
        print("Failed to send Z command")
        return 1
    time.sleep(0.2)
    st = link.query_status()
    if st is None:
        print("No status after zero")
        return 1
    print(f"Zeroed: encoder={st.encoder_count} deg={st.degrees:+.2f}")
    return 0


def run_relative(link: ArduinoServoLink, degrees: float) -> int:
    st0 = link.query_status()
    if st0 is None:
        print("No initial base status")
        return 1
    if not link.write_base_relative(degrees, wait=True):
        print("Base relative move failed")
        return 1
    st1 = link.query_status()
    if st1 is None:
        print("No final base status")
        return 1
    print(f"Moved {degrees:+.1f} -> encoder delta {st1.degrees - st0.degrees:+.2f} deg")
    return 0


def run_sector_test(link: ArduinoServoLink, max_yaw_deg: float) -> int:
    st = link.query_status()
    if st is None:
        print("No base status")
        return 1
    yaw = BaseYawState(max_yaw_deg=max_yaw_deg)
    yaw.update(st.degrees, head_pan_offset_deg=0.0)
    step = 12.0 if yaw.base_encoder_deg >= 0 else -12.0
    while yaw.allow_base_step(step, 0.0):
        if not link.write_base_relative(step, wait=True):
            print("Move failed while approaching sector edge")
            return 1
        st = link.query_status()
        if st is None:
            return 1
        yaw.update(st.degrees, 0.0)
        print(f"base_deg={yaw.base_encoder_deg:+.1f} world={yaw.world_yaw_deg:+.1f}")
    print(
        f"Sector block confirmed at base_deg={yaw.base_encoder_deg:+.1f}; "
        f"next step {step:+.1f} would exceed ±{max_yaw_deg:.0f}"
    )
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description="Base yaw smoke tests")
    parser.add_argument("--port", default="", help="ESP32 serial port")
    parser.add_argument("--baud", type=int, default=115200)
    parser.add_argument("--zero", action="store_true", help="Issue Z and print base status")
    parser.add_argument("--relative", type=float, help="Run one B relative move and report encoder delta")
    parser.add_argument("--sector-test", action="store_true", help="Step until sector gate would block")
    parser.add_argument("--max-yaw-deg", type=float, default=120.0)
    args = parser.parse_args()

    link = ArduinoServoLink(port=args.port, baud=args.baud)
    if not link.connect():
        print("Could not connect to ESP32")
        return 1
    try:
        if args.zero:
            rc = run_zero(link)
            if rc != 0:
                return rc
        if args.relative is not None:
            rc = run_relative(link, args.relative)
            if rc != 0:
                return rc
        if args.sector_test:
            rc = run_sector_test(link, args.max_yaw_deg)
            if rc != 0:
                return rc
        if not args.zero and args.relative is None and not args.sector_test:
            parser.print_help()
            return 1
        return 0
    finally:
        link.close()


if __name__ == "__main__":
    raise SystemExit(main())
