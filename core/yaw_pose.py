"""YawHomeTracker lock + Blackboard / TofState pose publishing."""

from __future__ import annotations

import time
from typing import TYPE_CHECKING

from lib.head_mech import signed_pan_mech_deg
from lib.yaw_home_tracker import YawHomeSample, YawHomeTracker

if TYPE_CHECKING:
    from core.blackboard import Blackboard
    from core.tof_state import TofState
    from hardware.arduino_servo import ArduinoServoLink


def query_enc(link: ArduinoServoLink, fallback: float) -> tuple[float, int, bool, float]:
    try:
        st = link.query_status()
        if st is not None:
            cpd = float(st.counts_per_degree)
            return float(st.degrees), int(st.encoder_count), bool(st.busy), cpd
    except Exception:
        pass
    return fallback, 0, False, 31.1667


def lock_home_tracker(
    tracker: YawHomeTracker,
    link: ArduinoServoLink,
    bb: Blackboard,
    servo_cfg: dict,
    *,
    zero_encoder: bool = False,
) -> None:
    """Lock HOME after IMU cal using encoder + blackboard IMU yaw."""
    if zero_encoder:
        link.write_base_stop()
        link.zero_base()
        time.sleep(0.2)
    enc, count, _, cpd = query_enc(link, 0.0)
    tracker.counts_per_degree = max(cpd, 0.05)
    state = bb.read(
        "servo_pan",
        "imu_yaw_integral_deg",
        "imu_available",
    )
    pan = float(state["servo_pan"])
    pan_mech = signed_pan_mech_deg(pan, servo_cfg)
    imu_yaw = float(state["imu_yaw_integral_deg"])
    imu_ok = bool(state["imu_available"])
    tracker.lock_home(
        encoder_deg=enc,
        encoder_count=count,
        imu_yaw_deg=imu_yaw,
        pan_mech_deg=pan_mech,
    )
    bb.write(
        base_encoder_deg=enc,
        base_encoder_synced=True,
        base_motion_busy=False,
        yaw_reference_locked=True,
    )
    print(
        f"[YawPose] HOME locked  enc={enc:+.1f}°  counts={count}  "
        f"imu={imu_yaw:+.1f}°  pan_mech={pan_mech:+.1f}°"
        + ("" if imu_ok else "  (IMU off)")
    )


def publish_tracker_pose(
    bb: Blackboard,
    tof_state: TofState | None,
    sample: YawHomeSample,
    *,
    imu_online: bool,
    base_yaw_sign: float,
    max_yaw_deg: float,
) -> None:
    """Map tracker sample to BB yaw fields and optional TofState pose."""
    pan_mech = sample.pan_mech_deg
    from_home_imu = sample.from_home_imu_deg
    from_home_enc = sample.from_home_enc_deg
    world_yaw = from_home_imu + pan_mech
    # Map uses encoder for floor localization; IMU can drift when watchdog resets integral.
    map_yaw = from_home_enc

    bb.write(
        from_home_enc_deg=sample.from_home_enc_deg,
        from_home_imu_deg=from_home_imu,
        disagreement_deg=sample.disagreement_deg,
        body_yaw_deg=from_home_imu,
        head_yaw_on_body_deg=pan_mech,
        base_world_yaw_deg=world_yaw,
        imu_yaw_rel_deg=from_home_imu,
        imu_drift_correction_deg=sample.imu_correction_deg,
        fusion_stationary=sample.stationary,
        imu_inferred_base_deg=sample.from_home_enc_deg,
    )

    if tof_state is None:
        return
    tof_state.update_pose(
        base_yaw_sign=base_yaw_sign,
        body_yaw_deg=from_home_imu,
        head_yaw_on_body_deg=pan_mech,
        encoder_yaw_deg=sample.from_home_enc_deg,
        front_offset_deg=sample.from_home_enc_deg,
        from_home_enc_deg=sample.from_home_enc_deg,
        from_home_imu_deg=from_home_imu,
        map_yaw_deg=map_yaw,
        disagreement_deg=sample.disagreement_deg,
        encoder_count_delta=sample.encoder_count_delta,
        imu_online=imu_online,
        max_yaw_deg=max_yaw_deg,
        imu_drift_correction_deg=sample.imu_correction_deg,
        imu_yaw_rel_deg=from_home_imu,
        fusion_stationary=sample.stationary,
    )


