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
from core.gesture_engine import GestureEngine
from core.emotion_engine import EmotionEngine
from core.eye_renderer import EyeRenderer
from core.debug_dashboard import DebugDashboard
from lib.live_tune import load_tune_defaults_from_config, sanitize_config
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
        return sanitize_config(yaml.safe_load(f) or {})


def _wait_imu_ready(bb: Blackboard, timeout_sec: float = 12.0) -> None:
    """Block until ImuService finishes startup calibration (or IMU disabled)."""
    deadline = time.time() + timeout_sec
    while time.time() < deadline:
        state = bb.read("imu_calibrated")
        if state["imu_calibrated"]:
            return
        time.sleep(0.05)
    print("[Bootstrap] WARNING: IMU calibration wait timed out.")


def _wait_fusion_ready(bb: Blackboard, timeout_sec: float = 3.0) -> None:
    """Block until ImuService finishes startup fusion resync."""
    deadline = time.time() + timeout_sec
    while time.time() < deadline:
        if not bb.read("base_fusion_resync_request")["base_fusion_resync_request"]:
            return
        time.sleep(0.05)
    print("[Bootstrap] WARNING: fusion resync wait timed out.")


def _print_yaw_decomposition(bb: Blackboard) -> None:
    """Log the three-layer yaw model locked at startup."""
    state = bb.read(
        "base_encoder_deg",
        "body_yaw_deg",
        "head_yaw_on_body_deg",
        "imu_yaw_rel_deg",
        "base_world_yaw_deg",
        "head_imu_vs_servo_delta_deg",
        "imu_available",
    )
    print("[Bootstrap] Yaw model (true north = 0° fixed at startup):")
    print(f"  encoder raw     {state['base_encoder_deg']:+.1f}°")
    if state["imu_available"]:
        print(
            f"  body (encoder)  {state['body_yaw_deg']:+.1f}°"
            f"  |  head-on-body (IMU) {state['head_yaw_on_body_deg']:+.1f}°"
        )
        print(
            f"  world aim       {state['base_world_yaw_deg']:+.1f}°"
            f"  |  imu rel {state['imu_yaw_rel_deg']:+.1f}°"
        )
        delta = state["head_imu_vs_servo_delta_deg"]
        if abs(delta) > 3.0:
            print(f"  WARNING: head IMU vs servo pan Δ {delta:+.1f}°")
    else:
        print("  IMU off — world yaw = encoder + servo pan (from ServoMixer)")


def _print_debug_viz_banner(debug_viz_cfg: dict) -> None:
    if not debug_viz_cfg.get("enabled", True):
        return
    host = str(debug_viz_cfg.get("host", "0.0.0.0"))
    port = int(debug_viz_cfg.get("port", 8082))
    display = "localhost" if host in ("0.0.0.0", "") else host
    url = f"http://{display}:{port}/"
    manual = bool(debug_viz_cfg.get("manual_control_enabled", False))
    print(f"[Bootstrap] Debug viz: {url}")
    if manual:
        print("[Bootstrap]   Manual WASD/Z/R enabled in browser when page is focused.")
    else:
        print("[Bootstrap]   3D yaw lines: orange=body, pink=head-on-body, yellow=world aim.")


def _lock_yaw_reference(bb: Blackboard, link, base_cfg: dict) -> None:
    """Zero encoder + IMU yaw reference at current forward pose."""
    if link is not None and link.connected:
        if base_cfg.get("zero_on_start", False):
            link.zero_base()
            print("[Bootstrap] Base encoder zeroed at startup forward pose.")
            time.sleep(0.2)
        try:
            st = link.query_status()
            if st is not None:
                bb.write(
                    base_encoder_deg=st.degrees,
                    base_encoder_synced=True,
                    base_motion_busy=st.busy,
                )
                print(
                    f"[Bootstrap] Encoder synced: {st.degrees:+.1f}° "
                    f"(CPD={st.counts_per_degree:.2f}, counts={st.encoder_count})"
                )
                if abs(st.counts_per_degree - 1.0) < 0.1:
                    print(
                        "[Bootstrap] WARNING: firmware CPD≈1 — base cal (C/E) may not be applied. "
                        "Moves will not stop correctly."
                    )
        except Exception as exc:
            print(f"[Bootstrap] WARNING: encoder sync failed: {exc}")

    bb.write(base_watchdog_reset=True)
    time.sleep(0.15)
    bb.write(yaw_reference_locked=True)
    print("[Bootstrap] Yaw reference locked (world yaw = 0 at startup pose).")


