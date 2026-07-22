"""Shared Botango animation playback helpers for voice-agentv5 tests."""

from __future__ import annotations

import time
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING

from lib.animation_player import AnimationPlayer, TrackSample
from lib.botango_loader import (
    HEAD_PAN_HOME_DEG,
    HEAD_TILT_HOME_DEG,
    load_arm_neutrals_from_json,
    load_botango_commands_file,
)

if TYPE_CHECKING:
    from arm_safety_envelope import ArmSafetyEnvelope
    from arduino_servo import ArduinoServoLink

DEFAULT_LOOP_HZ = 30.0
PAN_SMOOTH = 0.35
TILT_SMOOTH = 0.35
ARM_SMOOTH = 0.45


@dataclass(frozen=True)
class PlaybackLimits:
    pan_min: float = 25.0
    pan_max: float = 150.0
    tilt_min: float = 75.0
    tilt_max: float = 150.0
    pan_home: float = HEAD_PAN_HOME_DEG
    tilt_home: float = HEAD_TILT_HOME_DEG


def clamp(v: float, lo: float, hi: float) -> float:
    return max(lo, min(hi, v))


def blend_track(base: float, sample_value: float, mode: str, weight: float) -> float:
    w = max(0.0, min(1.0, float(weight)))
    if str(mode).lower() == "override":
        return base + (sample_value - base) * w
    return base + (sample_value * w)


def load_playback_limits(config_path: Path | None = None) -> PlaybackLimits:
    path = config_path or Path(__file__).resolve().parents[1] / "config.yaml"
    limits = PlaybackLimits()
    if not path.is_file():
        return limits
    current = None
    for raw_line in path.read_text(encoding="utf-8").splitlines():
        if not raw_line.strip() or raw_line.lstrip().startswith("#"):
            continue
        if not raw_line.startswith(" ") and raw_line.rstrip().endswith(":"):
            current = raw_line.strip()[:-1]
            continue
        if current != "servo":
            continue
        key, _, value = raw_line.partition(":")
        key = key.strip()
        value = value.split("#", 1)[0].strip()
        if not value:
            continue
        try:
            num = float(value)
        except ValueError:
            continue
        if key == "pan_min":
            limits = PlaybackLimits(
                pan_min=num,
                pan_max=limits.pan_max,
                tilt_min=limits.tilt_min,
                tilt_max=limits.tilt_max,
                pan_home=limits.pan_home,
                tilt_home=limits.tilt_home,
            )
        elif key == "pan_max":
            limits = PlaybackLimits(
                pan_min=limits.pan_min,
                pan_max=num,
                tilt_min=limits.tilt_min,
                tilt_max=limits.tilt_max,
                pan_home=limits.pan_home,
                tilt_home=limits.tilt_home,
            )
        elif key == "tilt_min":
            limits = PlaybackLimits(
                pan_min=limits.pan_min,
                pan_max=limits.pan_max,
                tilt_min=num,
                tilt_max=limits.tilt_max,
                pan_home=limits.pan_home,
                tilt_home=limits.tilt_home,
            )
        elif key == "tilt_max":
            limits = PlaybackLimits(
                pan_min=limits.pan_min,
                pan_max=limits.pan_max,
                tilt_min=limits.tilt_min,
                tilt_max=num,
                pan_home=limits.pan_home,
                tilt_home=limits.tilt_home,
            )
        elif key == "pan_center":
            limits = PlaybackLimits(
                pan_min=limits.pan_min,
                pan_max=limits.pan_max,
                tilt_min=limits.tilt_min,
                tilt_max=limits.tilt_max,
                pan_home=num,
                tilt_home=limits.tilt_home,
            )
        elif key == "tilt_center":
            limits = PlaybackLimits(
                pan_min=limits.pan_min,
                pan_max=limits.pan_max,
                tilt_min=limits.tilt_min,
                tilt_max=limits.tilt_max,
                pan_home=limits.pan_home,
                tilt_home=num,
            )
    return limits


def apply_animation_samples(
    samples: dict[str, TrackSample],
    *,
    pan_home: float,
    tilt_home: float,
    arm_home: dict[str, float],
    pan: float,
    tilt: float,
    arms: dict[str, float],
) -> tuple[float, float, dict[str, float]]:
    pan_target = pan_home
    tilt_target = tilt_home
    if "head_pan" in samples:
        s = samples["head_pan"]
        pan_target = blend_track(pan_target, s.value, s.mode, s.weight)
    if "head_tilt" in samples:
        s = samples["head_tilt"]
        tilt_target = blend_track(tilt_target, s.value, s.mode, s.weight)

    arm_targets = dict(arms)
    for arm_track in ("arm_0", "arm_1", "arm_2", "arm_3"):
        if arm_track in samples:
            s = samples[arm_track]
            base = arm_home[arm_track]
            arm_targets[arm_track] = blend_track(base, s.value, s.mode, s.weight)

    pan = pan + (pan_target - pan) * PAN_SMOOTH
    tilt = tilt + (tilt_target - tilt) * TILT_SMOOTH
    for key in arm_targets:
        base = arms.get(key, arm_home[key])
        arm_targets[key] = base + (arm_targets[key] - base) * ARM_SMOOTH
    return pan, tilt, arm_targets


