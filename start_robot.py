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
from core.arm_controller import ArmController
from core.emotion_engine import EmotionEngine
from core.eye_renderer import EyeRenderer
from core.debug_dashboard import DebugDashboard
from core.tof_state import STATE as TOF_STATE
from core.tof_stream import TofStreamHandler
from core.yaw_pose import lock_home_tracker, publish_tracker_pose, query_enc, update_tracker
from lib.yaw_home_tracker import YawHomeTracker
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
    deadline = time.time() + timeout_sec
    while time.time() < deadline:
        state = bb.read("imu_calibrated")
        if state["imu_calibrated"]:
            return
        time.sleep(0.05)
    print("[Bootstrap] WARNING: IMU calibration wait timed out.")


def _print_yaw_decomposition(bb: Blackboard) -> None:
    """Log YawHomeTracker pose locked at startup."""
    state = bb.read(
        "base_encoder_deg",
        "body_yaw_deg",
        "head_yaw_on_body_deg",
        "from_home_enc_deg",
        "from_home_imu_deg",
        "disagreement_deg",
        "base_world_yaw_deg",
        "imu_available",
    )
    print("[Bootstrap] Yaw model (HOME = 0° at startup forward):")
    print(f"  encoder raw       {state['base_encoder_deg']:+.1f}°")
    if state["imu_available"]:
        print(
            f"  FROM HOME enc     {state['from_home_enc_deg']:+.1f}°"
            f"  |  imu {state['from_home_imu_deg']:+.1f}°"
            f"  Δ {state['disagreement_deg']:+.1f}°"
        )
        print(
            f"  body (imu)        {state['body_yaw_deg']:+.1f}°"
            f"  |  pan-on-body {state['head_yaw_on_body_deg']:+.1f}°"
        )
        print(f"  world aim         {state['base_world_yaw_deg']:+.1f}°")
    else:
        print("  IMU off — world yaw = encoder + servo pan")


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
        print("[Bootstrap]   ToF proximity map + camera stream on debug port.")


def _check_tof_firmware(link) -> None:
    if link is None or not link.connected:
        return
    try:
        banner = link.firmware_banner() or ""
    except Exception:
        banner = ""
    if banner:
        print(f"[Bootstrap] Firmware: {banner.strip().split(chr(10))[-1]}")
    if "tof_stream" not in banner and "v16" not in banner:
        print(
            "[Bootstrap] WARNING: expected v16_tof_stream firmware for ToF dashboard map. "
            "Reflash: ./firmware/flash.sh prod"
        )


def _bootstrap_home_arms(
    link: ArduinoServoLink,
    bb: Blackboard,
    arms_cfg: dict,
    servo_cfg: dict,
) -> tuple[float, float, float, float] | None:
    """Send arm home pose immediately after connect (before IMU wait)."""
    if not arms_cfg.get("enabled", False):
        return None
    if not link.connected or not link.has_arm_firmware():
        print("[Bootstrap] Arms enabled in config but arm firmware not detected.")
        return None

    from arm_pose_presets import ArmPosePresets, DEFAULT_PRESETS_PATH
    from arm_safety_envelope import ArmSafetyEnvelope, DEFAULT_LIMITS_PATH

    limits_path = Path(arms_cfg.get("limits_path", DEFAULT_LIMITS_PATH))
    if not limits_path.is_absolute():
        limits_path = APP_DIR / limits_path
    presets_path = Path(arms_cfg.get("presets_path", DEFAULT_PRESETS_PATH))
    if not presets_path.is_absolute():
        presets_path = APP_DIR / presets_path

    envelope = ArmSafetyEnvelope.from_json(limits_path)
    base_pose = str(arms_cfg.get("base_pose", "home"))
    presets = ArmPosePresets.load_or_create_home(presets_path, home=envelope.homes)
    home = envelope.clamp_arms(*presets.get(base_pose))

    pan = float(servo_cfg.get("pan_center", 100.0))
    tilt = float(servo_cfg.get("tilt_center", 110.0))
    link.write_angles_and_arms(pan, tilt, *home, force=True)
    time.sleep(0.4)
    bb.write(arm_a0=home[0], arm_a1=home[1], arm_a2=home[2], arm_a3=home[3])
    print(
        f"[Bootstrap] Arms homed: A0={home[0]:.1f} A1={home[1]:.1f} "
        f"A2={home[2]:.1f} A3={home[3]:.1f}",
        flush=True,
    )
    return home