def main():
    cfg = _load_yaml(DEFAULT_CONFIG_PATH)
    servo_cfg = cfg.get("servo", {}) or {}
    base_cfg = cfg.get("base", {}) or {}
    imu_cfg = cfg.get("imu", {}) or {}
    debug_viz_cfg = cfg.get("debug_viz", {}) or {}
    port = servo_cfg.get("port") or ""
    baud = int(servo_cfg.get("baud", 115200))

    print("=== Voice Agent V5 (Modular) ===")

    bb = Blackboard()
    bb.write(
        running=True,
        yaw_reference_locked=False,
        imu_calibrated=False,
        base_encoder_synced=False,
        manual_control_enabled=bool(debug_viz_cfg.get("manual_control_enabled", False)),
        debug_control_cmd="",
        debug_control_seq=0,
        debug_head_step_deg=float(debug_viz_cfg.get("head_step_deg", 5.0)),
        debug_live_tune=load_tune_defaults_from_config(cfg),
        debug_tune_seq=0,
    )

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
        else:
            print("WARNING: ESP32 connect failed. Running in dry-run mode.")
            link.close(skip_home=True)
            link = None
    except Exception as e:
        print(f"WARNING: Serial connection failed: {e}. Running in dry-run mode.")
        link = None

    base_gate = BaseMotionGate(backoff_sec=float(base_cfg.get("error_backoff_sec", 45.0)))
    bb.write(base_motion_allowed=True)

    # ── Phase 1: IMU startup (yaw reference needs still samples) ─────────────
    imu_thread = threading.Thread(target=ImuService(bb).run, daemon=True, name="ImuService")
    imu_thread.start()
    if imu_cfg.get("enabled", False):
        settle = float(imu_cfg.get("auto_level_sec", 2.0)) + float(
            imu_cfg.get("auto_level_warmup_sec", 0.3)
        )
        print(f"[Bootstrap] Waiting {settle:.1f}s for IMU level calibration…")
        _wait_imu_ready(bb, timeout_sec=settle + 5.0)
    else:
        _wait_imu_ready(bb, timeout_sec=2.0)

    _lock_yaw_reference(bb, link, base_cfg)
    if imu_cfg.get("enabled", False):
        bb.write(base_fusion_resync_request=True)
        _wait_fusion_ready(bb)
        if bb.read("imu_available")["imu_available"]:
            _print_yaw_decomposition(bb)
        else:
            print("[Bootstrap] IMU unavailable — world yaw falls back to encoder + servo pan.")
    else:
        enc = bb.read("base_encoder_deg")["base_encoder_deg"]
        bb.write(
            body_yaw_deg=enc,
            head_yaw_on_body_deg=0.0,
            base_world_yaw_deg=enc,
        )

    _print_debug_viz_banner(debug_viz_cfg)

    # ── Phase 2: remaining services ───────────────────────────────────────────
    threads = [
        threading.Thread(target=FaceTracker(bb).run, daemon=True, name="FaceTracker"),
        threading.Thread(target=ServoLoop(bb).run, daemon=True, name="ServoLoop"),
        threading.Thread(
            target=BaseController(bb, link, gate=base_gate).run,
            daemon=True,
            name="BaseController",
        ),
        threading.Thread(
            target=ServoMixer(bb, link, gate=base_gate).run,
            daemon=True,
            name="ServoMixer",
        ),
        threading.Thread(target=GestureEngine(bb).run, daemon=True, name="GestureEngine"),
        threading.Thread(target=EmotionEngine(bb).run, daemon=True, name="EmotionEngine"),
        threading.Thread(target=EyeRenderer(bb).run, daemon=True, name="EyeRenderer"),
    ]

    if debug_viz_cfg.get("enabled", True):
        threads.append(
            threading.Thread(
                target=DebugDashboard(
                    bb,
                    host=str(debug_viz_cfg.get("host", "0.0.0.0")),
                    port=int(debug_viz_cfg.get("port", 8082)),
                    servo_cfg=servo_cfg,
                    debug_viz_cfg=debug_viz_cfg,
                    base_cfg=base_cfg,
                    config_path=DEFAULT_CONFIG_PATH,
                ).run,
                daemon=True,
                name="DebugDashboard",
            )
        )

    for t in threads:
        t.start()

    def signal_handler(sig, frame):
        print("\nShutting down...")
        bb.write(running=False)
        time.sleep(0.5)
        if link is not None:
            pan_center = float(servo_cfg.get("pan_center", 80.0))
            tilt_center = float(servo_cfg.get("tilt_center", 110.0))
            print(f"Homing servos (pan={pan_center}, tilt={tilt_center}, arms) and stopping base...")
            link.close(
                home_pan=pan_center, 
                home_tilt=tilt_center,
                home_arm0=0.0,
                home_arm1=180.0,
                home_arm2=90.0,
                home_arm3=90.0
            )
        sys.exit(0)

    signal.signal(signal.SIGINT, signal_handler)
    signal.signal(signal.SIGTERM, signal_handler)

    print("Robot running. Press Ctrl+C to exit.")

    while True:
        time.sleep(1.0)


if __name__ == "__main__":
    main()
