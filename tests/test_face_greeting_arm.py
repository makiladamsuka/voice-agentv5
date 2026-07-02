#!/usr/bin/env python3
"""Test face greeting arm gestures with memory.

This script demonstrates the FaceGreetingArmService:
- Detects new faces and plays random hi poses (hi1, hi2, hi3, hi4)
- Remembers each face for 30 minutes
- Won't greet the same face again within 30 minutes
- Greets again if face returns after 30+ minutes

Run this test standalone without start_robot.py to see greeting behavior.
"""

from __future__ import annotations

import sys
import time
from pathlib import Path

# Add parent directory to path
APP_DIR = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(APP_DIR))

from core.blackboard import Blackboard
from core.face_greeting_arm import FaceGreetingArmService
from core.face_tracking import FaceTracker

try:
    import cv2
except ImportError:
    print("ERROR: OpenCV not installed. Run: pip install opencv-python")
    sys.exit(1)


def print_greeting_state(bb: Blackboard):
    """Print current greeting state."""
    state = bb.read(
        "arm_greeting_seq",
        "arm_greeting_pose",
        "arm_greeting_active",
        "face_detected",
        "face_area_ratio",
    )
    
    if state["arm_greeting_active"]:
        print(f"\n🤖 GREETING: {state['arm_greeting_pose']} (seq={state['arm_greeting_seq']})")
    
    if state["face_detected"]:
        print(f"   Face detected (area ratio: {state['face_area_ratio']:.4f})")


def test_greeting_service():
    """Test the face greeting arm service with live camera."""
    print("=" * 60)
    print("Face Greeting Arm Gesture Test")
    print("=" * 60)
    print("\nThis test will:")
    print("  1. Detect faces from camera")
    print("  2. Play random hi pose for NEW faces")
    print("  3. Remember each face for 30 minutes")
    print("  4. Skip greeting for known faces")
    print("\nPress Ctrl+C to stop\n")
    
    # Create blackboard
    bb = Blackboard()
    
    # Create face tracker (to populate face_detected, face_candidates, etc.)
    print("[Test] Starting FaceTracker...")
    face_tracker = FaceTracker(bb)
    
    # Start face tracker in background thread
    import threading
    tracker_thread = threading.Thread(
        target=face_tracker.run,
        daemon=True,
        name="FaceTracker"
    )
    tracker_thread.start()
    
    # Wait for camera to initialize
    print("[Test] Waiting for camera initialization...")
    time.sleep(2.0)
    
    # Create and start greeting service
    print("[Test] Starting FaceGreetingArmService...")
    greeting_service = FaceGreetingArmService(bb)
    
    if not greeting_service.enabled:
        print("\nERROR: FaceGreetingArmService is disabled in config.yaml")
        print("Set face_greeting_arm.enabled: true to enable it.")
        return
    
    greeting_thread = threading.Thread(
        target=greeting_service.run,
        daemon=True,
        name="FaceGreetingArm"
    )
    greeting_thread.start()
    
    print("\n" + "=" * 60)
    print("Monitoring face greetings...")
    print("Available hi poses:", greeting_service.hi_poses)
    print(f"Memory timeout: {greeting_service.memory_timeout_sec / 60:.1f} minutes")
    print("=" * 60 + "\n")
    
    last_seq = 0
    last_print_time = 0.0
    
    try:
        while True:
            time.sleep(0.2)
            now = time.time()
            
            # Check for new greetings
            state = bb.read("arm_greeting_seq", "arm_greeting_pose")
            current_seq = state["arm_greeting_seq"]
            
            if current_seq != last_seq:
                last_seq = current_seq
                pose = state["arm_greeting_pose"]
                print(f"\n{'=' * 60}")
                print(f"🎉 NEW GREETING TRIGGERED!")
                print(f"   Pose: {pose}")
                print(f"   Sequence: {current_seq}")
                print(f"   Time: {time.strftime('%H:%M:%S')}")
                print(f"   Faces in memory: {len(greeting_service.greeted_faces)}")
                print("=" * 60 + "\n")
            
            # Periodic status update
            if (now - last_print_time) > 5.0:
                last_print_time = now
                state = bb.read("face_detected", "face_area_ratio")
                memory_count = len(greeting_service.greeted_faces)
                
                print(f"[{time.strftime('%H:%M:%S')}] "
                      f"Face: {'YES' if state['face_detected'] else 'NO'} | "
                      f"Area: {state['face_area_ratio']:.4f} | "
                      f"Memory: {memory_count} faces")
    
    except KeyboardInterrupt:
        print("\n\n[Test] Stopping...")
        bb.write(running=False)
        time.sleep(0.5)
        print("[Test] Test complete!")
        print(f"\nFinal stats:")
        print(f"  Total greetings: {last_seq}")
        print(f"  Faces in memory: {len(greeting_service.greeted_faces)}")


