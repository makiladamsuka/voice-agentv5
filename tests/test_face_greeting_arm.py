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

try:
    import json
except ImportError:
    json = None

# Try to import ServoMixer for real motor control
try:
    from core.servo_mixer import ServoMixer
    HARDWARE_AVAILABLE = True
except ImportError:
    HARDWARE_AVAILABLE = False
    print("[Test] WARNING: ServoMixer not available, motor control disabled")

# For HTTP debug stream
from http.server import HTTPServer, BaseHTTPRequestHandler
from threading import Thread
import numpy as np


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


class DebugStreamHandler(BaseHTTPRequestHandler):
    """HTTP handler for debug video stream with face memory overlay."""
    
    blackboard = None
    greeting_service = None
    
    def log_message(self, format, *args):
        """Suppress HTTP logs."""
        pass
    
    def do_GET(self):
        """Handle GET requests."""
        if self.path == '/':
            self.send_response(200)
            self.send_header('Content-type', 'text/html')
            self.end_headers()
            html = b"""
            <html>
            <head><title>Face Greeting Debug Stream</title></head>
            <body style="background: #000; margin: 0; display: flex; justify-content: center; align-items: center; height: 100vh;">
                <div style="text-align: center;">
                    <h1 style="color: #fff;">Face Greeting Debug Stream</h1>
                    <img src="/stream.mjpg" style="max-width: 90%; border: 2px solid #0f0;" />
                </div>
            </body>
            </html>
            """
            self.wfile.write(html)
        
        elif self.path == '/stream.mjpg':
            self.send_response(200)
            self.send_header('Content-type', 'multipart/x-mixed-replace; boundary=frame')
            self.end_headers()
            
            try:
                while True:
                    frame = self.get_annotated_frame()
                    if frame is not None:
                        _, jpg = cv2.imencode('.jpg', frame, [cv2.IMWRITE_JPEG_QUALITY, 80])
                        self.wfile.write(b'--frame\r\n')
                        self.wfile.write(b'Content-Type: image/jpeg\r\n\r\n')
                        self.wfile.write(jpg.tobytes())
                        self.wfile.write(b'\r\n')
                    time.sleep(0.1)
            except:
                pass
    
    def get_annotated_frame(self):
        """Get frame with face memory status overlay."""
        if self.blackboard is None or self.greeting_service is None:
            return None
        
        state = self.blackboard.read("stream_frame", "face_detected", "face_area_ratio")
        frame = state["stream_frame"]
        
        if frame is None:
            return None
        
        # Make a copy to annotate
        annotated = frame.copy()
        h, w = annotated.shape[:2]
        
        # Draw memory status
        memory_count = len(self.greeting_service.greeted_faces)
        status_text = f"Memory: {memory_count} faces"
        cv2.putText(annotated, status_text, (10, 30), 
                   cv2.FONT_HERSHEY_SIMPLEX, 0.7, (0, 255, 0), 2)
        
        # Draw face status
        if state["face_detected"]:
            face_text = "Face: DETECTED"
            color = (0, 255, 255)  # Yellow
            
            # Check if this face is in memory
            if memory_count > 0 and self.greeting_service._current_embedding is not None:
                matching_face = self.greeting_service.find_matching_face(
                    self.greeting_service._current_embedding
                )
                if matching_face is not None:
                    age_min = (time.time() - matching_face.timestamp) / 60.0
                    face_text = f"Face: MEMORIZED ({age_min:.1f}m ago)"
                    color = (255, 0, 255)  # Magenta
                else:
                    face_text = "Face: NEW!"
                    color = (0, 255, 0)  # Green
            
            cv2.putText(annotated, face_text, (10, 60), 
                       cv2.FONT_HERSHEY_SIMPLEX, 0.7, color, 2)
        else:
            cv2.putText(annotated, "Face: NONE", (10, 60), 
                       cv2.FONT_HERSHEY_SIMPLEX, 0.7, (0, 0, 255), 2)
        
        # Draw greeting status
        greeting_state = self.blackboard.read("arm_greeting_active", "arm_greeting_pose")
        if greeting_state["arm_greeting_active"]:
            pose = greeting_state["arm_greeting_pose"]
            cv2.putText(annotated, f"GREETING: {pose}", (10, h - 20), 
                       cv2.FONT_HERSHEY_SIMPLEX, 1.0, (0, 255, 0), 3)
        
        return annotated


def start_debug_stream(bb: Blackboard, greeting_service: FaceGreetingArmService, port: int = 9000):
    """Start HTTP debug stream server."""
    DebugStreamHandler.blackboard = bb
    DebugStreamHandler.greeting_service = greeting_service
    
    server = HTTPServer(('0.0.0.0', port), DebugStreamHandler)
    print(f"[DebugStream] Started at http://localhost:{port}")
    print(f"[DebugStream] Shows: Face status + Memory info + Greeting status")
    
    thread = Thread(target=server.serve_forever, daemon=True)
    thread.start()
    return server


