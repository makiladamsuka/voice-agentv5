#!/usr/bin/env python3
"""
Face Tracking Eyes for Dual SPI Displays (Picamera2)
Combines face tracking (YuNet) with dual SPI display output (ST7735).
"""

import time
import math
import random
import sys
import io
import threading
import socketserver
from http.server import BaseHTTPRequestHandler, HTTPServer
import numpy as np
import cv2
from pathlib import Path
import argparse

try:
    import yaml
except ImportError:
    yaml = None

from arduino_servo import ArduinoServoLink
from elastic_head_motion import (
    HeadMotionParams,
    OrganicWanderSearch,
    clamp,
    scale_head_motion,
    tick_toward,
)
from person_detector import PersonDetector

# Hardware / Display Imports
import board
import busio
import digitalio
from PIL import Image, ImageDraw
try:
    from adafruit_rgb_display import st7735
except ImportError:
    print("Error: adafruit-circuitpython-rgb-display not found.")
    print("pip3 install adafruit-circuitpython-rgb-display")
    sys.exit(1)

# Camera Import
try:
    from picamera2 import Picamera2
except ImportError:
    print("Error: picamera2 not found. Please install with: sudo apt install python3-picamera2")
    sys.exit(1)


# --- Configuration ---
SCREEN_WIDTH = 128
SCREEN_HEIGHT = 160
EYE_COLOR = (255, 255, 255)  # White
BG_COLOR = (0, 0, 0)      # Black
EYE_SIZE = 120
FLOOR_Y = SCREEN_HEIGHT - 5

# Camera / Face Tracking Config
APP_DIR = Path(__file__).resolve().parent
DEFAULT_CONFIG_PATH = APP_DIR / "config.yaml"

def _load_yaml_config(path):
    if yaml is None or not path.exists():
        return {}
    with open(path, encoding="utf-8") as f:
        return yaml.safe_load(f) or {}

def _cfg(data, *keys, default=None):
    cur = data
    for key in keys:
        if not isinstance(cur, dict) or key not in cur:
            return default
        cur = cur[key]
    return cur

def parse_args():
    parser = argparse.ArgumentParser(description="Voice Agent V5 face tracking eyes + ESP32 head servos")
    parser.add_argument("--config", default=str(DEFAULT_CONFIG_PATH), help="Path to config.yaml")
    parser.add_argument("--port", default=None, help="ESP32 serial port; overrides config")
    parser.add_argument("--baud", type=int, default=None, help="ESP32 serial baud; overrides config")
    parser.add_argument("--no-servo", action="store_true", help="Run camera/TFT tracking without ESP32 servos")
    parser.add_argument("--no-stream", action="store_true", help="Disable MJPEG preview stream")
    return parser.parse_args()

ARGS = parse_args()
CONFIG = _load_yaml_config(Path(ARGS.config))
SERVO_CFG = _cfg(CONFIG, "servo", default={}) or {}
BASE_CFG = _cfg(CONFIG, "base", default={}) or {}
CAMERA_CFG = _cfg(CONFIG, "camera", default={}) or {}
STREAM_CFG = _cfg(CONFIG, "stream", default={}) or {}

FACE_MODEL_PATH = str(APP_DIR / _cfg(CAMERA_CFG, "face_model_path", default="face_detection_yunet_2023mar.onnx"))
BODY_MODEL_PATH = str(APP_DIR / _cfg(CAMERA_CFG, "body_model_path", default="yolov8n.onnx"))
BODY_ENABLED = bool(_cfg(CAMERA_CFG, "body_enabled", default=True))
BODY_CONFIDENCE_THRESHOLD = float(_cfg(CAMERA_CFG, "body_confidence_threshold", default=0.35))
BODY_NMS_THRESHOLD = float(_cfg(CAMERA_CFG, "body_nms_threshold", default=0.45))
BODY_INPUT_SIZE = int(_cfg(CAMERA_CFG, "body_input_size", default=640))
BODY_DETECT_STRIDE = int(_cfg(CAMERA_CFG, "body_detect_stride", default=3))
BODY_TRACK_SERVO_ALPHA = float(_cfg(CAMERA_CFG, "body_track_servo_alpha", default=0.30))
BODY_AIM_Y_RATIO = float(_cfg(CAMERA_CFG, "body_aim_y_ratio", default=0.22))
BODY_CACHE_SEC = float(_cfg(CAMERA_CFG, "body_cache_sec", default=0.75))
# Use a larger 16:9 main stream for wider/detail-rich source frames (wider field of view)
CAMERA_MAIN_RES = tuple(_cfg(CAMERA_CFG, "main_res", default=[1920, 1080]))
# Balanced 16:9 processing for detail + CPU headroom (better for emotion models)
CAMERA_RES = tuple(_cfg(CAMERA_CFG, "detect_res", default=[1280, 720]))
STREAM_RES = tuple(_cfg(STREAM_CFG, "res", default=[320, 180]))   # Downscaled for web preview (maintain 16:9, no lag)
CONFIDENCE_THRESHOLD = float(_cfg(CAMERA_CFG, "confidence_threshold", default=0.6))
NMS_THRESHOLD = float(_cfg(CAMERA_CFG, "nms_threshold", default=0.3))
# Camera adjustments
CAMERA_ROTATE_180 = bool(_cfg(CAMERA_CFG, "rotate_180", default=False))
# If stream colors look wrong, swap R/B for MJPEG output
STREAM_SWAP_RB = bool(_cfg(CAMERA_CFG, "stream_swap_rb", default=True))

# Eye Interaction Config
MAX_X_OFFSET = 30
MAX_Y_OFFSET = 22
FACE_ROLL_MULT = 0.75
FACE_ROLL_MAX_DEG = 10.0
EYE_BOUND_MARGIN = 8

# Blink Speed (Higher = Faster)
BLINK_SPEED_MIN = 2.0
BLINK_SPEED_MAX = 3.5
LOOK_SIDE_OFFSET = 16.0

# Distance-based behavior
CLOSE_FACE_AREA_RATIO = 0.05  # Trigger joy/excited when user is close (>5% of frame)
FAR_FACE_AREA_RATIO = 0.018   # Trigger squint when user is far (<1.8% of frame)
FAR_SQUINT_CHANCE = 0.08
FAR_SQUINT_MIN_SEC = 0.22
FAR_SQUINT_MAX_SEC = 0.55

# Reactive emotion behavior (surroundings-driven)
NO_FACE_GRACE_SEC = 0.9
NO_PERSON_HOLD_MIN_SEC = 2.8
NO_PERSON_HOLD_MAX_SEC = 5.2
PERSON_HOLD_MIN_SEC = 1.1
PERSON_HOLD_MAX_SEC = 2.8
DIRECTION_TRIGGER_NORM_X = 0.22
DIRECTION_HOLD_MIN_SEC = 0.6
DIRECTION_HOLD_MAX_SEC = 1.2
DIRECTION_COOLDOWN_SEC = 0.9

# Distance hysteresis to avoid rapid near/mid/far bouncing.
NEAR_EXIT_RATIO = CLOSE_FACE_AREA_RATIO * 0.82
FAR_EXIT_RATIO = FAR_FACE_AREA_RATIO * 1.25
THINK_POS_Y = -2

# --- Emotion Presets ---
EMOTION_PRESETS = {
    "idle": {"scale_w": 1.0, "scale_h": 1.0, "top_lid": 0.0, "bottom_lid": 0.0, "lid_angle": 0.0, "mirror_angle": True},
    "happy": {"scale_w": 1.10, "scale_h": 0.84, "top_lid": 0.0, "bottom_lid": 0.30, "lid_angle": -6.0, "mirror_angle": True},
    "sad": {"scale_w": 0.98, "scale_h": 0.96, "top_lid": 0.12, "bottom_lid": 0.0, "lid_angle": -8.0, "mirror_angle": True, "pos": (0, 4)},
    "surprised": {"scale_w": 0.98, "scale_h": 1.12, "top_lid": 0.0, "bottom_lid": 0.0, "lid_angle": 0.0, "mirror_angle": True},
    "suspicious": {"scale_w": 1.06, "scale_h": 0.74, "top_lid": 0.38, "bottom_lid": 0.35, "lid_angle": 0.0, "mirror_angle": True},
    "sleepy": {"scale_w": 1.04, "scale_h": 0.88, "top_lid": 0.56, "bottom_lid": 0.0, "lid_angle": 0.0, "mirror_angle": True},
    "looking_left_natural": {"scale_w": 1.02, "scale_h": 0.98, "top_lid": 0.0, "bottom_lid": 0.05, "lid_angle": -3.0, "mirror_angle": False},
    "looking_right_natural": {"scale_w": 1.02, "scale_h": 0.98, "top_lid": 0.0, "bottom_lid": 0.05, "lid_angle": 3.0, "mirror_angle": False},
    "looking_left_happy": {"scale_w": 1.10, "scale_h": 0.84, "top_lid": 0.0, "bottom_lid": 0.30, "lid_angle": -6.0, "mirror_angle": False},
    "looking_right_happy": {"scale_w": 1.10, "scale_h": 0.84, "top_lid": 0.0, "bottom_lid": 0.30, "lid_angle": 6.0, "mirror_angle": False},
    "looking_left": {"scale_w": 1.02, "scale_h": 0.98, "top_lid": 0.0, "bottom_lid": 0.05, "lid_angle": -3.0, "mirror_angle": False},
    "looking_right": {"scale_w": 1.02, "scale_h": 0.98, "top_lid": 0.0, "bottom_lid": 0.05, "lid_angle": 3.0, "mirror_angle": False},
    "excited": {"scale_w": 1.14, "scale_h": 0.80, "top_lid": 0.0, "bottom_lid": 0.24, "lid_angle": 0.0, "mirror_angle": True},
    "calm": {"scale_w": 1.03, "scale_h": 0.90, "top_lid": 0.16, "bottom_lid": 0.12, "lid_angle": 0.0, "mirror_angle": True},
    "curious": {"scale_w": 1.02, "scale_h": 1.03, "top_lid": 0.0, "bottom_lid": 0.13, "lid_angle": 4.0, "mirror_angle": False},
    "afraid": {"scale_w": 0.92, "scale_h": 1.12, "top_lid": 0.0, "bottom_lid": 0.0, "lid_angle": 0.0, "mirror_angle": True},
    "thinking": {"scale_w": 1.0, "scale_h": 1.0, "top_lid": 0.0, "bottom_lid": 0.0, "lid_angle": 0.0, "mirror_angle": True, "pos": (0, THINK_POS_Y)},
    "concentrating": {"scale_w": 0.96, "scale_h": 0.84, "top_lid": 0.16, "bottom_lid": 0.08, "lid_angle": 0.0, "mirror_angle": True, "pos": (0, -2)},
    "remembering": {"scale_w": 1.04, "scale_h": 1.03, "top_lid": 0.02, "bottom_lid": 0.0, "lid_angle": 0.0, "mirror_angle": True, "pos": (0, -6)},
    "attentive": {"scale_w": 1.08, "scale_h": 1.06, "top_lid": 0.0, "bottom_lid": 0.0, "lid_angle": 0.0, "mirror_angle": True},
    "engaged": {"scale_w": 1.02, "scale_h": 1.00, "top_lid": 0.04, "bottom_lid": 0.06, "lid_angle": 5.0, "mirror_angle": True},
    "amused": {"scale_w": 1.00, "scale_h": 0.98, "top_lid": 0.0, "bottom_lid": 0.14, "lid_angle": 3.0, "mirror_angle": False, "left_bias": {"bottom_lid": 0.09, "lid_angle": 4.0}, "right_bias": {"bottom_lid": 0.02, "lid_angle": -2.0}},
    "warm": {"scale_w": 1.06, "scale_h": 1.00, "top_lid": 0.0, "bottom_lid": 0.16, "lid_angle": 2.0, "mirror_angle": True, "pos": (0, -4)},
    "awkward": {"scale_w": 0.96, "scale_h": 0.93, "top_lid": 0.10, "bottom_lid": 0.10, "lid_angle": 0.0, "mirror_angle": True, "pos": (0, 8)},
    "uncertain": {"scale_w": 0.98, "scale_h": 0.96, "top_lid": 0.08, "bottom_lid": 0.04, "lid_angle": 0.0, "mirror_angle": True, "pos": (0, -2), "left_bias": {"top_lid": 0.08, "pos_x": -4.0}, "right_bias": {"top_lid": 0.02, "pos_x": 4.0}},
    "proud": {"scale_w": 1.06, "scale_h": 1.02, "top_lid": 0.0, "bottom_lid": 0.0, "lid_angle": -2.0, "mirror_angle": True, "pos": (0, -8)},
    "playful": {"scale_w": 1.02, "scale_h": 1.00, "top_lid": 0.0, "bottom_lid": 0.06, "lid_angle": 0.0, "mirror_angle": False, "left_bias": {"scale_w": 0.10, "lid_angle": 6.0}, "right_bias": {"scale_w": -0.06, "lid_angle": -6.0}},
    "squint": {"scale_w": 1.0, "scale_h": 0.62, "top_lid": 0.42, "bottom_lid": 0.35, "lid_angle": 0.0, "mirror_angle": True},
}

