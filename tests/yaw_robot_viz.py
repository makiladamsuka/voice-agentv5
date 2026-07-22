#!/usr/bin/env python3
"""
Yaw rotation test — keyboard base spin + live 3D viz from HOME.

Locks HOME (forward) once at start. Heading follows IMU; when the base is still
(no encoder tick change), IMU is snapped to the encoder offset to prevent drift.

  cd voice-agentv5/tests
  python yaw_robot_viz.py --port /dev/ttyUSB0

  M / N     hold = spin base left / right  (same as robottest.py)
  W A S D   head tilt / pan
  C         center head
  H         spin base to HOME IMU yaw 0° + center head + re-lock HOME
  Z         zero encoder here (no move) + re-lock HOME
  ?         print status
  Q         quit

Viz: http://localhost:8766  — click page, then keyboard (or use terminal).
  3D view: grey base (IMU, M/N) + pink head (pan A/D, pitch from HOME at startup W/S).

Stop start_robot.py / approach.py first — one serial port.
"""

from __future__ import annotations

import argparse
import signal
import sys
import threading
import time
from pathlib import Path

import _bootstrap  # noqa: F401

from arduino_servo import ArduinoServoLink
from base_motor_utils import apply_base_calibration_to_nano
from core.yaw_pose import lock_head_home, pan_cmd_from_home, pitch_from_home
from lib.base_home_drive import (
    drive_base_to_encoder_zero,
    drive_base_to_imu_angle,
    drive_base_to_imu_zero,
    ensure_base_idle,
)
from lib.head_mech import signed_pan_mech_deg, signed_tilt_mech_deg
from lib.yaw_home_tracker import YawHomeTracker
from robottest import (
    RawKeyReader,
    apply_base_spin,
    clamp,
    desired_base_spin,
    format_status,
    load_servo_limits,
    verify_spin_firmware,
)
from yaw_viz_server import CONTROL, STATE, start_server

APP_DIR = Path(__file__).resolve().parents[1]
CONFIG_PATH = APP_DIR / "config.yaml"

POLL_SEC = 0.02
ENCODER_POLL_SEC = 0.05

try:
    import yaml
except ImportError:
    yaml = None


def _load_yaml(path: Path) -> dict:
    if yaml is None or not path.exists():
        return {}
    with open(path, encoding="utf-8") as f:
        return yaml.safe_load(f) or {}


def _start_imu(imu_cfg: dict):
    if not imu_cfg.get("enabled", True):
        return None
    try:
        from imu_sensor import ImuReader, startup_level_calibrate
    except ImportError:
        print("[YawViz] WARNING: imu_sensor not available — encoder-only mode.")
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
        print(f"[YawViz] IMU level calibration ({settle:.1f}s) — hold still…")
        startup_level_calibrate(
            reader,
            duration_sec=settle,
            warmup_sec=float(imu_cfg.get("auto_level_warmup_sec", 0.3)),
            max_gyro_dps=float(imu_cfg.get("auto_level_gyro_max_dps", 8.0)),
            min_samples=int(imu_cfg.get("auto_level_min_samples", 40)),
        )
    time.sleep(0.15)
    return reader


def _read_imu(
    reader, yaw_sign: float, pitch_sign: float = 1.0
) -> tuple[float, float, float, bool]:
    if reader is None:
        return 0.0, 0.0, 0.0, False
    sample = reader.latest()
    if sample is None:
        return 0.0, 0.0, 0.0, False
    imu_yaw = reader.filter.yaw_integral_deg() * yaw_sign
    imu_pitch = float(sample.pitch_deg) * pitch_sign
    gyro = max(abs(sample.gyro_x_dps), abs(sample.gyro_y_dps), abs(sample.gyro_z_dps))
    return imu_yaw, imu_pitch, gyro, True


def _query_enc(
    link: ArduinoServoLink, fallback: float
) -> tuple[float, int, bool, float]:
    try:
        st = link.query_status()
        if st is not None:
            cpd = float(st.counts_per_degree)
            return float(st.degrees), int(st.encoder_count), bool(st.busy), cpd
    except Exception:
        pass
    return fallback, 0, False, 31.1667


