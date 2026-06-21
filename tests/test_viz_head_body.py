#!/usr/bin/env python3
"""Manual head+body 3D visualization test.

Controls and fusion debug live in the browser at http://localhost:8082/
(WASD buttons + keys when the page is focused). Rotate the base by hand.

Stop start_robot.py first (serial + port 8082 conflict)::

  cd voice-agentv5
  python tests/test_viz_head_body.py

Browser controls
----------------
  W/S/A/D   tilt / pan head
  C         center head
  Z         zero base encoder + fusion reset
  R         fusion reset (keep encoder)
  Q         quit

Verification
------------
1. WASD only (base still): enc base ~0, orange imu base ~0, world/pan change.
2. Hand-turn only: blue arrow + orange tick track together; fusion Δ small when still.
3. When still, encoder drift-correction snaps IMU yaw (drift fix row in viz).
"""

from __future__ import annotations

import argparse
import sys
import threading
import time
from pathlib import Path

import _bootstrap  # noqa: F401

from arduino_servo import ArduinoServoLink
from base_motor_utils import apply_base_calibration_to_nano
from base_yaw_controller import (
    EncoderImuDriftCorrector,
    HeadYawFusion,
    angular_delta_deg,
    decompose_yaw,
    resolve_fusion_yaw,
)
from core.blackboard import Blackboard
from core.debug_dashboard import DebugDashboard
from head_debug_viz import servo_pan_to_mechanical, servo_tilt_to_mechanical
from robottest import clamp, load_servo_limits

CONFIG_PATH = Path(__file__).resolve().parents[1] / "config.yaml"

POLL_SEC = 0.05

try:
    import yaml
except ImportError:
    yaml = None


def _load_yaml(path: Path) -> dict:
    if yaml is None or not path.exists():
        return {}
    with open(path, encoding="utf-8") as f:
        return yaml.safe_load(f) or {}


def _mech_kwargs(servo_cfg: dict) -> tuple[dict, dict]:
    pan_center = float(servo_cfg.get("pan_center", 100.0))
    tilt_center = float(servo_cfg.get("tilt_center", 110.0))
    pan_kw = dict(
        center=pan_center,
        p_min=float(servo_cfg.get("pan_min", 25.0)),
        p_max=float(servo_cfg.get("pan_max", 150.0)),
        mech_left_deg=float(servo_cfg.get("pan_mech_left_deg", -40.0)),
        mech_right_deg=float(servo_cfg.get("pan_mech_right_deg", 40.0)),
    )
    tilt_kw = dict(
        center=tilt_center,
        t_min=float(servo_cfg.get("tilt_min", 100.0)),
        t_max=float(servo_cfg.get("tilt_max", 150.0)),
        mech_down_deg=float(servo_cfg.get("tilt_min_mechanical_deg", -35.0)),
        mech_up_deg=float(servo_cfg.get("tilt_max_mechanical_deg", 45.0)),
    )
    return pan_kw, tilt_kw


def _pan_mech(pan_cmd: float, pan_kw: dict, pan_sign: float = 1.0) -> float:
    return servo_pan_to_mechanical(pan_cmd, **pan_kw) * pan_sign


def _tilt_mech(tilt_cmd: float, tilt_kw: dict, tilt_sign: float = -1.0) -> float:
    return servo_tilt_to_mechanical(tilt_cmd, **tilt_kw) * tilt_sign


def _fusion_resync(
    fusion: HeadYawFusion,
    corrector: EncoderImuDriftCorrector,
    *,
    pan_mech: float,
    base_enc: float,
    imu_yaw: float,
    now: float | None = None,
    lock_startup: bool = True,
) -> None:
    fusion.reset_reference(
        pan_mech_deg=pan_mech,
        base_encoder_deg=base_enc,
        imu_yaw_total_deg=imu_yaw,
        now=now,
        lock_startup=lock_startup,
    )
    corrector.reset_motion_tracking()


def _start_imu(imu_cfg: dict):
    if not imu_cfg.get("enabled", True):
        return None
    try:
        from imu_sensor import ImuReader, startup_level_calibrate
    except ImportError:
        print("WARNING: imu_sensor not available — encoder-only mode.")
        return None

    _axis = imu_cfg.get("axis_remap")
    axis_remap = tuple(int(v) for v in _axis) if _axis else (-3, 2, -1)
    reader = ImuReader(
        bus=int(imu_cfg.get("i2c_bus", 1)),
        address=int(imu_cfg.get("address", 0x69)),
        sample_hz=float(imu_cfg.get("sample_hz", 100.0)),
        roll_pitch_alpha=float(imu_cfg.get("roll_pitch_alpha", 0.02)),
        axis_remap=axis_remap,
        roll_offset_deg=float(imu_cfg.get("roll_offset_deg", 0.0)),
        pitch_offset_deg=float(imu_cfg.get("pitch_offset_deg", 0.0)),
        yaw_sign=float(imu_cfg.get("yaw_sign", 1.0)),
    )
    reader.start()
    if imu_cfg.get("auto_level_on_start", True):
        settle = float(imu_cfg.get("auto_level_sec", 2.0))
        print(f"IMU level calibration ({settle:.1f}s) — hold head still…")
        startup_level_calibrate(
            reader,
            duration_sec=settle,
            warmup_sec=float(imu_cfg.get("auto_level_warmup_sec", 0.3)),
            max_gyro_dps=float(imu_cfg.get("auto_level_gyro_max_dps", 8.0)),
            min_samples=int(imu_cfg.get("auto_level_min_samples", 40)),
        )
    time.sleep(0.2)
    return reader