class MockArmController:
    """Mock ArmController that simulates arm movement and sends commands to real motors."""
    
    def __init__(self, bb: Blackboard, presets_path: Path, use_hardware: bool = False):
        self.bb = bb
        self.use_hardware = use_hardware and HARDWARE_AVAILABLE
        self._last_greeting_seq = 0
        self._current_pose = {"a0": 47.0, "a1": 65.0, "a2": 54.0, "a3": 76.0}  # home
        
        # Load poses
        self.poses = {}
        if presets_path.exists():
            with open(presets_path, 'r') as f:
                data = json.load(f)
                self.poses = data.get("poses", {})
        
        # Initialize ServoMixer if hardware is available
        self.servo_mixer = None
        if self.use_hardware:
            try:
                self.servo_mixer = ServoMixer(bb, port="")  # Auto-detect port
                print(f"[MockArmController] ServoMixer initialized - REAL MOTORS ENABLED")
            except Exception as e:
                print(f"[MockArmController] Failed to initialize ServoMixer: {e}")
                self.use_hardware = False
        
        print(f"[MockArmController] Loaded {len(self.poses)} poses")
        if self.use_hardware:
            print(f"[MockArmController] 🎯 REAL MOTOR MODE - Arms will move!")
        else:
            print(f"[MockArmController] 📺 SIMULATION MODE - No motors")
    
    def send_arm_command(self, pose: dict):
        """Send arm position command to ServoMixer."""
        if self.servo_mixer:
            # ServoMixer expects 'A' command format: A<a0>,<a1>,<a2>,<a3>
            cmd = f"A{pose['a0']:.1f},{pose['a1']:.1f},{pose['a2']:.1f},{pose['a3']:.1f}"
            try:
                self.servo_mixer._serial.write(cmd.encode() + b'\n')
                print(f"   📡 Sent to motors: {cmd}")
            except Exception as e:
                print(f"   ❌ Motor command failed: {e}")
    
    def execute_greeting(self, pose_name: str):
        """Execute a greeting pose."""
        if pose_name in self.poses:
            target_pose = self.poses[pose_name]
            print(f"\n{'=' * 60}")
            print(f"🎯 EXECUTING GREETING: {pose_name}")
            print(f"   Moving from: {self._current_pose}")
            print(f"   Moving to:   {target_pose}")
            print(f"{'=' * 60}")
            
            # Simulate movement
            time.sleep(0.5)
            
            # Update position
            self._current_pose = dict(target_pose)
            self.bb.write(
                arm_a0=target_pose["a0"],
                arm_a1=target_pose["a1"],
                arm_a2=target_pose["a2"],
                arm_a3=target_pose["a3"],
                arm_greeting_active=True
            )
            
            # Send to real motors if available
            if self.use_hardware:
                self.send_arm_command(target_pose)
            
            print(f"   ✅ Pose reached!")
            
            # Hold pose for 2 seconds
            time.sleep(2.0)
            
            # Return to home
            home = {"a0": 47.0, "a1": 65.0, "a2": 54.0, "a3": 76.0}
            print(f"   Returning to home: {home}")
            time.sleep(0.5)
            self._current_pose = home
            self.bb.write(
                arm_a0=home["a0"],
                arm_a1=home["a1"],
                arm_a2=home["a2"],
                arm_a3=home["a3"],
                arm_greeting_active=False
            )
            
            # Send to real motors if available
            if self.use_hardware:
                self.send_arm_command(home)
            
            print(f"   ✅ Returned to home\n")
        else:
            print(f"[MockArmController] WARNING: Pose '{pose_name}' not found")
    
    def run(self):
        """Monitor for greeting requests and execute them."""
        print("[MockArmController] Monitoring for greeting requests...")
        
        while self.bb.read("running")["running"]:
            state = self.bb.read("arm_greeting_seq", "arm_greeting_pose")
            current_seq = state["arm_greeting_seq"]
            
            if current_seq != self._last_greeting_seq:
                self._last_greeting_seq = current_seq
                pose_name = state["arm_greeting_pose"]
                if pose_name:
                    self.execute_greeting(pose_name)
            
            time.sleep(0.1)
        
        print("[MockArmController] Stopped.")


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
    print("  5. Move real arm motors (if hardware available)")
    print("  6. Show debug stream at http://localhost:9000")
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
    
    # Start debug stream
    print("[Test] Starting debug stream...")
    debug_server = start_debug_stream(bb, greeting_service, port=9000)
    
    # Create and start arm controller (with hardware if available)
    print("[Test] Starting ArmController...")
    presets_path = APP_DIR / "tests" / "arm_pose_presets.json"
    arm_controller = MockArmController(bb, presets_path, use_hardware=HARDWARE_AVAILABLE)
    
    arm_thread = threading.Thread(
        target=arm_controller.run,
        daemon=True,
        name="ArmController"
    )
    arm_thread.start()
    
    print("\n" + "=" * 60)
    print("Monitoring face greetings...")
    print("Available hi poses:", greeting_service.hi_poses)
    print(f"Memory timeout: {greeting_service.memory_timeout_sec / 60:.1f} minutes")
    print("Debug stream: http://localhost:9000")
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
        
        # Show memory details
        if greeting_service.greeted_faces:
            print(f"\n  Remembered faces:")
            for i, face in enumerate(greeting_service.greeted_faces, 1):
                age_sec = time.time() - face.timestamp
                print(f"    {i}. Greeted {face.greet_count}x, {age_sec / 60:.1f} min ago")


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