def _query_imu_home_pose(
    tracker: YawHomeTracker,
    link: ArduinoServoLink,
    reader,
    yaw_sign: float,
    pitch_sign: float,
    pan: float,
    servo_cfg: dict,
    *,
    tilt: float = 0.0,
    port: str = "",
    spin_label: str = "homing",
    publish: bool = False,
) -> tuple[float, float, bool, float]:
    """IMU base yaw from HOME (no drift snap while homing), enc deg, busy, gyro."""
    enc, count, busy, _cpd = _query_enc(link, 0.0)
    imu_yaw, imu_pitch, gyro, imu_ok = _read_imu(reader, yaw_sign, pitch_sign)
    pan_mech = signed_pan_mech_deg(pan, servo_cfg)
    sample = tracker.update(
        encoder_deg=enc,
        encoder_count=count,
        imu_yaw_deg=imu_yaw,
        pan_mech_deg=pan_mech,
        gyro_dps=gyro,
        base_busy=True,
    )
    imu_home = sample.from_home_imu_deg if sample is not None else 0.0
    if publish:
        _publish_state(
            sample,
            pan=pan,
            tilt=tilt,
            imu_pitch_deg=imu_pitch,
            spin_label=spin_label,
            imu_online=imu_ok,
            port=port,
            servo_cfg=servo_cfg,
        )
    return imu_home, enc, busy, gyro


def _lock_home(
    tracker: YawHomeTracker,
    link: ArduinoServoLink,
    reader,
    yaw_sign: float,
    pitch_sign: float,
    servo_cfg: dict,
    pan: float,
    tilt: float,
    *,
    zero_encoder: bool = False,
    label: str = "HOME",
) -> None:
    if zero_encoder:
        link.write_base_stop()
        link.zero_base()
        time.sleep(0.2)
    enc, count, _, _ = _query_enc(link, 0.0)
    imu_yaw, imu_pitch, _, imu_ok = _read_imu(reader, yaw_sign, pitch_sign)
    pan_mech = signed_pan_mech_deg(pan, servo_cfg)
    tracker.lock_home(
        encoder_deg=enc,
        encoder_count=count,
        imu_yaw_deg=imu_yaw,
        pan_mech_deg=pan_mech,
    )
    lock_head_home(imu_pitch, pan, tilt, servo_cfg)
    tilt_mech = signed_tilt_mech_deg(tilt, servo_cfg)
    print(
        f"[YawViz] {label} locked  enc={enc:+.1f}°  counts={count}  "
        f"imu={imu_yaw:+.1f}°  pan_cmd={pan:.0f}  pan_mech={pan_mech:+.1f}°  "
        f"pitch imu={imu_pitch:+.1f}° mech={tilt_mech:+.1f}° (viz pitch → 0°)"
        + ("" if imu_ok else "  (IMU off)")
    )


def _publish_state(
    sample,
    *,
    pan: float,
    tilt: float,
    imu_pitch_deg: float,
    spin_label: str,
    imu_online: bool,
    port: str,
    servo_cfg: dict,
) -> None:
    pitch_viz, imu_pitch_from_home = pitch_from_home(imu_pitch_deg, tilt, servo_cfg)
    pan_cmd_from_home = pan_cmd_from_home(pan)
    tilt_mech = signed_tilt_mech_deg(tilt, servo_cfg)
    if sample is None:
        STATE.update(
            connected=True,
            port=port,
            imu_online=imu_online,
            home_locked=False,
            head_pan=pan,
            head_tilt=tilt,
            pan_mech_deg=signed_pan_mech_deg(pan, servo_cfg),
            tilt_mech_deg=tilt_mech,
            pan_cmd_from_home_deg=pan_cmd_from_home,
            imu_pitch_deg=imu_pitch_deg,
            pitch_from_home_deg=pitch_viz,
            imu_pitch_from_home_deg=imu_pitch_from_home,
            spin_label=spin_label,
        )
        return
    STATE.update(
        connected=True,
        port=port,
        imu_online=imu_online,
        home_locked=True,
        base_busy=sample.base_busy,
        stationary=sample.stationary,
        spin_label=spin_label,
        from_home_enc_deg=sample.from_home_enc_deg,
        from_home_imu_deg=sample.from_home_imu_deg,
        imu_total_from_home_deg=sample.imu_total_from_home_deg,
        pan_from_home_deg=sample.pan_from_home_deg,
        pan_cmd_from_home_deg=pan_cmd_from_home,
        disagreement_deg=sample.disagreement_deg,
        encoder_deg=sample.encoder_deg,
        encoder_count=sample.encoder_count,
        encoder_count_delta=sample.encoder_count_delta,
        encoder_count_raw_delta=sample.encoder_count_raw_delta,
        imu_yaw_deg=sample.imu_yaw_deg,
        pan_mech_deg=sample.pan_mech_deg,
        tilt_mech_deg=tilt_mech,
        gyro_dps=sample.gyro_dps,
        imu_correction_deg=sample.imu_correction_deg,
        head_pan=pan,
        head_tilt=tilt,
        imu_pitch_deg=imu_pitch_deg,
        pitch_from_home_deg=pitch_viz,
        imu_pitch_from_home_deg=imu_pitch_from_home,
    )