def _read_imu_raw(reader, yaw_sign: float) -> tuple[float, float, float, float, bool]:
    if reader is None:
        return 0.0, 0.0, 0.0, 0.0, False
    sample = reader.latest()
    if sample is None:
        return 0.0, 0.0, 0.0, 0.0, False
    imu_yaw = reader.filter.yaw_integral_deg() * yaw_sign
    gyro = max(abs(sample.gyro_x_dps), abs(sample.gyro_y_dps), abs(sample.gyro_z_dps))
    return imu_yaw, sample.pitch_deg, sample.roll_deg, gyro, True


def _query_base_enc(link: ArduinoServoLink, fallback: float = 0.0) -> tuple[float, bool]:
    """Read base encoder; tolerate transient serial glitches."""
    try:
        st = link.query_status()
        if st is not None:
            enc = float(st.degrees)
            # Reject single-frame spikes (serial noise / limit bounce).
            if abs(angular_delta_deg(enc, fallback)) > 45.0:
                print(f"WARNING: ignoring encoder spike {enc:+.1f}° (prev {fallback:+.1f}°)")
                return fallback, bool(st.busy)
            return enc, bool(st.busy)
    except Exception as exc:
        print(f"WARNING: base status read failed: {exc}")
    return fallback, False


def _publish_blackboard(
    bb: Blackboard,
    *,
    cfg: dict,
    pan: float,
    tilt: float,
    base_enc: float,
    base_busy: bool,
    decomp,
    imu_yaw: float,
    imu_yaw_raw: float,
    imu_inferred: float,
    drift_correction: float,
    stationary: bool,
    imu_pitch: float,
    imu_roll: float,
    imu_gyro: float,
    imu_ok: bool,
    step: float,
) -> None:
    """Write the same blackboard fields start_robot services use for /api/state."""
    servo_cfg = cfg.get("servo", {}) or {}
    tilt_center = float(servo_cfg.get("tilt_center", 110.0))
    bb.write(
        servo_pan=pan,
        servo_tilt=tilt,
        servo_mode="manual_test",
        servo_forward_return_active=False,
        servo_pan_hold=False,
        track_kind="none",
        face_detected=False,
        face_norm_x=0.0,
        face_norm_y=0.0,
        face_count=0,
        body_detected=False,
        manual_control_enabled=True,
        debug_head_step_deg=step,
        base_encoder_deg=base_enc,
        base_world_yaw_deg=decomp.world_head_yaw_deg,
        base_motion_busy=base_busy,
        imu_yaw_integral_deg=imu_yaw,
        imu_yaw_raw_deg=imu_yaw_raw,
        imu_drift_correction_deg=drift_correction,
        fusion_stationary=stationary,
        imu_inferred_base_deg=imu_inferred,
        body_yaw_deg=decomp.body_yaw_deg,
        head_yaw_on_body_deg=decomp.head_yaw_on_body_deg,
        imu_yaw_rel_deg=decomp.imu_yaw_rel_deg,
        head_imu_vs_servo_delta_deg=decomp.head_imu_vs_servo_delta_deg,
        imu_pitch_deg=imu_pitch,
        imu_roll_deg=imu_roll,
        imu_gyro_dps=imu_gyro,
        imu_horizon_ok=True,
        imu_available=imu_ok,
        imu_effective_tilt_center=tilt_center,
        person_snapshots=[],
        last_seen_world_yaw=None,
    )


def _print_viz_banner(viz_url: str, *, include_camera: bool) -> None:
    print(f"\nManual head+body viz running.")
    print(f"Open {viz_url}")
    print("  LEFT  = 3D head model (drag to orbit). Stats panel on the right.")
    if include_camera:
        print("  Camera stream appears above stats when face tracking is active.")
    print("  WASD buttons / keys when the page is focused.")
    print("  Hand-turn the base to test encoder + IMU fusion.\n")


