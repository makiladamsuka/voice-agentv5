"""FaceGreetingArmService — trigger random hi poses when a new/forgotten face appears.

Uses face recognition to remember individual faces for 30 minutes.
Plays random hi pose (hi1, hi2, hi3, hi4) when:
- A new face is detected (not in memory)
- A face returns after 30+ minutes (forgotten)

Separate from voice greetings (FaceGreetingMonitor).
"""

from __future__ import annotations

import json
import random
import time
from pathlib import Path
from typing import Dict, List, Optional

try:
    import cv2
    import numpy as np
except ImportError:
    cv2 = None
    np = None

try:
    import yaml
except ImportError:
    yaml = None

from core.blackboard import Blackboard

APP_DIR = Path(__file__).resolve().parent.parent
DEFAULT_CONFIG_PATH = APP_DIR / "config.yaml"
DEFAULT_PRESETS_PATH = APP_DIR / "tests" / "arm_pose_presets.json"


def _load_yaml(path: Path) -> dict:
    if yaml is None or not path.exists():
        return {}
    with open(path, encoding="utf-8") as f:
        return yaml.safe_load(f) or {}


def _load_json(path: Path) -> dict:
    if not path.exists():
        return {}
    with open(path, encoding="utf-8") as f:
        return json.load(f)


def compute_face_embedding(face_roi: np.ndarray) -> Optional[np.ndarray]:
    """Compute a simple face embedding using histogram features.
    
    For production, you'd use dlib, face_recognition, or a deep model.
    This is a simple alternative using color histograms.
    """
    if cv2 is None or np is None or face_roi is None or face_roi.size == 0:
        return None
    
    try:
        # Resize to standard size
        resized = cv2.resize(face_roi, (64, 64))
        
        # Convert to HSV for better color features
        hsv = cv2.cvtColor(resized, cv2.COLOR_BGR2HSV)
        
        # Compute histograms for each channel
        h_hist = cv2.calcHist([hsv], [0], None, [32], [0, 180])
        s_hist = cv2.calcHist([hsv], [1], None, [32], [0, 256])
        v_hist = cv2.calcHist([hsv], [2], None, [32], [0, 256])
        
        # Normalize
        h_hist = cv2.normalize(h_hist, h_hist).flatten()
        s_hist = cv2.normalize(s_hist, s_hist).flatten()
        v_hist = cv2.normalize(v_hist, v_hist).flatten()
        
        # Concatenate into single feature vector
        embedding = np.concatenate([h_hist, s_hist, v_hist])
        
        return embedding
    except Exception as e:
        print(f"[FaceGreetingArm] Error computing embedding: {e}")
        return None


def embedding_distance(emb1: np.ndarray, emb2: np.ndarray) -> float:
    """Compute distance between two embeddings (lower = more similar)."""
    if emb1 is None or emb2 is None:
        return float('inf')
    return np.linalg.norm(emb1 - emb2)


class GreetedFace:
    """A face that has been greeted, with timestamp and embedding."""
    
    def __init__(self, embedding: np.ndarray, timestamp: float):
        self.embedding = embedding
        self.timestamp = timestamp
        self.greet_count = 1
    
    def is_expired(self, current_time: float, timeout_sec: float) -> bool:
        """Check if this face has been forgotten (timeout expired)."""
        return (current_time - self.timestamp) > timeout_sec
    
    def update(self, current_time: float):
        """Update timestamp (face seen again)."""
        self.timestamp = current_time
        self.greet_count += 1