def _apply_browser_cmd(
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
    tracker: YawHomeTracker,
    imu_reader,
    yaw_sign: float,
    pitch_sign: float,
    servo_cfg: dict,
    base_cfg: dict,
    active_spin: int,
    port: str = "",
) -> tuple[float, float, int, bool, bool]:
    """Returns (pan, tilt, active_spin, request_status, should_quit)."""
    quit_requested = False
    status = False
    cmd = cmd.lower()

    if cmd == "quit":
        link.write_base_stop()
        quit_requested = True
        return pan, tilt, 0, status, quit_requested

    if cmd == "status":
        status = True
        return pan, tilt, active_spin, status, quit_requested

    if cmd == "center":
        pan = pan_center
        tilt = tilt_center
        link.write_angles(pan, tilt, force=True)
        return pan, tilt, active_spin, status, quit_requested

    if cmd == "home_lock":
        link.write_base_stop()
        pan = pan_center
        tilt = tilt_center
        link.write_angles(pan, tilt, force=True)
        time.sleep(0.3)
        ensure_base_idle(link)
        if imu_reader is not None and tracker.home_locked:
            ok, final_imu = drive_base_to_imu_zero(
                link,
                base_cfg,
                query_imu_home=lambda: _query_imu_home_pose(
                    tracker,
                    link,
                    imu_reader,
                    yaw_sign,
                    pitch_sign,
                    pan,
                    servo_cfg,
                    tilt=tilt,
                    port=port,
                    spin_label="homing",
                    publish=True,
                ),
                log=print,
            )
            fail_val = final_imu
            fail_unit = "IMU"
        else:
            ok, final_enc = drive_base_to_encoder_zero(
                link,
                base_cfg,
                query_enc=lambda fb: _query_enc(link, fb),
                log=print,
            )
            fail_val = final_enc
            fail_unit = "enc"
        if ok:
            _lock_home(
                tracker,
                link,
                imu_reader,
                yaw_sign,
                pitch_sign,
                servo_cfg,
                pan,
                tilt,
                zero_encoder=False,
                label="HOME",
            )
        else:
            print(
                f"[YawViz] HOME not relocked — still {fail_val:+.1f}° {fail_unit} (try again)"
            )
        return pan, tilt, 0, status, quit_requested

    if cmd.startswith("goto_imu:"):
        link.write_base_stop()
        time.sleep(0.15)
        ensure_base_idle(link)
        try:
            target = float(cmd.split(":", 1)[1])
        except (IndexError, ValueError):
            print("[YawViz] GOTO: invalid angle")
            return pan, tilt, 0, status, quit_requested
        max_yaw = float(base_cfg.get("max_yaw_deg", 120.0))
        target = clamp(target, -max_yaw, max_yaw)
        if imu_reader is None or not tracker.home_locked:
            print("[YawViz] GOTO needs IMU and HOME locked")
            return pan, tilt, 0, status, quit_requested
        ok, final_imu = drive_base_to_imu_angle(
            link,
            base_cfg,
            target,
            query_imu_home=lambda: _query_imu_home_pose(
                tracker,
                link,
                imu_reader,
                yaw_sign,
                pitch_sign,
                pan,
                servo_cfg,
                tilt=tilt,
                port=port,
                spin_label="goto",
                publish=True,
            ),
            log=print,
        )
        if ok:
            print(f"[YawViz] GOTO done — imu from HOME {final_imu:+.1f}° (target {target:+.1f}°)")
        else:
            print(
                f"[YawViz] GOTO failed — imu {final_imu:+.1f}° "
                f"(wanted {target:+.1f}°)"
            )
        return pan, tilt, 0, status, quit_requested

    if cmd == "zero_home":
        link.write_base_stop()
        link.zero_base()
        time.sleep(0.2)
        _lock_home(
            tracker,
            link,
            imu_reader,
            yaw_sign,
            pitch_sign,
            servo_cfg,
            pan,
            tilt,
            zero_encoder=False,
            label="ZERO+HOME",
        )
        return pan, tilt, 0, status, quit_requested

    if cmd == "tilt_up":
        tilt = clamp(tilt + head_step, tilt_min, tilt_max)
        link.write_angles(pan, tilt)
    elif cmd == "tilt_down":
        tilt = clamp(tilt - head_step, tilt_min, tilt_max)
        link.write_angles(pan, tilt)
    elif cmd == "pan_left":
        pan = clamp(pan - head_step, pan_min, pan_max)
        link.write_angles(pan, tilt)
    elif cmd == "pan_right":
        pan = clamp(pan + head_step, pan_min, pan_max)
        link.write_angles(pan, tilt)

    return pan, tilt, active_spin, status, quit_requested


