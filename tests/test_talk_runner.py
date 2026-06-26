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
        debug: bool = False,
    ) -> None:
        self._bb = bb
        self._presets_path = presets_path
        self._pose_duration = pose_duration
        self._poll_interval = poll_interval
        self._debug = debug
        self._running = False
        self._thread: threading.Thread | None = None
        self._talk_pose_keys: list[str] = []
        self._poses: dict = {}
        
        # Load poses once at init
        self._load_talk_poses()
        
        # Test Blackboard connection
        print("[DEBUG] Testing Blackboard connection...")
        try:
            bb_data = self._bb.read()
            agent_speaking = bb_data.get("agent_speaking", None)
            print(f"[DEBUG] Blackboard read successful. Current agent_speaking = {agent_speaking}")
            print(f"[DEBUG] Blackboard keys available: {list(bb_data.keys())[:10]}...")  # Show first 10 keys
        except Exception as exc:
            print(f"[ERROR] Blackboard read failed: {exc}", file=sys.stderr)

    def _load_talk_poses(self) -> None:
        """Load all talk* poses from the presets file."""
        print(f"[DEBUG] Loading talk poses from: {self._presets_path}")
        try:
            data = json.loads(self._presets_path.read_text())
            self._poses = data.get("poses", {})
            
            print(f"[DEBUG] Total poses in file: {len(self._poses)}")
            print(f"[DEBUG] Available pose keys: {list(self._poses.keys())}")
            
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
                print(f"[INFO] ✓ Loaded {len(self._talk_pose_keys)} talk poses: {', '.join(self._talk_pose_keys)}")
                for key in self._talk_pose_keys:
                    pose = self._poses[key]
                    print(f"[DEBUG]   {key}: a0={pose['a0']}, a1={pose['a1']}, a2={pose['a2']}, a3={pose['a3']}")
                
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
        print("[DEBUG] Starting to monitor agent_speaking flag...")
        
        last_speaking_state = None
        
        while self._running:
            # Read current state
            bb_data = self._bb.read()
            agent_speaking = bb_data.get("agent_speaking", False)
            
            # Debug: Log state changes
            if agent_speaking != last_speaking_state:
                print(f"[DEBUG] agent_speaking flag changed: {last_speaking_state} -> {agent_speaking}")
                last_speaking_state = agent_speaking
            
            # Wait for agent to start speaking
            if not agent_speaking:
                time.sleep(self._poll_interval)
                continue
            
            if not self._running:
                break
                
            print("[INFO] ✓ Agent started speaking — triggering talk poses")
            
            # Agent is speaking — continuously switch between random talk poses
            pose_count = 0
            while self._running:
                bb_data = self._bb.read()
                agent_speaking = bb_data.get("agent_speaking", False)
                
                if not agent_speaking:
                    print(f"[INFO] ✓ Agent stopped speaking (played {pose_count} poses)")
                    break
                
                if not self._talk_pose_keys:
                    # No talk poses available — just wait
                    print("[WARN] No talk poses available")
                    time.sleep(self._poll_interval)
                    continue
                
                # Pick a random talk pose
                pose_key = random.choice(self._talk_pose_keys)
                pose = self._poses[pose_key]
                
                # Apply the pose
                print(f"[DEBUG] Playing pose: {pose_key} (a0={pose['a0']:.1f}, a1={pose['a1']:.1f}, a2={pose['a2']:.1f}, a3={pose['a3']:.1f})")
                self._apply_pose(pose)
                pose_count += 1
                
                # Hold the pose for the specified duration (or until agent stops speaking)
                start = time.time()
                while time.time() - start < self._pose_duration:
                    bb_data = self._bb.read()
                    if not bb_data.get("agent_speaking", False):
                        print(f"[DEBUG] Agent stopped mid-pose (pose {pose_count})")
                        break
                    time.sleep(self._poll_interval)
        
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
    parser.add_argument(
        "--debug",
        action="store_true",
        help="Enable verbose debug logging",
    )

    args = parser.parse_args()

    # Validate presets file exists
    if not args.presets.exists():
        print(f"[ERROR] Presets file not found: {args.presets}", file=sys.stderr)
        sys.exit(1)
    
    print(f"[DEBUG] Using presets file: {args.presets}")
    print(f"[DEBUG] Pose duration: {args.frame_delay}s")
    print(f"[DEBUG] Poll interval: {args.poll_interval}s")
    print(f"[DEBUG] Debug mode: {args.debug}")

    # Initialize Blackboard
    print("[DEBUG] Connecting to Blackboard...")
    try:
        bb = Blackboard()
        print("[INFO] ✓ Blackboard connected successfully")
    except Exception as exc:
        print(f"[ERROR] Failed to connect to Blackboard: {exc}", file=sys.stderr)
        print("[ERROR] Make sure start_robot.py is running on the Raspberry Pi", file=sys.stderr)
        sys.exit(1)

    # Create and start runner
    print("[DEBUG] Creating TalkAnimationRunner...")
    runner = TalkAnimationRunner(
        bb=bb,
        presets_path=args.presets,
        pose_duration=args.frame_delay,
        poll_interval=args.poll_interval,
        debug=args.debug,
    )
    
    try:
        print("[DEBUG] Starting runner thread...")
        runner.start()
        print("[INFO] ✓ Talk pose runner started successfully")
        print("[INFO] Monitoring agent_speaking flag... Press Ctrl+C to stop")
        print("[INFO] Talk poses will switch randomly when voice agent speaks")
        print()
        print("=" * 60)
        print("WAITING FOR AGENT TO SPEAK...")
        print("=" * 60)
        
        # Keep main thread alive and show periodic status
        check_count = 0
        while True:
            time.sleep(5.0)
            check_count += 1
            bb_data = bb.read()
            agent_speaking = bb_data.get("agent_speaking", False)
            print(f"[STATUS] Check #{check_count}: agent_speaking = {agent_speaking}")
            
    except KeyboardInterrupt:
        print("\n[INFO] Shutting down...")
    finally:
        runner.stop()


if __name__ == "__main__":
    main()
