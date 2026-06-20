#!/usr/bin/env python3
"""
Base motor test — spin control (same as robottest M/N, automated).

  python tests/test_base_motor.py --zero --relative 10 --verify
  python tests/test_base_motor.py --status
  python tests/robottest.py          # keyboard M/N spin

Closed-loop B±deg commands are not used; moves use firmware L/R spin until
encoder reaches the target (robottest style).
"""

from __future__ import annotations

import argparse
import sys
import threading
import time

import _bootstrap  # noqa: F401

from arduino_servo import ArduinoServoLink, BASE_MOVE_TIMEOUT_SEC
from base_motor_utils import (
    CONFIG_PATH,
    apply_base_calibration_to_nano,
    configure_base_link,
    correct_command_scale,
    load_move_timeout,
    load_zero_on_start,
    write_command_scale_to_config,
    write_cpd_to_config,
    write_encoder_sign_to_config,
)

DEFAULT_HOLD_SEC = 1.0
POLL_INTERVAL_SEC = 0.1
MIN_ENCODER_DELTA = 5
MAX_SAFE_RELATIVE_DEG = 15.0

DEMO_STEPS = [
    ("+10 deg", 10.0),
    ("-10 deg", -10.0),
    ("return 0", 0.0),
]


def needs_config_cpd_on_connect(args: argparse.Namespace) -> bool:
    if getattr(args, "no_config_cpd", False):
        return False
    if args.calibrate_manual:
        return False
    return (
        args.apply_config_cpd
        or args.relative is not None
        or args.combined
        or args.demo
        or args.calibrate
    )


def warn_if_uncalibrated(link: ArduinoServoLink) -> None:
    if not link.is_calibrated():
        print(
            "WARNING: base not calibrated — run:\n"
            "  python tests/test_base_motor.py --calibrate-manual --degrees 90 --write-config"
        )


def print_stall_diagnosis(delta_counts: int) -> None:
    if abs(delta_counts) >= MIN_ENCODER_DELTA:
        print("  → encoder changed (spin control OK).")
        return
    print(
        "  → encoder did not change. Stop start_robot.py, reflash firmware (L/R spin), "
        "or try: python tests/robottest.py"
    )


def poll_status_loop(link: ArduinoServoLink, stop: threading.Event) -> None:
    max_abs = 0
    while not stop.is_set():
        st = link.query_status()
        if st is not None:
            max_abs = max(max_abs, abs(st.encoder_count))
            print(
                f"\rPOS {st.encoder_count:7d} DEG {st.degrees:7.2f} BUSY {int(st.busy)} "
                f"max|POS|={max_abs:<5d}   ",
                end="",
                flush=True,
            )
        time.sleep(POLL_INTERVAL_SEC)
    print()


def run_watch(link: ArduinoServoLink) -> None:
    link.zero_base()
    time.sleep(0.2)
    print("Live encoder monitor — turn the base slowly by hand. Ctrl+C to stop.\n")
    stop = threading.Event()
    thread = threading.Thread(target=poll_status_loop, args=(link, stop), daemon=True)
    thread.start()
    try:
        while True:
            time.sleep(0.5)
    except KeyboardInterrupt:
        stop.set()
        thread.join(timeout=1.0)
        print("\nStopped.")


def run_calibrate_manual(
    link: ArduinoServoLink,
    degrees: float,
    *,
    write_config: bool,
) -> None:
    if degrees <= 0:
        print("ERROR: --degrees must be positive.")
        sys.exit(1)

    print("Manual calibration — rotate the base by hand, encoder only.\n")
    link.zero_base()
    time.sleep(0.3)
    st0 = link.query_status()
    pos_start = st0.encoder_count if st0 else 0
    print(f"Zero set (POS {pos_start}).")
    print(f"Rotate the base exactly {degrees:.0f}° by hand (use a reference mark).")
    input("Press Enter when done.\n")

    st1 = link.query_status()
    pos_end = st1.encoder_count if st1 else pos_start
    signed_delta = pos_end - pos_start
    delta = abs(signed_delta)
    cpd = delta / degrees
    encoder_sign = 1.0 if signed_delta > 0 else -1.0

    print(f"  counts_per_degree: {cpd:.4f}")
    print(f"  encoder_sign: {encoder_sign:+.0f}")

    if delta < MIN_ENCODER_DELTA:
        print("\nERROR: encoder barely moved — check GPIO 34/35 wiring.")
        sys.exit(1)

    link.set_counts_per_degree(cpd)
    link.set_encoder_sign(encoder_sign)
    if write_config:
        write_cpd_to_config(cpd)
        write_encoder_sign_to_config(encoder_sign)
        print(f"Wrote {CONFIG_PATH}")


def run_demo(link: ArduinoServoLink, *, verify: bool, hold_sec: float) -> None:
    for label, deg in DEMO_STEPS:
        if deg == 0.0:
            link.zero_base()
            print("Zero")
            time.sleep(hold_sec)
            continue
        print(f"  -> {label}: spin {deg:+.1f}°", end="")
        ok = link.write_base_step_spin(deg)
        st = link.query_status()
        if not ok:
            print(" — incomplete")
        elif verify and st is not None:
            print(f" → enc {st.degrees:.1f}°")
        else:
            print()
        time.sleep(hold_sec)