def _apply_terminal_key(
    key_l: str,
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
    tracker: YawHomeTracker,
    imu_reader,
    yaw_sign: float,
    pitch_sign: float,
    servo_cfg: dict,
    base_cfg: dict,
    active_spin: int,
    now: float,
    last_m: float,
    last_n: float,
    port: str = "",
) -> tuple[float, float, int, float, float, bool, bool]:
    """Returns pan, tilt, spin, last_m, last_n, status, quit."""
    status = False
    quit_requested = False

    if key_l == "m":
        last_m = now
    elif key_l == "n":
        last_n = now
    elif key_l in ("q", "\x03"):
        link.write_base_stop()
        quit_requested = True
    elif key_l == "?":
        status = True
    elif key_l == "c":
        pan, tilt, active_spin, status, quit_requested = _apply_browser_cmd(
            "center",
            link=link,
            pan=pan,
            tilt=tilt,
            head_step=head_step,
            pan_min=pan_min,
            pan_max=pan_max,
            tilt_min=tilt_min,
            tilt_max=tilt_max,
            pan_center=pan_center,
            tilt_center=tilt_center,
            tracker=tracker,
            imu_reader=imu_reader,
            yaw_sign=yaw_sign,
            pitch_sign=pitch_sign,
            servo_cfg=servo_cfg,
            base_cfg=base_cfg,
            active_spin=active_spin,
            port=port,
        )
    elif key_l == "h":
        pan, tilt, active_spin, status, quit_requested = _apply_browser_cmd(
            "home_lock",
            link=link,
            pan=pan,
            tilt=tilt,
            head_step=head_step,
            pan_min=pan_min,
            pan_max=pan_max,
            tilt_min=tilt_min,
            tilt_max=tilt_max,
            pan_center=pan_center,
            tilt_center=tilt_center,
            tracker=tracker,
            imu_reader=imu_reader,
            yaw_sign=yaw_sign,
            pitch_sign=pitch_sign,
            servo_cfg=servo_cfg,
            base_cfg=base_cfg,
            active_spin=active_spin,
            port=port,
        )
    elif key_l == "z":
        pan, tilt, active_spin, status, quit_requested = _apply_browser_cmd(
            "zero_home",
            link=link,
            pan=pan,
            tilt=tilt,
            head_step=head_step,
            pan_min=pan_min,
            pan_max=pan_max,
            tilt_min=tilt_min,
            tilt_max=tilt_max,
            pan_center=pan_center,
            tilt_center=tilt_center,
            tracker=tracker,
            imu_reader=imu_reader,
            yaw_sign=yaw_sign,
            pitch_sign=pitch_sign,
            servo_cfg=servo_cfg,
            base_cfg=base_cfg,
            active_spin=active_spin,
            port=port,
        )
    elif key_l == "w":
        pan, tilt, _, _, _ = _apply_browser_cmd(
            "tilt_up",
            link=link,
            pan=pan,
            tilt=tilt,
            head_step=head_step,
            pan_min=pan_min,
            pan_max=pan_max,
            tilt_min=tilt_min,
            tilt_max=tilt_max,
            pan_center=pan_center,
            tilt_center=tilt_center,
            tracker=tracker,
            imu_reader=imu_reader,
            yaw_sign=yaw_sign,
            pitch_sign=pitch_sign,
            servo_cfg=servo_cfg,
            base_cfg=base_cfg,
            active_spin=active_spin,
            port=port,
        )
    elif key_l == "s":
        pan, tilt, _, _, _ = _apply_browser_cmd(
            "tilt_down",
            link=link,
            pan=pan,
            tilt=tilt,
            head_step=head_step,
            pan_min=pan_min,
            pan_max=pan_max,
            tilt_min=tilt_min,
            tilt_max=tilt_max,
            pan_center=pan_center,
            tilt_center=tilt_center,
            tracker=tracker,
            imu_reader=imu_reader,
            yaw_sign=yaw_sign,
            pitch_sign=pitch_sign,
            servo_cfg=servo_cfg,
            base_cfg=base_cfg,
            active_spin=active_spin,
            port=port,
        )
    elif key_l == "a":
        pan, tilt, _, _, _ = _apply_browser_cmd(
            "pan_left",
            link=link,
            pan=pan,
            tilt=tilt,
            head_step=head_step,
            pan_min=pan_min,
            pan_max=pan_max,
            tilt_min=tilt_min,
            tilt_max=tilt_max,
            pan_center=pan_center,
            tilt_center=tilt_center,
            tracker=tracker,
            imu_reader=imu_reader,
            yaw_sign=yaw_sign,
            pitch_sign=pitch_sign,
            servo_cfg=servo_cfg,
            base_cfg=base_cfg,
            active_spin=active_spin,
            port=port,
        )
    elif key_l == "d":
        pan, tilt, _, _, _ = _apply_browser_cmd(
            "pan_right",
            link=link,
            pan=pan,
            tilt=tilt,
            head_step=head_step,
            pan_min=pan_min,
            pan_max=pan_max,
            tilt_min=tilt_min,
            tilt_max=tilt_max,
            pan_center=pan_center,
            tilt_center=tilt_center,
            tracker=tracker,
            imu_reader=imu_reader,
            yaw_sign=yaw_sign,
            pitch_sign=pitch_sign,
            servo_cfg=servo_cfg,
            base_cfg=base_cfg,
            active_spin=active_spin,
            port=port,
        )

    return pan, tilt, active_spin, last_m, last_n, status, quit_requested