def test_greeting_memory():
    """Test the greeting memory system without camera (simulated)."""
    print("=" * 60)
    print("Face Greeting Memory Test (Simulated)")
    print("=" * 60)
    
    bb = Blackboard()
    
    # Mock face detection
    bb.write(
        face_detected=True,
        face_area_ratio=0.02,
        face_candidates=[{"x": 0.5, "y": 0.5, "w": 0.1, "h": 0.1}],
        stream_frame=None,
        agent_speaking=False,
        user_speaking=False,
        bye_wave_active=False,
    )
    
    greeting_service = FaceGreetingArmService(bb)
    
    if not greeting_service.enabled:
        print("\nERROR: FaceGreetingArmService is disabled in config.yaml")
        return
    
    print(f"\nMemory timeout: {greeting_service.memory_timeout_sec / 60:.1f} minutes")
    print(f"Available poses: {greeting_service.hi_poses}\n")
    
    # Simulate face embeddings
    import numpy as np
    
    # Create 3 different "faces"
    face1 = np.random.rand(96)
    face2 = np.random.rand(96)
    face3 = np.random.rand(96)
    
    print("Test 1: New face → should greet")
    match = greeting_service.find_matching_face(face1)
    if match is None:
        greeting_service.add_greeted_face(face1, time.time())
        print("  ✓ Face1 added to memory")
    else:
        print("  ✗ Face1 already in memory (unexpected)")
    
    print("\nTest 2: Same face again → should NOT greet")
    match = greeting_service.find_matching_face(face1)
    if match is not None:
        print("  ✓ Face1 found in memory (no greeting)")
    else:
        print("  ✗ Face1 not found in memory (unexpected)")
    
    print("\nTest 3: Different face → should greet")
    match = greeting_service.find_matching_face(face2)
    if match is None:
        greeting_service.add_greeted_face(face2, time.time())
        print("  ✓ Face2 added to memory")
    else:
        print("  ✗ Face2 already in memory (unexpected)")
    
    print("\nTest 4: Simulate 30-minute timeout")
    print("  Setting Face1 timestamp to 31 minutes ago...")
    greeting_service.greeted_faces[0].timestamp = time.time() - (31 * 60)
    
    if greeting_service.greeted_faces[0].is_expired(time.time(), greeting_service.memory_timeout_sec):
        print("  ✓ Face1 is expired (should greet if seen again)")
    else:
        print("  ✗ Face1 is not expired (unexpected)")
    
    print("\nTest 5: Cleanup expired faces")
    print(f"  Before cleanup: {len(greeting_service.greeted_faces)} faces")
    greeting_service.cleanup_expired_faces(time.time())
    print(f"  After cleanup: {len(greeting_service.greeted_faces)} faces")
    
    print("\n" + "=" * 60)
    print("Memory test complete!")
    print("=" * 60)


if __name__ == "__main__":
    import argparse
    
    parser = argparse.ArgumentParser(description="Test face greeting arm gestures")
    parser.add_argument(
        "--mode",
        choices=["live", "memory"],
        default="live",
        help="Test mode: 'live' with camera or 'memory' simulation"
    )
    
    args = parser.parse_args()
    
    if args.mode == "live":
        test_greeting_service()
    else:
        test_greeting_memory()
