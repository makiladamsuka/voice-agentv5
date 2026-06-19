#!/usr/bin/env python3
"""Person memory bearing conversion and decay tests."""

from __future__ import annotations

import _bootstrap  # noqa: F401

from person_memory import PersonMemory, angular_error_deg


def test_detection_to_bearing_center() -> None:
    mem = PersonMemory(camera_hfov_deg=60.0)
    yaw, pitch = mem.detection_to_bearing(
        norm_x=0.0,
        norm_y=0.0,
        base_world_yaw_deg=10.0,
        pan_mech_deg=5.0,
        tilt_mech_deg=2.0,
    )
    assert yaw == 15.0
    assert pitch == 0.0


def test_detection_to_bearing_edges_and_wrap() -> None:
    mem = PersonMemory(camera_hfov_deg=60.0)
    yaw, pitch = mem.detection_to_bearing(
        norm_x=1.0,
        norm_y=-1.0,
        base_world_yaw_deg=170.0,
        pan_mech_deg=20.0,
        tilt_mech_deg=0.0,
    )
    assert yaw == -140.0
    assert pitch == 0.0


def test_merge_and_timeout() -> None:
    mem = PersonMemory(timeout_sec=2.0, merge_angle_deg=10.0, camera_hfov_deg=60.0)
    first = mem.observe(
        norm_x=0.0,
        norm_y=0.0,
        base_world_yaw_deg=0.0,
        pan_mech_deg=0.0,
        tilt_mech_deg=0.0,
        kind="face",
        now=100.0,
    )
    second = mem.observe(
        norm_x=0.1,
        norm_y=0.0,
        base_world_yaw_deg=0.0,
        pan_mech_deg=0.0,
        tilt_mech_deg=0.0,
        kind="body",
        now=101.0,
    )
    assert first.id == second.id
    assert len(mem.active(101.0)) == 1
    assert len(mem.active(104.0)) == 0


def test_best_for_current_view_prefers_nearby_fresh_memory() -> None:
    mem = PersonMemory(timeout_sec=20.0, merge_angle_deg=2.0, camera_hfov_deg=60.0)
    mem.observe(
        norm_x=0.0,
        norm_y=0.0,
        base_world_yaw_deg=0.0,
        pan_mech_deg=0.0,
        tilt_mech_deg=0.0,
        kind="face",
        now=10.0,
    )
    mem.observe(
        norm_x=0.0,
        norm_y=0.0,
        base_world_yaw_deg=45.0,
        pan_mech_deg=0.0,
        tilt_mech_deg=0.0,
        kind="face",
        now=11.0,
    )
    best = mem.best_for_current_view(current_world_yaw_deg=42.0, now=12.0)
    assert best is not None
    assert abs(angular_error_deg(best.world_yaw_deg, 45.0)) < 0.1


def test_best_for_reacquire_face_only() -> None:
    mem = PersonMemory(timeout_sec=20.0, merge_angle_deg=2.0, camera_hfov_deg=60.0)
    mem.observe(
        norm_x=0.0,
        norm_y=0.0,
        base_world_yaw_deg=30.0,
        pan_mech_deg=0.0,
        kind="body",
        now=10.0,
    )
    mem.observe(
        norm_x=0.0,
        norm_y=0.0,
        base_world_yaw_deg=-20.0,
        pan_mech_deg=0.0,
        kind="face",
        now=11.0,
    )
    best = mem.best_for_reacquire(current_world_yaw_deg=0.0, now=12.0)
    assert best is not None
    assert best.kind == "face"
    assert abs(angular_error_deg(best.world_yaw_deg, -20.0)) < 0.1

