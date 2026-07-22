#!/usr/bin/env python3
"""
Interactive Botango animation playback for voice-agentv5.

Uses AnimationCommands.json from voice-agentv4 (or --json), proper Bottango
bezier sampling (lib/botango_loader.py), and arm safety from
tests/captured_arm_limits.json.

  cd voice-agentv5/tests && python test_botango_playback.py
  python test_botango_playback.py --play 1
  python test_botango_playback.py --list
  python test_botango_playback.py --dry-run --play 2
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

import _bootstrap  # noqa: F401

from arm_safety_envelope import ArmSafetyEnvelope, DEFAULT_LIMITS_PATH
from arduino_servo import ArduinoServoLink
from lib.botango_loader import (
    find_animation_commands_json,
    format_servo_stop_pose,
    servo_stop_pose,
)
from lib.botango_playback import (
    build_player_from_json,
    default_arm_home,
    load_playback_limits,
    play_clip,
    smooth_home_pose,
)
from test_servo_manual import load_servo_cfg


def print_menu(clips: list) -> None:
    print("\nAnimations (from AnimationCommands.json):")
    print("-" * 56)
    for i, clip in enumerate(clips, start=1):
        tracks = ", ".join(tr.servo for tr in clip.tracks)
        print(f"  {i:2d}. {clip.clip_id:<22} ({clip.duration_ms / 1000:.1f}s) [{tracks}]")
    print("-" * 56)
    print("  Enter number to play | r = replay | h = home | l = list | q = quit")


def main() -> int:
    parser = argparse.ArgumentParser(description="Botango animation playback (v5)")
    parser.add_argument("--json", default="", help="Path to AnimationCommands.json")
    parser.add_argument("--clip", default="", help="Clip name to play")
    parser.add_argument("--play", type=int, default=0, help="Play clip by number (1-based)")
    parser.add_argument("--seconds", type=float, default=0.0, help="Unused; clip runs to end")
    parser.add_argument("--hz", type=float, default=30.0, help="Playback sample rate")
    parser.add_argument("--list", action="store_true", help="List clips and exit")
    parser.add_argument("--all", action="store_true", help="Play all clips sequentially")
    parser.add_argument("--loop", action="store_true", help="Loop selected clip")
    parser.add_argument("--port", default="", help="ESP32 serial port")
    parser.add_argument("--baud", type=int, default=115200)
    parser.add_argument("--limits", default=str(DEFAULT_LIMITS_PATH), help="captured_arm_limits.json")
    parser.add_argument("--dry-run", action="store_true", help="Print poses only, no serial")
    args = parser.parse_args()

    try:
        json_path = find_animation_commands_json(args.json)
    except FileNotFoundError as e:
        print(f"Error: {e}")
        return 1

    player, clips = build_player_from_json(json_path)
    if not clips:
        print(f"No clips found in {json_path}")
        return 1

    limits = load_playback_limits()
    try:
        envelope = ArmSafetyEnvelope.from_json(args.limits)
        arm_home = dict(zip(("arm_0", "arm_1", "arm_2", "arm_3"), envelope.homes))
    except FileNotFoundError:
        print(f"Warning: {args.limits} missing — using Botango JSON arm neutrals")
        envelope = None
        arm_home = default_arm_home(json_path)

    pan_home = limits.pan_home
    tilt_home = limits.tilt_home

    print(f"Loaded {len(clips)} clip(s) from {json_path}")
    print(format_servo_stop_pose(servo_stop_pose(arm_home)))
    print_menu(clips)

    if args.list:
        return 0

    servo = load_servo_cfg()
    port = args.port or str(servo.get("port") or "")
    baud = args.baud or int(servo.get("baud", 115200))

    link: ArduinoServoLink | None = None
    if not args.dry_run:
        link = ArduinoServoLink(port=port, baud=baud)
        if not link.connect():
            print("Robot connection failed; continuing print-only.")
            link = None
        elif not link.has_arm_firmware():
            print("Warning: ESP32 missing arm firmware — arms may not move.")
            if link.arm_firmware_hint():
                print(link.arm_firmware_hint())
        else:
            smooth_home_pose(
                link,
                pan_home=pan_home,
                tilt_home=tilt_home,
                arm_home=arm_home,
                envelope=envelope,
                limits=limits,
            )
            print(f"Robot ready at pan={pan_home:.1f} tilt={tilt_home:.1f}")

    def _play_one(clip_id: str, *, loop: bool = False) -> None:
        play_clip(
            link,
            player,
            clip_id,
            pan_home=pan_home,
            tilt_home=tilt_home,
            arm_home=arm_home,
            limits=limits,
            envelope=envelope,
            loop=loop,
            hz=args.hz,
            verbose=True,
        )
        if link is not None:
            smooth_home_pose(
                link,
                pan_home=pan_home,
                tilt_home=tilt_home,
                arm_home=arm_home,
                envelope=envelope,
                limits=limits,
                hz=args.hz,
            )

    try:
        if args.all:
            for clip in clips:
                _play_one(clip.clip_id)
            return 0

        if args.play:
            if args.play < 1 or args.play > len(clips):
                print(f"Invalid --play {args.play}; choose 1-{len(clips)}")
                return 1
            _play_one(clips[args.play - 1].clip_id, loop=args.loop)
            return 0

        if args.clip:
            names = {c.clip_id for c in clips}
            if args.clip not in names:
                print(f"Unknown clip {args.clip!r}. Available: {', '.join(sorted(names))}")
                return 1
            _play_one(args.clip, loop=args.loop)
            return 0

        if not sys.stdin.isatty():
            print("Interactive mode needs a terminal, or use --play N / --clip NAME")
            return 1

        last_idx = 0
        while True:
            try:
                raw = input("\n> ").strip()
            except (EOFError, KeyboardInterrupt):
                print()
                break
            if not raw:
                continue
            lower = raw.lower()
            if lower in ("q", "quit", "exit"):
                break
            if lower in ("l", "list"):
                print_menu(clips)
                continue
            if lower in ("h", "home"):
                if link is not None:
                    smooth_home_pose(
                        link,
                        pan_home=pan_home,
                        tilt_home=tilt_home,
                        arm_home=arm_home,
                        envelope=envelope,
                        limits=limits,
                        hz=args.hz,
                    )
                continue
            if lower in ("r", "replay") and last_idx:
                _play_one(clips[last_idx - 1].clip_id)
                continue
            if raw.isdigit():
                idx = int(raw)
                if idx < 1 or idx > len(clips):
                    print(f"Choose 1-{len(clips)}")
                    continue
                last_idx = idx
                _play_one(clips[idx - 1].clip_id)
                continue
            if raw in {c.clip_id for c in clips}:
                _play_one(raw)
                continue
            print("Enter a number, clip name, h, l, r, or q")
    except KeyboardInterrupt:
        print("\nInterrupted.")
    finally:
        if link is not None:
            smooth_home_pose(
                link,
                pan_home=pan_home,
                tilt_home=tilt_home,
                arm_home=arm_home,
                envelope=envelope,
                limits=limits,
                hz=args.hz,
            )
            link.close(
                home_pan=pan_home,
                home_tilt=tilt_home,
                home_arm0=arm_home["arm_0"],
                home_arm1=arm_home["arm_1"],
                home_arm2=arm_home["arm_2"],
                home_arm3=arm_home["arm_3"],
            )

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