def update_tracker(
    tracker: YawHomeTracker,
    bb: Blackboard,
    *,
    encoder_deg: float,
    encoder_count: int,
    counts_per_degree: float,
    pan: float,
    servo_cfg: dict,
    base_busy: bool,
) -> YawHomeSample | None:
    state = bb.read(
        "imu_yaw_integral_deg",
        "imu_gyro_dps",
        "imu_available",
    )
    imu_yaw = float(state["imu_yaw_integral_deg"])
    gyro = float(state["imu_gyro_dps"])
    imu_ok = bool(state["imu_available"])
    pan_mech = signed_pan_mech_deg(pan, servo_cfg)
    tracker.counts_per_degree = max(counts_per_degree, 0.05)
    sample = tracker.update(
        encoder_deg=encoder_deg,
        encoder_count=encoder_count,
        imu_yaw_deg=imu_yaw,
        pan_mech_deg=pan_mech,
        gyro_dps=gyro,
        base_busy=base_busy,
    )
    if sample is not None and not base_busy and abs(sample.disagreement_deg) > 0.5:
        tracker.force_snap_imu_to_encoder(
            encoder_deg=encoder_deg,
            imu_yaw_deg=imu_yaw,
            pan_mech_deg=pan_mech,
        )
        sample = tracker.update(
            encoder_deg=encoder_deg,
            encoder_count=encoder_count,
            imu_yaw_deg=imu_yaw,
            pan_mech_deg=pan_mech,
            gyro_dps=gyro,
            base_busy=False,
        )
    return sample


def notify_imu_yaw_reset(
    tracker: YawHomeTracker,
    bb: Blackboard,
    *,
    encoder_deg: float,
    encoder_count: int,
    pan: float,
    servo_cfg: dict,
    base_busy: bool = True,
    imu_yaw_deg: float = 0.0,
) -> YawHomeSample | None:
    """Re-anchor tracker after IMU yaw integral reset (base watchdog per-spin)."""
    state = bb.read("imu_gyro_dps")
    gyro = float(state["imu_gyro_dps"])
    pan_mech = signed_pan_mech_deg(pan, servo_cfg)
    tracker.note_imu_yaw_reset(
        encoder_deg=encoder_deg,
        imu_yaw_deg=imu_yaw_deg,
        pan_mech_deg=pan_mech,
    )
    return tracker.update(
        encoder_deg=encoder_deg,
        encoder_count=encoder_count,
        imu_yaw_deg=imu_yaw_deg,
        pan_mech_deg=pan_mech,
        gyro_dps=gyro,
        base_busy=base_busy,
    )


def resnap_tracker_after_spin(
    tracker: YawHomeTracker,
    bb: Blackboard,
    *,
    encoder_deg: float,
    encoder_count: int,
    pan: float,
    servo_cfg: dict,
) -> YawHomeSample | None:
    state = bb.read("imu_yaw_integral_deg", "imu_gyro_dps")
    imu_yaw = float(state["imu_yaw_integral_deg"])
    gyro = float(state["imu_gyro_dps"])
    pan_mech = signed_pan_mech_deg(pan, servo_cfg)
    tracker.force_snap_imu_to_encoder(
        encoder_deg=encoder_deg,
        imu_yaw_deg=imu_yaw,
        pan_mech_deg=pan_mech,
    )
    return tracker.update(
        encoder_deg=encoder_deg,
        encoder_count=encoder_count,
        imu_yaw_deg=imu_yaw,
        pan_mech_deg=pan_mech,
        gyro_dps=gyro,
        base_busy=False,
    )
