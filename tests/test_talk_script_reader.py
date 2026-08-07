#!/usr/bin/env python3
"""
Test Talk Script Reader - Simulates or plays voice script reading while triggering Talking Hand gestures.

Usage:
    python tests/test_talk_script_reader.py --text "Hello! I am your robot assistant. How can I help you today?"
    python tests/test_talk_script_reader.py --file script.txt
"""

from __future__ import annotations

import _bootstrap  # noqa: F401
import argparse
import pathlib
import sys
import threading
import time

from core.blackboard import Blackboard
from core.talk_gesture_service import TalkGestureService
from voice.speaking_flag import write_speaking_flag, clear_speaking_flag


def estimate_speech_duration(text: str, words_per_minute: int = 150) -> float:
    """Estimate speech duration in seconds based on word count."""
    words = len(text.split())
    if words == 0:
        return 1.0
    return max(2.0, (words / words_per_minute) * 60.0)


def main() -> None:
    parser = argparse.ArgumentParser(description="Test Talking Hand Gestures with Voice Script Reading")
    parser.add_argument(
        "--text",
        type=str,
        default="Welcome! I am fully equipped with dynamic talking hand gestures. Watch my arms animate naturally while I read out this voice script.",
        help="Text script to read out",
    )
    parser.add_argument("--file", type=pathlib.Path, help="Path to text file script to read")
    parser.add_argument("--wpm", type=int, default=150, help="Speech pace in words per minute (default: 150)")
    parser.add_argument("--duration", type=float, help="Explicit speech duration in seconds (overrides WPM estimation)")
    parser.add_argument("--presets", type=pathlib.Path, help="Path to arm_pose_presets.json")

    args = parser.parse_args()

    script_text = args.text
    if args.file and args.file.exists():
        script_text = args.file.read_text(encoding="utf-8").strip()

    app_dir = pathlib.Path(__file__).resolve().parent.parent
    presets_path = args.presets or (app_dir / "tests" / "arm_pose_presets.json")

    if not presets_path.exists():
        print(f"[ERROR] Presets file not found at {presets_path}")
        sys.exit(1)

    print("=" * 60)
    print("[TEST] TALKING HAND SCRIPT READER TEST")
    print("=" * 60)
    print(f"Script: {script_text}\n")

    # Initialize shared Blackboard
    bb = Blackboard()
    bb.write(running=True, agent_speaking=False, bye_wave_active=False, animation_override=False)

    # Initialize TalkGestureService
    gesture_service = TalkGestureService(
        bb=bb,
        presets_path=presets_path,
        pose_duration=0.4,
        vertical_speed=1.0,
        horizontal_speed=1.5,
    )

    # Start gesture service in daemon thread
    service_thread = threading.Thread(target=gesture_service.run, daemon=True)
    service_thread.start()

    time.sleep(0.5)  # Allow service thread to start

    duration = args.duration or estimate_speech_duration(script_text, wpm=args.wpm)
    print(f"[AUDIO] Starting speech output (Estimated Duration: {duration:.1f} seconds)...")

    # 1. Signal Speech Start
    write_speaking_flag(True)
    bb.write(agent_speaking=True, conv_state="speaking")

    start_time = time.time()
    try:
        while time.time() - start_time < duration:
            elapsed = time.time() - start_time
            arm_state = bb.read("arm_a0", "arm_a1", "arm_a2", "arm_a3", "talk_gesture_active")
            print(
                f"\r[{elapsed:4.1f}s / {duration:4.1f}s] "
                f"Active: {arm_state.get('talk_gesture_active')} | "
                f"Arms: a0={arm_state.get('arm_a0', 0):.1f} deg "
                f"a1={arm_state.get('arm_a1', 0):.1f} deg "
                f"a2={arm_state.get('arm_a2', 0):.1f} deg "
                f"a3={arm_state.get('arm_a3', 0):.1f} deg",
                end="",
                flush=True,
            )
            time.sleep(0.1)

    except KeyboardInterrupt:
        print("\n[INFO] Stopped by user.")
    finally:
        # 2. Signal Speech Stop
        print("\n\n[STOP] Speech finished. Signaling agent_speaking=False...")
        write_speaking_flag(False)
        bb.write(agent_speaking=False, conv_state="waiting")

        print("[HOME] Waiting for arms to return home...")
        time.sleep(1.5)

        bb.write(running=False)
        clear_speaking_flag()
        print("[SUCCESS] Cleanup complete.")


if __name__ == "__main__":
    main()