def run(
    link: ArduinoServoLink,
    *,
    head_step: float,
    imu_reader,
    imu_cfg: dict,
    yaw_sign: float,
    pitch_sign: float,
    servo_cfg: dict,
    base_cfg: dict,
    zero_on_start: bool,
    port: str,
) -> None:
    pan_min, pan_max, tilt_min, tilt_max, pan_center, tilt_center = load_servo_limits()
    pan = pan_center
    tilt = tilt_center
    link.write_angles(pan, tilt, force=True)
    link.write_base_stop()

    tracker = YawHomeTracker(
        counts_per_degree=float(base_cfg.get("counts_per_degree", 31.1667)),
        encoder_sign=float(base_cfg.get("encoder_sign", -1.0)),
        still_hold_sec=float(imu_cfg.get("drift_stationary_hold_sec", 0.35)),
        gyro_max_dps=float(imu_cfg.get("drift_gyro_max_dps", 6.0)),
        snap_max_disagreement_deg=float(imu_cfg.get("drift_snap_max_disagreement_deg", 5.0)),
        pan_stable_deg=float(imu_cfg.get("drift_pan_stable_deg", 0.2)),
        enc_stable_deg=float(imu_cfg.get("drift_enc_stable_deg", 0.2)),
    )
    enc0, count0, _, cpd0 = _query_enc(link, 0.0)
    tracker.counts_per_degree = max(cpd0, 0.05)
    settle = float(imu_cfg.get("auto_level_settle_sec", 0.8))
    if settle > 0:
        time.sleep(min(settle, 1.5))
    _lock_home(
        tracker,
        link,
        imu_reader,
        yaw_sign,
        pitch_sign,
        servo_cfg,
        pan,
        tilt,
        zero_encoder=zero_on_start,
        label="STARTUP HOME",
    )

    print(
        "\n[yaw_robot_viz] Controls: browser (click page + keys) or terminal\n"
        f"  M/N hold spin   WASD head   C center   H → HOME (base+head)   Z zero here   ? status   Q quit\n"
    )

    running = True
    last_m = 0.0
    last_n = 0.0
    active_spin = 0
    enc_deg = enc0
    enc_count = count0
    base_busy = False
    last_enc_poll = 0.0
    status_every = 0.0
    use_terminal = sys.stdin.isatty()

    def stop(_sig=None, _frame=None) -> None:
        nonlocal running
        running = False

    signal.signal(signal.SIGINT, stop)
    signal.signal(signal.SIGTERM, stop)

    if not use_terminal:
        print("[YawViz] No TTY — use browser keyboard on the viz page.")

    key_reader = RawKeyReader() if use_terminal else None
    if key_reader is not None:
        key_reader.__enter__()

    homing = False
    last_home_ts = 0.0

    try:
        while running:
            now = time.time()

            if key_reader is not None:
                for key in key_reader.drain_keys():
                    key_l = key.lower() if len(key) == 1 else key
                    pan, tilt, active_spin, last_m, last_n, want_status, quit_req = (
                        _apply_terminal_key(
                            key_l,
                            link=link,
                            pan=pan,
                            tilt=tilt,
                            head_step=head_step,
                            pan_min=pan_min,
                            pan_max=pan_max,
                            tilt_min=tilt_min,
                            tilt_max=tilt_max,
                            pan_center=pan_center,
                            tilt_center=tilt_center,
                            tracker=tracker,
                            imu_reader=imu_reader,
                            yaw_sign=yaw_sign,
                            pitch_sign=pitch_sign,
                            servo_cfg=servo_cfg,
                            base_cfg=base_cfg,
                            active_spin=active_spin,
                            now=now,
                            last_m=last_m,
                            last_n=last_n,
                            port=port,
                        )
                    )
                    if want_status:
                        print(format_status(link, pan, tilt, active_spin))
                    if quit_req:
                        running = False
                        break

            cmds, api_m, api_n, api_step = CONTROL.drain()
            if api_m > last_m:
                last_m = api_m
            if api_n > last_n:
                last_n = api_n
            step = api_step if api_step > 0 else head_step
            for cmd in cmds:
                if cmd == "home_lock" or cmd.startswith("goto_imu:"):
                    if homing or (now - last_home_ts) < 1.5:
                        continue
                    homing = True
                    last_m = 0.0
                    last_n = 0.0
                    active_spin = 0
                    link.write_base_stop()
                pan, tilt, active_spin, want_status, quit_req = _apply_browser_cmd(
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
                    tracker=tracker,
                    imu_reader=imu_reader,
                    yaw_sign=yaw_sign,
                    pitch_sign=pitch_sign,
                    servo_cfg=servo_cfg,
                    base_cfg=base_cfg,
                    active_spin=active_spin,
                )
                if want_status:
                    print(format_status(link, pan, tilt, active_spin))
                if quit_req:
                    running = False
                    break
                if cmd == "home_lock" or cmd.startswith("goto_imu:"):
                    homing = False
                    last_home_ts = time.time()

            if not running:
                break

            if homing:
                want = 0
            else:
                want = desired_base_spin(last_m, last_n, now)
            active_spin = apply_base_spin(link, want, active_spin)
            spin_label = {0: "stop", -1: "left", 1: "right"}.get(active_spin, "?")

            if now - last_enc_poll >= ENCODER_POLL_SEC or active_spin != 0 or base_busy:
                enc_deg, enc_count, base_busy, cpd_live = _query_enc(link, enc_deg)
                if cpd_live > 0.05:
                    tracker.counts_per_degree = cpd_live
                last_enc_poll = now

            imu_yaw, imu_pitch, gyro, imu_ok = _read_imu(imu_reader, yaw_sign, pitch_sign)
            pan_mech = signed_pan_mech_deg(pan, servo_cfg)
            sample = tracker.update(
                encoder_deg=enc_deg,
                encoder_count=enc_count,
                imu_yaw_deg=imu_yaw,
                pan_mech_deg=pan_mech,
                gyro_dps=gyro,
                base_busy=base_busy or active_spin != 0,
                now=now,
            )
            if (
                sample is not None
                and sample.stationary
                and sample.pan_stable
                and not sample.head_only_motion
                and active_spin == 0
                and not base_busy
                and abs(sample.disagreement_deg) > 0.5
            ):
                tracker.force_snap_imu_to_encoder(
                    encoder_deg=enc_deg,
                    imu_yaw_deg=imu_yaw,
                    pan_mech_deg=pan_mech,
                )
                sample = tracker.update(
                    encoder_deg=enc_deg,
                    encoder_count=enc_count,
                    imu_yaw_deg=imu_yaw,
                    pan_mech_deg=pan_mech,
                    gyro_dps=gyro,
                    base_busy=False,
                    now=now,
                )
            _publish_state(
                sample,
                pan=pan,
                tilt=tilt,
                imu_pitch_deg=imu_pitch,
                spin_label=spin_label,
                imu_online=imu_ok,
                port=port,
                servo_cfg=servo_cfg,
            )

            if active_spin != 0 and now - status_every > 0.5 and sample is not None:
                print(
                    f"\r[yaw] enc {sample.from_home_enc_deg:+.1f}°  "
                    f"imu {sample.from_home_imu_deg:+.1f}°  "
                    f"Δ {sample.disagreement_deg:+.1f}°  "
                    f"ticks {sample.encoder_count_delta:+d}  "
                    f"POS {sample.encoder_count}  "
                    f"imu_raw {sample.imu_yaw_deg:+.1f}°   ",
                    end="",
                    flush=True,
                )
                status_every = now
            elif active_spin == 0 and status_every != 0:
                print()
                status_every = 0

            time.sleep(POLL_SEC)
    finally:
        if key_reader is not None:
            key_reader.__exit__(None, None, None)

    link.write_base_stop()


