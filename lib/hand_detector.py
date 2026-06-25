from __future__ import annotations
from dataclasses import dataclass
import cv2
import mediapipe as mp
from typing import Any

@dataclass
class HandDetection:
    """Dataclass storing the attributes of a single detected hand."""
    label: str  # "Left" or "Right" classification from MediaPipe
    physical_side: str  # Corrected physical side ("Left" or "Right")
    confidence: float  # Classification confidence score
    landmarks: list[dict[str, float]]  # List of 21 normalized landmarks (x, y, z)
    pixel_landmarks: list[tuple[int, int]]  # List of 21 pixel coordinates (x, y)
    palm_center: tuple[int, int]  # (x, y) coordinates of the palm center
    is_frontside: bool  # True if the palm is facing the camera, False if backside/knuckles

class HandDetector:
    """Wrapper class for MediaPipe Hands detection-only API."""
    def __init__(
        self,
        max_num_hands: int = 2,
        model_complexity: int = 1,
        min_detection_confidence: float = 0.6,
        min_tracking_confidence: float = 0.6
    ):
        self.mp_hands = mp.solutions.hands
        self.hands = self.mp_hands.Hands(
            max_num_hands=max_num_hands,
            model_complexity=model_complexity,
            min_detection_confidence=min_detection_confidence,
            min_tracking_confidence=min_tracking_confidence
        )

    def process(self, frame: cv2.Mat, mirrored: bool = True) -> list[HandDetection]:
        """
        Process a BGR frame and return a list of HandDetection objects.
        
        Args:
            frame: Input image (BGR format).
            mirrored: True if the camera feed is horizontally mirrored.
                      Corrects handedness accordingly.
        """
        h, w, _ = frame.shape
        rgb_frame = cv2.cvtColor(frame, cv2.COLOR_BGR2RGB)
        results = self.hands.process(rgb_frame)
        
        detections = []
        
        if results.multi_hand_landmarks and results.multi_handedness:
            for hand_landmarks, handedness in zip(results.multi_hand_landmarks, results.multi_handedness):
                # Classify hand label and physical side (swap if camera is mirrored)
                mp_label = handedness.classification[0].label  # "Left" or "Right"
                if mirrored:
                    physical_side = "Left" if mp_label == "Right" else "Right"
                else:
                    physical_side = mp_label
                
                confidence = handedness.classification[0].score
                
                # Extract landmarks
                landmarks = [{"x": lm.x, "y": lm.y, "z": lm.z} for lm in hand_landmarks.landmark]
                pixel_landmarks = [(int(lm.x * w), int(lm.y * h)) for lm in hand_landmarks.landmark]
                
                # Calculate palm center: average of Wrist (0), Index MCP (5), and Pinky MCP (17)
                wrist = pixel_landmarks[0]
                index_mcp = pixel_landmarks[5]
                pinky_mcp = pixel_landmarks[17]
                palm_x = (wrist[0] + index_mcp[0] + pinky_mcp[0]) // 3
                palm_y = (wrist[1] + index_mcp[1] + pinky_mcp[1]) // 3
                palm_center = (palm_x, palm_y)
                
                # Check palm orientation (Frontside vs Backside Knuckles)
                # Compute 2D cross product of vectors from Wrist to Index MCP and Pinky MCP
                w_lm = hand_landmarks.landmark[0]
                i_lm = hand_landmarks.landmark[5]
                p_lm = hand_landmarks.landmark[17]
                
                v1_x = i_lm.x - w_lm.x
                v1_y = i_lm.y - w_lm.y
                v2_x = p_lm.x - w_lm.x
                v2_y = p_lm.y - w_lm.y
                
                cp = (v1_x * v2_y) - (v1_y * v2_x)
                
                # In mirrored space, cross-product sign shifts
                if mp_label == "Left":
                    is_frontside = cp < 0.0
                else:
                    is_frontside = cp > 0.0
                
                detections.append(HandDetection(
                    label=mp_label,
                    physical_side=physical_side,
                    confidence=confidence,
                    landmarks=landmarks,
                    pixel_landmarks=pixel_landmarks,
                    palm_center=palm_center,
                    is_frontside=is_frontside
                ))
                
        return detections

    def close(self) -> None:
        """Release MediaPipe resources."""
        self.hands.close()

def draw_skeleton(frame: cv2.Mat, detection: HandDetection, is_active: bool = False) -> None:
    """
    Draw a premium neon-styled skeleton overlay for the detected hand.
    
    Args:
        frame: Target image canvas.
        detection: The HandDetection object containing landmarks.
        is_active: True if the hand is currently performing an action (e.g., waving).
    """
    # Define color scheme (neon orange for idle, neon cyan for active)
    if is_active:
        joint_col = (255, 255, 0)    # Cyan
        line_col = (255, 200, 0)     # Bright Cyan/Yellow
    else:
        joint_col = (0, 100, 255)    # Neon Orange
        line_col = (0, 165, 255)     # Deep Orange
        
    mp_hands = mp.solutions.hands
    
    # Draw skeleton lines
    for connection in mp_hands.HAND_CONNECTIONS:
        pt1 = detection.pixel_landmarks[connection[0]]
        pt2 = detection.pixel_landmarks[connection[1]]
        cv2.line(frame, pt1, pt2, line_col, 2, cv2.LINE_AA)
        
    # Draw joint points with white concentric ring highlights
    for pt in detection.pixel_landmarks:
        cv2.circle(frame, pt, 4, joint_col, -1, cv2.LINE_AA)
        cv2.circle(frame, pt, 5, (255, 255, 255), 1, cv2.LINE_AA)

def draw_motion_trail(frame: cv2.Mat, x_history: list[int], y_history: list[int], is_active: bool = False) -> None:
    """
    Draw a glowing, fading trail representing the palm center's motion history.
    """
    points = list(zip(x_history, y_history))
    num_pts = len(points)
    if num_pts < 2:
        return
        
    overlay = frame.copy()
    trail_color = (255, 255, 0) if is_active else (0, 200, 100) # Cyan vs Mint Green
    
    for i in range(num_pts - 1):
        alpha = (i + 1) / num_pts
        radius = int(2 + 6 * alpha)
        pt1 = (int(points[i][0]), int(points[i][1]))
        pt2 = (int(points[i+1][0]), int(points[i+1][1]))
        
        cv2.line(overlay, pt1, pt2, trail_color, int(radius / 2), cv2.LINE_AA)
        cv2.circle(overlay, pt2, radius, trail_color, -1, cv2.LINE_AA)
        
    cv2.addWeighted(overlay, 0.4, frame, 0.6, 0, frame)
