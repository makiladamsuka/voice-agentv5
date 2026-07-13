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
from lib.elastic_head_motion import HeadMotionParams, tick_toward

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
        smoothness: float = 3.0,  # Easing power (1.0=linear, 3.0=cubic, 5.0=quintic)
    ) -> None:
        self.bb = bb
        self.presets_path = presets_path
        self.pose_duration = pose_duration
        self.poll_interval = poll_interval
        self.vertical_speed = vertical_speed  # Speed for a0, a1 (up/down)
        self.horizontal_speed = horizontal_speed  # Speed for a2, a3 (swap)
        self.return_home_delay = return_home_delay  # Wait time before returning home
        self._smoothness = smoothness
        self._vel = [0.0, 0.0, 0.0, 0.0]
        
        # Velocity-based arm motion params (matching ArmController style)
        base_vel = 80.0
        base_accel = 200.0
        base_decel = 250.0
        
        self._params_vert = HeadMotionParams(
            max_vel_pos=base_vel * self.vertical_speed,
            max_vel_neg=base_vel * self.vertical_speed,
            accel=base_accel * self.vertical_speed,
            decel=base_decel * self.vertical_speed,
            goal_deadband_deg=0.1,
            track_gain=10.0,
        )
        self._params_horiz = HeadMotionParams(
            max_vel_pos=base_vel * self.horizontal_speed,
            max_vel_neg=base_vel * self.horizontal_speed,
            accel=base_accel * self.horizontal_speed,
            decel=base_decel * self.horizontal_speed,
            goal_deadband_deg=0.1,
            track_gain=10.0,
        )
        
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
            # Reset velocity so leftover momentum doesn't fight the home direction
            self._vel = [0.0, 0.0, 0.0, 0.0]
            self._apply_pose_smooth(home_pose, self.pose_duration, wait_until_reached=True)
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
    
    def _apply_pose_smooth(self, target_pose: dict, duration: float, wait_until_reached: bool = False) -> None:
        """Smoothly interpolate to target pose using elastic physics.
        
        Uses high-frequency updates and velocity ticking for organic motion.
        
        Args:
            target_pose: Target pose dictionary with a0, a1, a2, a3 keys.
            duration: Total duration for the movement loop in seconds.
            wait_until_reached: If True, blocks until target is reached (used for returning home).
        """
        # Read current arm positions
        current = self.bb.read("arm_a0", "arm_a1", "arm_a2", "arm_a3")
        pos = [current["arm_a0"], current["arm_a1"], current["arm_a2"], current["arm_a3"]]
        
        target = [target_pose["a0"], target_pose["a1"], target_pose["a2"], target_pose["a3"]]
        
        poll_interval = 0.01  # 50 Hz
        start_time = time.time()
        
        while True:
            elapsed = time.time() - start_time
            
            if wait_until_reached:
                err = sum(abs(pos[i] - target[i]) for i in range(4))
                if err < 1.0 or elapsed > 5.0:  # Timeout after 5s
                    break
            else:
                if elapsed >= duration:
                    break
            
            # Tick each axis using elastic_head_motion physics
            pos[0], self._vel[0] = tick_toward(pos[0], self._vel[0], target[0], poll_interval, lo=0.0, hi=180.0, params=self._params_vert)
            pos[1], self._vel[1] = tick_toward(pos[1], self._vel[1], target[1], poll_interval, lo=0.0, hi=180.0, params=self._params_vert)
            pos[2], self._vel[2] = tick_toward(pos[2], self._vel[2], target[2], poll_interval, lo=0.0, hi=180.0, params=self._params_horiz)
            pos[3], self._vel[3] = tick_toward(pos[3], self._vel[3], target[3], poll_interval, lo=0.0, hi=180.0, params=self._params_horiz)
            
            # Write interpolated position
            self.bb.write(
                arm_a0=pos[0],
                arm_a1=pos[1],
                arm_a2=pos[2],
                arm_a3=pos[3],
            )
            
            time.sleep(poll_interval)

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
            
            # Apply the pose with smooth interpolation — physics ticks continuously
            # for the full duration (movement + hold), so arms never freeze mid-air
            wait_time = random.uniform(0.5, 1.0)
            total_duration = self.pose_duration + wait_time
            self._apply_pose_smooth(pose, total_duration)
        
        _active_talk_gesture_service = None  # Unregister on exit
        self.bb.write(talk_gesture_active=False)
        print("[TalkGesture] Service stopped")