def main() -> int:
    parser = argparse.ArgumentParser(description="Test base motor via L/R spin (v5)")
    parser.add_argument("--port", default="", help="Serial port")
    parser.add_argument("--baud", type=int, default=115200)
    parser.add_argument("--relative", type=float, default=None, help="Spin move in plate degrees")
    parser.add_argument("--combined", action="store_true")
    parser.add_argument("--pan", type=float, default=100.0)
    parser.add_argument("--tilt", type=float, default=110.0)
    parser.add_argument("--hold", type=float, default=DEFAULT_HOLD_SEC)
    parser.add_argument("--verify", action="store_true", help="Report encoder after move")
    parser.add_argument("--calibrate-manual", action="store_true")
    parser.add_argument("--degrees", type=float, default=90.0)
    parser.add_argument("--write-config", action="store_true")
    parser.add_argument("--apply-config-cpd", action="store_true")
    parser.add_argument("--no-config-cpd", action="store_true")
    parser.add_argument("--zero", action="store_true")
    parser.add_argument("--status", action="store_true")
    parser.add_argument("--watch", action="store_true")
    parser.add_argument("--jog", type=int, default=None)
    parser.add_argument("--jog-ms", type=int, default=250)
    parser.add_argument("--demo", action="store_true")
    parser.add_argument(
        "--correct-move",
        nargs=2,
        type=float,
        metavar=("COMMANDED", "ACTUAL"),
        help="Legacy: adjust command_scale (spin mode ignores scale for moves)",
    )
    args = parser.parse_args()

    link = ArduinoServoLink(port=args.port, baud=args.baud)
    link.base_move_timeout_sec = load_move_timeout()
    configure_base_link(link)

    if not link.connect():
        print("Failed to connect. Check USB and stop start_robot.py / robottest.")
        return 1

    try:
        if needs_config_cpd_on_connect(args):
            apply_base_calibration_to_nano(link)

        if args.correct_move:
            commanded, actual = args.correct_move
            scale = max(0.01, min(2.0, correct_command_scale(commanded, actual)))
            write_command_scale_to_config(scale)
            print(f"command_scale set to {scale:.4f} (legacy; spin moves ignore this)")
            return 0

        if args.zero:
            link.zero_base()
            print("Zero sent (Z).")
        if load_zero_on_start() and args.relative is not None:
            link.zero_base()

        if args.status:
            st = link.query_status()
            if st:
                print(
                    f"POS {st.encoder_count} DEG {st.degrees:.2f} "
                    f"CPD {st.counts_per_degree:.3f} BUSY {int(st.busy)}"
                )

        if args.watch:
            run_watch(link)
        elif args.jog is not None:
            st0 = link.query_status()
            link.write_base_jog(args.jog, args.jog_ms)
            time.sleep(args.jog_ms / 1000.0 + 0.3)
            st1 = link.query_status()
            if st0 and st1:
                print(f"jog delta counts {st1.encoder_count - st0.encoder_count:+d}")
        elif args.calibrate_manual:
            run_calibrate_manual(link, args.degrees, write_config=args.write_config)
        elif args.combined:
            rel = args.relative if args.relative is not None else 10.0
            link.write_angles(args.pan, args.tilt, force=True)
            print(f"Combined head + spin {rel:+.1f}°")
            ok = link.write_base_step_spin(rel)
            print("  → OK" if ok else "  → incomplete")
        elif args.relative is not None:
            warn_if_uncalibrated(link)
            deg = args.relative
            if abs(deg) > MAX_SAFE_RELATIVE_DEG:
                print(f"Capping single move to ±{MAX_SAFE_RELATIVE_DEG:.0f}° (wire-safe).")
                deg = MAX_SAFE_RELATIVE_DEG if deg > 0 else -MAX_SAFE_RELATIVE_DEG
            st0 = link.query_status()
            print(f"Spin move {deg:+.1f}° (L/R like robottest)")
            ok = link.write_base_step_spin(deg)
            st1 = link.query_status()
            if args.verify:
                print("  → OK" if ok else "  → incomplete (timeout or no encoder progress)")
            if st0 and st1:
                delta_counts = st1.encoder_count - st0.encoder_count
                print(
                    f"  → encoder {st0.degrees:.1f}° → {st1.degrees:.1f}° "
                    f"(delta {delta_counts:+d} counts)"
                )
                print_stall_diagnosis(delta_counts)
            time.sleep(args.hold)
        elif args.demo:
            run_demo(link, verify=args.verify, hold_sec=args.hold)
        elif not any([args.zero, args.status]):
            print(
                "Try --watch, --calibrate-manual, --relative 10, --demo, "
                "or keyboard: python tests/robottest.py"
            )
            return 1
        print("Done.")
    except KeyboardInterrupt:
        print("\nInterrupted.")
    finally:
        link.write_base_stop()
        link.close()

    return 0


if __name__ == "__main__":
    sys.exit(main())
