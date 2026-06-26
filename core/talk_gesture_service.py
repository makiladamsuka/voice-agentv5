"""TalkGestureService - Animate arms while voice agent speaks.

Monitors the agent_speaking flag (via file) and randomly cycles through
talk poses to create a natural "talking with hands" animation effect.
"""

from __future__ import annotations

import json
import pathlib
import random
import time

from core.blackboard import Blackboard
from voice.speaking_flag import read_speaking_flag


class TalkGestureService:
    """Animates arms with talk poses while voice agent speaks.
    
    Runs in its own daemon thread alongside other robot services.
    Reads agent_speaking flag from file (/tmp/voice_agent_speaking.json)
    written by VoiceService.
    
    Parameters
    ----------
    bb:
        Blackboard instance to write arm poses.
    presets_path:
        Path to arm_pose_presets.json containing talk poses.
    pose_duration:
        Seconds to hold each random talk pose (default 0.5).
    poll_interval:
        Seconds between checking agent_speaking flag (default 0.05).
    """

    def __init__(
        self,
        bb: Blackboard,
        presets_path: pathlib.Path,
        pose_duration: float = 0.5,
        poll_interval: float = 0.05,
    ) -> None:
        self.bb = bb
        self.presets_path = presets_path
        self.pose_duration = pose_duration
        self.poll_interval = poll_interval
        self._talk_pose_keys: list[str] = []
        self._poses: dict = {}
        
        # Load talk poses on init
        self._load_talk_poses()

    def _load_talk_poses(self) -> None:
        """Load all talk* poses from the presets file."""
        try:
            data = json.loads(self.presets_path.read_text())
            self._poses = data.get("poses", {})
            
            # Find all pose keys that start with "talk"
            self._talk_pose_keys = [
                key for key in self._poses.keys() 
                if key.startswith("talk")
            ]
            
            if self._talk_pose_keys:
                print(
                    f"[TalkGesture] Loaded {len(self._talk_pose_keys)} talk poses: "
                    f"{', '.join(self._talk_pose_keys)}"
                )
            else:
                print("[TalkGesture] WARNING: No talk poses found in presets file")
                
        except Exception as exc:
            print(f"[TalkGesture] ERROR loading talk poses: {exc}")
            self._talk_pose_keys = []

    def _apply_pose(self, pose: dict) -> None:
        """Apply a single pose to the arms via Blackboard."""
        self.bb.write(
            arm_a0=pose["a0"],
            arm_a1=pose["a1"],
            arm_a2=pose["a2"],
            arm_a3=pose["a3"],
        )

    def run(self) -> None:
        """Main loop: continuously switch between random talk poses while agent speaks."""
        print("[TalkGesture] Service started")
        
        if not self._talk_pose_keys:
            print("[TalkGesture] No talk poses available - service disabled")
            return
        
        last_speaking = False
        
        while self.bb.read("running")["running"]:
            # Read current agent_speaking state from file
            is_speaking = read_speaking_flag()
            
            # Log state changes
            if is_speaking and not last_speaking:
                print("[TalkGesture] Agent started speaking")
            elif not is_speaking and last_speaking:
                print("[TalkGesture] Agent stopped speaking")
            
            last_speaking = is_speaking
            
            # Only play poses while speaking
            if not is_speaking:
                time.sleep(self.poll_interval)
                continue
            
            # Pick a random talk pose
            pose_key = random.choice(self._talk_pose_keys)
            pose = self._poses[pose_key]
            
            # Apply the pose
            self._apply_pose(pose)
            
            # Hold the pose for the specified duration (or until agent stops speaking)
            start = time.time()
            while time.time() - start < self.pose_duration:
                is_speaking = read_speaking_flag()
                if not is_speaking:
                    break
                time.sleep(self.poll_interval)
                
                # Also check if robot is shutting down
                if not self.bb.read("running")["running"]:
                    break
        
        print("[TalkGesture] Service stopped")
