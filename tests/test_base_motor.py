#!/usr/bin/env python3
"""
Standalone base motor test for ESP32 v5 firmware (encoder + TB6612).

  cd voice-agentv5 && python tests/test_base_motor.py --status
  python tests/test_base_motor.py --calibrate-manual --degrees 90 --write-config
  python tests/test_base_motor.py --relative 30 --verify
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
    load_command_scale,
    load_move_timeout,
    load_zero_on_start,
    write_command_scale_to_config,
    write_cpd_to_config,
    write_encoder_sign_to_config,
)

DEFAULT_HOLD_SEC = 3.0
POLL_INTERVAL_SEC = 0.1
MIN_ENCODER_DELTA = 5

DEMO_STEPS = [
    ("+90 deg", 90.0),
    ("-180 deg", -180.0),
    ("+90 deg", 90.0),
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
        print(
            "  → encoder changed, so the motor/encoder loop is alive; tune CPD/sign/scale next."
        )
        return
    print(
        "  → encoder did not change. This is not a PID tuning problem: check motor power/TB6612 "
        "STBY/AIN/PWM wiring, then run --jog and --watch to isolate motor output vs encoder input."
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
    if max_abs < MIN_ENCODER_DELTA:
        print(
            "\nEncoder barely moved (max count < 5). Check encoder wiring on GPIO 34/35 "
            "and turn the motor shaft (not just the base plate)."
        )


def run_watch(link: ArduinoServoLink) -> None:
    link.zero_base()
    time.sleep(0.2)
    print("Live encoder monitor — turn the base slowly by hand. Ctrl+C to stop.")
    print("POS should change as the motor encoder turns.\n")
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
    print("Watch POS below. Press Enter when done.\n")

    stop = threading.Event()
    thread = threading.Thread(target=poll_status_loop, args=(link, stop), daemon=True)
    thread.start()
    try:
        input()
    finally:
        stop.set()
        thread.join(timeout=1.0)

    st1 = link.query_status()
    pos_end = st1.encoder_count if st1 else pos_start
    signed_delta = pos_end - pos_start
    delta = abs(signed_delta)
    cpd = delta / degrees
    encoder_sign = 1.0 if signed_delta > 0 else -1.0

    print(f"\n  Start POS: {pos_start}")
    print(f"  End POS:   {pos_end}")
    print(f"  Delta:     {signed_delta:+d} counts for {degrees:.0f}°")
    print(f"  counts_per_degree: {cpd:.6f}")
    print(f"  encoder_sign: {encoder_sign:+.0f}  (+deg command → sign×cpd counts)")
    print(f"  counts_per_revolution: {cpd * 360:.1f}")

    if delta < MIN_ENCODER_DELTA:
        print(
            "\nERROR: encoder barely moved — check GPIO 34/35 wiring and GND."
            "\nRun --watch first and turn the base by hand to confirm POS changes."
        )
        sys.exit(1)

    if not link.set_counts_per_degree(cpd):
        print("ERROR: failed to apply C command to ESP32.")
        sys.exit(1)
    if not link.set_encoder_sign(encoder_sign):
        print("ERROR: failed to apply E command to ESP32.")
        sys.exit(1)

    print(f"\nApplied C{cpd:.4f} and E{encoder_sign:+.0f} to ESP32.")
    print(f"  config.yaml:  counts_per_degree: {cpd:.6f}")
    print(f"  config.yaml:  encoder_sign: {encoder_sign:+.0f}")

    if write_config:
        write_cpd_to_config(cpd)
        write_encoder_sign_to_config(encoder_sign)
        print(f"  Wrote {CONFIG_PATH}")

    print("\nVerify with:")
    print("  python tests/test_base_motor.py --zero --status")
    print("  python tests/test_base_motor.py --relative 30 --verify")


def run_calibrate(link: ArduinoServoLink, cal_deg: float = 360.0) -> None:
    print(f"Motor calibration: zero, then B+{cal_deg:.0f} relative move.")
    print("(Prefer --calibrate-manual if motor moves are unreliable.)\n")
    link.zero_base()
    time.sleep(0.5)
    st0 = link.query_status()
    if st0:
        print(f"  Zero: POS {st0.encoder_count} CPD {st0.counts_per_degree:.3f}")
    print(f"Moving B+{cal_deg:.1f} ...")
    if not link.write_base_relative(cal_deg, wait=True):
        print("Move failed.")
        sys.exit(1)
    st1 = link.query_status()
    if st0 and st1:
        counts = abs(st1.encoder_count - st0.encoder_count)
        cpd = counts / cal_deg if cal_deg > 0 else 0.0
        print(f"  Encoder delta: {counts} counts for {cal_deg:.0f}° command")
        print(f"  Measured counts_per_degree: {cpd:.4f}")
        if counts < MIN_ENCODER_DELTA:
            print("  WARNING: encoder did not count — use --calibrate-manual instead.")
        elif cpd > 0.05:
            link.set_counts_per_degree(cpd)
            print(f"  Applied C{cpd:.4f} to ESP32 (until reboot)")
    if link._last_base_ack is not None:
        print(f"  ACK B{link._last_base_ack:.1f}")


def run_demo(link: ArduinoServoLink, *, verify: bool, hold_sec: float) -> None:
    warn_if_uncalibrated(link)
    print("Running base demo (Ctrl+C to stop early)...")
    for label, deg in DEMO_STEPS:
        print(f"  -> {label}: relative B{deg:+.1f}", end="")
        ok = link.write_base_relative(deg, wait=verify)
        ack = link._last_base_ack if verify else None
        if not ok:
            print(" — failed")
            sys.exit(1)
        if verify:
            if ack is None:
                print(" — no ACK")
            else:
                print(f" → ACK B{ack:.1f}")
        else:
            print()
        time.sleep(hold_sec)


def main() -> int:
    parser = argparse.ArgumentParser(description="Test base motor via ESP32 USB serial (v5)")
    parser.add_argument("--port", default="", help="Serial port")
    parser.add_argument("--baud", type=int, default=115200)
    parser.add_argument("--relative", type=float, default=None, help="Relative degrees (B+/-)")
    parser.add_argument("--combined", action="store_true", help="With --pan/--tilt/--relative")
    parser.add_argument("--pan", type=float, default=40.0)
    parser.add_argument("--tilt", type=float, default=105.0)
    parser.add_argument("--hold", type=float, default=DEFAULT_HOLD_SEC)
    parser.add_argument("--verify", action="store_true", help="Wait for OK B ACK")
    parser.add_argument("--calibrate", action="store_true", help="Motor-driven calibrate (legacy)")
    parser.add_argument("--calibrate-manual", action="store_true", help="Hand-rotate base, measure encoder")
    parser.add_argument("--degrees", type=float, default=90.0)
    parser.add_argument("--write-config", action="store_true")
    parser.add_argument("--apply-config-cpd", action="store_true")
    parser.add_argument("--no-config-cpd", action="store_true")
    parser.add_argument("--cal-deg", type=float, default=360.0)
    parser.add_argument("--zero", action="store_true")
    parser.add_argument("--status", action="store_true")
    parser.add_argument("--watch", action="store_true", help="Live encoder via ? status")
    parser.add_argument("--jog", type=int, default=None, help="Open-loop base jog PWM (-130..130)")
    parser.add_argument("--jog-ms", type=int, default=250, help="Open-loop jog duration, capped in firmware")
    parser.add_argument("--demo", action="store_true")
    parser.add_argument(
        "--correct-move",
        nargs=2,
        type=float,
        metavar=("COMMANDED", "ACTUAL"),
        help="Set command_scale from measured move (e.g. 30 200 if B+30 moved 200°)",
    )
    args = parser.parse_args()

    link = ArduinoServoLink(port=args.port, baud=args.baud)
    link.base_move_timeout_sec = load_move_timeout()
    configure_base_link(link)

    if not link.connect():
        print("Failed to connect. Check USB, dialout, and firmware READY.")
        return 1

    try:
        if needs_config_cpd_on_connect(args):
            apply_base_calibration_to_nano(link)
            if link.base_command_scale != 1.0:
                print(f"Base command_scale {link.base_command_scale:.4f} (plate vs encoder)")
            
            if load_zero_on_start():
                link.zero_base()
                print("Auto-zeroed base from config (zero_on_start=true)")

        if args.correct_move:
            commanded, actual = args.correct_move
            if commanded <= 0 or actual <= 0:
                print("ERROR: --correct-move values must be positive degrees.")
                return 1
            scale = max(0.01, min(2.0, correct_command_scale(commanded, actual)))
            configure_base_link(link)
            link.base_command_scale = scale
            write_command_scale_to_config(scale)
            print(
                f"command_scale set to {scale:.4f} "
                f"(commanded {commanded:.0f}° → measured {actual:.0f}° on base plate)"
            )
            print(f"Wrote {CONFIG_PATH}")
            return 0

        if args.zero:
            link.zero_base()
            print("Zero sent (Z).")
        if args.status:
            st = link.query_status()
            if st:
                print(
                    f"POS {st.encoder_count} DEG {st.degrees:.2f} "
                    f"CPD {st.counts_per_degree:.3f} BUSY {int(st.busy)}"
                )
            else:
                print("No status response.")
        if args.watch:
            run_watch(link)
        elif args.jog is not None:
            st0 = link.query_status()
            print(f"Open-loop jog J{args.jog:+d} M{args.jog_ms}")
            ok = link.write_base_jog(args.jog, args.jog_ms)
            time.sleep((max(1, args.jog_ms) / 1000.0) + 0.2)
            st1 = link.query_status()
            if not ok:
                print("  → jog command was not acknowledged")
            if st0 is not None and st1 is not None:
                print(
                    f"  → encoder delta {st1.encoder_count - st0.encoder_count} "
                    f"({st1.degrees - st0.degrees:+.2f}°)"
                )
        elif args.calibrate_manual:
            run_calibrate_manual(link, args.degrees, write_config=args.write_config)
        elif args.calibrate:
            run_calibrate(link, cal_deg=args.cal_deg)
        elif args.combined:
            warn_if_uncalibrated(link)
            rel = args.relative if args.relative is not None else 30.0
            print(f"Combined P{args.pan:.1f} T{args.tilt:.1f} B{rel:+.1f}")
            link.write_combined(args.pan, args.tilt, rel, wait_servo=True, wait_base=args.verify)
            if args.verify and link._last_base_ack is not None:
                print(f"  → ACK B{link._last_base_ack:.1f}")
            time.sleep(args.hold)
        elif args.relative is not None:
            warn_if_uncalibrated(link)
            st0 = link.query_status()
            print(f"Relative B{args.relative:+.1f}")
            link.write_base_relative(args.relative, wait=args.verify)
            st1 = link.query_status()
            if args.verify and link._last_base_ack is not None:
                print(f"  → ACK B{link._last_base_ack:.1f}")
                if st1 is not None:
                    print(
                        f"  → encoder POS {st1.encoder_count} "
                        f"(reported {st1.degrees:.1f}°; plate angle is ~command_scale × that)"
                    )
            elif args.verify:
                print("  → no base ACK (timeout or lost)")
            if st0 is not None and st1 is not None:
                delta_counts = st1.encoder_count - st0.encoder_count
                print(f"  → encoder delta {delta_counts:+d} counts")
                print_stall_diagnosis(delta_counts)
            time.sleep(args.hold)
        elif args.demo:
            run_demo(link, verify=args.verify, hold_sec=args.hold)
        elif not any([args.zero, args.status]):
            print(
                "Nothing to do. Try --watch, --calibrate-manual, --relative, "
                "--jog, --demo, or --calibrate."
            )
            return 1
        print("Done.")
    except KeyboardInterrupt:
        print("\nInterrupted.")
    finally:
        link.close()

    return 0


if __name__ == "__main__":
    sys.exit(main())
