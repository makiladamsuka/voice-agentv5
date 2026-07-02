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
        vertical_speed: float = 1.0,
        horizontal_speed: float = 1.5,
    ) -> None:
        self.bb = bb
        self.presets_path = presets_path
        self.pose_duration = pose_duration
        self.poll_interval = poll_interval
        self.vertical_speed = vertical_speed  # Speed for a0, a1 (up/down)
        self.horizontal_speed = horizontal_speed  # Speed for a2, a3 (swap)
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
    
    def _apply_pose_smooth(self, target_pose: dict, duration: float) -> None:
        """Smoothly interpolate to target pose with different speeds per motor group.
        
        Uses high-frequency updates (100Hz) and easing for continuous smooth motion.
        
        Args:
            target_pose: Target pose dictionary with a0, a1, a2, a3 keys.
            duration: Total duration for the movement in seconds.
        """
        # Read current arm positions
        current = self.bb.read("arm_a0", "arm_a1", "arm_a2", "arm_a3")
        start_a0 = current["arm_a0"]
        start_a1 = current["arm_a1"]
        start_a2 = current["arm_a2"]
        start_a3 = current["arm_a3"]
        
        target_a0 = target_pose["a0"]
        target_a1 = target_pose["a1"]
        target_a2 = target_pose["a2"]
        target_a3 = target_pose["a3"]
        
        # Calculate deltas
        delta_a0 = target_a0 - start_a0
        delta_a1 = target_a1 - start_a1
        delta_a2 = target_a2 - start_a2
        delta_a3 = target_a3 - start_a3
        
        # Calculate durations based on speed (inverse relationship)
        vertical_duration = duration / max(0.1, self.vertical_speed)
        horizontal_duration = duration / max(0.1, self.horizontal_speed)
        
        # Use longer duration for frame timing
        frame_duration = max(vertical_duration, horizontal_duration)
        
        poll_interval = 0.01  # 100 Hz for smooth continuous motion
        start_time = time.time()
        
        while True:
            elapsed = time.time() - start_time
            if elapsed >= frame_duration:
                break
            
            # Calculate progress for each motor group
            vertical_t = min(1.0, elapsed / vertical_duration)
            horizontal_t = min(1.0, elapsed / horizontal_duration)
            
            # Apply easing for smooth motion
            vertical_progress = self._ease_in_out(vertical_t)
            horizontal_progress = self._ease_in_out(horizontal_t)
            
            # Interpolate with different speeds
            new_a0 = start_a0 + delta_a0 * vertical_progress
            new_a1 = start_a1 + delta_a1 * vertical_progress
            new_a2 = start_a2 + delta_a2 * horizontal_progress
            new_a3 = start_a3 + delta_a3 * horizontal_progress
            
            # Write interpolated position
            self.bb.write(
                arm_a0=new_a0,
                arm_a1=new_a1,
                arm_a2=new_a2,
                arm_a3=new_a3,
            )
            
            # Check if agent stopped speaking
            if not read_speaking_flag():
                break
            
            time.sleep(poll_interval)
        
        # Ensure we reach the target
        self.bb.write(
            arm_a0=target_a0,
            arm_a1=target_a1,
            arm_a2=target_a2,
            arm_a3=target_a3,
        )
    
    def _ease_in_out(self, t):
        """Smooth ease-in-out function for continuous motion.
        
        Uses cubic easing for smooth acceleration and deceleration.
        """
        if t < 0.5:
            return 4 * t * t * t
        else:
            return 1 - pow(-2 * t + 2, 3) / 2

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
            
            # Apply the pose with smooth interpolation (different speeds for vertical/horizontal)
            self._apply_pose_smooth(pose, self.pose_duration)
            
            # No additional hold needed - interpolation takes pose_duration
        
        print("[TalkGesture] Service stopped")