def main() -> int:
    parser = argparse.ArgumentParser(description="Yaw-from-HOME viz + robottest keys")
    parser.add_argument("--port", default="", help="Serial port e.g. /dev/ttyUSB0")
    parser.add_argument("--viz-port", type=int, default=8766, help="HTTP port (default 8766)")
    parser.add_argument("--host", default="0.0.0.0", help="HTTP bind address")
    parser.add_argument("--head-step", type=float, default=5.0)
    parser.add_argument("--no-imu", action="store_true")
    parser.add_argument(
        "--no-zero-on-start",
        action="store_true",
        help="Do not zero encoder at startup (HOME = current pose)",
    )
    args = parser.parse_args()

    cfg = _load_yaml(CONFIG_PATH)
    imu_cfg = cfg.get("imu", {}) or {}
    base_cfg = dict(cfg.get("base", {}) or {})
    prox_cfg = cfg.get("proximity", {}) or {}
    base_cfg.setdefault("home_step_deg", float(prox_cfg.get("turn_step_deg", 35.0)))
    base_cfg.setdefault("encoder_sign", float(base_cfg.get("encoder_sign", -1.0)))
    base_cfg.setdefault("spin_positive_uses_left", bool(base_cfg.get("spin_positive_uses_left", False)))
    base_cfg.setdefault("spin_stall_sec", float(base_cfg.get("spin_stall_sec", 0.35)))
    base_cfg.setdefault("home_timeout_sec", float(base_cfg.get("spin_timeout_sec", 6.0)))
    base_cfg.setdefault("home_imu_burst_sec", 0.45)
    base_cfg.setdefault("home_imu_fine_burst_sec", 0.12)
    servo_cfg = cfg.get("servo", {}) or {}
    viz_cfg = cfg.get("debug_viz", {}) or {}
    yaw_sign = float(imu_cfg.get("yaw_sign", -1.0))
    pitch_sign = float(viz_cfg.get("imu_pitch_sign", -1.0))
    zero_on_start = bool(base_cfg.get("zero_on_start", True)) and not args.no_zero_on_start

    link = ArduinoServoLink(port=args.port or None)
    if not link.connect():
        print("ERROR: Could not connect to ESP32.")
        return 1

    port_name = link._port_name or args.port or "serial"
    STATE.update(connected=True, port=port_name)

    imu_reader = None
    try:
        apply_base_calibration_to_nano(link)
        if not verify_spin_firmware(link):
            return 1

        imu_reader = None if args.no_imu else _start_imu(imu_cfg)

        server = start_server(args.host, args.viz_port)
        print(f"[YawViz] Browser: http://localhost:{args.viz_port}")
        try:
            import socket

            print(f"[YawViz] Browser: http://{socket.gethostbyname(socket.gethostname())}:{args.viz_port}")
        except OSError:
            pass

        run(
            link,
            head_step=args.head_step,
            imu_reader=imu_reader,
            imu_cfg=imu_cfg,
            yaw_sign=yaw_sign,
            pitch_sign=pitch_sign,
            servo_cfg=servo_cfg,
            base_cfg=base_cfg,
            zero_on_start=zero_on_start,
            port=port_name,
        )
        server.shutdown()
    except KeyboardInterrupt:
        print("\nInterrupted.")
    finally:
        link.write_base_stop()
        link.close(skip_home=True)
        if imu_reader is not None:
            imu_reader.stop()

    return 0


if __name__ == "__main__":
    sys.exit(main())
