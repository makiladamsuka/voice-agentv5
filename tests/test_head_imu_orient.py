"""Unit tests for head IMU mount frame and orientation (no hardware)."""

from __future__ import annotations

from lib.head_imu_mount import DEFAULT_AXIS_REMAP, HeadMount, load_head_mount
from lib.head_imu_orient import HeadImuOrient, wrap_degrees


def test_default_mount_remap():
    mount = HeadMount()
    assert mount.axis_remap == DEFAULT_AXIS_REMAP
    # sensor +Z back → forward = -Z
    fwd, left, up = mount.remap_vec((0.0, 0.0, -1.0))
    assert abs(fwd - 1.0) < 0.01
    assert abs(left) < 0.01
    # sensor +X up → head up = -X
    fwd, left, up = mount.remap_vec((1.0, 0.0, 0.0))
    assert abs(up + 1.0) < 0.01


def test_yaw_sign_flips_integration():
    mount = HeadMount(yaw_sign=-1.0)
    assert mount.signed_yaw_rate_dps(10.0) == -10.0
    mount_pos = HeadMount(yaw_sign=1.0)
    assert mount_pos.signed_yaw_rate_dps(10.0) == 10.0


def test_upright_level_pose():
    """Upright head: +X up on chip → pitch ≈ 0°, roll ≈ ±180° before level cal."""
    mount = HeadMount()
    orient = HeadImuOrient(mount=mount, device=object())
    sample = orient.update_from_raw(1.0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.01)
    assert abs(sample.pitch_deg) < 5.0
    assert abs(abs(sample.roll_deg) - 180.0) < 5.0


def test_level_calibrate_stores_offsets_without_hiding_real_tilt():
    class _FakeDev:
        def read_raw(self):
            return 1.0, 0.0, 0.0, 0.0, 0.0, 0.0

        def open(self):
            pass

        def close(self):
            pass

    orient = HeadImuOrient(mount=HeadMount(), device=_FakeDev())
    orient.open()
    orient.calibrate_level_stationary(duration_sec=0.5, max_gyro_dps=8.0, min_samples=5)
    sample = orient.update_from_raw(1.0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.01)
    assert orient._leveled is True
    assert abs(sample.pitch_deg) < 5.0
    assert abs(abs(sample.roll_deg) - 180.0) < 5.0


def test_yaw_integrates_from_gyro():
    mount = HeadMount(yaw_sign=-1.0)
    orient = HeadImuOrient(mount=mount, device=object())
    orient.update_from_raw(1.0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.01)
    # sensor +X gyro 100 dps → head up axis = -100 → yaw_sign -1 → +100 pan rate
    for _ in range(10):
        sample = orient.update_from_raw(1.0, 0.0, 0.0, 100.0, 0.0, 0.0, 0.1)
    assert sample.yaw_deg > 50.0
    assert sample.delta_pan_deg == sample.delta_yaw_deg


def test_zero_reference_clears_deltas():
    orient = HeadImuOrient(mount=HeadMount(), device=object())
    orient.update_from_raw(1.0, 0.0, 0.0, 100.0, 0.0, 0.0, 0.1)
    orient.zero_reference()
    sample = orient.update_from_raw(1.0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.01)
    assert abs(sample.delta_pan_deg) < 0.5
    assert abs(sample.delta_tilt_deg) < 0.5


def test_reset_yaw():
    orient = HeadImuOrient(mount=HeadMount(), device=object())
    orient.update_from_raw(1.0, 0.0, 0.0, 100.0, 0.0, 0.0, 0.1)
    orient.reset_yaw()
    sample = orient.update_from_raw(1.0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.01)
    assert abs(sample.yaw_deg) < 0.5
    assert abs(sample.pan_deg) < 0.5


def test_wrap_degrees():
    assert abs(wrap_degrees(370.0) - 10.0) < 0.01
    assert abs(wrap_degrees(-370.0) + 10.0) < 0.01


def test_load_head_mount_from_config():
    from pathlib import Path

    cfg = Path(__file__).resolve().parents[1] / "config.yaml"
    mount = load_head_mount(cfg)
    assert mount.axis_remap == (-3, 2, -1)
    assert mount.yaw_sign == -1.0