SPECIAL_EMOTIONS = ["happy", "suspicious", "sleepy"]

KEY_TO_EMOTION = {
    "0": "idle",
    "1": "happy",
    "2": "sad",
    "3": "angry",
    "4": "surprised",
    "5": "suspicious",
    "6": "sleepy",
    "7": "looking_left_natural",
    "8": "looking_right_natural",
    "9": "excited",
    "a": "calm",
    "s": "curious",
    "d": "afraid",
    "f": "attentive",
    "g": "thinking",
    "m": "amused",
    "p": "playful",
    "w": "warm",
    "o": "awkward",
    "k": "concentrating",
    "r": "remembering",
    "x": "proud",
}

# MJPEG Stream Config (for headless SSH viewing)
STREAM_ENABLED = bool(_cfg(STREAM_CFG, "enabled", default=True)) and not ARGS.no_stream
STREAM_HOST = str(_cfg(STREAM_CFG, "host", default="0.0.0.0"))
STREAM_PORT = int(_cfg(STREAM_CFG, "port", default=8081))
STREAM_FPS = int(_cfg(STREAM_CFG, "fps", default=8))
STREAM_JPEG_QUALITY = int(_cfg(STREAM_CFG, "jpeg_quality", default=70))
RENDER_FPS = int(_cfg(STREAM_CFG, "render_fps", default=24))
VISION_FPS = int(_cfg(STREAM_CFG, "vision_fps", default=10))
EMOTION_INTENSITY = 1.0
EMOTION_LOG_TO_TERMINAL = True
EMOTION_CHANGE_COOLDOWN = 0.75
TERMINAL_CONTROL_ENABLED = True

# ESP32 head servo tracking config. Protocol and smoothing follow voice-agentv4
# tests/test_head_servos.py, but this file is standalone for v5.
SERVO_ENABLED = bool(SERVO_CFG.get("enabled", True)) and not ARGS.no_servo
SERVO_PORT = ARGS.port if ARGS.port is not None else str(SERVO_CFG.get("port", ""))
SERVO_BAUD = int(ARGS.baud if ARGS.baud is not None else SERVO_CFG.get("baud", 115200))
PAN_MIN = float(SERVO_CFG.get("pan_min", 40.0))
PAN_MAX = float(SERVO_CFG.get("pan_max", 120.0))
TILT_MIN = float(SERVO_CFG.get("tilt_min", 100.0))
TILT_MAX = float(SERVO_CFG.get("tilt_max", 120.0))
PAN_CENTER = float(SERVO_CFG.get("pan_center", (PAN_MIN + PAN_MAX) * 0.5))
TILT_CENTER = float(SERVO_CFG.get("tilt_center", (TILT_MIN + TILT_MAX) * 0.5))
PAN_TRACK_RANGE = float(SERVO_CFG.get("pan_track_range", 26.0))
TILT_TRACK_RANGE = float(SERVO_CFG.get("tilt_track_range", 12.0))
PAN_SIGN = float(SERVO_CFG.get("pan_sign", 1.0))
TILT_SIGN = float(SERVO_CFG.get("tilt_sign", -1.0))
FACE_SERVO_DEADZONE_X = float(SERVO_CFG.get("deadzone_x", 0.04))
FACE_SERVO_DEADZONE_Y = float(SERVO_CFG.get("deadzone_y", 0.05))
SERVO_LOOP_HZ = float(SERVO_CFG.get("loop_hz", 100.0))
SERVO_NO_FACE_HOME_SEC = float(SERVO_CFG.get("no_face_home_sec", 0.8))
SERVO_DEBUG_HZ = float(SERVO_CFG.get("debug_hz", 2.0))
SERVO_FACE_ALPHA_X = float(SERVO_CFG.get("face_alpha_x", 0.22))
SERVO_FACE_ALPHA_Y = float(SERVO_CFG.get("face_alpha_y", 0.06))
PAN_PID_KP = float(SERVO_CFG.get("pan_pid_kp", 0.95))
PAN_PID_KI = float(SERVO_CFG.get("pan_pid_ki", 0.02))
PAN_PID_KD = float(SERVO_CFG.get("pan_pid_kd", 0.14))
TILT_PID_KP = float(SERVO_CFG.get("tilt_pid_kp", 0.38))
TILT_PID_KI = float(SERVO_CFG.get("tilt_pid_ki", 0.0))
TILT_PID_KD = float(SERVO_CFG.get("tilt_pid_kd", 0.08))
PID_INTEGRAL_LIMIT = float(SERVO_CFG.get("pid_integral_limit", 0.6))
PREDICT_HOLD_SEC = float(SERVO_CFG.get("predict_hold_sec", 1.2))
PREDICT_EDGE_NORM = float(SERVO_CFG.get("predict_edge_norm", 0.55))
PREDICT_GAIN = float(SERVO_CFG.get("predict_gain", 0.72))
LOST_SEARCH_HOLD_SEC = float(SERVO_CFG.get("lost_search_hold_sec", 3.2))
LOST_SEARCH_VELOCITY_GAIN = float(SERVO_CFG.get("lost_search_velocity_gain", 0.45))
LOST_SEARCH_MIN_NORM_X = float(SERVO_CFG.get("lost_search_min_norm_x", 0.22))
LOST_SEARCH_BASE_STEP_DEG = float(SERVO_CFG.get("lost_search_base_step_deg", 5.0))
LOST_SEARCH_BASE_COOLDOWN_SEC = float(SERVO_CFG.get("lost_search_base_cooldown_sec", 1.2))
WANDER_PAN_AMP_DEG = float(SERVO_CFG.get("wander_pan_amp_deg", 26.0))
WANDER_STEP_MIN_DEG = float(SERVO_CFG.get("wander_step_min_deg", 6.0))
WANDER_STEP_MAX_DEG = float(SERVO_CFG.get("wander_step_max_deg", 28.0))
WANDER_HOLD_MIN_SEC = float(SERVO_CFG.get("wander_hold_min_sec", 1.3))
WANDER_HOLD_MAX_SEC = float(SERVO_CFG.get("wander_hold_max_sec", 5.8))
WANDER_JUMP_CHANCE = float(SERVO_CFG.get("wander_jump_chance", 0.34))
WANDER_ARRIVAL_DEG = float(SERVO_CFG.get("wander_arrival_deg", 2.0))
WANDER_TILT_MAX_UP_DEG = float(SERVO_CFG.get("wander_tilt_max_up_deg", 1.1))
WANDER_TILT_MAX_DOWN_DEG = float(SERVO_CFG.get("wander_tilt_max_down_deg", 1.6))
WANDER_THINKING_HOLD_CHANCE = float(SERVO_CFG.get("wander_thinking_hold_chance", 0.34))
WANDER_THINKING_HOLD_MIN_SEC = float(SERVO_CFG.get("wander_thinking_hold_min_sec", 3.0))
WANDER_THINKING_HOLD_MAX_SEC = float(SERVO_CFG.get("wander_thinking_hold_max_sec", 7.5))
WANDER_LONG_STARE_CHANCE = float(SERVO_CFG.get("wander_long_stare_chance", 0.12))
WANDER_PAN_TARGET_ALPHA = float(SERVO_CFG.get("wander_pan_target_alpha", 0.10))
WANDER_TILT_TARGET_ALPHA = float(SERVO_CFG.get("wander_tilt_target_alpha", 0.035))
MOTION_EMOTION_MIN_SEC = float(SERVO_CFG.get("motion_emotion_min_sec", 1.0))
ATTENTION_HOLD_MIN_SEC = float(SERVO_CFG.get("attention_hold_min_sec", 1.0))
ATTENTION_HOLD_MAX_SEC = float(SERVO_CFG.get("attention_hold_max_sec", 2.2))
MULTI_FACE_DEBOUNCE_SEC = float(SERVO_CFG.get("multi_face_debounce_sec", 0.18))
MULTI_FACE_CENTER_CHANCE = float(SERVO_CFG.get("multi_face_center_chance", 0.28))
MULTI_FACE_ALTERNATE_CHANCE = float(SERVO_CFG.get("multi_face_alternate_chance", 0.42))
MULTI_FACE_TRACK_SERVO_ALPHA = float(SERVO_CFG.get("multi_face_track_servo_alpha", 0.34))
MULTI_FACE_TRACK_GAIN = float(SERVO_CFG.get("multi_face_track_gain", 0.62))
SOCIAL_MULTI_GRACE_SEC = float(SERVO_CFG.get("social_multi_grace_sec", 2.4))
TARGET_GLIDE_FREQ = float(SERVO_CFG.get("target_glide_freq", 2.8))
TARGET_GLIDE_DAMP = float(SERVO_CFG.get("target_glide_damp", 0.82))

BASE_ENABLED = bool(BASE_CFG.get("enabled", False))
BASE_COUNTS_PER_DEGREE = float(BASE_CFG.get("counts_per_degree", 1.0))
BASE_ZERO_ON_START = bool(BASE_CFG.get("zero_on_start", False))
BASE_SIGN = float(BASE_CFG.get("sign", 1.0))
BASE_TRIGGER_NORM_X = float(BASE_CFG.get("trigger_norm_x", 0.52))
BASE_TRIGGER_HOLD_SEC = float(BASE_CFG.get("trigger_hold_sec", 1.2))
BASE_COOLDOWN_SEC = float(BASE_CFG.get("cooldown_sec", 2.4))
BASE_MIN_STEP_DEG = float(BASE_CFG.get("min_step_deg", 0.8))
BASE_MAX_STEP_DEG = float(BASE_CFG.get("max_step_deg", 2.5))
BASE_NORM_TO_DEG_GAIN = float(BASE_CFG.get("norm_to_deg_gain", 2.2))
BASE_PAN_SOFT_LIMIT_DEG = float(BASE_CFG.get("pan_soft_limit_deg", 18.0))
BASE_PAN_RECENTER_BIAS = float(BASE_CFG.get("pan_recenter_bias", 0.35))
BASE_TRACK_COMPENSATION_GAIN = float(BASE_CFG.get("track_compensation_gain", 0.45))
BASE_FAST_FACE_ENABLED = bool(BASE_CFG.get("fast_face_enabled", True))
BASE_FAST_FACE_VELOCITY_NORM_SEC = float(BASE_CFG.get("fast_face_velocity_norm_sec", 3.0))
BASE_FAST_FACE_COOLDOWN_SEC = float(BASE_CFG.get("fast_face_cooldown_sec", 0.9))
BASE_FAST_FACE_MIN_STEP_DEG = float(BASE_CFG.get("fast_face_min_step_deg", 4.0))
BASE_FAST_FACE_MAX_STEP_DEG = float(BASE_CFG.get("fast_face_max_step_deg", 10.0))
BASE_FAST_FACE_VELOCITY_TO_DEG_GAIN = float(BASE_CFG.get("fast_face_velocity_to_deg_gain", 2.2))
BASE_FAST_FACE_COMPENSATION_GAIN = float(BASE_CFG.get("fast_face_compensation_gain", 0.55))
BASE_WANDER_ENABLED = bool(BASE_CFG.get("wander_enabled", True))
BASE_WANDER_STEP_DEG = float(BASE_CFG.get("wander_step_deg", 1.0))
BASE_WANDER_COOLDOWN_SEC = float(BASE_CFG.get("wander_cooldown_sec", 5.5))
BASE_WANDER_MIN_PAN_OFFSET_DEG = float(BASE_CFG.get("wander_min_pan_offset_deg", 10.0))
BASE_WANDER_COMPENSATION_GAIN = float(BASE_CFG.get("wander_compensation_gain", 0.25))
BASE_MAX_TOTAL_AUTO_DEG = float(BASE_CFG.get("max_total_auto_deg", 18.0))
BASE_ALLOW_MULTI_FACE = bool(BASE_CFG.get("allow_multi_face", False))
BASE_WAIT_FOR_ACK = bool(BASE_CFG.get("wait_for_ack", False))
BASE_ERROR_BACKOFF_SEC = float(BASE_CFG.get("error_backoff_sec", 45.0))
BASE_REQUIRE_CALIBRATED_CPD = bool(BASE_CFG.get("require_calibrated_cpd", True))

PAN_MOTION = HeadMotionParams(
    max_vel_pos=float(SERVO_CFG.get("pan_max_vel", 22.0)),
    max_vel_neg=float(SERVO_CFG.get("pan_max_vel", 22.0)),
    accel=float(SERVO_CFG.get("pan_accel", 55.0)),
    decel=float(SERVO_CFG.get("pan_decel", 85.0)),
    vel_blend=float(SERVO_CFG.get("head_vel_blend", 0.28)),
    track_gain=float(SERVO_CFG.get("pan_track_gain", 1.6)),
    goal_deadband_deg=float(SERVO_CFG.get("goal_deadband_deg", 0.04)),
)
TILT_MOTION = HeadMotionParams(
    max_vel_pos=float(SERVO_CFG.get("tilt_max_vel_up", 18.0)),
    max_vel_neg=float(SERVO_CFG.get("tilt_max_vel_down", 10.0)),
    accel=float(SERVO_CFG.get("tilt_accel", 45.0)),
    decel=float(SERVO_CFG.get("tilt_decel", 70.0)),
    vel_blend=float(SERVO_CFG.get("tilt_head_vel_blend", 0.14)),
    decel_boost_dir=-1.0,
    decel_boost_mult=float(SERVO_CFG.get("tilt_decel_down_mult", 1.85)),
    track_gain=float(SERVO_CFG.get("tilt_track_gain", 2.4)),
    goal_deadband_deg=float(SERVO_CFG.get("goal_deadband_deg", 0.04)),
)



