from __future__ import annotations
from dataclasses import dataclass
import cv2
import mediapipe as mp
import numpy as np
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
        model_complexity: int = 0,
        min_detection_confidence: float = 0.38,
        min_tracking_confidence: float = 0.38
    ):
        self.mp_hands = mp.solutions.hands
        self.hands = self.mp_hands.Hands(
            max_num_hands=max_num_hands,
            model_complexity=model_complexity,
            min_detection_confidence=min_detection_confidence,
            min_tracking_confidence=min_tracking_confidence
        )

    def process(self, frame: cv2.Mat, mirrored: bool = True, is_bgr: bool = True) -> list[HandDetection]:
        """
        Process a frame and return a list of HandDetection objects.
        
        Args:
            frame: Input image.
            mirrored: True if the camera feed is horizontally mirrored.
                      Corrects handedness accordingly.
            is_bgr: True if frame is BGR (default for OpenCV VideoCapture),
                    False if frame is already RGB (e.g. from Picamera2 RGB888).
        """
        if frame is None or frame.size == 0:
            return []

        # Enforce C-contiguous memory array for MediaPipe C++ bindings
        if not isinstance(frame, np.ndarray) or not frame.flags.c_contiguous:
            frame = np.ascontiguousarray(frame)

        h, w, _ = frame.shape
        if is_bgr:
            rgb_frame = cv2.cvtColor(frame, cv2.COLOR_BGR2RGB)
        else:
            rgb_frame = frame

        results = self.hands.process(rgb_frame)

        # Dual-pass color space fallback:
        # Camera buffers (Picamera2 RGB888 vs libcamera ISP vs OpenCV) can deliver BGR or RGB.
        # If the primary pass detects 0 hands, try swapping Red and Blue channels.
        if (not results.multi_hand_landmarks) and frame.ndim == 3 and frame.shape[2] == 3:
            swapped = cv2.cvtColor(rgb_frame, cv2.COLOR_RGB2BGR)
            if not swapped.flags.c_contiguous:
                swapped = np.ascontiguousarray(swapped)
            alt_results = self.hands.process(swapped)
            if alt_results.multi_hand_landmarks:
                results = alt_results

        detections = []
        
        if results.multi_hand_landmarks and results.multi_handedness:
            for hand_landmarks, handedness in zip(results.multi_hand_landmarks, results.multi_handedness):
                # MediaPipe classifies handedness assuming a mirrored/selfie camera.
                # When camera is NOT mirrored (standard robot view of world), classification is inverted.
                mp_label = handedness.classification[0].label  # "Left" or "Right"
                if mirrored:
                    physical_side = mp_label
                else:
                    physical_side = "Right" if mp_label == "Left" else "Left"
                
                confidence = handedness.classification[0].score
                
                # Extract landmarks
                landmarks = [{"x": lm.x, "y": lm.y, "z": lm.z} for lm in hand_landmarks.landmark]
                pixel_landmarks = [(int(lm.x * w), int(lm.y * h)) for lm in hand_landmarks.landmark]
                
                # Calculate palm center: average of Wrist (0) and Middle MCP (9) for stability
                wrist = pixel_landmarks[0]
                middle_mcp = pixel_landmarks[9]
                palm_x = (wrist[0] + middle_mcp[0]) // 2
                palm_y = (wrist[1] + middle_mcp[1]) // 2
                palm_center = (palm_x, palm_y)
                
                # Check palm orientation (Frontside vs Backside Knuckles)
                w_lm = hand_landmarks.landmark[0]
                i_lm = hand_landmarks.landmark[5]
                p_lm = hand_landmarks.landmark[17]
                
                v1_x = i_lm.x - w_lm.x
                v1_y = i_lm.y - w_lm.y
                v2_x = p_lm.x - w_lm.x
                v2_y = p_lm.y - w_lm.y
                
                cp = (v1_x * v2_y) - (v1_y * v2_x)
                if physical_side == "Right":
                    is_frontside = cp > 0.0
                else:
                    is_frontside = cp < 0.0
                
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
    """
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

    # Highlight Palm Middle (palm_center)
    if detection.palm_center:
        px, py = detection.palm_center
        cv2.circle(frame, (px, py), 7, (0, 255, 255), -1, cv2.LINE_AA)
        cv2.circle(frame, (px, py), 10, (255, 255, 255), 2, cv2.LINE_AA)
        cv2.putText(frame, "PALM CENTER", (px + 12, py + 4), cv2.FONT_HERSHEY_SIMPLEX, 0.4, (0, 255, 255), 1, cv2.LINE_AA)

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


# ── Robust Gesture Classifiers (Thumbs Up, Bye Wave, Love/Heart) ──────────────

def _dist_2d(p1: dict[str, float] | tuple[int, int], p2: dict[str, float] | tuple[int, int]) -> float:
    if isinstance(p1, dict) and isinstance(p2, dict):
        return ((p1['x'] - p2['x']) ** 2 + (p1['y'] - p2['y']) ** 2) ** 0.5
    return ((p1[0] - p2[0]) ** 2 + (p1[1] - p2[1]) ** 2) ** 0.5

def get_palm_scale(detection: HandDetection) -> float:
    """Returns stable palm scale (distance between Wrist 0 and Middle MCP 9 in pixels)."""
    pts = detection.pixel_landmarks
    if len(pts) >= 21:
        d = _dist_2d(pts[0], pts[9])
        return max(d, 12.0)
    lms = detection.landmarks
    if len(lms) >= 21:
        d = _dist_2d(lms[0], lms[9])
        return max(d, 0.05)
    return 40.0

def _get_mcp_idx(pip_idx: int) -> int:
    """Returns default MCP index for a given PIP joint index."""
    if pip_idx == 6:
        return 5
    elif pip_idx == 10:
        return 9
    elif pip_idx == 14:
        return 13
    elif pip_idx == 18:
        return 17
    return max(0, pip_idx - 1)

def is_finger_extended(
    target: HandDetection | list[dict[str, float]],
    tip_idx: int,
    pip_idx: int,
    mcp_idx: int | None = None
) -> bool:
    """Returns True if the finger is extended outward."""
    if mcp_idx is None:
        mcp_idx = _get_mcp_idx(pip_idx)

    if isinstance(target, HandDetection):
        pts = target.pixel_landmarks
        if len(pts) < 21:
            return False
        wrist = pts[0]
        tip = pts[tip_idx]
        pip = pts[pip_idx]
        mcp = pts[mcp_idx]
        palm_scale = get_palm_scale(target)

        d_wrist_tip = _dist_2d(wrist, tip)
        d_wrist_pip = _dist_2d(wrist, pip)
        d_wrist_mcp = _dist_2d(wrist, mcp)
        d_mcp_tip = _dist_2d(mcp, tip)

        return (d_wrist_tip > d_wrist_pip * 1.05) and (d_wrist_tip > d_wrist_mcp * 1.18) and (d_mcp_tip > palm_scale * 0.65)
    else:
        # Fallback to normalized landmarks list
        lms = target
        if len(lms) < 21:
            return False
        wrist = lms[0]
        tip = lms[tip_idx]
        pip = lms[pip_idx]
        mcp = lms[mcp_idx]
        palm_scale = max(_dist_2d(wrist, lms[9]), 0.05)

        d_wrist_tip = _dist_2d(wrist, tip)
        d_wrist_pip = _dist_2d(wrist, pip)
        d_wrist_mcp = _dist_2d(wrist, mcp)
        d_mcp_tip = _dist_2d(mcp, tip)

        return (d_wrist_tip > d_wrist_pip * 1.05) and (d_wrist_tip > d_wrist_mcp * 1.18) and (d_mcp_tip > palm_scale * 0.65)

def is_finger_curled(
    target: HandDetection | list[dict[str, float]],
    tip_idx: int,
    pip_idx: int,
    mcp_idx: int | None = None
) -> bool:
    """Returns True if the finger is curled inward toward the palm."""
    if mcp_idx is None:
        mcp_idx = _get_mcp_idx(pip_idx)

    if isinstance(target, HandDetection):
        pts = target.pixel_landmarks
        if len(pts) < 21:
            return False
        wrist = pts[0]
        tip = pts[tip_idx]
        pip = pts[pip_idx]
        mcp = pts[mcp_idx]
        palm_scale = get_palm_scale(target)

        d_wrist_tip = _dist_2d(wrist, tip)
        d_wrist_pip = _dist_2d(wrist, pip)
        d_mcp_tip = _dist_2d(mcp, tip)

        return (d_wrist_tip < d_wrist_pip * 1.05) or (d_mcp_tip < palm_scale * 0.65)
    else:
        # Fallback to normalized landmarks list
        lms = target
        if len(lms) < 21:
            return False
        wrist = lms[0]
        tip = lms[tip_idx]
        pip = lms[pip_idx]
        mcp = lms[mcp_idx]
        palm_scale = max(_dist_2d(wrist, lms[9]), 0.05)

        d_wrist_tip = _dist_2d(wrist, tip)
        d_wrist_pip = _dist_2d(wrist, pip)
        d_mcp_tip = _dist_2d(mcp, tip)

        return (d_wrist_tip < d_wrist_pip * 1.05) or (d_mcp_tip < palm_scale * 0.65)


def detect_thumbs_up(detection: HandDetection) -> bool:
    """
    Robust Thumbs-Up detector:
    - Thumb points upwards (tip is higher in screen than IP and MCP joints)
    - Thumb tip is above the wrist and index knuckle
    - At least 3 non-thumb fingers (Index, Middle, Ring, Pinky) are curled
    - Index and Middle fingers are NOT extended
    """
    pts = detection.pixel_landmarks
    if len(pts) < 21:
        return False

    palm_scale = get_palm_scale(detection)
    wrist = pts[0]
    thumb_mcp = pts[2]
    thumb_ip = pts[3]
    thumb_tip = pts[4]
    index_mcp = pts[5]

    # Thumb tip pointing upwards (smaller Y in screen coords)
    thumb_pointing_up = (thumb_tip[1] < thumb_ip[1]) and (thumb_tip[1] < thumb_mcp[1])
    thumb_above_wrist = thumb_tip[1] < wrist[1] - (palm_scale * 0.25)
    thumb_above_knuckle = thumb_tip[1] < index_mcp[1]

    if not (thumb_pointing_up and thumb_above_wrist and thumb_above_knuckle):
        return False

    curled_count = 0
    for tip, pip, mcp in [(8, 6, 5), (12, 10, 9), (16, 14, 13), (20, 18, 17)]:
        if is_finger_curled(detection, tip, pip, mcp):
            curled_count += 1

    index_ext = is_finger_extended(detection, 8, 6, 5)
    middle_ext = is_finger_extended(detection, 12, 10, 9)

    return (curled_count >= 3) and (not index_ext) and (not middle_ext)


def detect_bye_wave(detection: HandDetection) -> bool:
    """
    Robust Bye Wave detector:
    - Hand is raised upright (knuckles/palm center above wrist)
    - At least 3 of the 4 main fingers (Index, Middle, Ring, Pinky) are extended
    - Fingertips are higher than knuckles
    - Index and Middle fingers are both extended (open hand)
    """
    pts = detection.pixel_landmarks
    if len(pts) < 21:
        return False

    palm_scale = get_palm_scale(detection)
    wrist = pts[0]
    middle_mcp = pts[9]

    # Hand upright check: wrist is below the middle knuckle
    if wrist[1] < middle_mcp[1] + (palm_scale * 0.15):
        return False

    extended_count = 0
    for tip, pip, mcp in [(8, 6, 5), (12, 10, 9), (16, 14, 13), (20, 18, 17)]:
        if is_finger_extended(detection, tip, pip, mcp):
            if pts[tip][1] < pts[mcp][1]:  # fingertip higher than knuckle
                extended_count += 1

    if extended_count < 3:
        return False

    index_ext = is_finger_extended(detection, 8, 6, 5)
    middle_ext = is_finger_extended(detection, 12, 10, 9)

    return index_ext and middle_ext


def detect_love_gesture(detections: list[HandDetection]) -> bool:
    """
    Robust Love / Heart detector:
    1. Two-hand heart (🫶): Two hands with index fingertips touching/close and thumbs touching/close.
    2. 'I Love You' sign (🤟): Thumb, Index, and Pinky extended; Middle and Ring curled.
    3. Korean Finger Heart (🫰): Thumb tip & Index tip pinched together; other fingers curled.
    """
    if not detections:
        return False

    # 1. Two-hand heart (🫶)
    if len(detections) >= 2:
        h1 = detections[0]
        h2 = detections[1]
        p1 = h1.pixel_landmarks
        p2 = h2.pixel_landmarks
        if len(p1) >= 21 and len(p2) >= 21:
            scale_avg = (get_palm_scale(h1) + get_palm_scale(h2)) * 0.5
            d_index_tips = _dist_2d(p1[8], p2[8])
            d_thumb_tips = _dist_2d(p1[4], p2[4])
            if d_index_tips < scale_avg * 1.6 and d_thumb_tips < scale_avg * 1.6:
                return True

    # 2. Single-hand love gestures
    for d in detections:
        pts = d.pixel_landmarks
        if len(pts) < 21:
            continue

        palm_scale = get_palm_scale(d)
        wrist = pts[0]

        index_ext = is_finger_extended(d, 8, 6, 5)
        pinky_ext = is_finger_extended(d, 20, 18, 17)
        middle_curled = is_finger_curled(d, 12, 10, 9)
        ring_curled = is_finger_curled(d, 16, 14, 13)

        # 2a. 'I Love You' sign (🤟): Index & Pinky extended; Middle & Ring curled; Thumb out
        thumb_out = (_dist_2d(pts[4], pts[2]) > palm_scale * 0.55) or (pts[4][1] < pts[2][1])
        if index_ext and pinky_ext and middle_curled and ring_curled and thumb_out:
            return True

        # 2b. Korean Finger Heart (🫰): Thumb tip & Index tip pinched close together; Middle/Ring curled
        d_thumb_index = _dist_2d(pts[4], pts[8])
        if d_thumb_index < palm_scale * 0.50:
            if pts[4][1] < wrist[1] and pts[8][1] < wrist[1]:
                if middle_curled or ring_curled:
                    return True

    return False
