#!/usr/bin/env python3
"""Offline tests for Botango AnimationCommands.json loading and playback sampling."""

from __future__ import annotations

import time

import _bootstrap  # noqa: F401

from arm_safety_envelope import ArmSafetyEnvelope, DEFAULT_LIMITS_PATH
from lib.animation_player import AnimationPlayer
from lib.botango_loader import find_animation_commands_json, load_botango_commands_file
from lib.botango_playback import (
    apply_animation_samples,
    clamp_pose,
    load_playback_limits,
)


def test_botango_export_loads_four_clips() -> None:
    src = find_animation_commands_json()
    clips = load_botango_commands_file(src)
    ids = {c.clip_id for c in clips}
    assert len(clips) == 4, f"expected 4 clips, got {len(clips)}: {ids}"
    for expected in (
        "Left_hand_bye",
        "Right_hand_bye",
        "Display_showing",
        "location_showing",
    ):
        assert expected in ids, f"missing clip {expected}"


def test_head_and_arm_tracks_present() -> None:
    src = find_animation_commands_json()
    clips = load_botango_commands_file(src)
    display = next(c for c in clips if c.clip_id == "Display_showing")
    servos = {tr.servo for tr in display.tracks}
    assert "head_pan" in servos
    assert "head_tilt" in servos
    assert any(s.startswith("arm_") for s in servos)


def test_player_samples_active_clip() -> None:
    src = find_animation_commands_json()
    player = AnimationPlayer()
    for clip in load_botango_commands_file(src):
        player.register_clip(clip)

    t0 = 1000.0
    ok = player.play("Display_showing", loop=False, now=t0)
    assert ok

    sample = player.sample(now=t0 + 0.5)
    assert sample, "expected non-empty sample mid-clip"

    after_end = player.sample(now=t0 + 30.0)
    assert after_end == {}, "non-loop clip should end"


def test_arm_envelope_clamps_animation_pose() -> None:
    envelope = ArmSafetyEnvelope.from_json(DEFAULT_LIMITS_PATH)
    limits = load_playback_limits()
    arm_home = dict(zip(("arm_0", "arm_1", "arm_2", "arm_3"), envelope.homes))

    src = find_animation_commands_json()
    player = AnimationPlayer()
    for clip in load_botango_commands_file(src):
        player.register_clip(clip)

    t0 = 2000.0
    player.play("Left_hand_bye", loop=False, now=t0)
    samples = player.sample(now=t0 + 1.0)
    assert samples

    pan, tilt, arms = apply_animation_samples(
        samples,
        pan_home=limits.pan_home,
        tilt_home=limits.tilt_home,
        arm_home=arm_home,
        pan=limits.pan_home,
        tilt=limits.tilt_home,
        arms=dict(arm_home),
    )
    pan, tilt, a0, a1, a2, a3 = clamp_pose(
        pan, tilt, arms, limits=limits, envelope=envelope
    )
    clamped = envelope.clamp_arms(a0, a1, a2, a3)
    assert (a0, a1, a2, a3) == clamped


def test_clip_durations_reasonable() -> None:
    src = find_animation_commands_json()
    for clip in load_botango_commands_file(src):
        assert clip.duration_ms > 500.0, f"{clip.clip_id} too short"
        assert clip.duration_ms < 30_000.0, f"{clip.clip_id} too long"


if __name__ == "__main__":
    test_botango_export_loads_four_clips()
    test_head_and_arm_tracks_present()
    test_player_samples_active_clip()
    test_arm_envelope_clamps_animation_pose()
    test_clip_durations_reasonable()
    print("botango animation playback tests passed")