# --- Round Eye Class (PIL Version with emotion controls) ---
class BlockyEye:
    def __init__(self, x, y, scale=1.0, is_left=True):
        self.base_x, self.base_y = x, y
        self.current_pos = [float(x), float(y)]
        self.target_pos = [float(x), float(y)]

        self.vel_x = 0.0
        self.vel_y = 0.0

        self.base_w = EYE_SIZE * scale
        self.base_h = EYE_SIZE * scale

        self.current_w = self.base_w
        self.current_h = self.base_h
        self.target_w = self.base_w
        self.target_h = self.base_h

        self.vel_w = 0.0
        self.vel_h = 0.0

        self.w = self.base_w
        self.h = self.base_h

        self.current_rotation = 0.0
        self.target_rotation = 0.0
        self.rot_sensitivity = random.uniform(0.3, 0.5)
        self.rot_speed = random.uniform(0.15, 0.25)

        self.is_left = is_left
        self.blink_state = "IDLE"
        self.vy = 0
        self.blink_speed_mult = 1.0

        self.target_scale_w = 1.0
        self.target_scale_h = 1.0
        self.scale_w = 1.0
        self.scale_h = 1.0
        self.scale_w_vel = 0.0
        self.scale_h_vel = 0.0
        self.top_lid = 0.0
        self.bottom_lid = 0.0
        self.lid_angle = 0.0
        self.top_lid_vel = 0.0
        self.bottom_lid_vel = 0.0
        self.lid_angle_vel = 0.0
        self.target_top_lid = 0.0
        self.target_bottom_lid = 0.0
        self.target_lid_angle = 0.0
        self.current_emotion = "idle"
        self.last_emotion_change_time = 0.0
        self.pending_emotion = None
        self.pending_intensity = 1.0
        self.pending_apply_time = 0.0
        self.happy_phase = random.uniform(0.0, math.pi * 2)
        self.happy_burst_until = 0.0

        self.noise_t = random.uniform(0, 100)
        self.emotion_pos_bias_x = 0.0
        self.emotion_pos_bias_y = 0.0

    def start_blink(self, speed_mult=None):
        if self.blink_state == "IDLE":
            self.blink_state = "DROPPING"
            if speed_mult is not None:
                self.blink_speed_mult = speed_mult
            else:
                self.blink_speed_mult = random.uniform(BLINK_SPEED_MIN, BLINK_SPEED_MAX)
            self.vy = 40 * self.blink_speed_mult

    def set_emotion(self, emotion_name: str, intensity: float = 1.0, force: bool = False):
        if emotion_name not in EMOTION_PRESETS:
            return

        now = time.time()
        if (
            emotion_name != self.current_emotion
            and not force
            and (now - self.last_emotion_change_time) < EMOTION_CHANGE_COOLDOWN
        ):
            # Keep only the latest requested emotion to avoid stale transitions.
            self.pending_emotion = emotion_name
            self.pending_intensity = intensity
            self.pending_apply_time = self.last_emotion_change_time + EMOTION_CHANGE_COOLDOWN
            return

        if emotion_name == "happy" and self.current_emotion != "happy":
            self.happy_burst_until = time.time() + 0.35

        if emotion_name != self.current_emotion:
            self.last_emotion_change_time = now
        self.pending_emotion = None
        self.current_emotion = emotion_name
        preset = EMOTION_PRESETS[emotion_name]
        idle = EMOTION_PRESETS["idle"]
        side_bias = preset.get("left_bias", {}) if self.is_left else preset.get("right_bias", {})

        intensity = max(0.0, min(1.0, intensity))
        self.emotion_pos_bias_x = side_bias.get("pos_x", 0.0) * intensity
        self.emotion_pos_bias_y = side_bias.get("pos_y", 0.0) * intensity
        scale_w = idle["scale_w"] + (preset["scale_w"] - idle["scale_w"]) * intensity
        scale_h = idle["scale_h"] + (preset["scale_h"] - idle["scale_h"]) * intensity
        top_lid = idle["top_lid"] + (preset["top_lid"] - idle["top_lid"]) * intensity
        bottom_lid = idle["bottom_lid"] + (preset["bottom_lid"] - idle["bottom_lid"]) * intensity
        lid_angle = idle["lid_angle"] + (preset["lid_angle"] - idle["lid_angle"]) * intensity

        if side_bias:
            scale_w += side_bias.get("scale_w", 0.0) * intensity
            scale_h += side_bias.get("scale_h", 0.0) * intensity
            top_lid += side_bias.get("top_lid", 0.0) * intensity
            bottom_lid += side_bias.get("bottom_lid", 0.0) * intensity
            lid_angle += side_bias.get("lid_angle", 0.0) * intensity

        self.target_scale_w = scale_w
        self.target_scale_h = scale_h
        self.target_top_lid = top_lid
        self.target_bottom_lid = bottom_lid

        if preset.get("mirror_angle", True) and not self.is_left and abs(lid_angle) > 0:
            lid_angle = -lid_angle
        self.target_lid_angle = lid_angle

    def update(self):
        if self.pending_emotion is not None and time.time() >= self.pending_apply_time:
            queued_emotion = self.pending_emotion
            queued_intensity = self.pending_intensity
            self.pending_emotion = None
            self.set_emotion(queued_emotion, queued_intensity, force=True)

        if self.blink_state == "IDLE":
            t = time.time() + self.noise_t
            noise_x = (math.sin(t * 1.3) * 0.2 + math.sin(t * 0.7) * 0.1)
            noise_y = (math.cos(t * 1.1) * 0.2 + math.cos(t * 0.9) * 0.1)

            target_x_phys = self.target_pos[0] + noise_x
            target_y_phys = self.target_pos[1] + noise_y

            # Use per-frame temporary eyelid targets so one-shot effects
            # (like happy burst) do not permanently override emotion targets.
            top_lid_target = self.target_top_lid
            bottom_lid_target = self.target_bottom_lid
            lid_angle_target = self.target_lid_angle

            burst_active = time.time() < self.happy_burst_until
            if burst_active:
                target_y_phys -= 8.0
                # Keep the happy burst as a quick upward motion only.
                # Avoid temporary lid squeeze to prevent an initial "squint" look.

            # Happy: small jump + wiggle
            if self.current_emotion == "happy":
                ht = time.time() * 6.0 + self.happy_phase
                target_y_phys -= 2.5 + math.sin(ht) * 2.0
                target_x_phys += math.sin(ht * 1.7) * 1.2
            elif self.current_emotion.startswith("looking_") and "left" in self.current_emotion:
                target_x_phys -= LOOK_SIDE_OFFSET
            elif self.current_emotion.startswith("looking_") and "right" in self.current_emotion:
                target_x_phys += LOOK_SIDE_OFFSET

            emotion_offset = EMOTION_PRESETS[self.current_emotion].get("pos", (0, 0))
            target_x_phys += emotion_offset[0] + self.emotion_pos_bias_x
            target_y_phys += emotion_offset[1] + self.emotion_pos_bias_y

            dx = target_x_phys - self.current_pos[0]
            dy = target_y_phys - self.current_pos[1]

            speed_x = 0.20
            speed_y = 0.22
            if dy < -1.0:
                speed_y = 0.14
            elif dy > 1.0:
                speed_y = 0.38

            self.current_pos[0] += dx * speed_x
            self.current_pos[1] += dy * speed_y

            self.vel_x = dx * speed_x
            self.vel_y = dy * speed_y

            rel_x = self.current_pos[0] - self.base_x
            rel_y = self.current_pos[1] - self.base_y
            look_rot = (rel_x * 0.5 + rel_y * 0.8) * self.rot_sensitivity
            if self.current_emotion == "happy":
                look_rot += math.sin(time.time() * 8.0 + self.happy_phase) * 1.2
            final_target_rot = look_rot + self.target_rotation
            self.current_rotation += (final_target_rot - self.current_rotation) * self.rot_speed

            t = time.time()
            breath_w = (math.sin(t * 1.5 + self.base_x) * 1.5 + math.sin(t * 0.5) * 1.0)
            breath_h = (math.cos(t * 1.8 + self.base_y) * 1.5 + math.cos(t * 0.6) * 1.0)

            move_stretch_x = (dx * speed_x) * 2.5
            move_stretch_y = (dy * speed_y) * 2.5
            if self.current_emotion == "surprised":
                # Keep surprised wide-open but avoid rubbery stretching.
                move_stretch_x *= 0.25
                move_stretch_y *= 0.25

            k = 0.12
            d = 0.7
            if self.current_emotion == "surprised":
                # Snap into surprised quickly.
                k = 0.30
                d = 0.52
            self.scale_w_vel = (self.scale_w_vel + (self.target_scale_w - self.scale_w) * k) * d
            self.scale_h_vel = (self.scale_h_vel + (self.target_scale_h - self.scale_h) * k) * d
            self.scale_w += self.scale_w_vel
            self.scale_h += self.scale_h_vel

            self.top_lid_vel = (self.top_lid_vel + (top_lid_target - self.top_lid) * k) * d
            self.bottom_lid_vel = (self.bottom_lid_vel + (bottom_lid_target - self.bottom_lid) * k) * d
            self.lid_angle_vel = (self.lid_angle_vel + (lid_angle_target - self.lid_angle) * k) * d

            self.top_lid += self.top_lid_vel
            self.bottom_lid += self.bottom_lid_vel
            self.lid_angle += self.lid_angle_vel

            self.target_w = (self.base_w * self.scale_w) + breath_w + (move_stretch_x * 0.5)
            self.target_h = (self.base_h * self.scale_h) + breath_h - (move_stretch_y * 0.2)

        elif self.blink_state == "DROPPING":
            self.vy += 10 * self.blink_speed_mult
            self.current_pos[1] += self.vy
            self.current_w = self.base_w - 10
            self.current_h = self.base_h + 20
            self.target_w = self.current_w
            self.target_h = self.current_h

            if self.current_pos[1] + self.current_h // 2 >= FLOOR_Y:
                self.current_pos[1] = FLOOR_Y - self.current_h // 2
                self.blink_state = "SQUASHING"
                self.velocity = [0.0, 0.0]

        elif self.blink_state == "SQUASHING":
            squeeze_speed = 65 * self.blink_speed_mult
            spread_speed = 40 * self.blink_speed_mult
            self.current_h -= squeeze_speed
            self.current_w += spread_speed
            self.current_pos[1] = FLOOR_Y - self.current_h // 2

            if self.current_h <= 22:
                self.current_h = 22
                self.blink_state = "JUMPING"

        elif self.blink_state == "JUMPING":
            recovery_speed = max(0.15, min(0.95, 0.85 * self.blink_speed_mult))
            self.current_h += (self.base_h - self.current_h) * recovery_speed
            self.current_w += (self.base_w - self.current_w) * recovery_speed

            self.vel_x = (self.vel_x + (self.target_pos[0] - self.current_pos[0]) * 0.1) * 0.8
            self.current_pos[0] += self.vel_x

            target_y = self.target_pos[1]
            self.current_pos[1] += (target_y - self.current_pos[1]) * 0.8

            if abs(self.current_h - self.base_h) < 5 and abs(self.current_pos[1] - target_y) < 5:
                self.current_h = self.base_h
                self.current_w = self.base_w
                self.blink_state = "IDLE"
                self.vy = 0
                self.vel_x = 0
                self.vel_y = 0

        if self.blink_state == "IDLE":
            k = 0.08
            d = 0.90
            force_w = (self.target_w - self.current_w) * k
            self.vel_w = (self.vel_w + force_w) * d
            self.current_w += self.vel_w

            force_h = (self.target_h - self.current_h) * k
            self.vel_h = (self.vel_h + force_h) * d
            self.current_h += self.vel_h
        else:
            self.vel_w = 0
            self.vel_h = 0

        self.w = self.current_w
        self.h = self.current_h

        # Keep the eye fully inside the display area.
        half_w = max(2.0, self.w * 0.5)
        half_h = max(2.0, self.h * 0.5)
        min_x = half_w
        max_x = SCREEN_WIDTH - half_w
        min_y = half_h
        max_y = SCREEN_HEIGHT - half_h

        if min_x > max_x:
            self.current_pos[0] = SCREEN_WIDTH * 0.5
        else:
            self.current_pos[0] = max(min_x, min(max_x, self.current_pos[0]))

        if min_y > max_y:
            self.current_pos[1] = SCREEN_HEIGHT * 0.5
        else:
            self.current_pos[1] = max(min_y, min(max_y, self.current_pos[1]))

    def draw_radial_rect(self, draw, x, y, w, h, color, radius, pupil_offset=(0,0)):
        center_x = x + w/2
        center_y = y + h/2
        shift_x = pupil_offset[0] * w * 0.22
        shift_y = pupil_offset[1] * h * 0.18
        cx = center_x + shift_x
        cy = center_y + shift_y
        x0 = cx - w / 2
        y0 = cy - h / 2
        x1 = cx + w / 2
        y1 = cy + h / 2
        draw.ellipse([x0, y0, x1, y1], fill=color)

    def draw_eyelids(self, eye_img, rect):
        x0, y0, x1, y1 = rect
        w = int(x1 - x0)
        h = int(y1 - y0)
        lid_color = BG_COLOR

        # Angle-aware padding keeps diagonal lids from exposing tiny bright slivers.
        angle_abs = abs(self.lid_angle)
        angle_pad = int(min(8.0, 2.0 + angle_abs * 0.18))
        top_bleed = 6 + angle_pad
        bottom_bleed = 6 + angle_pad

        def _crop_rotated_fringes(img, px):
            if px <= 0:
                return img
            if img.width <= px * 2 or img.height <= px * 2:
                return img
            return img.crop((px, px, img.width - px, img.height - px))

        if self.top_lid > 0.01:
            lid_h = max(1, int(h * self.top_lid))
            lid_width = int(w + (top_bleed * 2) + (angle_pad * 2))
            lid_height = int(lid_h + 14 + top_bleed + angle_pad)
            lid_src = Image.new("RGBA", (lid_width, lid_height), (*lid_color, 255))
            if angle_abs > 0.1:
                lid_src = lid_src.rotate(self.lid_angle, resample=Image.BICUBIC, expand=True)
                fringe_px = 1 + (1 if angle_abs > 10.0 else 0)
                lid_src = _crop_rotated_fringes(lid_src, fringe_px)
            lid_x = int(x0 + (w / 2) - (lid_src.width / 2))
            lid_y = int(y0 - top_bleed - (angle_pad // 2))
            eye_img.alpha_composite(lid_src, (lid_x, lid_y))

        if self.bottom_lid > 0.01:
            lid_h = max(1, int(h * self.bottom_lid))
            lid_width = int(w + (bottom_bleed * 2) + (angle_pad * 2))
            lid_height = int(lid_h + 12 + bottom_bleed + angle_pad)
            lid_src = Image.new("RGBA", (lid_width, lid_height), (*lid_color, 255))
            if angle_abs > 0.1:
                lid_src = lid_src.rotate(self.lid_angle, resample=Image.BICUBIC, expand=True)
                fringe_px = 1 + (1 if angle_abs > 10.0 else 0)
                lid_src = _crop_rotated_fringes(lid_src, fringe_px)
            lid_x = int(x0 + (w / 2) - (lid_src.width / 2))
            lid_y = int(y1 + bottom_bleed + (angle_pad // 2) - lid_src.height)
            eye_img.alpha_composite(lid_src, (lid_x, lid_y))

        # Safety seal strips close residual anti-aliased seams without bulky lids.
        seal_draw = ImageDraw.Draw(eye_img)
        if self.top_lid > 0.01:
            top_seal_h = max(1, min(4, int(1 + (h * self.top_lid * 0.06) + (angle_pad * 0.25))))
            seal_draw.rectangle(
                [int(x0) - 1, int(y0), int(x1) + 1, int(y0) + top_seal_h],
                fill=(*lid_color, 255),
            )
        if self.bottom_lid > 0.01:
            bot_seal_h = max(1, min(4, int(1 + (h * self.bottom_lid * 0.06) + (angle_pad * 0.25))))
            seal_draw.rectangle(
                [int(x0) - 1, int(y1) - bot_seal_h, int(x1) + 1, int(y1)],
                fill=(*lid_color, 255),
            )

    def draw(self, bg_image):
        draw_w = max(4, int(self.w))
        draw_h = max(4, int(self.h))

        eye_img_size = int(max(self.base_w, self.base_h) * 2.5)
        eye_img = Image.new("RGBA", (eye_img_size, eye_img_size), (0, 0, 0, 0))
        eye_draw = ImageDraw.Draw(eye_img)

        base_radius = int(min(self.base_w, self.base_h) * 0.25)
        corner_radius = min(base_radius, int(min(draw_w, draw_h) / 2))
        off_x = max(-1, min(1, (self.current_pos[0] - self.base_x) / 30.0))
        off_y = max(-1, min(1, (self.current_pos[1] - self.base_y) / 20.0))

        cx, cy = eye_img_size / 2, eye_img_size / 2
        x0 = cx - draw_w / 2
        y0 = cy - draw_h / 2
        x1 = cx + draw_w / 2
        y1 = cy + draw_h / 2

        self.draw_radial_rect(eye_draw, x0, y0, draw_w, draw_h, EYE_COLOR, corner_radius, (off_x, off_y))
        self.draw_eyelids(eye_img, (x0, y0, x1, y1))

        rotated = eye_img.rotate(self.current_rotation, resample=Image.BICUBIC, expand=False)

        paste_x = int(self.current_pos[0] - eye_img_size / 2)
        paste_y = int(self.current_pos[1] - eye_img_size / 2)
        bg_image.alpha_composite(rotated, (paste_x, paste_y))


# --- MJPEG Streaming Server ---
latest_frame = None
frame_lock = threading.Lock()
stream_server = None


class ThreadingHTTPServer(socketserver.ThreadingMixIn, HTTPServer):
    daemon_threads = True


class MJPEGHandler(BaseHTTPRequestHandler):
    def do_GET(self):
        if self.path not in ("/", "/stream"):
            self.send_error(404)
            return

        self.send_response(200)
        self.send_header("Age", "0")
        self.send_header("Cache-Control", "no-cache, private")
        self.send_header("Pragma", "no-cache")
        self.send_header("Content-Type", "multipart/x-mixed-replace; boundary=frame")
        self.end_headers()

        try:
            while True:
                with frame_lock:
                    frame = None if latest_frame is None else latest_frame.copy()

                if frame is None:
                    time.sleep(0.05)
                    continue

                # Encode to JPEG (frame is RGB)
                img = Image.fromarray(frame)
                buf = io.BytesIO()
                img.save(buf, format="JPEG", quality=STREAM_JPEG_QUALITY)
                jpg = buf.getvalue()

                self.wfile.write(b"--frame\r\n")
                self.wfile.write(b"Content-Type: image/jpeg\r\n")
                self.wfile.write(f"Content-Length: {len(jpg)}\r\n\r\n".encode("utf-8"))
                self.wfile.write(jpg)
                self.wfile.write(b"\r\n")
                time.sleep(1.0 / max(1, STREAM_FPS))
        except (BrokenPipeError, ConnectionResetError):
            return

    def log_message(self, format, *args):
        return


def start_stream_server():
    global stream_server
    stream_server = ThreadingHTTPServer((STREAM_HOST, STREAM_PORT), MJPEGHandler)
    thread = threading.Thread(target=stream_server.serve_forever, daemon=True)
    thread.start()
    print(f"MJPEG stream started: http://{STREAM_HOST}:{STREAM_PORT}/stream")


# --- Display Setup ---
print("Initializing Displays (Dual SPI)...")
disp_l = None
disp_r = None

# SPI 0 (Left Screen)
try:
    spi0 = board.SPI()
    disp_l = st7735.ST7735R(
        spi0, 
        rotation=0, 
        baudrate=24000000, 
        bgr=True,
        cs=digitalio.DigitalInOut(board.CE1),   
        dc=digitalio.DigitalInOut(board.D24),   
        rst=digitalio.DigitalInOut(board.D25)
    )
except Exception as e:
    print(f"Error init Left Display (SPI0): {e}")

# SPI 1 (Right Screen)
try:
    spi1 = busio.SPI(clock=board.D21, MOSI=board.D20, MISO=board.D19)
    disp_r = st7735.ST7735R(
        spi1, 
        rotation=0, 
        baudrate=24000000, 
        bgr=True,
        cs=digitalio.DigitalInOut(board.D18),   
        dc=digitalio.DigitalInOut(board.D23),   
        rst=digitalio.DigitalInOut(board.D27)
    )
except Exception as e:
    print(f"Error init Right Display (SPI1): {e}")


# --- Camera & Face Detector Setup ---
print("Initializing Picamera2...")
picam2 = None
try:
    picam2 = Picamera2()

    # Pi Camera v2: full sensor (3280x2464) for widest FOV — needs cma-256 in config.txt
    config = picam2.create_video_configuration(
        main={"format": "RGB888", "size": CAMERA_MAIN_RES},
        raw={"size": (3280, 2464)},
        buffer_count=1,
    )
    picam2.configure(config)
    picam2.set_controls({"ScalerCrop": (0, 0, 3280, 2464)})
    picam2.start()
    print(f"Camera started: Full sensor (3280x2464) -> Main ({CAMERA_MAIN_RES[0]}x{CAMERA_MAIN_RES[1]}), detect ({CAMERA_RES[0]}x{CAMERA_RES[1]})")
except Exception as e:
    print(f"Error starting Picamera2: {e}")
    sys.exit(1)

print("Initializing YuNet Face Detector...")
try:
    if not Path(FACE_MODEL_PATH).exists():
        print(f"Error: Face model not found at {FACE_MODEL_PATH}")
        sys.exit(1)
        
    detector = cv2.FaceDetectorYN.create(
        model=FACE_MODEL_PATH,
        config="",
        input_size=CAMERA_RES,
        score_threshold=CONFIDENCE_THRESHOLD,
        nms_threshold=NMS_THRESHOLD,
        top_k=5000,
        backend_id=cv2.dnn.DNN_BACKEND_OPENCV,
        target_id=cv2.dnn.DNN_TARGET_CPU
    )
    print("YuNet initialized.")
except Exception as e:
    print(f"Error initializing detector: {e}")
    sys.exit(1)

person_detector = None
if BODY_ENABLED:
    try:
        person_detector = PersonDetector(
            BODY_MODEL_PATH,
            confidence_threshold=BODY_CONFIDENCE_THRESHOLD,
            nms_threshold=BODY_NMS_THRESHOLD,
            input_size=BODY_INPUT_SIZE,
        )
        print(f"YOLO body detector initialized: {BODY_MODEL_PATH}")
    except Exception as e:
        print(f"Body detector disabled: {e}")


# --- MJPEG Stream ---
if STREAM_ENABLED:
    try:
        start_stream_server()
    except Exception as e:
        print(f"Error starting MJPEG stream: {e}")


# --- Eye Objects ---
center_x = SCREEN_WIDTH / 2
center_y = SCREEN_HEIGHT / 2

left_eye = BlockyEye(center_x, center_y, scale=1.0, is_left=True)
right_eye = BlockyEye(center_x, center_y, scale=1.0, is_left=False)
# Keep both eyes using identical dynamics to avoid drift during blink phases.
right_eye.noise_t = left_eye.noise_t
right_eye.rot_sensitivity = left_eye.rot_sensitivity
right_eye.rot_speed = left_eye.rot_speed
right_eye.happy_phase = left_eye.happy_phase
left_eye.set_emotion("idle", EMOTION_INTENSITY)
right_eye.set_emotion("idle", EMOTION_INTENSITY)

# Animation Loop Vars
running = True
next_blink_time = time.time() + random.uniform(3, 6)
last_blink_time = time.time()
smoothed_x_off = 0.0
smoothed_y_off = 0.0
smoothed_rotation = 0.0
current_emotion = "idle"  # Track current emotion to avoid redundant updates

target_lock = threading.Lock()
target_x_off = 0.0
target_y_off = 0.0
target_rotation = 0.0
target_squint = 0.0
target_is_close = False  # Track if user is close for emotion switching
target_face_detected = False
target_face_area_ratio = 0.0
target_face_norm_x = 0.0
target_face_norm_y = 0.0
target_face_count = 0
target_multi_face = False
target_face_candidates = []
target_body_detected = False
target_kind = "none"
squint_until = 0.0

last_seen_face_time = 0.0
distance_zone = "mid"
no_person_next_emotion = "sleepy"
next_emotion_change_time = time.time() + random.uniform(1.6, 3.0)
direction_cooldown_until = 0.0
last_multi_face_time = 0.0
emotion_history = []
EMOTION_HISTORY_LEN = 3
prev_target_x = 0.0
prev_target_y = 0.0
prev_target_rot = 0.0
manual_emotion_override = None
command_lock = threading.Lock()

def clamp_eye_target(eye):
    # Keep eye center inside the panel bounds even during shape changes.
    half_w = max(12.0, eye.base_w * 0.42)
    half_h = max(12.0, eye.base_h * 0.42)
    min_x = half_w + EYE_BOUND_MARGIN
    max_x = SCREEN_WIDTH - half_w - EYE_BOUND_MARGIN
    min_y = half_h + EYE_BOUND_MARGIN
    max_y = SCREEN_HEIGHT - half_h - EYE_BOUND_MARGIN
    eye.target_pos[0] = max(min_x, min(max_x, eye.target_pos[0]))
    eye.target_pos[1] = max(min_y, min(max_y, eye.target_pos[1]))


def trigger_synced_blink(speed_mult):
    # Align blink start conditions so both displays animate the same phase.
    avg_y = (left_eye.current_pos[1] + right_eye.current_pos[1]) * 0.5
    avg_w = (left_eye.current_w + right_eye.current_w) * 0.5
    avg_h = (left_eye.current_h + right_eye.current_h) * 0.5
    for eye in (left_eye, right_eye):
        eye.blink_state = "IDLE"
        eye.vy = 0
        eye.current_pos[1] = avg_y
        eye.current_w = avg_w
        eye.current_h = avg_h
        eye.w = avg_w
        eye.h = avg_h
    left_eye.start_blink(speed_mult)
    right_eye.start_blink(speed_mult)


def mirror_blink_state(master, slave):
    # Force exact blink phase matching once a blink is active.
    slave.blink_state = master.blink_state
    slave.vy = master.vy
    slave.current_pos[1] = master.current_pos[1]
    slave.current_w = master.current_w
    slave.current_h = master.current_h
    slave.target_w = master.target_w
    slave.target_h = master.target_h
    slave.w = master.w
    slave.h = master.h


def mirror_full_state(master, slave):
    # Keep both eyes identical by driving one master state.
    slave.blink_state = master.blink_state
    slave.vy = master.vy
    slave.current_pos[0] = master.current_pos[0]
    slave.current_pos[1] = master.current_pos[1]
    slave.target_pos[0] = master.target_pos[0]
    slave.target_pos[1] = master.target_pos[1]
    slave.current_w = master.current_w
    slave.current_h = master.current_h
    slave.target_w = master.target_w
    slave.target_h = master.target_h
    slave.current_rotation = master.current_rotation
    slave.target_rotation = master.target_rotation
    slave.scale_w = master.scale_w
    slave.scale_h = master.scale_h
    slave.target_scale_w = master.target_scale_w
    slave.target_scale_h = master.target_scale_h
    slave.top_lid = master.top_lid
    slave.bottom_lid = master.bottom_lid
    slave.lid_angle = master.lid_angle
    slave.target_top_lid = master.target_top_lid
    slave.target_bottom_lid = master.target_bottom_lid
    slave.target_lid_angle = master.target_lid_angle
    slave.w = master.w
    slave.h = master.h


def push_emotion_history(emotion_name):
    emotion_history.append(emotion_name)
    if len(emotion_history) > EMOTION_HISTORY_LEN:
        emotion_history.pop(0)


def apply_emotion(emotion_name):
    global current_emotion
    if emotion_name != current_emotion:
        left_eye.set_emotion(emotion_name, EMOTION_INTENSITY)
        right_eye.set_emotion(emotion_name, EMOTION_INTENSITY)
        current_emotion = emotion_name
        push_emotion_history(emotion_name)
        if EMOTION_LOG_TO_TERMINAL:
            ts = time.strftime("%H:%M:%S")
            print(f"[emotion {ts}] {emotion_name}")


def terminal_command_worker():
    global running, manual_emotion_override, EMOTION_INTENSITY, next_emotion_change_time

    print("Terminal control ready. Commands: emotion <name>, <shortcut>, auto, list, intensity <0..1>, blink, status, help")
    print("Shortcuts: 0-9, a, s, d")
    while running:
        try:
            raw = input().strip()
        except EOFError:
            time.sleep(0.1)
            continue
        except Exception:
            time.sleep(0.1)
            continue

        if not raw:
            continue

        cmd = raw.lower()
        parts = cmd.split()

        if cmd in ("help", "h", "?"):
            print("Commands: emotion <name> | <name> | <shortcut> | auto | list | intensity <0..1> | blink | status")
            print("Shortcuts: 0-9, a, s, d")
            continue

        if cmd in ("list", "emotions"):
            print("Available emotions:", ", ".join(sorted(EMOTION_PRESETS.keys())))
            continue

        if cmd in ("auto", "clear", "reactive"):
            with command_lock:
                manual_emotion_override = None
            next_emotion_change_time = time.time() + random.uniform(0.2, 0.6)
            print("[mode] reactive auto mode enabled")
            continue

        if cmd == "blink":
            trigger_synced_blink(random.uniform(BLINK_SPEED_MIN, BLINK_SPEED_MAX))
            print("[action] blink")
            continue

        if parts and parts[0] == "intensity" and len(parts) >= 2:
            try:
                value = float(parts[1])
                EMOTION_INTENSITY = max(0.0, min(1.0, value))
                print(f"[emotion] intensity={EMOTION_INTENSITY:.2f}")
            except ValueError:
                print("[error] intensity must be a number between 0 and 1")
            continue

        if cmd == "status":
            with command_lock:
                manual = manual_emotion_override
            mode = "manual" if manual else "auto"
            print(f"[status] mode={mode}, current={current_emotion}, manual={manual}, intensity={EMOTION_INTENSITY:.2f}")
            continue

        selected = None
        if parts and parts[0] == "emotion" and len(parts) >= 2:
            selected = parts[1]
        elif cmd in KEY_TO_EMOTION:
            selected = KEY_TO_EMOTION[cmd]
        elif cmd in EMOTION_PRESETS:
            selected = cmd

        if selected is not None:
            if selected not in EMOTION_PRESETS:
                print(f"[error] unknown emotion: {selected}")
                continue
            with command_lock:
                manual_emotion_override = selected
            print(f"[mode] manual emotion={selected}")
            continue

        print(f"[error] unknown command: {raw}")


def classify_distance_zone(face_area_ratio, prev_zone):
    if prev_zone == "near" and face_area_ratio >= NEAR_EXIT_RATIO:
        return "near"
    if prev_zone == "far" and face_area_ratio <= FAR_EXIT_RATIO:
        return "far"
    if face_area_ratio >= CLOSE_FACE_AREA_RATIO:
        return "near"
    if face_area_ratio < FAR_FACE_AREA_RATIO:
        return "far"
    return "mid"


def weighted_pick(weights, fallback="idle"):
    total = 0.0
    cleaned = {}
    for name, w in weights.items():
        if w > 0.0:
            cleaned[name] = float(w)
            total += float(w)
    if total <= 0.0:
        return fallback
    r = random.uniform(0.0, total)
    acc = 0.0
    for name, w in cleaned.items():
        acc += w
        if r <= acc:
            return name
    return fallback


def choose_no_person_emotion():
    global no_person_next_emotion
    base = no_person_next_emotion
    no_person_next_emotion = "idle" if no_person_next_emotion == "sleepy" else "sleepy"

    # Keep no-person mode mostly boring, with rare subtle accents.
    r = random.random()
    if r < 0.08:
        return "sad"
    if r < 0.14:
        return "calm"
    return base


def choose_person_emotion(zone, activity, squint_hint):
    if zone == "near":
        weights = {
            "excited": 1.0,
            "happy": 0.55,
            "curious": 0.12,
            "calm": 0.12,
            "surprised": 0.09,
            "afraid": 0.06,
            "angry": 0.04,
        }
        if activity > 0.75:
            weights["surprised"] += 0.20
            weights["afraid"] += 0.08
        if activity < 0.25:
            weights["calm"] += 0.10
    elif zone == "far":
        weights = {
            "curious": 1.0,
            "happy": 0.25,
            "calm": 0.30,
            "squint": 0.22,
            "sad": 0.10,
            "sleepy": 0.08,
            "idle": 0.10,
        }
        if squint_hint > 0.5:
            weights["squint"] += 0.35
    else:
        weights = {
            "happy": 1.0,
            "excited": 0.22,
            "curious": 0.30,
            "calm": 0.28,
            "suspicious": 0.15,
            "surprised": 0.08,
            "angry": 0.04,
            "afraid": 0.03,
        }
        if activity > 0.8:
            weights["surprised"] += 0.20
        if activity < 0.2:
            weights["calm"] += 0.12

    # Anti-repeat so expressions feel less robotic.
    for recent in emotion_history[-EMOTION_HISTORY_LEN:]:
        if recent in weights:
            weights[recent] *= 0.35
    if current_emotion in weights:
        weights[current_emotion] *= 0.45

    return weighted_pick(weights, fallback="happy")


def _face_box(face):
    fx, fy, fw, fh = [float(v) for v in face[0:4]]
    return fx, fy, fw, fh


def _face_center_norm(face):
    fx, fy, fw, fh = _face_box(face)
    cx = (fx + fw * 0.5) / CAMERA_RES[0]
    cy = (fy + fh * 0.5) / CAMERA_RES[1]
    return -((cx - 0.5) * 2.0), (cy - 0.5) * 2.0


def _roll_from_face(face):
    re_x, re_y = face[4], face[5]
    le_x, le_y = face[6], face[7]
    dx = re_x - le_x
    dy = re_y - le_y
    if dx == 0:
        return 0.0
    angle_rad = math.atan2(dy, dx)
    angle_deg = math.degrees(angle_rad)
    return max(-FACE_ROLL_MAX_DEG, min(FACE_ROLL_MAX_DEG, -angle_deg * FACE_ROLL_MULT))


class MultiFaceAttention:
    def __init__(self):
        self.mode = "largest"
        self.index = 0
        self.hold_until = 0.0
        self.stable_since = 0.0

    def _next_hold(self, now):
        self.hold_until = now + random.uniform(
            ATTENTION_HOLD_MIN_SEC,
            ATTENTION_HOLD_MAX_SEC,
        )

    def select(self, faces, now):
        ranked = sorted(faces, key=lambda f: f[2] * f[3], reverse=True)
        count = len(ranked)
        if count <= 1:
            self.mode = "largest"
            self.index = 0
            self.stable_since = 0.0
            self._next_hold(now)
            return ranked[0], "face", 0

        if self.stable_since <= 0.0:
            self.stable_since = now

        if now >= self.hold_until and now - self.stable_since >= MULTI_FACE_DEBOUNCE_SEC:
            r = random.random()
            if r < MULTI_FACE_CENTER_CHANCE:
                self.mode = "center"
                self.index = 0
            elif r < MULTI_FACE_CENTER_CHANCE + MULTI_FACE_ALTERNATE_CHANCE:
                self.mode = "alternate"
                self.index = random.randrange(1, count)
            else:
                self.mode = "largest"
                self.index = 0
            self._next_hold(now)

        if self.mode == "center" and count >= 2:
            return (ranked[0], ranked[1]), "center", -1

        self.index = min(max(0, self.index), count - 1)
        kind = "multi" if self.index > 0 else "face"
        return ranked[self.index], kind, self.index


def vision_worker():
    global running, target_x_off, target_y_off, target_rotation, target_squint, target_is_close
    global target_face_detected, target_face_area_ratio, target_face_norm_x, target_face_norm_y, squint_until, latest_frame
    global target_face_count, target_multi_face, target_face_candidates, target_body_detected, target_kind

    interval = 1.0 / max(1.0, float(VISION_FPS))
    next_tick = time.perf_counter()
    attention = MultiFaceAttention()
    frame_index = 0
    cached_body = None
    cached_body_ts = 0.0

    while running:
        try:
            # Capture full frame and resize once for detector input
            large_frame = picam2.capture_array()
            frame = cv2.resize(large_frame, CAMERA_RES)

            if CAMERA_ROTATE_180:
                frame = cv2.rotate(frame, cv2.ROTATE_180)

            local_x = 0.0
            local_y = 0.0
            local_rot = 0.0
            local_squint = 0.0
            local_face_detected = False
            local_face_area_ratio = 0.0
            local_face_norm_x = 0.0
            local_face_norm_y = 0.0
            local_face_count = 0
            local_face_candidates = []
            local_body_detected = False
            local_kind = "none"
            is_close = False  # Initialize before any conditional use

            if frame is not None and frame.size > 0:
                stream_frame = None
                if STREAM_ENABLED:
                    stream_frame = cv2.resize(frame, STREAM_RES)
                    if STREAM_SWAP_RB:
                        stream_frame = cv2.cvtColor(stream_frame, cv2.COLOR_BGR2RGB)
                    scale_x = STREAM_RES[0] / CAMERA_RES[0]
                    scale_y = STREAM_RES[1] / CAMERA_RES[1]

                detector.setInputSize((frame.shape[1], frame.shape[0]))
                faces = detector.detect(frame)
                now = time.time()

                if faces[1] is not None:
                    detected_faces = list(faces[1])
                    local_face_count = len(detected_faces)
                    local_face_candidates = [
                        tuple(_face_box(face)) for face in detected_faces
                    ]
                    active_target, local_kind, active_index = attention.select(detected_faces, now)

                    if local_kind == "center":
                        f1, f2 = active_target
                        norm_x1, norm_y1 = _face_center_norm(f1)
                        norm_x2, norm_y2 = _face_center_norm(f2)
                        norm_x = (norm_x1 + norm_x2) * 0.5
                        norm_y = (norm_y1 + norm_y2) * 0.5
                        fx1, fy1, fw1, fh1 = _face_box(f1)
                        fx2, fy2, fw2, fh2 = _face_box(f2)
                        fx = min(fx1, fx2)
                        fy = min(fy1, fy2)
                        fw = max(fx1 + fw1, fx2 + fw2) - fx
                        fh = max(fy1 + fh1, fy2 + fh2) - fy
                        local_rot = (_roll_from_face(f1) + _roll_from_face(f2)) * 0.5
                    else:
                        selected_face = active_target
                        fx, fy, fw, fh = _face_box(selected_face)
                        norm_x, norm_y = _face_center_norm(selected_face)
                        local_rot = _roll_from_face(selected_face)

                    if STREAM_ENABLED and stream_frame is not None:
                        for idx, face in enumerate(sorted(detected_faces, key=lambda f: f[2] * f[3], reverse=True)):
                            bx, by, bw, bh = _face_box(face)
                            color = (0, 255, 255) if idx == active_index else (0, 160, 0)
                            bx_s, by_s = int(bx * scale_x), int(by * scale_y)
                            bw_s, bh_s = int(bw * scale_x), int(bh * scale_y)
                            cv2.rectangle(stream_frame, (bx_s, by_s), (bx_s + bw_s, by_s + bh_s), color, 2)
                            cv2.putText(stream_frame, f"face{idx}", (bx_s, max(12, by_s - 4)), cv2.FONT_HERSHEY_SIMPLEX, 0.4, color, 1)
                        if local_kind == "center":
                            cx_s = int((0.5 - norm_x * 0.5) * STREAM_RES[0])
                            cy_s = int((0.5 + norm_y * 0.5) * STREAM_RES[1])
                            cv2.circle(stream_frame, (cx_s, cy_s), 7, (0, 255, 255), 2)
                            cv2.putText(stream_frame, "center", (cx_s + 8, cy_s), cv2.FONT_HERSHEY_SIMPLEX, 0.4, (0, 255, 255), 1)

                    local_face_detected = True
                    local_face_norm_x = norm_x
                    local_face_norm_y = norm_y

                    local_x = max(-MAX_X_OFFSET, min(MAX_X_OFFSET, norm_x * MAX_X_OFFSET))
                    local_y = max(-MAX_Y_OFFSET, min(MAX_Y_OFFSET, norm_y * MAX_Y_OFFSET))

                    # Distance-based emotion: squint when far, excited when close
                    face_area_ratio = (fw * fh) / float(CAMERA_RES[0] * CAMERA_RES[1])
                    local_face_area_ratio = face_area_ratio
                    
                    # Check for far-distance squinting
                    if face_area_ratio < FAR_FACE_AREA_RATIO:
                        if now > squint_until and random.random() < FAR_SQUINT_CHANCE:
                            squint_until = now + random.uniform(FAR_SQUINT_MIN_SEC, FAR_SQUINT_MAX_SEC)
                        if now < squint_until:
                            local_squint = 1.0
                    else:
                        squint_until = 0.0
                    
                    # Track if user is close for emotion switching
                    is_close = face_area_ratio >= CLOSE_FACE_AREA_RATIO

                else:
                    squint_until = 0.0
                    if person_detector is not None:
                        if frame_index % max(1, BODY_DETECT_STRIDE) == 0:
                            try:
                                body_frame = cv2.cvtColor(frame, cv2.COLOR_RGB2BGR)
                                cached_body = person_detector.detect_largest(body_frame)
                                cached_body_ts = now if cached_body is not None else 0.0
                            except Exception as e:
                                cached_body = None
                                cached_body_ts = 0.0
                                print(f"Body detect error: {e}")
                        if cached_body is not None and now - cached_body_ts <= BODY_CACHE_SEC:
                            body_cx = cached_body.cx / CAMERA_RES[0]
                            body_ay = cached_body.aim_y(BODY_AIM_Y_RATIO) / CAMERA_RES[1]
                            norm_x = -((body_cx - 0.5) * 2.0)
                            norm_y = (body_ay - 0.5) * 2.0
                            local_face_detected = True
                            local_body_detected = True
                            local_kind = "body"
                            local_face_norm_x = norm_x
                            local_face_norm_y = norm_y
                            local_x = max(-MAX_X_OFFSET, min(MAX_X_OFFSET, norm_x * MAX_X_OFFSET))
                            local_y = 0.0
                            local_squint = 0.0
                            is_close = False
                            if STREAM_ENABLED and stream_frame is not None:
                                bx_s = int(cached_body.x * scale_x)
                                by_s = int(cached_body.y * scale_y)
                                bw_s = int(cached_body.w * scale_x)
                                bh_s = int(cached_body.h * scale_y)
                                aim_x_s = int(body_cx * STREAM_RES[0])
                                aim_y_s = int(body_ay * STREAM_RES[1])
                                cv2.rectangle(stream_frame, (bx_s, by_s), (bx_s + bw_s, by_s + bh_s), (255, 120, 0), 2)
                                cv2.circle(stream_frame, (aim_x_s, aim_y_s), 6, (255, 120, 0), 2)
                                cv2.putText(stream_frame, "body", (bx_s, max(12, by_s - 4)), cv2.FONT_HERSHEY_SIMPLEX, 0.4, (255, 120, 0), 1)

                with target_lock:
                    target_x_off = local_x
                    target_y_off = local_y
                    target_rotation = local_rot
                    target_squint = local_squint
                    target_is_close = is_close
                    target_face_detected = local_face_detected
                    target_face_area_ratio = local_face_area_ratio
                    target_face_norm_x = local_face_norm_x
                    target_face_norm_y = local_face_norm_y
                    target_face_count = local_face_count
                    target_multi_face = local_face_count > 1
                    target_face_candidates = local_face_candidates
                    target_body_detected = local_body_detected
                    target_kind = local_kind

                if STREAM_ENABLED and stream_frame is not None:
                    with frame_lock:
                        latest_frame = stream_frame
                frame_index += 1

        except Exception as e:
            print(f"Capture/Detect Error: {e}")

        next_tick += interval
        sleep_time = next_tick - time.perf_counter()
        if sleep_time > 0:
            time.sleep(sleep_time)
        else:
            next_tick = time.perf_counter()



def _apply_deadzone(value, deadzone):
    if abs(value) <= deadzone:
        return 0.0
    sign = 1.0 if value > 0 else -1.0
    return sign * ((abs(value) - deadzone) / max(0.001, 1.0 - deadzone))


class PidAxis:
    def __init__(self, kp, ki, kd, integral_limit):
        self.kp = kp
        self.ki = ki
        self.kd = kd
        self.integral_limit = max(0.0, integral_limit)
        self.integral = 0.0
        self.prev_error = 0.0
        self.initialized = False

    def reset(self):
        self.integral = 0.0
        self.prev_error = 0.0
        self.initialized = False

    def soften(self, keep=0.35):
        self.integral *= clamp(keep, 0.0, 1.0)
        self.initialized = False

    def tick(self, error, dt):
        dt = max(0.001, dt)
        self.integral = clamp(
            self.integral + error * dt,
            -self.integral_limit,
            self.integral_limit,
        )
        derivative = 0.0 if not self.initialized else (error - self.prev_error) / dt
        self.prev_error = error
        self.initialized = True
        return self.kp * error + self.ki * self.integral + self.kd * derivative


class TargetGlide:
    def __init__(self, freq_hz=2.8, damping=0.82):
        self.freq_hz = max(0.1, freq_hz)
        self.damping = max(0.1, damping)
        self.x = 0.0
        self.y = 0.0
        self.vx = 0.0
        self.vy = 0.0

    def soften(self):
        self.vx *= 0.35
        self.vy *= 0.35

    def tick(self, target_x, target_y, dt, alpha_scale=1.0):
        dt = max(0.001, min(0.05, dt))
        omega = 2.0 * math.pi * self.freq_hz * clamp(alpha_scale, 0.15, 1.25)
        damp = 2.0 * self.damping * omega

        ax = ((target_x - self.x) * omega * omega) - (damp * self.vx)
        ay = ((target_y - self.y) * omega * omega) - (damp * self.vy)
        self.vx += ax * dt
        self.vy += ay * dt
        self.x = clamp(self.x + self.vx * dt, -1.0, 1.0)
        self.y = clamp(self.y + self.vy * dt, -1.0, 1.0)
        return self.x, self.y


def _looking_emotion_for_pan_goal(pan_goal):
    if pan_goal > PAN_CENTER + 3.0:
        return "looking_right_natural"
    if pan_goal < PAN_CENTER - 3.0:
        return "looking_left_natural"
    return "attentive"


def _motion_emotion_from_hint(hint):
    if hint in EMOTION_PRESETS:
        return hint
    if hint == "look":
        return "attentive"
    return "thinking"


def servo_worker():
    global running

    if not SERVO_ENABLED:
        print("Servo tracking disabled (--no-servo or config servo.enabled=false).")
        return

    link = ArduinoServoLink(port=SERVO_PORT, baud=SERVO_BAUD)
    if not link.connect():
        print("Servo tracking unavailable; camera and TFT eyes will continue without head motion.")
        return
    if BASE_ENABLED:
        if link.set_counts_per_degree(BASE_COUNTS_PER_DEGREE):
            print(f"Base CPD set to {BASE_COUNTS_PER_DEGREE:.3f}")
        else:
            print("Base CPD not acknowledged; base assist may be uncalibrated.")
        if BASE_ZERO_ON_START:
            link.zero_base()
            print("Base zero reference sent.")

    base_auto_enabled = BASE_ENABLED
    if BASE_ENABLED and BASE_REQUIRE_CALIBRATED_CPD and not link.is_calibrated():
        base_auto_enabled = False
        print(
            "Base auto motion disabled until CPD is calibrated — head servos still active.\n"
            "  Run: python tests/test_base_motor.py --calibrate-manual --degrees 90 --write-config"
        )

    loop_delay = 1.0 / max(1.0, SERVO_LOOP_HZ)
    pan = PAN_CENTER
    tilt = TILT_CENTER
    pan_vel = 0.0
    tilt_vel = 0.0
    last_seen_face = 0.0
    last_debug = 0.0
    last_mode = None
    last_track_kind = None
    last_base_nudge_ts = 0.0
    last_fast_base_nudge_ts = 0.0
    last_lost_base_nudge_ts = 0.0
    last_wander_base_nudge_ts = 0.0
    last_base_step = 0.0
    last_base_source = None
    last_base_comp = 0.0
    base_auto_total_deg = 0.0
    base_trigger_since = 0.0
    last_norm_sample_x = 0.0
    last_norm_sample_ts = 0.0
    fast_face_vel_x = 0.0
    last_motion_emotion = None
    last_motion_emotion_ts = 0.0
    last_face_norm_x = 0.0
    last_face_norm_y = 0.0
    last_face_vel_x = 0.0
    last_face_vel_y = 0.0
    last_face_near_edge = False
    servo_target_pan = PAN_CENTER
    servo_target_tilt = TILT_CENTER
    filtered_norm_x = 0.0
    filtered_norm_y = 0.0
    pan_pid = PidAxis(PAN_PID_KP, PAN_PID_KI, PAN_PID_KD, PID_INTEGRAL_LIMIT)
    tilt_pid = PidAxis(TILT_PID_KP, TILT_PID_KI, TILT_PID_KD, PID_INTEGRAL_LIMIT)
    target_glide = TargetGlide(TARGET_GLIDE_FREQ, TARGET_GLIDE_DAMP)
    wander = OrganicWanderSearch()
    wander.reset(PAN_CENTER, TILT_CENTER, time.time())
    link.write_angles(pan, tilt, force=True)
    base_yaw_target = 0.0
    base_yaw_hw = 0.0
    base_yaw_glide = 0.0
    base_yaw_vel = 0.0
    last_base_busy_check = 0.0
    base_motion_busy = False
    base_elastic_motion = HeadMotionParams(
        max_vel_pos=20.0,
        max_vel_neg=20.0,
        accel=34.0,
        decel=46.0,
        vel_blend=0.34,
        track_gain=2.6,
        goal_deadband_deg=0.18,
    )

    def request_base_nudge(raw_step, source, compensation_gain):
        nonlocal base_auto_total_deg, filtered_norm_x, servo_target_pan
        nonlocal last_base_step, last_base_source, last_base_comp, base_yaw_target
        if not base_auto_enabled:
            return None
        step = clamp(raw_step * BASE_SIGN, -BASE_MAX_STEP_DEG, BASE_MAX_STEP_DEG)
        if abs(step) < BASE_MIN_STEP_DEG:
            return None
        projected_total = base_auto_total_deg + step
        if abs(projected_total) > BASE_MAX_TOTAL_AUTO_DEG:
            return None

        base_auto_total_deg = projected_total
        # The base yaws the camera too, so reduce the neck pan demand immediately.
        comp = step * compensation_gain
        servo_target_pan = clamp(servo_target_pan - comp, PAN_MIN, PAN_MAX)
        filtered_norm_x *= max(0.0, 1.0 - BASE_PAN_RECENTER_BIAS)
        target_glide.x *= max(0.0, 1.0 - BASE_PAN_RECENTER_BIAS)
        pan_pid.soften(0.15)

        base_yaw_target += step
        last_base_step = step
        last_base_source = source
        last_base_comp = comp
        return step

    try:
        while running:
            now = time.time()
            wander_base_request = None
            with target_lock:
                face_seen = target_face_detected
                norm_x = target_face_norm_x
                norm_y = target_face_norm_y
                track_kind = target_kind
                face_count = target_face_count
                body_seen = target_body_detected

            if face_seen:
                if track_kind != "body":
                    if last_norm_sample_ts > 0.0:
                        dt_norm = max(1.0 / max(1.0, VISION_FPS), now - last_norm_sample_ts)
                        instant_vel_x = (norm_x - last_norm_sample_x) / dt_norm
                        fast_face_vel_x += (instant_vel_x - fast_face_vel_x) * 0.35
                        last_face_vel_x = fast_face_vel_x
                        last_face_vel_y = (norm_y - last_face_norm_y) / dt_norm
                    last_norm_sample_x = norm_x
                    last_norm_sample_ts = now
                last_seen_face = now
                last_face_norm_x = norm_x
                last_face_norm_y = norm_y
                last_face_near_edge = track_kind != "body" and (
                    abs(norm_x) >= PREDICT_EDGE_NORM
                    or abs(norm_y) >= PREDICT_EDGE_NORM
                )

            since_face = now - last_seen_face
            if face_seen:
                mode = "track"
            elif since_face <= PREDICT_HOLD_SEC:
                mode = "predict"
            elif since_face <= LOST_SEARCH_HOLD_SEC:
                mode = "lost_search"
            else:
                mode = "wander"

            if mode != last_mode:
                if mode == "track":
                    pan_pid.soften(0.25)
                    tilt_pid.soften(0.25)
                    target_glide.soften()
                elif mode == "wander":
                    wander.reset(pan, tilt, now)
                last_mode = mode

            if mode == "track" and track_kind != last_track_kind:
                pan_pid.soften(0.20)
                tilt_pid.soften(0.20)
                target_glide.soften()
                last_track_kind = track_kind

            if mode == "track":
                glide_alpha = BODY_TRACK_SERVO_ALPHA if body_seen else (
                    MULTI_FACE_TRACK_SERVO_ALPHA if face_count > 1 else 1.0
                )
                glide_x, glide_y = target_glide.tick(norm_x, norm_y, loop_delay, glide_alpha)
                filtered_norm_x += (glide_x - filtered_norm_x) * SERVO_FACE_ALPHA_X
                filtered_norm_y += (glide_y - filtered_norm_y) * SERVO_FACE_ALPHA_Y
                err_x = _apply_deadzone(filtered_norm_x, FACE_SERVO_DEADZONE_X)
                err_y = _apply_deadzone(filtered_norm_y, FACE_SERVO_DEADZONE_Y)
                pan_corr = clamp(pan_pid.tick(err_x, loop_delay), -1.0, 1.0)
                tilt_corr = clamp(tilt_pid.tick(err_y, loop_delay), -1.0, 1.0)
                if face_count > 1:
                    pan_corr *= MULTI_FACE_TRACK_GAIN
                    tilt_corr *= MULTI_FACE_TRACK_GAIN
                servo_target_pan = clamp(
                    PAN_CENTER + (pan_corr * PAN_TRACK_RANGE * PAN_SIGN),
                    PAN_MIN,
                    PAN_MAX,
                )
                servo_target_tilt = clamp(
                    TILT_CENTER + (tilt_corr * TILT_TRACK_RANGE * TILT_SIGN),
                    TILT_MIN,
                    TILT_MAX,
                )
                if track_kind == "body":
                    motion_emotion = "attentive"
                elif track_kind == "center":
                    motion_emotion = "warm"
                elif track_kind == "multi" and abs(norm_x) > DIRECTION_TRIGGER_NORM_X:
                    motion_emotion = "engaged"
                elif face_count > 1:
                    motion_emotion = "warm"
                else:
                    motion_emotion = None
            elif mode == "predict":
                predicted_x = clamp(
                    last_face_norm_x + (last_face_vel_x * since_face * LOST_SEARCH_VELOCITY_GAIN),
                    -1.0,
                    1.0,
                )
                predicted_y = clamp(
                    last_face_norm_y + (last_face_vel_y * since_face * LOST_SEARCH_VELOCITY_GAIN),
                    -1.0,
                    1.0,
                )
                filtered_norm_x += (predicted_x - filtered_norm_x) * SERVO_FACE_ALPHA_X
                filtered_norm_y += (predicted_y - filtered_norm_y) * SERVO_FACE_ALPHA_Y
                err_x = _apply_deadzone(filtered_norm_x * PREDICT_GAIN, FACE_SERVO_DEADZONE_X)
                err_y = _apply_deadzone(filtered_norm_y * PREDICT_GAIN, FACE_SERVO_DEADZONE_Y)
                pan_corr = clamp(pan_pid.tick(err_x, loop_delay), -1.0, 1.0)
                tilt_corr = clamp(tilt_pid.tick(err_y, loop_delay), -0.45, 0.45)
                servo_target_pan = clamp(
                    PAN_CENTER + (pan_corr * PAN_TRACK_RANGE * PAN_SIGN),
                    PAN_MIN,
                    PAN_MAX,
                )
                servo_target_tilt = clamp(
                    TILT_CENTER + (tilt_corr * TILT_TRACK_RANGE * TILT_SIGN),
                    TILT_MIN,
                    TILT_MAX,
                )
                motion_emotion = _looking_emotion_for_pan_goal(servo_target_pan)
            elif mode == "lost_search":
                search_x = last_face_norm_x + (last_face_vel_x * LOST_SEARCH_VELOCITY_GAIN)
                if abs(search_x) < LOST_SEARCH_MIN_NORM_X:
                    search_x = LOST_SEARCH_MIN_NORM_X if last_face_norm_x >= 0 else -LOST_SEARCH_MIN_NORM_X
                search_x = clamp(search_x, -1.0, 1.0)
                search_y = clamp(last_face_norm_y * 0.65, -0.65, 0.65)
                filtered_norm_x += (search_x - filtered_norm_x) * SERVO_FACE_ALPHA_X
                filtered_norm_y += (search_y - filtered_norm_y) * SERVO_FACE_ALPHA_Y
                err_x = _apply_deadzone(filtered_norm_x, FACE_SERVO_DEADZONE_X)
                err_y = _apply_deadzone(filtered_norm_y * 0.55, FACE_SERVO_DEADZONE_Y)
                pan_corr = clamp(pan_pid.tick(err_x, loop_delay), -1.0, 1.0)
                tilt_corr = clamp(tilt_pid.tick(err_y, loop_delay), -0.35, 0.35)
                servo_target_pan = clamp(
                    PAN_CENTER + (pan_corr * PAN_TRACK_RANGE * PAN_SIGN),
                    PAN_MIN,
                    PAN_MAX,
                )
                servo_target_tilt = clamp(
                    TILT_CENTER + (tilt_corr * TILT_TRACK_RANGE * TILT_SIGN),
                    TILT_MIN,
                    TILT_MAX,
                )
                motion_emotion = _looking_emotion_for_pan_goal(servo_target_pan)
            else:
                filtered_norm_x += (0.0 - filtered_norm_x) * SERVO_FACE_ALPHA_X
                filtered_norm_y += (0.0 - filtered_norm_y) * SERVO_FACE_ALPHA_Y
                was_wander_moving = wander.moving
                wpan, wtilt = wander.tick(
                    now,
                    pan_center=PAN_CENTER,
                    tilt_center=TILT_CENTER,
                    pan_current=pan,
                    pan_min=PAN_MIN,
                    pan_max=PAN_MAX,
                    tilt_min=TILT_MIN,
                    tilt_max=TILT_MAX,
                    amp_deg=WANDER_PAN_AMP_DEG,
                    step_min_deg=WANDER_STEP_MIN_DEG,
                    step_max_deg=WANDER_STEP_MAX_DEG,
                    hold_min_sec=WANDER_HOLD_MIN_SEC,
                    hold_max_sec=WANDER_HOLD_MAX_SEC,
                    jump_chance=WANDER_JUMP_CHANCE,
                    arrival_deg=WANDER_ARRIVAL_DEG,
                    tilt_max_up_deg=WANDER_TILT_MAX_UP_DEG,
                    tilt_max_down_deg=WANDER_TILT_MAX_DOWN_DEG,
                    thinking_hold_chance=WANDER_THINKING_HOLD_CHANCE,
                    thinking_hold_min_sec=WANDER_THINKING_HOLD_MIN_SEC,
                    thinking_hold_max_sec=WANDER_THINKING_HOLD_MAX_SEC,
                    long_stare_chance=WANDER_LONG_STARE_CHANCE,
                )
                if (
                    BASE_ENABLED
                    and BASE_WANDER_ENABLED
                    and was_wander_moving
                    and not wander.moving
                    and now - last_wander_base_nudge_ts >= BASE_WANDER_COOLDOWN_SEC
                    and abs(wpan - PAN_CENTER) >= BASE_WANDER_MIN_PAN_OFFSET_DEG
                ):
                    wander_dir = 1.0 if wpan > PAN_CENTER else -1.0
                    wander_base_request = wander_dir * clamp(
                        BASE_WANDER_STEP_DEG,
                        BASE_MIN_STEP_DEG,
                        BASE_MAX_STEP_DEG,
                    )
                speed = wander.move_speed_scale
                hold_scale = 0.45
                if not wander.moving:
                    if wander.pause_kind == "thinking":
                        hold_scale = 0.28
                    elif wander.pause_kind == "long_stare":
                        hold_scale = 0.32
                    elif wander.pause_kind == "glance":
                        hold_scale = 0.52
                pan_alpha = WANDER_PAN_TARGET_ALPHA * speed * (
                    1.35 if wander.moving else hold_scale
                )
                servo_target_pan += (wpan - servo_target_pan) * pan_alpha
                servo_target_tilt += (wtilt - servo_target_tilt) * WANDER_TILT_TARGET_ALPHA
                servo_target_pan = clamp(servo_target_pan, PAN_MIN, PAN_MAX)
                servo_target_tilt = clamp(servo_target_tilt, TILT_MIN, TILT_MAX)
                motion_emotion = (
                    _looking_emotion_for_pan_goal(servo_target_pan)
                    if wander.moving
                    else _motion_emotion_from_hint(wander.hold_emotion_hint)
                )

            pan_motion = PAN_MOTION
            tilt_motion = TILT_MOTION
            if mode == "wander":
                pan_motion = scale_head_motion(PAN_MOTION, wander.move_speed_scale)
                tilt_motion = scale_head_motion(TILT_MOTION, 0.65 + (wander.move_speed_scale * 0.35))

            pan, pan_vel = tick_toward(
                pan,
                pan_vel,
                servo_target_pan,
                loop_delay,
                lo=PAN_MIN,
                hi=PAN_MAX,
                params=pan_motion,
            )
            tilt, tilt_vel = tick_toward(
                tilt,
                tilt_vel,
                servo_target_tilt,
                loop_delay,
                lo=TILT_MIN,
                hi=TILT_MAX,
                params=tilt_motion,
            )

            base_step = None
            if base_auto_enabled:
                if mode == "wander" and wander_base_request is not None:
                    base_step = request_base_nudge(
                        wander_base_request,
                        "wander",
                        BASE_WANDER_COMPENSATION_GAIN,
                    )
                    if base_step is not None:
                        last_base_nudge_ts = now
                        last_wander_base_nudge_ts = now

                fast_allowed_for_scene = (
                    base_step is None
                    and BASE_FAST_FACE_ENABLED
                    and mode == "track"
                    and face_seen
                    and track_kind not in ("body", "center")
                    and (BASE_ALLOW_MULTI_FACE or face_count <= 1)
                    and now - last_fast_base_nudge_ts >= BASE_FAST_FACE_COOLDOWN_SEC
                    and now - last_base_nudge_ts >= BASE_FAST_FACE_COOLDOWN_SEC
                    and abs(fast_face_vel_x) >= BASE_FAST_FACE_VELOCITY_NORM_SEC
                )
                if fast_allowed_for_scene:
                    direction = 1.0 if fast_face_vel_x >= 0.0 else -1.0
                    magnitude = clamp(
                        abs(fast_face_vel_x) * BASE_FAST_FACE_VELOCITY_TO_DEG_GAIN,
                        BASE_FAST_FACE_MIN_STEP_DEG,
                        BASE_FAST_FACE_MAX_STEP_DEG,
                    )
                    base_step = request_base_nudge(
                        direction * magnitude,
                        "fast",
                        BASE_FAST_FACE_COMPENSATION_GAIN,
                    )
                    if base_step is not None:
                        last_base_nudge_ts = now
                        last_fast_base_nudge_ts = now
                        base_trigger_since = 0.0
                        fast_face_vel_x *= 0.25

                lost_allowed_for_scene = (
                    base_step is None
                    and mode == "lost_search"
                    and now - last_lost_base_nudge_ts >= LOST_SEARCH_BASE_COOLDOWN_SEC
                    and now - last_base_nudge_ts >= LOST_SEARCH_BASE_COOLDOWN_SEC
                )
                if lost_allowed_for_scene:
                    lost_dir_source = last_face_norm_x + (last_face_vel_x * LOST_SEARCH_VELOCITY_GAIN)
                    direction = 1.0 if lost_dir_source >= 0.0 else -1.0
                    base_step = request_base_nudge(
                        direction * LOST_SEARCH_BASE_STEP_DEG,
                        "lost",
                        BASE_TRACK_COMPENSATION_GAIN,
                    )
                    if base_step is not None:
                        last_base_nudge_ts = now
                        last_lost_base_nudge_ts = now

                base_allowed_for_scene = (
                    base_step is None
                    and mode == "track"
                    and face_seen
                    and track_kind != "body"
                    and (BASE_ALLOW_MULTI_FACE or face_count <= 1)
                )
                pan_offset = pan - PAN_CENTER
                base_error = filtered_norm_x
                side_error = abs(base_error) >= BASE_TRIGGER_NORM_X
                pan_near_limit = abs(pan_offset) >= BASE_PAN_SOFT_LIMIT_DEG
                if base_allowed_for_scene and (side_error or pan_near_limit):
                    if base_trigger_since <= 0.0:
                        base_trigger_since = now
                    ready_for_nudge = (
                        now - base_trigger_since >= BASE_TRIGGER_HOLD_SEC
                        and now - last_base_nudge_ts >= BASE_COOLDOWN_SEC
                    )
                    if ready_for_nudge:
                        direction = 1.0 if base_error >= 0.0 else -1.0
                        if pan_near_limit and not side_error:
                            direction = 1.0 if pan_offset >= 0.0 else -1.0
                        magnitude = clamp(
                            abs(base_error) * BASE_NORM_TO_DEG_GAIN,
                            BASE_MIN_STEP_DEG,
                            BASE_MAX_STEP_DEG,
                        )
                        base_step = request_base_nudge(
                            direction * magnitude,
                            "track",
                            BASE_TRACK_COMPENSATION_GAIN,
                        )
                        if base_step is not None:
                            last_base_nudge_ts = now
                            base_trigger_since = 0.0
                else:
                    base_trigger_since = 0.0

            if (
                motion_emotion
                and motion_emotion != last_motion_emotion
                and now - last_motion_emotion_ts >= MOTION_EMOTION_MIN_SEC
            ):
                apply_emotion(motion_emotion)
                last_motion_emotion = motion_emotion
                last_motion_emotion_ts = now

            base_send_slice = None
            if base_auto_enabled:
                yaw_remaining = base_yaw_target - base_yaw_hw
                if abs(yaw_remaining) <= base_elastic_motion.goal_deadband_deg:
                    base_yaw_glide = base_yaw_hw = base_yaw_target
                    base_yaw_vel = 0.0
                elif now - last_base_busy_check >= 0.05:
                    status = link.query_status()
                    base_motion_busy = status.busy if status else False
                    last_base_busy_check = now
                if not base_motion_busy and abs(yaw_remaining) > base_elastic_motion.goal_deadband_deg:
                    glide_lo = min(base_yaw_hw, base_yaw_target)
                    glide_hi = max(base_yaw_hw, base_yaw_target)
                    base_yaw_glide, base_yaw_vel = tick_toward(
                        base_yaw_glide,
                        base_yaw_vel,
                        base_yaw_target,
                        loop_delay,
                        lo=glide_lo,
                        hi=glide_hi,
                        params=base_elastic_motion,
                    )
                    slice_deg = base_yaw_glide - base_yaw_hw
                    if abs(slice_deg) >= 0.35:
                        base_send_slice = slice_deg
                        base_yaw_hw += slice_deg
                        base_motion_busy = True

            if base_send_slice is not None:
                if not link.write_combined(
                    pan,
                    tilt,
                    base_send_slice,
                    wait_servo=False,
                    wait_base=False,
                ):
                    link.write_angles(pan, tilt)
            else:
                link.write_angles(pan, tilt)

            if SERVO_DEBUG_HZ > 0 and now - last_debug >= 1.0 / SERVO_DEBUG_HZ:
                label = track_kind if mode == "track" else mode
                base_text = ""
                if base_auto_enabled and now - last_base_nudge_ts < 2.0:
                    source = last_base_source or "base"
                    base_text = f" base[{source}] {last_base_step:+.1f} comp {last_base_comp:+.1f}"
                sys.stdout.write(
                    f"\r[servo] {label} pan {pan:5.1f}->{servo_target_pan:5.1f} "
                    f"tilt {tilt:5.1f}->{servo_target_tilt:5.1f}{base_text}   "
                )
                sys.stdout.flush()
                last_debug = now

            time.sleep(loop_delay)
    except Exception as e:
        print(f"\nServo tracking error: {e}")
    finally:
        try:
            link.close(home_pan=PAN_CENTER, home_tilt=TILT_CENTER)
            print("\nServo link closed.")
        except Exception as e:
            print(f"\nServo close error: {e}")

print("Starting Tracking Loop...")
time.sleep(1.0) # Warmup

vision_thread = threading.Thread(target=vision_worker, daemon=True)
vision_thread.start()

servo_thread = threading.Thread(target=servo_worker, daemon=True)
servo_thread.start()

cmd_thread = None
if TERMINAL_CONTROL_ENABLED:
    cmd_thread = threading.Thread(target=terminal_command_worker, daemon=True)
    cmd_thread.start()

try:
    while running:
        loop_start = time.perf_counter()

        with target_lock:
            local_target_x = target_x_off
            local_target_y = target_y_off
            local_target_rot = target_rotation
            local_target_squint = target_squint
            local_target_is_close = target_is_close
            local_face_detected = target_face_detected
            local_face_area_ratio = target_face_area_ratio
            local_face_norm_x = target_face_norm_x
            local_target_kind = target_kind
            local_face_count = target_face_count
            local_body_detected = target_body_detected

        # Smooth tracking to reduce jitter
        smooth_alpha = 0.15
        smoothed_x_off = smoothed_x_off + (local_target_x - smoothed_x_off) * smooth_alpha
        smoothed_y_off = smoothed_y_off + (local_target_y - smoothed_y_off) * smooth_alpha
        smoothed_rotation = smoothed_rotation + (local_target_rot - smoothed_rotation) * smooth_alpha
        
        # 2. Update Eye Targets
        left_eye.target_pos[0] = left_eye.base_x + smoothed_x_off
        left_eye.target_pos[1] = left_eye.base_y
        clamp_eye_target(left_eye)

        right_eye.target_pos[0] = left_eye.target_pos[0]
        right_eye.target_pos[1] = left_eye.target_pos[1]
        left_eye.target_rotation = smoothed_rotation
        right_eye.target_rotation = smoothed_rotation

        now = time.time()
        if local_face_detected:
            last_seen_face_time = now
        if local_face_count > 1:
            last_multi_face_time = now
        person_present = (now - last_seen_face_time) <= NO_FACE_GRACE_SEC
        social_multi_present = (now - last_multi_face_time) <= SOCIAL_MULTI_GRACE_SEC

        dx_activity = abs(local_target_x - prev_target_x) / max(1.0, float(MAX_X_OFFSET))
        dy_activity = abs(local_target_y - prev_target_y) / max(1.0, float(MAX_Y_OFFSET))
        dr_activity = abs(local_target_rot - prev_target_rot) / max(1.0, float(FACE_ROLL_MAX_DEG))
        activity = min(1.0, (dx_activity + dy_activity + dr_activity) / 3.0)
        prev_target_x = local_target_x
        prev_target_y = local_target_y
        prev_target_rot = local_target_rot

        with command_lock:
            manual_emotion = manual_emotion_override

        if manual_emotion is not None:
            apply_emotion(manual_emotion)
        elif now >= next_emotion_change_time:
            if not person_present:
                next_emotion = choose_no_person_emotion()
                hold_sec = random.uniform(NO_PERSON_HOLD_MIN_SEC, NO_PERSON_HOLD_MAX_SEC)
            elif local_body_detected or local_target_kind == "body":
                next_emotion = weighted_pick(
                    {"attentive": 0.55, "calm": 0.30, "curious": 0.15},
                    fallback="attentive",
                )
                hold_sec = random.uniform(PERSON_HOLD_MIN_SEC, PERSON_HOLD_MAX_SEC)
            elif local_target_kind == "center" or (social_multi_present and local_target_kind == "face"):
                next_emotion = weighted_pick(
                    {
                        "warm": 0.34,
                        "engaged": 0.26,
                        "attentive": 0.20,
                        "happy": 0.12,
                        "amused": 0.08,
                    },
                    fallback="warm",
                )
                hold_sec = random.uniform(2.6, 5.2)
            elif local_target_kind == "multi" or local_face_count > 1 or social_multi_present:
                if abs(local_face_norm_x) >= DIRECTION_TRIGGER_NORM_X:
                    next_emotion = weighted_pick(
                        {
                            "warm": 0.34,
                            "engaged": 0.28,
                            "attentive": 0.18,
                            "looking_left_natural" if local_face_norm_x > 0 else "looking_right_natural": 0.12,
                            "amused": 0.08,
                        },
                        fallback="engaged",
                    )
                else:
                    next_emotion = weighted_pick(
                        {
                            "warm": 0.32,
                            "engaged": 0.28,
                            "attentive": 0.22,
                            "happy": 0.10,
                            "amused": 0.08,
                        },
                        fallback="engaged",
                    )
                hold_sec = random.uniform(2.8, 5.8)
                direction_cooldown_until = now + hold_sec + DIRECTION_COOLDOWN_SEC
            else:
                distance_zone = classify_distance_zone(local_face_area_ratio, distance_zone)
                can_directional = now >= direction_cooldown_until and abs(local_face_norm_x) >= DIRECTION_TRIGGER_NORM_X
                if can_directional and random.random() < (0.5 if distance_zone == "mid" else 0.35):
                    next_emotion = "looking_right" if local_face_norm_x > 0 else "looking_left"
                    hold_sec = random.uniform(DIRECTION_HOLD_MIN_SEC, DIRECTION_HOLD_MAX_SEC)
                    direction_cooldown_until = now + hold_sec + DIRECTION_COOLDOWN_SEC
                else:
                    next_emotion = choose_person_emotion(distance_zone, activity, local_target_squint)
                    hold_base = random.uniform(PERSON_HOLD_MIN_SEC, PERSON_HOLD_MAX_SEC)
                    hold_sec = max(PERSON_HOLD_MIN_SEC, hold_base * (1.12 - 0.42 * activity))

            apply_emotion(next_emotion)
            next_emotion_change_time = now + hold_sec
        
        # 3. Blink Logic
        if time.time() > next_blink_time:
            blink_speed = random.uniform(BLINK_SPEED_MIN, BLINK_SPEED_MAX)
            trigger_synced_blink(blink_speed)
            last_blink_time = time.time()
            next_blink_time = time.time() + random.uniform(3.5, 7.0)

        # Keep idle motion deterministic to avoid perceived micro-jitter.
        
        # 5. Physics Update
        left_eye.update()
        right_eye.update()
        
        # 6. Draw
        rgb_l = None
        rgb_r = None
        if disp_l:
            img_l = Image.new("RGBA", (SCREEN_WIDTH, SCREEN_HEIGHT), BG_COLOR)
            left_eye.draw(img_l)
            rgb_l = img_l.convert("RGB")
        if disp_r:
            img_r = Image.new("RGBA", (SCREEN_WIDTH, SCREEN_HEIGHT), BG_COLOR)
            right_eye.draw(img_r)
            rgb_r = img_r.convert("RGB")

        try:
            if disp_l and rgb_l is not None:
                disp_l.image(rgb_l)
            if disp_r and rgb_r is not None:
                disp_r.image(rgb_r)
        except Exception as e:
            print(f"Display update error: {e}")

        frame_budget = (1.0 / max(1.0, float(RENDER_FPS))) - (time.perf_counter() - loop_start)
        if frame_budget > 0:
            time.sleep(frame_budget)

except KeyboardInterrupt:
    print("\nStopping...")
finally:
    running = False
    if vision_thread.is_alive():
        vision_thread.join(timeout=1.0)
    if servo_thread.is_alive():
        servo_thread.join(timeout=1.5)

    # Cleanup attributes
    try:
        if picam2:
            picam2.stop()
            picam2.close()
            print("Camera closed.")
    except Exception as e:
        print(e)

    if stream_server:
        try:
            stream_server.shutdown()
            stream_server.server_close()
            print("MJPEG stream stopped.")
        except Exception as e:
            print(e)
        
    # Clear screens
    black = Image.new("RGB", (SCREEN_WIDTH, SCREEN_HEIGHT), (0, 0, 0))
    if disp_l: disp_l.image(black)
    if disp_r: disp_r.image(black)
    print("Displays cleared.")