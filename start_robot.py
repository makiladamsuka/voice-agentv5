"""Main entry point for the modular Voice Agent V5."""

import signal
import sys
import threading
import time
from pathlib import Path

from base_safety import BaseMotionGate
from core.blackboard import Blackboard
from core.face_tracking import FaceTracker
from core.imu_service import ImuService
from core.servo_loop import ServoLoop
from core.base_controller import BaseController
from core.servo_mixer import ServoMixer
from core.emotion_engine import EmotionEngine
from core.eye_renderer import EyeRenderer
from core.debug_dashboard import DebugDashboard
from hardware.arduino_servo import ArduinoServoLink
from base_motor_utils import apply_base_calibration_to_nano

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
    base_cfg = cfg.get("base", {}) or {}
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
        if link.connect():
            if apply_base_calibration_to_nano(link):
                print("Applied base cal (CPD/sign); base moves use L/R spin like robottest.")
            else:
                cpd = float(base_cfg.get("counts_per_degree", 31.1667))
                esign = float(base_cfg.get("encoder_sign", -1.0))
                scale = float(base_cfg.get("command_scale", 1.0))
                link.set_counts_per_degree(cpd)
                link.set_encoder_sign(esign)
                link.base_command_scale = scale
                print(f"Applied base cal: CPD={cpd:.2f}, sign={esign}, scale={scale:.2f}")
            
            if base_cfg.get("zero_on_start", False):
                link.zero_base()
                print("Zeroed base encoder (assumed current position is forward center).")
        else:
            print("WARNING: ESP32 connect failed. Running in dry-run mode.")
            link.close(skip_home=True)
            link = None
    except Exception as e:
        print(f"WARNING: Serial connection failed: {e}. Running in dry-run mode.")
        link = None

    # 3. Instantiate Core Services
    threads = []
    base_gate = BaseMotionGate(backoff_sec=float(base_cfg.get("error_backoff_sec", 45.0)))
    bb.write(base_motion_allowed=True, base_encoder_synced=False)
    
    # Vision Pipeline
    threads.append(threading.Thread(target=FaceTracker(bb).run, daemon=True, name="FaceTracker"))
    
    # IMU / Attitude
    threads.append(threading.Thread(target=ImuService(bb).run, daemon=True, name="ImuService"))
    
    # Head Motion / PID
    threads.append(threading.Thread(target=ServoLoop(bb).run, daemon=True, name="ServoLoop"))
    
    # Base Motion Decisions
    threads.append(threading.Thread(
        target=BaseController(bb, link, gate=base_gate).run,
        daemon=True,
        name="BaseController",
    ))
    
    # Hardware Mixer (ESP32 TX/RX)
    threads.append(threading.Thread(
        target=ServoMixer(bb, link, gate=base_gate).run,
        daemon=True,
        name="ServoMixer",
    ))
    
    # Emotion Engine
    threads.append(threading.Thread(target=EmotionEngine(bb).run, daemon=True, name="EmotionEngine"))
    
    # Screen Rendering
    threads.append(threading.Thread(target=EyeRenderer(bb).run, daemon=True, name="EyeRenderer"))

    # Unified Debug Dashboard (Camera Stream + 3D Viz)
    threads.append(threading.Thread(target=DebugDashboard(bb).run, daemon=True, name="DebugDashboard"))

    # 4. Start all services
    for t in threads:
        t.start()

    # 5. Handle graceful shutdown
    def signal_handler(sig, frame):
        print("\nShutting down...")
        bb.write(running=False)
        time.sleep(0.5)
        if link is not None:
            pan_center = float(servo_cfg.get("pan_center", 80.0))
            tilt_center = float(servo_cfg.get("tilt_center", 110.0))
            print(f"Homing servos (pan={pan_center}, tilt={tilt_center}) and stopping base...")
            link.close(home_pan=pan_center, home_tilt=tilt_center)
        sys.exit(0)

    signal.signal(signal.SIGINT, signal_handler)
    signal.signal(signal.SIGTERM, signal_handler)

    print("Robot running. Press Ctrl+C to exit.")
    
    # Keep main thread alive
    while True:
        time.sleep(1.0)


if __name__ == "__main__":
    main()