def clamp_pose(
    pan: float,
    tilt: float,
    arms: dict[str, float],
    *,
    limits: PlaybackLimits,
    envelope: ArmSafetyEnvelope | None = None,
) -> tuple[float, float, float, float, float, float]:
    pan = clamp(pan, limits.pan_min, limits.pan_max)
    tilt = clamp(tilt, limits.tilt_min, limits.tilt_max)
    a0 = arms.get("arm_0", 0.0)
    a1 = arms.get("arm_1", 0.0)
    a2 = arms.get("arm_2", 0.0)
    a3 = arms.get("arm_3", 0.0)
    if envelope is not None:
        a0, a1, a2, a3 = envelope.clamp_arms(a0, a1, a2, a3)
    return pan, tilt, a0, a1, a2, a3


def send_pose(
    link: ArduinoServoLink,
    pan: float,
    tilt: float,
    a0: float,
    a1: float,
    a2: float,
    a3: float,
    *,
    force: bool = False,
) -> None:
    link.write_angles_and_arms(pan, tilt, a0, a1, a2, a3, force=force)


def smooth_home_pose(
    link: ArduinoServoLink,
    *,
    pan_home: float,
    tilt_home: float,
    arm_home: dict[str, float],
    envelope: ArmSafetyEnvelope | None,
    limits: PlaybackLimits,
    hz: float = DEFAULT_LOOP_HZ,
    steps: int = 40,
) -> None:
    a0, a1, a2, a3 = (
        arm_home["arm_0"],
        arm_home["arm_1"],
        arm_home["arm_2"],
        arm_home["arm_3"],
    )
    if envelope is not None:
        a0, a1, a2, a3 = envelope.clamp_arms(a0, a1, a2, a3)
    interval = 1.0 / max(1.0, hz)
    for i in range(steps):
        force = i == steps - 1
        send_pose(link, pan_home, tilt_home, a0, a1, a2, a3, force=force)
        time.sleep(interval)


def play_clip(
    link: ArduinoServoLink | None,
    player: AnimationPlayer,
    clip_id: str,
    *,
    pan_home: float,
    tilt_home: float,
    arm_home: dict[str, float],
    limits: PlaybackLimits,
    envelope: ArmSafetyEnvelope | None = None,
    loop: bool = False,
    hz: float = DEFAULT_LOOP_HZ,
    verbose: bool = True,
) -> bool:
    if not player.play(clip_id, loop=loop):
        return False

    pan = pan_home
    tilt = tilt_home
    arms = dict(arm_home)
    interval = 1.0 / max(1.0, hz)
    start = time.time()
    if verbose:
        mode = "loop" if loop else "oneshot"
        print(f"Playing '{clip_id}' ({mode})...")

    try:
        while True:
            now = time.time()
            samples = player.sample(now)
            if not samples:
                break

            pan, tilt, arms = apply_animation_samples(
                samples,
                pan_home=pan_home,
                tilt_home=tilt_home,
                arm_home=arm_home,
                pan=pan,
                tilt=tilt,
                arms=arms,
            )
            pan, tilt, a0, a1, a2, a3 = clamp_pose(
                pan, tilt, arms, limits=limits, envelope=envelope
            )
            if verbose:
                print(
                    f"  pan={pan:.1f} tilt={tilt:.1f} "
                    f"A0={a0:.1f} A1={a1:.1f} A2={a2:.1f} A3={a3:.1f}"
                )
            if link is not None:
                send_pose(link, pan, tilt, a0, a1, a2, a3)
            time.sleep(interval)
    finally:
        player.stop()

    if verbose:
        print(f"Done ({time.time() - start:.1f}s)")
    return True


def build_player_from_json(json_path: Path) -> tuple[AnimationPlayer, list]:
    clips = load_botango_commands_file(json_path)
    player = AnimationPlayer()
    for clip in clips:
        player.register_clip(clip)
    return player, clips


def default_arm_home(json_path: Path) -> dict[str, float]:
    return load_arm_neutrals_from_json(json_path)
