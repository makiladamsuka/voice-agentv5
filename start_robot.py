"""Unified entry point for the greeting robot.

Starts the Blackboard, core Life Force threads, and optional Agent/Animation threads.
"""

import sys
import threading
import time

from core.blackboard import Blackboard
from core.face_tracking import FaceTracker
from core.servo_loop import ServoLoop
from core.servo_mixer import ServoMixer
from core.eye_renderer import EyeRenderer
from core.emotion_engine import EmotionEngine
from hardware.arduino_servo import ArduinoServoLink

# Adjust these if your hardware uses a specific port. Empty string auto-detects.
SERVO_PORT = ""
SERVO_BAUD = 115200


def main():
    print("Initializing Unified Greeting Robot...")
    bb = Blackboard()

    # Hardware link (ESP32)
    print("Connecting to servo hardware...")
    link = ArduinoServoLink(port=SERVO_PORT, baud=SERVO_BAUD)
    link.configure_servo_stream(
        min_deg=0.06,
        send_hz=25.0,
        quantum_deg=0.2,
    )
    if not link.connect():
        print("WARNING: Servo tracking unavailable (hardware not found).")
        print("Camera and TFT eyes will continue without head motion.")

    # Core threads (Life Force)
    face_tracker = FaceTracker(bb)
    servo_loop = ServoLoop(bb)
    mixer = ServoMixer(bb, link)
    eye_renderer = EyeRenderer(bb)
    emotion_engine = EmotionEngine(bb)

    threads = [
        threading.Thread(target=face_tracker.run, daemon=True, name="vision"),
        threading.Thread(target=servo_loop.run, daemon=True, name="servo"),
        threading.Thread(target=mixer.run, daemon=True, name="mixer"),
        threading.Thread(target=eye_renderer.run, daemon=True, name="eyes"),
        threading.Thread(target=emotion_engine.run, daemon=True, name="emotion"),
    ]

    # Try loading optional components
    try:
        from agent.voice_agent import start_voice_agent
        threads.append(
            threading.Thread(target=start_voice_agent, args=(bb,), daemon=True, name="voice")
        )
        print("Voice agent module loaded.")
    except ImportError:
        print("Voice agent not available. Running face tracking only.")

    try:
        from animations.animation_player import AnimationRunner
        anim = AnimationRunner(bb)
        threads.append(
            threading.Thread(target=anim.run, daemon=True, name="animation")
        )
        print("Animation module loaded.")
    except ImportError:
        print("Animations not available.")

    # Start all
    print("\nStarting threads...")
    for t in threads:
        t.start()
        print(f"  Started: {t.name}")

    print("\nRobot is ALIVE. Press Ctrl+C to stop.\n")

    try:
        # Keep main thread alive and print status occasionally
        while True:
            time.sleep(2)
            state = bb.read("servo_pan", "servo_tilt", "emotion", "track_kind", "session_active")
            pan = state["servo_pan"]
            tilt = state["servo_tilt"]
            emotion = state["emotion"]
            kind = state["track_kind"]
            active = "VOICE" if state["session_active"] else "IDLE"
            
            sys.stdout.write(f"\r[Status] {active} | {kind} | {emotion} | P:{pan:.1f} T:{tilt:.1f}   ")
            sys.stdout.flush()

    except KeyboardInterrupt:
        print("\nShutting down...")
    finally:
        bb.write(running=False)
        time.sleep(0.5)
        
        try:
            link.close(home_pan=80.0, home_tilt=110.0)
            print("Servo link closed.")
        except Exception as e:
            print(f"Servo close error: {e}")

if __name__ == "__main__":
    main()
