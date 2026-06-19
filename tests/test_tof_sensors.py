#!/usr/bin/env python3
"""Bench test for v5 ESP32 ToF telemetry (L/C/R)."""

from __future__ import annotations

import argparse
import sys
import time

import _bootstrap  # noqa: F401

from arduino_servo import ArduinoServoLink
from tof_presence import (
    TofPresenceTracker,
    TofSnapshotFilter,
    format_tof_channel,
)


def _print_reading(snap, tracker, *, tof_filter: TofSnapshotFilter) -> None:
    snap = tof_filter.apply(snap)
    presence = tracker.update(snap)
    print(
        f"L={format_tof_channel(snap.left_mm, snap.left_valid):>8} "
        f"C={format_tof_channel(snap.center_mm, snap.center_valid):>8} "
        f"R={format_tof_channel(snap.right_mm, snap.right_valid):>8} "
        f"present L={presence.left} C={presence.center} R={presence.right} "
        f"count={presence.count_present}"
    )


def _snap_key(snap) -> tuple:
    return (
        snap.left_mm,
        snap.center_mm,
        snap.right_mm,
        snap.left_valid,
        snap.center_valid,
        snap.right_valid,
    )


def main() -> int:
    parser = argparse.ArgumentParser(description="Poll/stream ESP32 ToF telemetry")
    parser.add_argument("--port", default="", help="Serial port (default: auto)")
    parser.add_argument("--baud", type=int, default=115200)
    parser.add_argument("--hz", type=float, default=3.0, help="Poll/stream cadence")
    parser.add_argument("--poll", action="store_true", help="Use Pi F polls instead of stream")
    parser.add_argument("--present-max-mm", type=float, default=1500.0)
    parser.add_argument("--absent-min-mm", type=float, default=1800.0)
    parser.add_argument("--min-valid-mm", type=int, default=30)
    parser.add_argument("--max-valid-mm", type=int, default=800)
    parser.add_argument("--max-jump-mm", type=int, default=100)
    parser.add_argument("--hold-sec", type=float, default=0.35)
    args = parser.parse_args()

    tof_filter = TofSnapshotFilter(
        min_valid_mm=args.min_valid_mm,
        max_valid_mm=args.max_valid_mm,
        max_jump_mm=args.max_jump_mm,
        hold_sec=args.hold_sec,
    )
    tracker = TofPresenceTracker(
        present_max_mm=args.present_max_mm,
        absent_min_mm=args.absent_min_mm,
        debounce_present_sec=0.10,
        debounce_absent_sec=0.20,
    )

    link = ArduinoServoLink(port=args.port, baud=args.baud)
    if not link.connect():
        print("Could not connect to ESP32.")
        return 1

    if link.boot_lines:
        print("Boot log:")
        seen: set[str] = set()
        for line in link.boot_lines[-12:]:
            if line in seen:
                continue
            seen.add(line)
            print(f"  {line}")

    if not link.tof_capable:
        print(
            "Note: ToF not seen in boot log — will probe on first read. "
            "If no data, check mux/VL53 wiring and reflash firmware."
        )

    use_stream = not args.poll
    interval = 1.0 / max(0.5, args.hz)
    # Single-shot mux sweep on ESP32 can take ~1s; allow headroom over stream interval.
    stream_timeout = max(2.0, interval * 4.0)
    if use_stream:
        if not link.set_tof_stream(True, args.hz):
            print("Failed to enable ToF stream mode.")
            return 1
        print(f"ToF stream enabled at ~{args.hz:.1f} Hz")
        prime = link.read_tof_stream(timeout=stream_timeout)
        if prime is None:
            prime = link.poll_tof(timeout=stream_timeout)
        if prime is not None:
            _print_reading(prime, tracker, tof_filter=tof_filter)
    else:
        print(f"ToF polling at {args.hz:.1f} Hz")

    misses = 0
    last_key: tuple | None = None
    started = time.time()
    warmup_sec = 20.0
    try:
        miss_streak = 0
        while True:
            loop_start = time.time()
            if use_stream:
                snap = link.read_tof_stream(timeout=stream_timeout)
            else:
                snap = link.poll_tof(timeout=min(0.45, interval * 0.7))

            if snap is None:
                misses += 1
                miss_streak += 1
                if miss_streak <= 5 or miss_streak % 10 == 0:
                    print(f"(no TOF response, misses={misses})")
                if miss_streak >= 12 and (time.time() - started) > warmup_sec:
                    print("Attempting ToF recovery (USB reset)...")
                    try:
                        link.set_tof_stream(False)
                        link._esp32_reset()
                        time.sleep(0.8)
                        link._boot_lines = []
                        if link._wait_for_ready(6.0):
                            link._probe_tof()
                        if use_stream:
                            link.set_tof_stream(True, args.hz)
                        last_key = None
                    except Exception:
                        pass
                    miss_streak = 0
                time.sleep(0.03)
                continue

            miss_streak = 0
            key = _snap_key(snap)
            if key == last_key and any((snap.left_valid, snap.center_valid, snap.right_valid)):
                if use_stream:
                    elapsed = time.time() - loop_start
                    time.sleep(max(0.0, interval - elapsed))
                continue
            last_key = key
            _print_reading(snap, tracker, tof_filter=tof_filter)
            if use_stream:
                elapsed = time.time() - loop_start
                time.sleep(max(0.0, interval - elapsed))
            else:
                time.sleep(interval)
    except KeyboardInterrupt:
        print("\nDone.")
    finally:
        link.set_tof_stream(False)
        link.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