def _resolve_arm_home_pose(
    arms_cfg: dict,
    arm_controller: ArmController | None,
) -> tuple[float, float, float, float] | None:
    """Home arm pose for shutdown (same source as bootstrap)."""
    if not arms_cfg.get("enabled", False):
        return None
    if arm_controller is not None and arm_controller.enabled:
        return arm_controller.home_pose

    from arm_pose_presets import ArmPosePresets, DEFAULT_PRESETS_PATH
    from arm_safety_envelope import ArmSafetyEnvelope, DEFAULT_LIMITS_PATH

    limits_path = Path(arms_cfg.get("limits_path", DEFAULT_LIMITS_PATH))
    if not limits_path.is_absolute():
        limits_path = APP_DIR / limits_path
    presets_path = Path(arms_cfg.get("presets_path", DEFAULT_PRESETS_PATH))
    if not presets_path.is_absolute():
        presets_path = APP_DIR / presets_path

    envelope = ArmSafetyEnvelope.from_json(limits_path)
    base_pose = str(arms_cfg.get("base_pose", "home"))
    presets = ArmPosePresets.load_or_create_home(presets_path, home=envelope.homes)
    return envelope.clamp_arms(*presets.get(base_pose))


def _load_arm_envelope(
    arms_cfg: dict,
    arm_controller: ArmController | None,
) -> "ArmSafetyEnvelope":
    from arm_safety_envelope import ArmSafetyEnvelope, DEFAULT_LIMITS_PATH

    if arm_controller is not None and arm_controller.enabled:
        return arm_controller.envelope

    limits_path = Path(arms_cfg.get("limits_path", DEFAULT_LIMITS_PATH))
    if not limits_path.is_absolute():
        limits_path = APP_DIR / limits_path
    return ArmSafetyEnvelope.from_json(limits_path)


def _shutdown_home_servos(
    link: ArduinoServoLink,
    bb: Blackboard,
    *,
    arms_cfg: dict,
    arm_controller: ArmController | None,
    servo_cfg: dict,
) -> None:
    """Smooth arm + head homing (same path as test_servo_arms_safe.py exit)."""
    from lib.arm_home_motion import smooth_home_arms

    pan_center = float(servo_cfg.get("pan_center", 100.0))
    tilt_center = float(servo_cfg.get("tilt_center", 110.0))
    link.write_base_stop()

    arm_home = _resolve_arm_home_pose(arms_cfg, arm_controller)
    if arm_home is not None and link.has_arm_firmware():
        state = bb.read("arm_a0", "arm_a1", "arm_a2", "arm_a3")
        start = [
            float(state["arm_a0"]),
            float(state["arm_a1"]),
            float(state["arm_a2"]),
            float(state["arm_a3"]),
        ]
        for i, attr in enumerate(("_last_a0", "_last_a1", "_last_a2", "_last_a3")):
            sent = getattr(link, attr, None)
            if sent is not None:
                start[i] = float(sent)

        envelope = _load_arm_envelope(arms_cfg, arm_controller)
        blend_sec = float(arms_cfg.get("shutdown_blend_sec", 0.6))
        link.configure_servo_stream(send_hz=30.0, min_deg=0.02, quantum_deg=0.1)
        final = smooth_home_arms(
            link, tuple(start), arm_home, envelope, blend_sec=blend_sec
        )
        bb.write(
            arm_a0=final[0],
            arm_a1=final[1],
            arm_a2=final[2],
            arm_a3=final[3],
        )
        print(
            f"Arms at home: A0={final[0]:.1f} A1={final[1]:.1f} "
            f"A2={final[2]:.1f} A3={final[3]:.1f}",
            flush=True,
        )

    print(f"Homing head (pan={pan_center}, tilt={tilt_center})...", flush=True)
    link.home_smooth(pan_center, tilt_center)
    time.sleep(0.12)


