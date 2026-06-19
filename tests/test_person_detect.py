#!/usr/bin/env python3
"""
Minimal Person Tracker for dual MG996R (Arms) & dual SG90 (Arms) Servos.
Uses YOLOv8 via person_detector.py and PiCamera2.
"""

import time
import cv2
import serial
import os
import argparse
import random
from picamera2 import Picamera2
from person_detector import PersonDetector

# Adjust based on your setup. E.g., /dev/ttyUSB0 or /dev/ttyACM0
ESP32_PORT = "/dev/ttyUSB0" 
BAUD_RATE = 115200

def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--model", default="yolov8n.onnx", help="Path to YOLOv8 model")
    parser.add_argument("--port", default=ESP32_PORT, help="ESP32 serial port")
    args = parser.parse_args()

    # 1. Connect to ESP32
    print(f"Connecting to ESP32 on {args.port}...")
    ser = None
    if os.path.exists(args.port):
        try:
            ser = serial.Serial(args.port, BAUD_RATE, timeout=0.1)
            time.sleep(2) # Wait for ESP32 boot
            ser.write(b"H\n")
            print("Serial connected successfully.")
        except Exception as e:
            print(f"Failed to connect to Serial: {e}")
            return
    else:
        print("WARNING: Serial port not found. Tracking will run in console only.")

    # 2. Init YOLOv8 Person Detector
    print(f"Loading YOLO body detector: {args.model}")
    detector = PersonDetector(
        model_path=args.model,
        confidence_threshold=0.45,
        nms_threshold=0.45,
        input_size=640
    )

    # 3. Init Camera
    print("Starting Picamera2...")
    picam2 = Picamera2()
    # Moderate resolution for tracking speed
    config = picam2.create_video_configuration(
        main={"format": "RGB888", "size": (640, 480)},
        buffer_count=2,
    )
    picam2.configure(config)
    picam2.start()
    
    # Arm Timing State
    next_arm_action_time = time.time()
    arm_active = False
    active_arm = None

    print("Tracking loop started. Press Ctrl+C to exit.")
    try:
        while True:
            # Capture Frame (RGB) -> Convert to BGR for OpenCV/YOLO
            frame = picam2.capture_array()
            bgr_frame = cv2.cvtColor(frame, cv2.COLOR_RGB2BGR)

            person = detector.detect_largest(bgr_frame)
            now = time.time()
            
            # Default Arm Positions (Down/Home)
            A = 0.0    # MG996R_L Down
            B = 180.0  # MG996R_R Down
            C = 90.0   # SG90_1 stationary (90)
            D = 90.0   # SG90_2 stationary (90)

            if person is not None:
                # --- Arm Raise Logic ---
                if not arm_active and now >= next_arm_action_time:
                    arm_active = True
                    active_arm = random.choice(["left", "right"])
                    next_arm_action_time = now + 3.0 # Keep arm up for 3 seconds
                    print(f"*** Raising {active_arm.upper()} arm! ***")
                    
                elif arm_active and now >= next_arm_action_time:
                    arm_active = False
                    next_arm_action_time = now + 10.0 # 10 second cooldown
                    print("*** Lowering arm, 10s cooldown started. ***")
                
                # Apply the UP position if an arm is active
                if arm_active:
                    if active_arm == "left":
                        A = 180.0 # Left Arm Up (MG996R_L to 180)
                    else:
                        B = 0.0   # Right Arm Up (MG996R_R to 0)

                cmd = f"A{A:.1f} B{B:.1f} C{C:.1f} D{D:.1f}\n"
                
                print(f"Person detected! (Conf: {person.confidence:.2f}) -> Sending: {cmd.strip()}")
                
                if ser:
                    ser.write(cmd.encode("ascii"))
                    ser.flush()
            else:
                # If no person is seen, lower arms and reset cooldown
                if arm_active:
                    arm_active = False
                    next_arm_action_time = now + 10.0
                    print("Person lost. Lowering arms, 10s cooldown started.")
                
                cmd = f"A{A:.1f} B{B:.1f} C{C:.1f} D{D:.1f}\n"
                if ser:
                    ser.write(cmd.encode("ascii"))

            # Small delay for CPU breathing room
            time.sleep(0.1)

    except KeyboardInterrupt:
        print("\nStopping tracker...")
    finally:
        picam2.stop()
        if ser:
            # Home all servos before exit
            ser.write(b"A0.0 B180.0 C90.0 D90.0\n")
            ser.close()

if __name__ == "__main__":
    main()