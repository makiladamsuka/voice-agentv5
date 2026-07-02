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


def compute_face_embedding(face_roi: np.ndarray, landmarks: Optional[np.ndarray] = None, use_landmarks_only: bool = False) -> Optional[np.ndarray]:
    """Compute face embedding using landmarks + appearance features.
    
    Args:
        face_roi: Face image
        landmarks: 5 facial landmark points (10 values)
        use_landmarks_only: If True, only use geometric features (MORE STABLE!)
    
    Uses:
    - Face landmarks (eyes, nose, mouth) for geometric features [STABLE]
    - Local Binary Patterns (LBP) for texture [OPTIONAL]
    - Color histograms for appearance [OPTIONAL]
    
    This combines geometric and appearance features for better recognition.
    """
    if cv2 is None or np is None or face_roi is None or face_roi.size == 0:
        return None
    
    try:
        # Resize to standard size
        resized = cv2.resize(face_roi, (64, 64))
        gray = cv2.cvtColor(resized, cv2.COLOR_BGR2GRAY)
        
        features = []
        
        # 1. LANDMARKS (Geometric Features) - if available
        if landmarks is not None and len(landmarks) >= 10:
            # YuNet provides 5 landmarks: left_eye, right_eye, nose, left_mouth, right_mouth
            # Normalize landmark positions relative to face size
            landmarks_norm = landmarks.reshape(-1, 2).astype(float)
            
            # Compute inter-landmark distances (geometric relationships)
            eye_dist = np.linalg.norm(landmarks_norm[0] - landmarks_norm[1])  # Inter-eye distance
            nose_to_left_eye = np.linalg.norm(landmarks_norm[2] - landmarks_norm[0])
            nose_to_right_eye = np.linalg.norm(landmarks_norm[2] - landmarks_norm[1])
            mouth_width = np.linalg.norm(landmarks_norm[3] - landmarks_norm[4])
            
            # Additional geometric features for better accuracy
            nose_to_mouth_left = np.linalg.norm(landmarks_norm[2] - landmarks_norm[3])
            nose_to_mouth_right = np.linalg.norm(landmarks_norm[2] - landmarks_norm[4])
            eye_left_to_mouth_left = np.linalg.norm(landmarks_norm[0] - landmarks_norm[3])
            eye_right_to_mouth_right = np.linalg.norm(landmarks_norm[1] - landmarks_norm[4])
            
            # Normalize by eye distance (scale invariant)
            if eye_dist > 0:
                landmarks_norm = landmarks_norm / eye_dist
                nose_to_left_eye /= eye_dist
                nose_to_right_eye /= eye_dist
                mouth_width /= eye_dist
                nose_to_mouth_left /= eye_dist
                nose_to_mouth_right /= eye_dist
                eye_left_to_mouth_left /= eye_dist
                eye_right_to_mouth_right /= eye_dist
            
            # Flatten normalized landmarks
            landmark_features = landmarks_norm.flatten()
            geometric_features = np.array([
                eye_dist, 
                nose_to_left_eye, 
                nose_to_right_eye, 
                mouth_width,
                nose_to_mouth_left,
                nose_to_mouth_right,
                eye_left_to_mouth_left,
                eye_right_to_mouth_right,
            ])
            
            features.append(landmark_features)
            features.append(geometric_features)
            
            # LANDMARKS-ONLY MODE: Return early (skip texture/color)
            if use_landmarks_only:
                embedding = np.concatenate(features)
                print(f"[FaceGreetingArm] DEBUG: Landmarks-only embedding computed (size={len(embedding)})")
                return embedding
        
        # 2. LOCAL BINARY PATTERNS (Texture Features)
        # LBP is robust to illumination changes
        try:
            # Simple LBP implementation
            lbp_img = np.zeros_like(gray)
            for i in range(1, gray.shape[0] - 1):
                for j in range(1, gray.shape[1] - 1):
                    center = gray[i, j]
                    code = 0
                    code |= (gray[i-1, j-1] > center) << 7
                    code |= (gray[i-1, j] > center) << 6
                    code |= (gray[i-1, j+1] > center) << 5
                    code |= (gray[i, j+1] > center) << 4
                    code |= (gray[i+1, j+1] > center) << 3
                    code |= (gray[i+1, j] > center) << 2
                    code |= (gray[i+1, j-1] > center) << 1
                    code |= (gray[i, j-1] > center) << 0
                    lbp_img[i, j] = code
            
            # LBP histogram (texture descriptor)
            lbp_hist = cv2.calcHist([lbp_img], [0], None, [32], [0, 256])
            lbp_hist = cv2.normalize(lbp_hist, lbp_hist).flatten()
            features.append(lbp_hist)
        except Exception:
            pass  # Skip LBP if it fails
        
        # 3. COLOR HISTOGRAMS (Appearance Features)
        # HSV for robustness to lighting
        # Note: picamera2 outputs RGB, not BGR
        hsv = cv2.cvtColor(resized, cv2.COLOR_RGB2HSV)
        h_hist = cv2.calcHist([hsv], [0], None, [16], [0, 180])
        s_hist = cv2.calcHist([hsv], [1], None, [16], [0, 256])
        v_hist = cv2.calcHist([hsv], [2], None, [16], [0, 256])
        
        h_hist = cv2.normalize(h_hist, h_hist).flatten()
        s_hist = cv2.normalize(s_hist, s_hist).flatten()
        v_hist = cv2.normalize(v_hist, v_hist).flatten()
        
        features.extend([h_hist, s_hist, v_hist])
        
        # Concatenate all features
        embedding = np.concatenate(features)
        
        return embedding
    except Exception as e:
        print(f"[FaceGreetingArm] Error computing embedding: {e}")
        import traceback
        traceback.print_exc()
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
        self.use_landmarks_only = bool(fg.get("use_landmarks_only", False))  # NEW: landmarks-only mode
        
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
        print(f"[FaceGreetingArm] Recognition mode: {'LANDMARKS ONLY (geometric)' if self.use_landmarks_only else 'Full (landmarks + texture + color)'}")
    
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
        
        best_match = None
        best_distance = float('inf')
        
        for i, greeted_face in enumerate(self.greeted_faces):
            distance = embedding_distance(embedding, greeted_face.embedding)
            print(f"[FaceGreetingArm] DEBUG: Distance to face #{i+1}: {distance:.4f} (threshold: {self.embedding_threshold:.4f})")
            
            if distance < self.embedding_threshold and distance < best_distance:
                best_match = greeted_face
                best_distance = distance
        
        if best_match:
            print(f"[FaceGreetingArm] DEBUG: MATCH FOUND with distance {best_distance:.4f}")
        else:
            print(f"[FaceGreetingArm] DEBUG: NO MATCH - all distances above threshold")
        
        return best_match
    
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
    
    def extract_face_roi(self, frame: np.ndarray, face_data: dict) -> tuple[Optional[np.ndarray], Optional[np.ndarray]]:
        """Extract face region of interest and landmarks from frame.
        
        Returns: (face_roi, landmarks) tuple
        - face_roi: cropped face image
        - landmarks: 5 facial landmark points (10 values: x1,y1,x2,y2,...)
        
        NOTE: face_data coordinates are in DETECTION resolution (1280x720),
        but stream_frame is in STREAM resolution (320x180 by default).
        We need to use normalized coordinates instead!
        """
        if frame is None or face_data is None:
            print("[FaceGreetingArm] DEBUG: frame or face_data is None")
            return None, None
        
        try:
            # Use NORMALIZED coordinates which work across any resolution
            norm_x = face_data.get("norm_x", 0)
            norm_y = face_data.get("norm_y", 0)
            area_ratio = face_data.get("area_ratio", 0)
            
            # Estimate face box size from area ratio
            # area_ratio = (w * h) / (frame_w * frame_h)
            # Assume square-ish face: w ≈ h ≈ sqrt(area_ratio * frame_w * frame_h)
            frame_h, frame_w = frame.shape[:2]
            face_size = int((area_ratio * frame_w * frame_h) ** 0.5)
            
            # Convert normalized center to pixel coordinates
            center_x = int((norm_x * 0.5 + 0.5) * frame_w)  # norm_x: -1 to +1, convert to 0 to 1
            center_y = int((norm_y * 0.5 + 0.5) * frame_h)  # norm_y: -1 to +1, convert to 0 to 1
            
            # Calculate bounding box
            half_size = face_size // 2
            x1 = max(0, center_x - half_size)
            y1 = max(0, center_y - half_size)
            x2 = min(frame_w, center_x + half_size)
            y2 = min(frame_h, center_y + half_size)
            
            print(f"[FaceGreetingArm] DEBUG: norm_x={norm_x:.3f}, norm_y={norm_y:.3f}, "
                  f"area={area_ratio:.4f}, frame={frame.shape}")
            print(f"[FaceGreetingArm] DEBUG: center=({center_x},{center_y}), "
                  f"size={face_size}, roi=[{x1}:{x2}, {y1}:{y2}]")
            
            if x2 > x1 and y2 > y1:
                roi = frame[y1:y2, x1:x2]
                print(f"[FaceGreetingArm] DEBUG: ROI extracted, shape={roi.shape}")
                
                # Extract landmarks if available
                landmarks = face_data.get("landmarks", None)
                if landmarks is not None and len(landmarks) >= 10:
                    # Scale landmarks to ROI coordinates
                    landmarks_arr = np.array(landmarks).reshape(-1, 2)
                    
                    # Transform from frame coordinates to ROI coordinates
                    landmarks_arr[:, 0] = (landmarks_arr[:, 0] - x1) / (x2 - x1) * 64  # Scale to 64x64
                    landmarks_arr[:, 1] = (landmarks_arr[:, 1] - y1) / (y2 - y1) * 64
                    
                    landmarks_scaled = landmarks_arr.flatten()
                    print(f"[FaceGreetingArm] DEBUG: Landmarks extracted: {len(landmarks_scaled)} values")
                    return roi, landmarks_scaled
                else:
                    print(f"[FaceGreetingArm] DEBUG: No landmarks in face_data")
                    return roi, None
            else:
                print(f"[FaceGreetingArm] DEBUG: Invalid ROI bounds")
            
        except Exception as e:
            print(f"[FaceGreetingArm] Error extracting face ROI: {e}")
            import traceback
            traceback.print_exc()
        
        return None, None
    
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
                    print(f"[FaceGreetingArm] DEBUG: Face appeared, starting hold timer")
                
                # Face held long enough - compute embedding and check if we should greet
                elif (now - self._face_since) >= self.hold_sec and self._current_embedding is None:
                    print(f"[FaceGreetingArm] DEBUG: Hold time passed, extracting face ROI...")
                    
                    # Get face ROI from stream_frame
                    frame = state["stream_frame"]
                    face_candidates = state["face_candidates"]
                    
                    print(f"[FaceGreetingArm] DEBUG: frame is None: {frame is None}, "
                          f"face_candidates length: {len(face_candidates) if face_candidates else 0}")
                    
                    if frame is not None and face_candidates and len(face_candidates) > 0:
                        # Use first/largest face
                        face_data = face_candidates[0]
                        print(f"[FaceGreetingArm] DEBUG: Face data: {face_data}")
                        face_roi, landmarks = self.extract_face_roi(frame, face_data)
                        
                        if face_roi is not None:
                            print(f"[FaceGreetingArm] DEBUG: Computing embedding...")
                            # Compute embedding with landmarks (and optionally texture/color)
                            embedding = compute_face_embedding(face_roi, landmarks, use_landmarks_only=self.use_landmarks_only)
                            self._current_embedding = embedding
                            
                            if embedding is not None:
                                print(f"[FaceGreetingArm] DEBUG: Embedding computed, checking memory...")
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
                                print(f"[FaceGreetingArm] DEBUG: Embedding computation failed")
                        else:
                            print(f"[FaceGreetingArm] DEBUG: Face ROI extraction failed")
                    else:
                        print(f"[FaceGreetingArm] DEBUG: No frame or face_candidates available")
            else:
                # No face or busy - reset detection state
                if self._face_since is not None:
                    print(f"[FaceGreetingArm] DEBUG: Face lost or busy, resetting")
                self._face_since = None
                self._current_embedding = None
            
            time.sleep(loop_delay)
        
        print("[FaceGreetingArm] Stopped.")