def main():
    cfg = _load_yaml(DEFAULT_CONFIG_PATH)
    servo_cfg = cfg.get("servo", {}) or {}
    base_cfg = cfg.get("base", {}) or {}
    arms_cfg = cfg.get("arms", {}) or {}
    imu_cfg = cfg.get("imu", {}) or {}
    prox_cfg = cfg.get("proximity", {}) or {}
    debug_viz_cfg = cfg.get("debug_viz", {}) or {}
    base_yaw_sign = float(debug_viz_cfg.get("base_yaw_sign", -1.0))
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

    if link is not None and link.connected:
        _check_tof_firmware(link)
        _bootstrap_home_arms(link, bb, arms_cfg, servo_cfg)

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

    tracker = YawHomeTracker(
        counts_per_degree=float(base_cfg.get("counts_per_degree", 31.1667)),
        encoder_sign=float(base_cfg.get("encoder_sign", -1.0)),
        still_hold_sec=float(imu_cfg.get("drift_stationary_hold_sec", 0.35)),
        gyro_max_dps=float(imu_cfg.get("drift_gyro_max_dps", 6.0)),
        snap_max_disagreement_deg=float(
            imu_cfg.get("drift_snap_max_disagreement_deg", 5.0)
        ),
    )
    tof_handler = TofStreamHandler(
        TOF_STATE,
        bb,
        spin_settle_sec=float(prox_cfg.get("tof_spin_settle_sec", 0.65)),
        gyro_settle_dps=float(prox_cfg.get("tof_gyro_settle_dps", 8.0)),
    )

    if link is not None and link.connected:
        TOF_STATE.set_connected(link._port_name or port or "serial")
        TOF_STATE.update_pose(base_yaw_sign=base_yaw_sign)
        # Reset IMU yaw integral before locking HOME (matches old fusion resync timing).
        bb.write(base_watchdog_reset=True)
        time.sleep(0.2)
        lock_home_tracker(
            tracker,
            link,
            bb,
            servo_cfg,
            zero_encoder=bool(base_cfg.get("zero_on_start", False)),
        )
        _, _, _, cpd0 = query_enc(link, 0.0)
        tracker.counts_per_degree = max(cpd0, 0.05)
        pan = float(bb.read("servo_pan")["servo_pan"])
        enc, count, _, cpd = query_enc(link, float(bb.read("base_encoder_deg")["base_encoder_deg"]))
        sample = update_tracker(
            tracker,
            bb,
            encoder_deg=enc,
            encoder_count=count,
            counts_per_degree=cpd,
            pan=pan,
            servo_cfg=servo_cfg,
            base_busy=False,
        )
        if sample is not None:
            imu_ok = bool(bb.read("imu_available")["imu_available"])
            publish_tracker_pose(
                bb,
                TOF_STATE,
                sample,
                imu_online=imu_ok,
                base_yaw_sign=base_yaw_sign,
                max_yaw_deg=float(base_cfg.get("max_yaw_deg", 120.0)),
            )
        if bb.read("imu_available")["imu_available"]:
            _print_yaw_decomposition(bb)
        else:
            print("[Bootstrap] IMU unavailable — world yaw falls back to encoder + servo pan.")
    else:
        bb.write(yaw_reference_locked=True)

    _print_debug_viz_banner(debug_viz_cfg)

    arm_controller: ArmController | None = None
    if arms_cfg.get("enabled", False):
        arm_controller = ArmController(bb)
        if link is not None and link.connected and not link.has_arm_firmware():
            print(
                "[Bootstrap] WARNING: arms.enabled but ESP32 has no arm firmware. "
                "Flash firmware/head_servo_hands/ for arm gestures."
            )

    if link is not None and link.connected:
        link._tof_callback = tof_handler.handle_tof_line

        def _serial_pump() -> None:
            while bb.read("running")["running"]:
                link._poll_prox_lines()
                time.sleep(0.008)

        threading.Thread(target=_serial_pump, name="SerialPump", daemon=True).start()

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
            target=ServoMixer(
                bb,
                link,
                gate=base_gate,
                tracker=tracker,
                tof_state=TOF_STATE,
                tof_handler=tof_handler,
                base_yaw_sign=base_yaw_sign,
            ).run,
            daemon=True,
            name="ServoMixer",
        ),
        threading.Thread(target=EmotionEngine(bb).run, daemon=True, name="EmotionEngine"),
        threading.Thread(target=EyeRenderer(bb).run, daemon=True, name="EyeRenderer"),
    ]
    if arm_controller is not None and arm_controller.enabled:
        threads.append(
            threading.Thread(target=arm_controller.run, daemon=True, name="ArmController")
        )

    if debug_viz_cfg.get("enabled", True):
        threads.append(
            threading.Thread(
                target=                DebugDashboard(
                    bb,
                    host=str(debug_viz_cfg.get("host", "0.0.0.0")),
                    port=int(debug_viz_cfg.get("port", 8082)),
                    servo_cfg=servo_cfg,
                    debug_viz_cfg=debug_viz_cfg,
                    base_cfg=base_cfg,
                    config_path=DEFAULT_CONFIG_PATH,
                    tof_state=TOF_STATE,
                ).run,
                daemon=True,
                name="DebugDashboard",
            )
        )

    # ── Phase 3: Voice / LiveKit Agent (Optional) ───────────────────────────
    voice_cfg = cfg.get("voice", {}) or {}
    voice_devmode = voice_cfg.get("devmode", True)
    if len(sys.argv) > 1 and sys.argv[1] == "start":
        voice_devmode = False
    elif len(sys.argv) > 1 and sys.argv[1] == "dev":
        voice_devmode = True

    voice_thread: threading.Thread | None = None
    if voice_cfg.get("enabled", False):
        from voice.voice_service import run_voice_service

        voice_thread = threading.Thread(
            target=run_voice_service,
            kwargs={"bb": bb, "devmode": voice_devmode},
            daemon=False,
            name="VoiceService",
        )
        threads.append(voice_thread)

    for t in threads:
        t.start()

    worker_threads = list(threads)

    def signal_handler(sig, frame):
        print("\nShutting down...")
        bb.write(running=False)
        if voice_thread is not None and voice_thread.is_alive():
            voice_thread.join(timeout=10.0)
            if voice_thread.is_alive():
                print("[Bootstrap] WARNING: VoiceService shutdown timed out.")
        for t in worker_threads:
            if t is voice_thread:
                continue
            t.join(timeout=2.0)
        if link is not None:
            _shutdown_home_servos(
                link,
                bb,
                arms_cfg=arms_cfg,
                arm_controller=arm_controller,
                servo_cfg=servo_cfg,
            )
            link.close(skip_home=True)
        sys.exit(0)

    signal.signal(signal.SIGINT, signal_handler)
    signal.signal(signal.SIGTERM, signal_handler)

    if voice_cfg.get("enabled", False):
        mode_label = "dev" if voice_devmode else "start"
        print(
            f"Voice agent enabled (LiveKit {mode_label}). "
            "Connect via frontend with AGENT_NAME=campus-greeting-agent."
        )

    print("Robot running. Press Ctrl+C to exit.")

    while True:
        time.sleep(1.0)


if __name__ == "__main__":
    main()
