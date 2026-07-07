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

# Module-level reference to active service instance
_active_talk_gesture_service: "TalkGestureService | None" = None


def get_active_service() -> "TalkGestureService | None":
    """Get the currently running TalkGestureService instance."""
    return _active_talk_gesture_service


def return_to_home_position() -> None:
    """Module-level function to return arms to home position.
    
    Can be called from anywhere (e.g., voice service) to reset arms.
    Safe to call even if service is not running.
    """
    global _active_talk_gesture_service
    if _active_talk_gesture_service is not None:
        _active_talk_gesture_service.return_home_now()
    else:
        print("[TalkGesture] No active service - cannot return to home")


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
        return_home_delay: float = 3.0,  # Delay before returning home after speaking stops
    ) -> None:
        self.bb = bb
        self.presets_path = presets_path
        self.pose_duration = pose_duration
        self.poll_interval = poll_interval
        self.vertical_speed = vertical_speed  # Speed for a0, a1 (up/down)
        self.horizontal_speed = horizontal_speed  # Speed for a2, a3 (swap)
        self.return_home_delay = return_home_delay  # Wait time before returning home
        self._talk_pose_keys: list[str] = []
        self._poses: dict = {}
        self._last_pose_key: str | None = None  # Track last played pose
        self._speaking_stopped_time: float | None = None  # Track when speaking stopped
        
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

    def _get_next_random_pose(self) -> str:
        """Select a random pose key that's different from the last played pose.
        
        Returns
        -------
        str
            A random pose key from available talk poses, guaranteed to be
            different from the last played pose (if there are 2+ poses available).
        """
        # If only one pose available, return it
        if len(self._talk_pose_keys) <= 1:
            return self._talk_pose_keys[0] if self._talk_pose_keys else ""
        
        # Filter out the last played pose
        available_poses = [
            key for key in self._talk_pose_keys 
            if key != self._last_pose_key
        ]
        
        # Pick randomly from remaining poses
        return random.choice(available_poses)
    
    def _return_to_home(self) -> None:
        """Return arms to home position smoothly."""
        if "home" in self._poses:
            home_pose = self._poses["home"]
            print("[TalkGesture] Returning to home position")
            self._apply_pose_smooth(home_pose, self.pose_duration)
        else:
            print("[TalkGesture] WARNING: Home pose not found in presets")
    
    def return_home_now(self) -> None:
        """Public method to immediately return arms to home position.
        
        Can be called externally (e.g., when voice session ends) to 
        reset arm position to home regardless of current state.
        """
        self._return_to_home()
        self.bb.write(talk_gesture_active=False)

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
        
        poll_interval = 0.02  # 50 Hz - reduced from 100Hz for better performance
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
        global _active_talk_gesture_service
        _active_talk_gesture_service = self  # Register this instance globally
        
        print("[TalkGesture] Service started")
        
        if not self._talk_pose_keys:
            print("[TalkGesture] No talk poses available - service disabled")
            _active_talk_gesture_service = None
            return
        
        last_speaking = False
        self.bb.write(talk_gesture_active=False)
        
        while self.bb.read("running")["running"]:
            # Check if bye wave is active - pause talk gestures during bye animations
            bye_wave_active = self.bb.read("bye_wave_active")["bye_wave_active"]
            if bye_wave_active:
                time.sleep(self.poll_interval)
                continue
            
            # Read current agent_speaking state from file
            is_speaking = read_speaking_flag()
            
            # Log state changes and track when speaking stopped
            if is_speaking and not last_speaking:
                print("[TalkGesture] Agent started speaking")
                self.bb.write(talk_gesture_active=True)
            elif not is_speaking and last_speaking:
                print("[TalkGesture] Agent stopped speaking - returning home")
                self._return_to_home()
                self.bb.write(talk_gesture_active=False)
            
            last_speaking = is_speaking
            
            # Only play poses while speaking
            if not is_speaking:
                time.sleep(self.poll_interval)
                continue
            
            # Pick a random talk pose (different from last one)
            pose_key = self._get_next_random_pose()
            pose = self._poses[pose_key]
            
            # Track this pose as the last played
            self._last_pose_key = pose_key
            
            # Apply the pose with smooth interpolation (different speeds for vertical/horizontal)
            self._apply_pose_smooth(pose, self.pose_duration)
            
            # Random wait time between poses (1-2 seconds) - hold pose even if speaking stops
            wait_time = random.uniform(1.0, 2.0)
            wait_start = time.time()
            
            # Hold pose for full duration - don't break early when speaking stops
            while time.time() - wait_start < wait_time:
                time.sleep(self.poll_interval)
        
        _active_talk_gesture_service = None  # Unregister on exit
        self.bb.write(talk_gesture_active=False)
        print("[TalkGesture] Service stopped")
