"""Main entry point for the modular Voice Agent V5."""

import signal
import sys
import threading
import time
from pathlib import Path

from core.blackboard import Blackboard
from core.face_tracking import FaceTracker
from core.imu_service import ImuService
from core.servo_loop import ServoLoop
from core.base_controller import BaseController
from core.servo_mixer import ServoMixer
from core.emotion_engine import EmotionEngine
from core.eye_renderer import EyeRenderer
from hardware.arduino_servo import ArduinoServoLink

APP_DIR = Path(__file__).resolve().parent
DEFAULT_CONFIG_PATH = APP_DIR / "config.yaml"

try:
    import yaml
except ImportError:
    yaml = None


def _load_yaml(path: Path) -> dict:
    if yaml is None or not path.exists():
        return {}
    with open(path, encoding="utf-8") as f:
        return yaml.safe_load(f) or {}


def main():
    cfg = _load_yaml(DEFAULT_CONFIG_PATH)
    servo_cfg = cfg.get("servo", {}) or {}
    port = servo_cfg.get("port") or ""
    baud = int(servo_cfg.get("baud", 115200))

    print("=== Voice Agent V5 (Modular) ===")
    
    # 1. Initialize Blackboard (the shared memory hub)
    bb = Blackboard()

    # 2. Initialize Hardware Link
    port_label = port if port else "auto"
    print(f"Connecting to ESP32 on {port_label}@{baud}...")
    link = None
    try:
        link = ArduinoServoLink(port=port, baud=baud)
        if not link.connect():
            print("WARNING: ESP32 connect failed. Running in dry-run mode.")
            link.close(skip_home=True)
            link = None
    except Exception as e:
        print(f"WARNING: Serial connection failed: {e}. Running in dry-run mode.")
        link = None

    # 3. Instantiate Core Services
    threads = []
    
    # Vision Pipeline
    threads.append(threading.Thread(target=FaceTracker(bb).run, daemon=True, name="FaceTracker"))
    
    # IMU / Attitude
    threads.append(threading.Thread(target=ImuService(bb).run, daemon=True, name="ImuService"))
    
    # Head Motion / PID
    threads.append(threading.Thread(target=ServoLoop(bb).run, daemon=True, name="ServoLoop"))
    
    # Base Motion Decisions
    threads.append(threading.Thread(target=BaseController(bb, link).run, daemon=True, name="BaseController"))
    
    # Hardware Mixer (ESP32 TX/RX)
    threads.append(threading.Thread(target=ServoMixer(bb, link).run, daemon=True, name="ServoMixer"))
    
    # Emotion Engine
    threads.append(threading.Thread(target=EmotionEngine(bb).run, daemon=True, name="EmotionEngine"))
    
    # Screen Rendering
    threads.append(threading.Thread(target=EyeRenderer(bb).run, daemon=True, name="EyeRenderer"))

    # 4. Start all services
    for t in threads:
        t.start()

    # 5. Handle graceful shutdown
    def signal_handler(sig, frame):
        print("\nShutting down...")
        bb.write(running=False)
        time.sleep(0.5)
        if link is not None:
            link.close()
        sys.exit(0)

    signal.signal(signal.SIGINT, signal_handler)
    signal.signal(signal.SIGTERM, signal_handler)

    print("Robot running. Press Ctrl+C to exit.")
    
    # Keep main thread alive
    while True:
        time.sleep(1.0)


if __name__ == "__main__":
    main()
