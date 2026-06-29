"""YawHomeTracker: IMU-primary heading, encoder anti-drift when base still."""

import _bootstrap  # noqa: F401

from lib.yaw_home_tracker import YawHomeTracker


def _locked(**kwargs) -> YawHomeTracker:
    t = YawHomeTracker(still_hold_sec=0.1, gyro_max_dps=10.0)
    t.lock_home(
        encoder_deg=0.0,
        encoder_count=0,
        imu_yaw_deg=0.0,
        pan_mech_deg=0.0,
        now=0.0,
    )
    return t


def test_imu_tracks_while_base_ticks_change():
    t = _locked()
    s = t.update(
        encoder_deg=10.0,
        encoder_count=312,
        imu_yaw_deg=12.0,
        pan_mech_deg=0.0,
        gyro_dps=20.0,
        base_busy=True,
        now=0.2,
    )
    assert s is not None
    assert abs(s.from_home_imu_deg - 12.0) < 0.01
    assert abs(s.from_home_enc_deg - 10.0) < 0.01
    assert abs(s.disagreement_deg - 2.0) < 0.01
    assert not s.stationary


def test_snaps_imu_to_encoder_when_ticks_stable_and_close():
    t = _locked()
    t.update(
        encoder_deg=0.0,
        encoder_count=0,
        imu_yaw_deg=0.0,
        pan_mech_deg=0.0,
        gyro_dps=0.0,
        now=0.05,
    )
    s = t.update(
        encoder_deg=0.0,
        encoder_count=0,
        imu_yaw_deg=5.0,
        pan_mech_deg=0.0,
        gyro_dps=0.0,
        now=0.25,
    )
    assert s is not None
    assert s.stationary
    assert abs(s.from_home_imu_deg) < 0.01
    assert abs(s.disagreement_deg) < 0.01


def test_no_imu_snap_when_encoder_disagrees_after_spin():
    t = _locked()
    t.update(encoder_deg=0.0, encoder_count=0, imu_yaw_deg=0.0, pan_mech_deg=0.0, gyro_dps=0.0, now=0.05)
    s = t.update(
        encoder_deg=4.0,
        encoder_count=125,
        imu_yaw_deg=18.0,
        pan_mech_deg=0.0,
        gyro_dps=0.0,
        now=0.25,
    )
    assert s is not None
    assert s.stationary
    # Large post-spin mismatch: keep IMU heading, do not yank to encoder.
    assert abs(s.from_home_imu_deg - 18.0) < 0.01
    assert abs(s.disagreement_deg) > 10.0


def test_head_pan_does_not_block_base_drift_correction():
    t = _locked()
    t.update(encoder_deg=0.0, encoder_count=0, imu_yaw_deg=0.0, pan_mech_deg=0.0, gyro_dps=0.0, now=0.05)
    s = t.update(
        encoder_deg=0.0,
        encoder_count=0,
        imu_yaw_deg=8.0,
        pan_mech_deg=15.0,
        gyro_dps=0.0,
        now=0.25,
    )
    assert s is not None
    assert s.stationary
    # imu_total +8, pan +15 → imu_base_raw = -7; encoder still 0
    assert abs(s.from_home_imu_deg - (-7.0)) < 0.01


def test_spin_direction_for_imu_homing():
    from lib.base_home_drive import spin_left_toward_zero

    assert spin_left_toward_zero(12.0, positive_uses_left=False) is False
    assert spin_left_toward_zero(-12.0, positive_uses_left=False) is True


def test_note_imu_yaw_reset_keeps_encoder_from_home():
    t = _locked()
    t.update(
        encoder_deg=-90.0,
        encoder_count=-2800,
        imu_yaw_deg=50.0,
        pan_mech_deg=10.0,
        gyro_dps=5.0,
        base_busy=False,
        now=0.5,
    )
    t.note_imu_yaw_reset(encoder_deg=-90.0, imu_yaw_deg=0.0, pan_mech_deg=10.0)
    s = t.update(
        encoder_deg=-90.0,
        encoder_count=-2800,
        imu_yaw_deg=0.0,
        pan_mech_deg=10.0,
        gyro_dps=5.0,
        base_busy=False,
        now=0.6,
    )
    assert s is not None
    assert abs(s.from_home_enc_deg - (-90.0)) < 0.01
    assert abs(s.from_home_imu_deg - (-90.0)) < 0.01
    assert abs(s.disagreement_deg) < 0.01
