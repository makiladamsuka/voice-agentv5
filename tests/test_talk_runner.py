#!/usr/bin/env python3
"""
Talk Pose Runner — Randomly switches between talk poses while voice agent speaks.

Monitors the Blackboard ``agent_speaking`` flag and continuously cycles through
random talk poses from ``tests/arm_pose_presets.json`` while the voice agent is
speaking. Stops immediately when the agent finishes.

How to run::

    python tests/test_talk_runner.py

Requirements::

    - Blackboard must be running (start_robot.py or standalone)
    - voice_service.py must be writing agent_speaking flag
    - arm_pose_presets.json must contain talk poses (talk1, talk2, ...)
"""

from __future__ import annotations

import _bootstrap  # noqa: F401
import argparse
import json
import pathlib
import random
import sys
import threading
import time

from core.blackboard import Blackboard


class TalkAnimationRunner:
    """Monitors agent_speaking and randomly switches between talk poses.
    
    When the voice agent starts speaking (``agent_speaking=True`` on Blackboard),
    this runner randomly selects talk poses from the presets file and continuously
    switches between them to create a "talking" animation effect. Stops immediately
    when ``agent_speaking`` becomes False.
    
    Parameters
    ----------
    bb:
        Blackboard instance to read ``agent_speaking`` and write arm poses.
    presets_path:
        Path to arm_pose_presets.json containing talk poses (talk1, talk2, ...).
    pose_duration:
        Seconds to hold each random talk pose before switching (default 0.5).
    poll_interval:
        Seconds between checking ``agent_speaking`` flag (default 0.05).
    """

    def __init__(
        self,
        bb: Blackboard,
        presets_path: pathlib.Path,
        pose_duration: float = 0.5,
        poll_interval: float = 0.05,
    ) -> None:
        self._bb = bb
        self._presets_path = presets_path
        self._pose_duration = pose_duration
        self._poll_interval = poll_interval
        self._running = False
        self._thread: threading.Thread | None = None
        self._talk_pose_keys: list[str] = []
        self._poses: dict = {}
        
        # Load poses once at init
        self._load_talk_poses()

    def _load_talk_poses(self) -> None:
        """Load all talk* poses from the presets file."""
        try:
            data = json.loads(self._presets_path.read_text())
            self._poses = data.get("poses", {})
            
            # Find all pose keys that start with "talk"
            self._talk_pose_keys = [
                key for key in self._poses.keys() 
                if key.startswith("talk")
            ]
            
            if not self._talk_pose_keys:
                print(
                    "[WARN] No talk poses found in presets file. "
                    "Add poses named 'talk1', 'talk2', etc.",
                    file=sys.stderr,
                )
            else:
                print(f"[INFO] Loaded {len(self._talk_pose_keys)} talk poses: {', '.join(self._talk_pose_keys)}")
                
        except Exception as exc:
            print(
                f"[ERROR] Failed to load talk poses: {exc}",
                file=sys.stderr,
            )
            self._talk_pose_keys = []

    def _apply_pose(self, pose: dict) -> None:
        """Apply a single pose to the arm."""
        self._bb.write(
            arm_a0=pose["a0"],
            arm_a1=pose["a1"],
            arm_a2=pose["a2"],
            arm_a3=pose["a3"],
        )

    def _run_loop(self) -> None:
        """Main loop: continuously switch between random talk poses while agent speaks."""
        print("[INFO] Talk pose loop started")
        
        while self._running:
            # Wait for agent to start speaking
            while self._running and not self._bb.read().get("agent_speaking", False):
                time.sleep(self._poll_interval)
            
            if not self._running:
                break
                
            print("[INFO] Agent started speaking — triggering talk poses")
            
            # Agent is speaking — continuously switch between random talk poses
            while self._running and self._bb.read().get("agent_speaking", False):
                if not self._talk_pose_keys:
                    # No talk poses available — just wait
                    time.sleep(self._poll_interval)
                    continue
                
                # Pick a random talk pose
                pose_key = random.choice(self._talk_pose_keys)
                pose = self._poses[pose_key]
                
                # Apply the pose
                self._apply_pose(pose)
                
                # Hold the pose for the specified duration (or until agent stops speaking)
                start = time.time()
                while time.time() - start < self._pose_duration:
                    if not self._bb.read().get("agent_speaking", False):
                        print("[INFO] Agent stopped speaking")
                        break
                    time.sleep(self._poll_interval)
            
            if self._bb.read().get("agent_speaking", False) == False:
                print("[INFO] Agent finished speaking")
        
        print("[INFO] Talk pose loop stopped")

    def start(self) -> None:
        """Start the background monitoring thread."""
        if self._running:
            print("[WARN] Talk animation runner already running", file=sys.stderr)
            return
        
        self._running = True
        self._thread = threading.Thread(target=self._run_loop, daemon=True)
        self._thread.start()
        print("[INFO] Talk animation runner started")

    def stop(self) -> None:
        """Stop the background thread."""
        if not self._running:
            return
        
        self._running = False
        if self._thread is not None:
            self._thread.join(timeout=2.0)
        
        # Return arm to home position
        self._bb.write(arm_a0=47.0, arm_a1=65.0, arm_a2=64.0, arm_a3=87.0)
        print("[INFO] Talk animation runner stopped")


def main() -> None:
    """Run the talk pose runner standalone."""
    parser = argparse.ArgumentParser(
        description="Talk Pose Runner — random arm poses while voice agent speaks",
    )
    parser.add_argument(
        "--presets",
        type=pathlib.Path,
        default=pathlib.Path(__file__).parent / "arm_pose_presets.json",
        help="Path to arm_pose_presets.json (default: tests/arm_pose_presets.json)",
    )
    parser.add_argument(
        "--frame-delay",
        type=float,
        default=0.5,
        help="Seconds to hold each talk pose before switching (default: 0.5)",
    )
    parser.add_argument(
        "--poll-interval",
        type=float,
        default=0.05,
        help="Seconds between checking agent_speaking flag (default: 0.05)",
    )

    args = parser.parse_args()

    # Validate presets file exists
    if not args.presets.exists():
        print(f"[ERROR] Presets file not found: {args.presets}", file=sys.stderr)
        sys.exit(1)

    # Initialize Blackboard
    try:
        bb = Blackboard()
        print("[INFO] Blackboard connected")
    except Exception as exc:
        print(f"[ERROR] Failed to connect to Blackboard: {exc}", file=sys.stderr)
        sys.exit(1)

    # Create and start runner
    runner = TalkAnimationRunner(
        bb=bb,
        presets_path=args.presets,
        pose_duration=args.frame_delay,
        poll_interval=args.poll_interval,
    )
    
    try:
        runner.start()
        print("[INFO] Monitoring agent_speaking flag... Press Ctrl+C to stop")
        print("[INFO] Talk poses will switch randomly when voice agent speaks")
        
        # Keep main thread alive
        while True:
            time.sleep(1.0)
            
    except KeyboardInterrupt:
        print("\n[INFO] Shutting down...")
    finally:
        runner.stop()


if __name__ == "__main__":
    main()