def _apply_head_cmd(
    cmd: str,
    *,
    link: ArduinoServoLink,
    pan: float,
    tilt: float,
    head_step: float,
    pan_min: float,
    pan_max: float,
    tilt_min: float,
    tilt_max: float,
    pan_center: float,
    tilt_center: float,
    pan_sign: float = 1.0,
    tilt_sign: float = -1.0,
) -> tuple[float, float]:
    if cmd == "tilt_up":
        tilt = clamp(tilt + tilt_sign * head_step, tilt_min, tilt_max)
        link.write_angles(pan, tilt)
    elif cmd == "tilt_down":
        tilt = clamp(tilt - tilt_sign * head_step, tilt_min, tilt_max)
        link.write_angles(pan, tilt)
    elif cmd == "pan_left":
        pan = clamp(pan + pan_sign * head_step, pan_min, pan_max)
        link.write_angles(pan, tilt)
    elif cmd == "pan_right":
        pan = clamp(pan - pan_sign * head_step, pan_min, pan_max)
        link.write_angles(pan, tilt)
    elif cmd == "center":
        pan = pan_center
        tilt = tilt_center
        link.write_angles(pan, tilt, force=True)
    return pan, tilt


def run_test(*, port: str, baud: int, head_step: float, no_config_cpd: bool) -> int:
    cfg = _load_yaml(CONFIG_PATH)
    servo_cfg = cfg.get("servo", {}) or {}
    base_cfg = cfg.get("base", {}) or {}
    imu_cfg = cfg.get("imu", {}) or {}
    debug_viz_cfg = cfg.get("debug_viz", {}) or {}

    viz_host = str(debug_viz_cfg.get("host", "0.0.0.0"))
    viz_port = int(debug_viz_cfg.get("port", 8082))
    viz_display = "localhost" if viz_host in ("0.0.0.0", "") else viz_host
    viz_url = f"http://{viz_display}:{viz_port}/"

    pan_min, pan_max, tilt_min, tilt_max, pan_center, tilt_center = load_servo_limits()
    pan_kw, tilt_kw = _mech_kwargs(servo_cfg)
    pan_sign = float(servo_cfg.get("pan_sign", 1.0))
    tilt_sign = float(servo_cfg.get("tilt_sign", -1.0))
    yaw_sign = float(imu_cfg.get("yaw_sign", 1.0))

    link = ArduinoServoLink(port=port, baud=baud)
    if not link.connect():
        print("Failed to connect. Check USB and stop other serial users.")
        return 1

    imu_reader = None
    bb = Blackboard()
    bb.write(
        running=True,
        yaw_reference_locked=False,
        imu_calibrated=False,
        imu_available=False,
        servo_mode="manual_test",
        manual_control_enabled=True,
        debug_head_step_deg=head_step,
        debug_control_cmd="",
        debug_control_seq=0,
    )

    dashboard = DebugDashboard(
        bb,
        host=viz_host,
        port=viz_port,
        servo_cfg=servo_cfg,
        debug_viz_cfg=debug_viz_cfg,
        base_cfg=base_cfg,
        include_camera_stream=False,
    )
    threading.Thread(target=dashboard.run, daemon=True, name="DebugDashboard").start()
    time.sleep(0.3)

    fusion = HeadYawFusion(imu_yaw_sign=yaw_sign)
    corrector = EncoderImuDriftCorrector()

    pan = pan_center
    tilt = tilt_center
    last_cmd_seq = 0
    prev_base_enc: float | None = None

    try:
        if not no_config_cpd:
            apply_base_calibration_to_nano(link)

        imu_reader = _start_imu(imu_cfg)
        link.write_angles(pan, tilt, force=True)

        st = link.query_status()
        base_enc = st.degrees if st is not None else 0.0
        base_busy = bool(st.busy) if st is not None else False
        imu_yaw_raw, _, _, _, imu_ok = _read_imu_raw(imu_reader, yaw_sign)
        pan_mech = _pan_mech(pan, pan_kw, pan_sign)
        _fusion_resync(
            fusion, corrector,
            pan_mech=pan_mech, base_enc=base_enc, imu_yaw=imu_yaw_raw,
        )
        bb.write(
            yaw_reference_locked=True,
            imu_calibrated=imu_ok,
            imu_available=imu_ok,
        )

        _print_viz_banner(viz_url, include_camera=False)

        while bb.read("running")["running"]:
            now = time.time()
            cmd_state = bb.read("debug_control_cmd", "debug_control_seq", "debug_head_step_deg")
            cmd = cmd_state["debug_control_cmd"]
            cmd_seq = int(cmd_state["debug_control_seq"])
            step = float(cmd_state["debug_head_step_deg"] or head_step)

            if cmd and cmd_seq > last_cmd_seq:
                last_cmd_seq = cmd_seq
                if cmd == "quit":
                    bb.write(running=False, debug_control_cmd="")
                    break
                if cmd == "zero_base":
                    link.zero_base()
                    time.sleep(0.15)
                    base_enc, base_busy = _query_base_enc(link, base_enc)
                    imu_yaw_raw, _, _, _, _ = _read_imu_raw(imu_reader, yaw_sign)
                    pan_mech = _pan_mech(pan, pan_kw, pan_sign)
                    _fusion_resync(
                        fusion, corrector,
                        pan_mech=pan_mech, base_enc=base_enc, imu_yaw=imu_yaw_raw, now=now,
                    )
                    prev_base_enc = base_enc
                elif cmd == "fusion_reset":
                    base_enc, base_busy = _query_base_enc(link, base_enc)
                    imu_yaw_raw, _, _, _, _ = _read_imu_raw(imu_reader, yaw_sign)
                    pan_mech = _pan_mech(pan, pan_kw, pan_sign)
                    _fusion_resync(
                        fusion, corrector,
                        pan_mech=pan_mech, base_enc=base_enc, imu_yaw=imu_yaw_raw, now=now,
                    )
                    prev_base_enc = base_enc
                elif cmd in ("tilt_up", "tilt_down", "pan_left", "pan_right", "center"):
                    pan, tilt = _apply_head_cmd(
                        cmd,
                        link=link,
                        pan=pan,
                        tilt=tilt,
                        head_step=step,
                        pan_min=pan_min,
                        pan_max=pan_max,
                        tilt_min=tilt_min,
                        tilt_max=tilt_max,
                        pan_center=pan_center,
                        tilt_center=tilt_center,
                        pan_sign=pan_sign,
                        tilt_sign=tilt_sign,
                    )
                    corrector.reset_motion_tracking()
                bb.write(debug_control_cmd="")

            base_enc, base_busy = _query_base_enc(link, base_enc)

            imu_yaw_raw, imu_pitch, imu_roll, imu_gyro, imu_ok = _read_imu_raw(
                imu_reader, yaw_sign
            )
            pan_mech = _pan_mech(pan, pan_kw, pan_sign)

            imu_yaw, drift_correction, stationary = corrector.update(
                fusion,
                imu_yaw_raw=imu_yaw_raw,
                base_encoder_deg=base_enc,
                pan_mech_deg=pan_mech,
                gyro_dps=imu_gyro,
                now=now,
            )
            if stationary and imu_reader is not None and abs(drift_correction) > 0.01:
                imu_reader.filter.set_yaw_integral_deg(imu_yaw / yaw_sign)

            imu_yaw, imu_inferred = resolve_fusion_yaw(
                fusion,
                imu_yaw_corrected=imu_yaw,
                base_encoder_deg=base_enc,
                pan_mech_deg=pan_mech,
                prev_base_encoder_deg=prev_base_enc,
            )
            fusion.imu_yaw_total_deg = imu_yaw
            decomp = decompose_yaw(
                fusion,
                imu_yaw_total=imu_yaw,
                base_encoder_deg=base_enc,
                pan_mech_deg=pan_mech,
            )
            prev_base_enc = base_enc

            _publish_blackboard(
                bb,
                cfg=cfg,
                pan=pan,
                tilt=tilt,
                base_enc=base_enc,
                base_busy=base_busy,
                decomp=decomp,
                imu_yaw=imu_yaw,
                imu_yaw_raw=imu_yaw_raw,
                imu_inferred=imu_inferred,
                drift_correction=drift_correction,
                stationary=stationary,
                imu_pitch=imu_pitch,
                imu_roll=imu_roll,
                imu_gyro=imu_gyro,
                imu_ok=imu_ok,
                step=step,
            )

            time.sleep(POLL_SEC)

        return 0

    except KeyboardInterrupt:
        print("\nInterrupted.")
        bb.write(running=False)
        return 0
    finally:
        bb.write(running=False)
        if imu_reader is not None:
            imu_reader.stop()
        link.write_angles(pan_center, tilt_center, force=True)
        time.sleep(0.2)
        link.close(skip_home=True)


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Browser-controlled head + hand-turn base 3D viz test",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__,
    )
    parser.add_argument("--port", default="", help="Serial port (default auto-detect)")
    parser.add_argument("--baud", type=int, default=115200)
    parser.add_argument("--head-step", type=float, default=5.0, help="Degrees per WASD step")
    parser.add_argument(
        "--no-config-cpd",
        action="store_true",
        help="Skip loading base calibration from config.yaml",
    )
    args = parser.parse_args()
    return run_test(
        port=args.port,
        baud=args.baud,
        head_step=args.head_step,
        no_config_cpd=args.no_config_cpd,
    )


if __name__ == "__main__":
    sys.exit(main())
