"""Main entry point for the modular Voice Agent V5."""

import os
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
from core.yaw_pose import (
    lock_head_home,
    lock_home_tracker,
    publish_tof_viz_pose,
    publish_tracker_pose,
    query_enc,
    resnap_tracker_after_spin,
    update_tracker,
)
from lib.yaw_home_tracker import YawHomeTracker
from lib.base_home_drive import drive_base_to_imu_zero
from lib.head_mech import signed_pan_mech_deg
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
    if not debug_viz_cfg.get("enabled", True) and os.environ.get("DEBUG_VIZ", "").strip().lower() not in (
        "1",
        "true",
        "yes",
    ):
        return
    if not _should_start_debug_viz(debug_viz_cfg):
        port = int(debug_viz_cfg.get("port", 8082))
        print(f"[Bootstrap] Debug viz off (enable with DEBUG_VIZ=1 → port {port})")
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


def _shutdown_home_base(
    link: ArduinoServoLink,
    bb: Blackboard,
    tracker: YawHomeTracker,
    *,
    servo_cfg: dict,
    base_cfg: dict,
) -> None:
    """IMU closed-loop spin back to startup forward before exit (like approach HOME)."""
    if not link.connected or not tracker.home_locked:
        return

    pan_center = float(servo_cfg.get("pan_center", 100.0))
    tilt_center = float(servo_cfg.get("tilt_center", 110.0))
    tol = float(base_cfg.get("home_success_tolerance_deg", 2.5))

    state = bb.read("from_home_imu_deg", "from_home_enc_deg")
    imu_off = float(state["from_home_imu_deg"])
    enc_off = float(state["from_home_enc_deg"])
    if abs(imu_off) <= tol and abs(enc_off) <= tol:
        print(
            f"[Bootstrap] Base already at HOME (imu={imu_off:+.1f}° enc={enc_off:+.1f}°)",
            flush=True,
        )
        return

    def query_imu_home() -> tuple[float, float, bool, float]:
        enc, count, busy, cpd = query_enc(link, float(bb.read("base_encoder_deg")["base_encoder_deg"]))
        imu_state = bb.read("imu_yaw_integral_deg", "imu_gyro_dps")
        imu_yaw = float(imu_state["imu_yaw_integral_deg"])
        gyro = float(imu_state["imu_gyro_dps"])
        pan_mech = signed_pan_mech_deg(pan_center, servo_cfg)
        tracker.counts_per_degree = max(cpd, 0.05)
        sample = tracker.update(
            encoder_deg=enc,
            encoder_count=count,
            imu_yaw_deg=imu_yaw,
            pan_mech_deg=pan_mech,
            gyro_dps=gyro,
            base_busy=True,
        )
        imu_home = sample.from_home_imu_deg if sample is not None else imu_off
        return imu_home, enc, busy, gyro

    print(
        f"[Bootstrap] Returning base to forward (imu {imu_off:+.1f}° from HOME)...",
        flush=True,
    )
    try:
        link.write_angles(pan_center, tilt_center)
        link.mute_tof()
        ok, final_imu = drive_base_to_imu_zero(
            link,
            base_cfg,
            query_imu_home=query_imu_home,
            log=print,
        )
        link.unmute_tof()
        enc, count, _, _ = query_enc(link, 0.0)
        sample = resnap_tracker_after_spin(
            tracker,
            bb,
            encoder_deg=enc,
            encoder_count=count,
            pan=pan_center,
            servo_cfg=servo_cfg,
        )
        enc_home = sample.from_home_enc_deg if sample is not None else enc_off
        tag = "OK" if ok else "FAIL"
        print(
            f"[Bootstrap] Base HOME {tag}: imu={final_imu:+.1f}° enc={enc_home:+.1f}°",
            flush=True,
        )
    except Exception as exc:
        print(f"[Bootstrap] Base HOME return failed: {exc}", flush=True)
        try:
            link.write_base_stop()
            link.unmute_tof()
        except Exception:
            pass