class FaceGreetingArmService:
    """Watch for faces and trigger arm greeting gestures for new/forgotten faces."""
    
    def __init__(
        self, 
        bb: Blackboard, 
        config_path: Path = DEFAULT_CONFIG_PATH,
        presets_path: Path = DEFAULT_PRESETS_PATH
    ) -> None:
        self.bb = bb
        
        # Load config
        cfg = _load_yaml(config_path)
        fg = (cfg.get("face_greeting_arm") or {}) if cfg else {}
        
        self.enabled = bool(fg.get("enabled", True))
        self.memory_timeout_sec = float(fg.get("memory_timeout_minutes", 30.0)) * 60.0
        self.min_face_area_ratio = float(fg.get("min_face_area_ratio", 0.012))
        self.hold_sec = float(fg.get("hold_sec", 0.5))  # Face must be visible this long
        self.embedding_threshold = float(fg.get("embedding_threshold", 0.45))
        self.cleanup_interval_sec = float(fg.get("cleanup_interval_sec", 60.0))
        
        # Load hi poses
        presets = _load_json(presets_path)
        poses = presets.get("poses", {})
        self.hi_poses = [name for name in poses.keys() if name.startswith("hi")]
        
        if not self.hi_poses:
            print("[FaceGreetingArm] WARNING: No 'hi' poses found in presets")
            self.hi_poses = ["home"]  # Fallback
        
        # Memory of greeted faces
        self.greeted_faces: List[GreetedFace] = []
        
        # Current detection state
        self._face_since: Optional[float] = None
        self._current_embedding: Optional[np.ndarray] = None
        self._last_cleanup = time.time()
        
        print(f"[FaceGreetingArm] Initialized with {len(self.hi_poses)} hi poses: {self.hi_poses}")
        print(f"[FaceGreetingArm] Memory timeout: {self.memory_timeout_sec / 60:.1f} minutes")
    
    def cleanup_expired_faces(self, current_time: float):
        """Remove faces that have been forgotten (timeout expired)."""
        original_count = len(self.greeted_faces)
        self.greeted_faces = [
            face for face in self.greeted_faces
            if not face.is_expired(current_time, self.memory_timeout_sec)
        ]
        removed = original_count - len(self.greeted_faces)
        if removed > 0:
            print(f"[FaceGreetingArm] Cleaned up {removed} expired faces (now {len(self.greeted_faces)} in memory)")
    
    def find_matching_face(self, embedding: np.ndarray) -> Optional[GreetedFace]:
        """Find a matching face in memory, or None if this is a new face."""
        if embedding is None:
            return None
        
        for greeted_face in self.greeted_faces:
            distance = embedding_distance(embedding, greeted_face.embedding)
            if distance < self.embedding_threshold:
                return greeted_face
        
        return None
    
    def add_greeted_face(self, embedding: np.ndarray, current_time: float):
        """Add a new face to memory."""
        if embedding is None:
            return
        
        self.greeted_faces.append(GreetedFace(embedding, current_time))
        print(f"[FaceGreetingArm] Added new face to memory (total: {len(self.greeted_faces)})")
    
    def trigger_greeting(self) -> str:
        """Trigger a random hi pose greeting."""
        pose_name = random.choice(self.hi_poses)
        
        # Write to blackboard for ArmController to execute
        self.bb.write(
            arm_greeting_seq=self.bb.read("arm_greeting_seq").get("arm_greeting_seq", 0) + 1,
            arm_greeting_pose=pose_name
        )
        
        print(f"[FaceGreetingArm] Triggered greeting: {pose_name}")
        return pose_name
    
    def extract_face_roi(self, frame: np.ndarray, face_data: dict) -> Optional[np.ndarray]:
        """Extract face region of interest from frame."""
        if frame is None or face_data is None:
            return None
        
        try:
            # Get face bounding box (normalized coordinates assumed)
            # Adjust based on actual face_candidates format
            x = face_data.get("x", 0)
            y = face_data.get("y", 0)
            w = face_data.get("w", 0)
            h = face_data.get("h", 0)
            
            # Convert to pixel coordinates if normalized
            if 0 <= x <= 1:
                h_frame, w_frame = frame.shape[:2]
                x = int(x * w_frame)
                y = int(y * h_frame)
                w = int(w * w_frame)
                h = int(h * h_frame)
            
            # Extract ROI
            x1, y1 = max(0, x), max(0, y)
            x2, y2 = min(frame.shape[1], x + w), min(frame.shape[0], y + h)
            
            if x2 > x1 and y2 > y1:
                return frame[y1:y2, x1:x2]
            
        except Exception as e:
            print(f"[FaceGreetingArm] Error extracting face ROI: {e}")
        
        return None
    
    def run(self) -> None:
        """Main loop: watch for faces and trigger greetings."""
        if not self.enabled:
            print("[FaceGreetingArm] Disabled in config.")
            return
        
        if cv2 is None or np is None:
            print("[FaceGreetingArm] OpenCV/NumPy not available. Disabled.")
            return
        
        loop_delay = 0.15  # Check every 150ms
        print("[FaceGreetingArm] Monitoring for new faces to greet with arm gestures.")
        
        while self.bb.read("running")["running"]:
            now = time.time()
            
            # Periodic cleanup of expired faces
            if (now - self._last_cleanup) > self.cleanup_interval_sec:
                self.cleanup_expired_faces(now)
                self._last_cleanup = now
            
            # Read current state
            state = self.bb.read(
                "face_detected",
                "face_area_ratio",
                "face_candidates",
                "stream_frame",
                "agent_speaking",
                "user_speaking",
                "bye_wave_active",
                "arm_greeting_seq"
            )
            
            face_visible = (
                state["face_detected"] 
                and float(state["face_area_ratio"]) >= self.min_face_area_ratio
            )
            
            # Don't greet during voice interaction or bye wave
            busy = (
                state["agent_speaking"] 
                or state["user_speaking"] 
                or state["bye_wave_active"]
            )
            
            if face_visible and not busy:
                # Face just appeared
                if self._face_since is None:
                    self._face_since = now
                    self._current_embedding = None
                
                # Face held long enough - compute embedding and check if we should greet
                elif (now - self._face_since) >= self.hold_sec and self._current_embedding is None:
                    # Get face ROI from stream_frame
                    frame = state["stream_frame"]
                    face_candidates = state["face_candidates"]
                    
                    if frame is not None and face_candidates and len(face_candidates) > 0:
                        # Use first/largest face
                        face_data = face_candidates[0]
                        face_roi = self.extract_face_roi(frame, face_data)
                        
                        if face_roi is not None:
                            # Compute embedding
                            embedding = compute_face_embedding(face_roi)
                            self._current_embedding = embedding
                            
                            if embedding is not None:
                                # Check if this face has been greeted recently
                                matching_face = self.find_matching_face(embedding)
                                
                                if matching_face is None:
                                    # New face - greet it!
                                    pose = self.trigger_greeting()
                                    self.add_greeted_face(embedding, now)
                                    print(f"[FaceGreetingArm] New face detected → {pose}")
                                
                                elif matching_face.is_expired(now, self.memory_timeout_sec):
                                    # Known face but forgotten (expired) - greet again!
                                    pose = self.trigger_greeting()
                                    matching_face.update(now)
                                    print(f"[FaceGreetingArm] Forgotten face returned → {pose}")
                                
                                else:
                                    # Known face, recently greeted - skip
                                    time_remaining = self.memory_timeout_sec - (now - matching_face.timestamp)
                                    print(f"[FaceGreetingArm] Known face (greeted {matching_face.greet_count}x, "
                                          f"{time_remaining / 60:.1f} min until forgotten)")
            else:
                # No face or busy - reset detection state
                self._face_since = None
                self._current_embedding = None
            
            time.sleep(loop_delay)
        
        print("[FaceGreetingArm] Stopped.")