def _resolve_config_path() -> Path:
    """Config file: --config path, CONFIG_PATH env, or config.yaml."""
    import argparse

    parser = argparse.ArgumentParser(add_help=False)
    parser.add_argument("--config", type=str, default=None)
    args, _ = parser.parse_known_args()
    raw = args.config or os.environ.get("CONFIG_PATH", "").strip()
    if not raw:
        return DEFAULT_CONFIG_PATH
    path = Path(raw)
    if not path.is_absolute():
        path = APP_DIR / path
    return path


def _should_start_debug_viz(debug_viz_cfg: dict) -> bool:
    force = os.environ.get("DEBUG_VIZ", "").strip().lower() in ("1", "true", "yes")
    if force:
        return True
    if not debug_viz_cfg.get("enabled", True):
        return False
    return bool(debug_viz_cfg.get("auto_start", True))


def main():
    config_path = _resolve_config_path()
    cfg = _load_yaml(config_path)
    servo_cfg = cfg.get("servo", {}) or {}
    base_cfg = cfg.get("base", {}) or {}
    arms_cfg = cfg.get("arms", {}) or {}
    imu_cfg = cfg.get("imu", {}) or {}
    prox_cfg = cfg.get("proximity", {}) or {}
    debug_viz_cfg = cfg.get("debug_viz", {}) or {}
    base_yaw_sign = float(debug_viz_cfg.get("base_yaw_sign", -1.0))
    pan_yaw_sign = float(debug_viz_cfg.get("pan_yaw_sign", -1.0))
    tilt_sign = float(debug_viz_cfg.get("tilt_sign", 1.0))
    imu_pitch_sign = float(debug_viz_cfg.get("imu_pitch_sign", -1.0))
    port = servo_cfg.get("port") or ""
    baud = int(servo_cfg.get("baud", 115200))

    print("=== Voice Agent V5 (Modular) ===")
    if config_path != DEFAULT_CONFIG_PATH:
        print(f"[Bootstrap] Config: {config_path}")
    vp = cfg.get("voice_profile", {}) or {}
    print(
        f"[Bootstrap] Servo loop {servo_cfg.get('loop_hz', '?')} Hz "
        f"(voice {vp.get('servo_loop_hz', '?')} Hz), "
        f"base {base_cfg.get('loop_hz', 50)} Hz "
        f"(voice {vp.get('base_loop_hz', '?')} Hz)"
    )

    eyes_cfg = cfg.get("eyes", {}) or {}
    default_eye_color = tuple(eyes_cfg.get("eye_color", [255, 255, 255]))

    bb = Blackboard()
    bb.write(
        running=True,
        yaw_reference_locked=False,
        imu_calibrated=False,
        base_encoder_synced=False,
        eye_color=default_eye_color,
        manual_control_enabled=bool(debug_viz_cfg.get("manual_control_enabled", False)),
        debug_control_cmd="",
        debug_control_seq=0,
        debug_head_step_deg=float(debug_viz_cfg.get("head_step_deg", 5.0)),
        debug_live_tune=load_tune_defaults_from_config(cfg),
        debug_tune_seq=0,
        stream_viewers=0,
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
    imu_thread = threading.Thread(
        target=ImuService(bb, config_path=config_path).run,
        daemon=True,
        name="ImuService",
    )
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
        pan_stable_deg=float(imu_cfg.get("drift_pan_stable_deg", 0.2)),
        enc_stable_deg=float(imu_cfg.get("drift_enc_stable_deg", 0.2)),
    )
    tof_handler = TofStreamHandler(
        TOF_STATE,
        bb,
        spin_settle_sec=float(prox_cfg.get("tof_spin_settle_sec", 0.65)),
        gyro_settle_dps=float(prox_cfg.get("tof_gyro_settle_dps", 8.0)),
    )

    if link is not None and link.connected:
        TOF_STATE.set_connected(link._port_name or port or "serial")
        TOF_STATE.update_pose(
            base_yaw_sign=base_yaw_sign,
            pan_yaw_sign=pan_yaw_sign,
            tilt_sign=tilt_sign,
            imu_pitch_sign=imu_pitch_sign,
        )
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
        tilt = float(bb.read("servo_tilt")["servo_tilt"])
        imu_state = bb.read("imu_available", "imu_pitch_deg")
        imu_pitch = float(imu_state.get("imu_pitch_deg", 0.0) or 0.0)
        lock_head_home(imu_pitch, pan, tilt, servo_cfg)
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
            imu_ok = bool(imu_state["imu_available"])
            max_yaw = float(base_cfg.get("max_yaw_deg", 120.0))
            publish_tracker_pose(
                bb,
                TOF_STATE,
                sample,
                imu_online=imu_ok,
                base_yaw_sign=base_yaw_sign,
                max_yaw_deg=max_yaw,
            )
            publish_tof_viz_pose(
                TOF_STATE,
                sample,
                pan=pan,
                tilt=tilt,
                imu_pitch_deg=imu_pitch,
                servo_cfg=servo_cfg,
                imu_online=imu_ok,
                base_yaw_sign=base_yaw_sign,
                pan_yaw_sign=pan_yaw_sign,
                tilt_sign=tilt_sign,
                imu_pitch_sign=imu_pitch_sign,
                max_yaw_deg=max_yaw,
                home_locked=True,
                base_busy=False,
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
        arm_controller = ArmController(bb, config_path=config_path)
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
                pending = False
                try:
                    pending = link._ser is not None and link._ser.in_waiting > 0
                except Exception:
                    pending = False
                time.sleep(0.015 if pending else 0.04)

        threading.Thread(target=_serial_pump, name="SerialPump", daemon=True).start()

    # ── Phase 2: remaining services ───────────────────────────────────────────
    threads = [
        threading.Thread(target=FaceTracker(bb, config_path=config_path).run, daemon=True, name="FaceTracker"),
        threading.Thread(
            target=ServoLoop(bb, config_path=config_path).run,
            daemon=True,
            name="ServoLoop",
        ),
        threading.Thread(
            target=BaseController(bb, link, config_path=config_path, gate=base_gate).run,
            daemon=True,
            name="BaseController",
        ),
        threading.Thread(
            target=ServoMixer(
                bb,
                link,
                config_path=config_path,
                gate=base_gate,
                tracker=tracker,
                tof_state=TOF_STATE,
                tof_handler=tof_handler,
                base_yaw_sign=base_yaw_sign,
                pan_yaw_sign=pan_yaw_sign,
                tilt_sign=tilt_sign,
                imu_pitch_sign=imu_pitch_sign,
            ).run,
            daemon=True,
            name="ServoMixer",
        ),
        threading.Thread(
            target=EmotionEngine(bb, config_path=config_path).run,
            daemon=True,
            name="EmotionEngine",
        ),
        threading.Thread(target=EyeRenderer(bb).run, daemon=True, name="EyeRenderer"),
    ]
    face_greeting_cfg = cfg.get("face_greeting", {}) or {}
    if face_greeting_cfg.get("enabled", True):
        from core.face_greeting import FaceGreetingMonitor

        threads.append(
            threading.Thread(
                target=FaceGreetingMonitor(bb, config_path=config_path).run,
                daemon=True,
                name="FaceGreeting",
            )
        )
    
    # Face greeting arm gestures (separate from voice greetings)
    face_greeting_arm_cfg = cfg.get("face_greeting_arm", {}) or {}
    if face_greeting_arm_cfg.get("enabled", True) and arm_controller is not None and arm_controller.enabled:
        from core.face_greeting_arm import FaceGreetingArmService

        threads.append(
            threading.Thread(
                target=FaceGreetingArmService(bb, config_path=config_path).run,
                daemon=True,
                name="FaceGreetingArm",
            )
        )
        print("[Bootstrap] FaceGreetingArmService enabled — arm gestures for new faces")
    
    # ArmController - base arm movements
    if arm_controller is not None and arm_controller.enabled:
        threads.append(
            threading.Thread(target=arm_controller.run, daemon=True, name="ArmController")
        )
        print("[Bootstrap] ArmController enabled — base arm lean/sway movements")

    bye_wave_cfg = cfg.get("bye_wave", {}) or {}
    if bye_wave_cfg.get("enabled", False):
        from core.bye_wave_service import ByeWaveService

        bye_wave_svc = ByeWaveService(bb, cfg)
        threads.append(
            threading.Thread(
                target=bye_wave_svc.run,
                daemon=True,
                name="ByeWaveService",
            )
        )
        print(
            f"[Bootstrap] ByeWaveService enabled — "
            f"hand stream port {bye_wave_cfg.get('port', 8000)}."
        )

    if _should_start_debug_viz(debug_viz_cfg):
        stream_cfg = cfg.get("stream", {}) or {}
        threads.append(
            threading.Thread(
                target=DebugDashboard(
                    bb,
                    host=str(debug_viz_cfg.get("host", "0.0.0.0")),
                    port=int(debug_viz_cfg.get("port", 8082)),
                    servo_cfg=servo_cfg,
                    debug_viz_cfg=debug_viz_cfg,
                    base_cfg=base_cfg,
                    config_path=config_path,
                    stream_cfg=stream_cfg,
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

        # TalkGestureService - arm gestures while speaking
        talk_cfg = cfg.get("talk_gesture", {}) or {}
        if arms_cfg.get("enabled", False) and talk_cfg.get("enabled", True):
            from core.talk_gesture_service import TalkGestureService

            presets_path = Path(arms_cfg.get("presets_path", "tests/arm_pose_presets.json"))
            if not presets_path.is_absolute():
                presets_path = APP_DIR / presets_path

            talk_gesture_svc = TalkGestureService(
                bb=bb,
                presets_path=presets_path,
                pose_duration=float(talk_cfg.get("pose_duration", 0.4)),
                poll_interval=float(talk_cfg.get("poll_interval", 0.02)),
                vertical_speed=float(talk_cfg.get("vertical_speed", 0.8)),
                horizontal_speed=float(talk_cfg.get("horizontal_speed", 1.5)),
            )
            threads.append(
                threading.Thread(
                    target=talk_gesture_svc.run,
                    daemon=True,
                    name="TalkGestureService",
                )
            )
            v_speed = talk_cfg.get("vertical_speed", 0.8)
            h_speed = talk_cfg.get("horizontal_speed", 1.5)
            print(
                f"[Bootstrap] TalkGestureService enabled — "
                f"arms animate while speaking (v={v_speed}x, h={h_speed}x)"
            )

    from voice.voice_service import ensure_media_server

    ensure_media_server(bb, cfg)

    for t in threads:
        t.start()

    worker_threads = list(threads)
    shutdown_event = threading.Event()

    def signal_handler(sig, frame):
        if shutdown_event.is_set():
            return
        # Ignore further signals while cleanup runs (avoids re-entrant sys.exit during threading shutdown)
        signal.signal(signal.SIGINT, signal.SIG_IGN)
        signal.signal(signal.SIGTERM, signal.SIG_IGN)
        print("\nShutting down...")
        bb.write(base_step_ready=False, running=False)
        if link is not None and link.connected:
            _shutdown_home_base(
                link,
                bb,
                tracker,
                servo_cfg=servo_cfg,
                base_cfg=base_cfg,
            )
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
        shutdown_event.set()

    signal.signal(signal.SIGINT, signal_handler)
    signal.signal(signal.SIGTERM, signal_handler)

    if voice_cfg.get("enabled", False):
        mode_label = "dev" if voice_devmode else "start"
        print(
            f"Voice agent enabled (LiveKit {mode_label}). "
            "Connect via frontend with AGENT_NAME=campus-greeting-agent."
        )

    print("Robot running. Press Ctrl+C to exit.")

    while not shutdown_event.is_set():
        shutdown_event.wait(timeout=1.0)


if __name__ == "__main__":
    main()
