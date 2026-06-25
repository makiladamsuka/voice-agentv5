#!/usr/bin/env python3
"""Interactive arm jogger with multi-frame animation capture and playback.

Based on test_servo_arms_safe.py. Saves multi-pose animations to arm_pose_presets.json
(``animations`` key) without removing existing single poses.

  cd voice-agentv5/tests && python test_servo_arm_safe_multy_pose_animation.py

  B     start multi-pose capture (M=mark frame, C=finish & save, Q=discard capture)
  M     mark current pose as animation frame (capture mode only)
  C     finish capture, prompt for name, save animation
  , / . cycle saved animations (plays full sequence)
  N     recall animation by name (plays full sequence)
  R     replay current animation
  0     list saved animations
  H     home  P=print  +/-=step  Q=quit
"""

from __future__ import annotations

import argparse
import dataclasses
import select
import sys
import termios
import tty
import time
from typing import Optional

import _bootstrap  # noqa: F401

from arm_pose_presets import DEFAULT_PRESETS_PATH, ArmPosePresets, normalize_pose_name
from arm_safety_envelope import DEFAULT_LIMITS_PATH, ArmSafetyEnvelope
from arduino_servo import ArduinoServoLink
from test_servo_manual import load_servo_cfg

STEP_CHOICES = (1.0, 2.0, 5.0, 10.0, 15.0)
HOLD_RELEASE_SEC = 0.25
POLL_SEC = 0.033
JOG_HZ = 30.0
DEFAULT_BLEND_SEC = 0.6

_JOG_ACTIONS = frozenset(
    {"a0_up", "a0_down", "a2_left", "a2_right", "a1_up", "a1_down", "a3_left", "a3_right"}
)

_AXIS_PAIRS = (
    ("a0_down", "a0_up", 0),
    ("a2_left", "a2_right", 2),
    ("a1_down", "a1_up", 1),
    ("a3_right", "a3_left", 3),
)
_OPPOSITE: dict[str, str] = {}
for _neg, _pos, _ in _AXIS_PAIRS:
    _OPPOSITE[_neg] = _pos
    _OPPOSITE[_pos] = _neg

ARROW_UP = "\x1b[A"
ARROW_DOWN = "\x1b[B"
ARROW_LEFT = "\x1b[D"
ARROW_RIGHT = "\x1b[C"


class RawKeyReader:
    def __init__(self) -> None:
        self._fd = sys.stdin.fileno()
        self._old: list | None = None

    def __enter__(self) -> RawKeyReader:
        if sys.stdin.isatty():
            self._old = termios.tcgetattr(self._fd)
            tty.setcbreak(self._fd)
        return self

    def __exit__(self, *args: object) -> None:
        if self._old is not None:
            termios.tcsetattr(self._fd, termios.TCSADRAIN, self._old)

    def read_key(self, timeout: float = POLL_SEC) -> str | None:
        if not sys.stdin.isatty():
            return None
        ready, _, _ = select.select([sys.stdin], [], [], timeout)
        if not ready:
            return None
        ch = sys.stdin.read(1)
        if ch != "\x1b":
            return ch
        if not select.select([sys.stdin], [], [], 0.02)[0]:
            return ch
        seq = sys.stdin.read(2)
        if len(seq) == 2 and seq[0] == "[":
            code = {"A": ARROW_UP, "B": ARROW_DOWN, "C": ARROW_RIGHT, "D": ARROW_LEFT}.get(seq[1])
            if code:
                return code
        return ch

    def drain_keys(self) -> list[str]:
        keys: list[str] = []
        while True:
            k = self.read_key(0)
            if k is None:
                break
            keys.append(k)
        return keys

    def read_line_prompt(self, prompt: str) -> str:
        if sys.stdin.isatty():
            termios.tcflush(self._fd, termios.TCIFLUSH)
        if self._old is not None:
            termios.tcsetattr(self._fd, termios.TCSADRAIN, self._old)
        try:
            return input(prompt).strip()
        finally:
            if self._old is not None:
                tty.setcbreak(self._fd)


def _map_key(key: str) -> Optional[str]:
    if key in ("w", "W", ARROW_UP):
        return "a0_up"
    if key in ("s", "S", ARROW_DOWN):
        return "a0_down"
    if key in ("a", "A", ARROW_LEFT):
        return "a2_left"
    if key in ("d", "D", ARROW_RIGHT):
        return "a2_right"
    if key in ("i", "I"):
        return "a1_down"
    if key in ("k", "K"):
        return "a1_up"
    if key in ("j", "J"):
        return "a3_right"
    if key in ("l", "L"):
        return "a3_left"
    if key in ("+", "="):
        return "step_up"
    if key in ("-", "_"):
        return "step_down"
    if key in ("h", "H"):
        return "home"
    if key in ("b", "B"):
        return "start_capture"
    if key in ("m", "M"):
        return "mark_frame"
    if key in ("c", "C"):
        return "finish_capture"
    if key in ("n", "N"):
        return "recall_animation"
    if key in (",", "<"):
        return "anim_prev"
    if key in (".", ">"):
        return "anim_next"
    if key in ("r", "R"):
        return "replay_animation"
    if key == "0":
        return "list_animations"
    if key in ("p", "P"):
        return "print_pose"
    if key in ("q", "Q", "\x03"):
        return "quit"
    return None


def _sync_arms_from_link(link: ArduinoServoLink, arms: list[float]) -> None:
    for i, attr in enumerate(("_last_a0", "_last_a1", "_last_a2", "_last_a3")):
        sent = getattr(link, attr, None)
        if sent is not None:
            arms[i] = sent


def _axis_step(
    held: dict[str, float],
    now: float,
    neg_key: str,
    pos_key: str,
    step: float,
) -> float:
    neg_ts = held.get(neg_key, 0.0)
    pos_ts = held.get(pos_key, 0.0)
    neg_active = (now - neg_ts) < HOLD_RELEASE_SEC
    pos_active = (now - pos_ts) < HOLD_RELEASE_SEC
    if neg_active and pos_active:
        return step if pos_ts >= neg_ts else -step
    if pos_active:
        return step
    if neg_active:
        return -step
    return 0.0


def _apply_held(
    arms: list[float],
    held: dict[str, float],
    now: float,
    step: float,
    envelope: ArmSafetyEnvelope,
) -> bool:
    before = tuple(arms)
    moved = False
    for neg_key, pos_key, idx in _AXIS_PAIRS:
        delta = _axis_step(held, now, neg_key, pos_key, step)
        if delta == 0.0:
            continue
        arms[idx] += delta
        moved = True
    if not moved:
        return False
    clamped = envelope.clamp_arms(*arms)
    for i, v in enumerate(clamped):
        arms[i] = v
    return before != tuple(arms)


def _safe_bounds(envelope: ArmSafetyEnvelope, arms: list[float]) -> tuple[tuple[float, float], tuple[float, float]]:
    r = envelope.sweep_range(side="right", raise_deg=arms[0])
    l = envelope.sweep_range(side="left", raise_deg=arms[1])
    return r, l


def _print_pose(arms: list[float], envelope: ArmSafetyEnvelope) -> None:
    r_safe, l_safe = _safe_bounds(envelope, arms)
    print(
        f"R raise A0={arms[0]:.1f}  sweep A2={arms[2]:.1f}  "
        f"[safe {r_safe[0]:.0f}-{r_safe[1]:.0f}]  |  "
        f"L raise A1={arms[1]:.1f}  sweep A3={arms[3]:.1f}  "
        f"[safe {l_safe[0]:.0f}-{l_safe[1]:.0f}]",
        flush=True,
    )


def _fmt_frame(frame: tuple[float, float, float, float]) -> str:
    return f"A0={frame[0]:.1f} A1={frame[1]:.1f} A2={frame[2]:.1f} A3={frame[3]:.1f}"


@dataclasses.dataclass
class PresetBlend:
    start: tuple[float, float, float, float]
    target: tuple[float, float, float, float]
    t0: float
    duration: float
    name: str
    frame_idx: int = -1


@dataclasses.dataclass
class AnimationPlayback:
    name: str
    frames: list[tuple[float, float, float, float]]
    next_frame_idx: int = 0


def _smoothstep(t: float) -> float:
    t = max(0.0, min(1.0, t))
    return t * t * (3.0 - 2.0 * t)


def _lerp_pose(
    start: tuple[float, float, float, float],
    target: tuple[float, float, float, float],
    t: float,
    envelope: ArmSafetyEnvelope,
) -> tuple[float, float, float, float]:
    e = _smoothstep(t)
    raw = tuple(start[i] + (target[i] - start[i]) * e for i in range(4))
    return envelope.clamp_arms(*raw)


def _tick_blend(
    blend: PresetBlend,
    now: float,
    arms: list[float],
    envelope: ArmSafetyEnvelope,
) -> bool:
    t = (now - blend.t0) / blend.duration if blend.duration > 0 else 1.0
    if t >= 1.0:
        arms[:] = list(blend.target)
        return True
    blended = _lerp_pose(blend.start, blend.target, t, envelope)
    arms[:] = list(blended)
    return False


HOME_BLEND_DEG_PER_SEC = 45.0


def _smooth_home_arms(
    link: ArduinoServoLink,
    arms: list[float],
    envelope: ArmSafetyEnvelope,
    homes: tuple[float, float, float, float],
    blend_sec: float,
) -> None:
    target = envelope.clamp_arms(*homes)
    start = tuple(arms)
    max_delta = max(abs(start[i] - target[i]) for i in range(4))
    if max_delta < 0.5:
        arms[:] = list(target)
        link.write_arms(*arms, force=True)
        time.sleep(0.5)
        return
    duration = max(blend_sec, max_delta / HOME_BLEND_DEG_PER_SEC, 0.5)
    interval = 1.0 / JOG_HZ
    t0 = time.time()
    print(f"homing ({duration:.1f}s)...", flush=True)
    while True:
        now = time.time()
        t = (now - t0) / duration
        if t >= 1.0:
            arms[:] = list(target)
            link.write_arms(*arms, force=True)
            break
        arms[:] = list(_lerp_pose(start, target, t, envelope))
        link.write_arms(*arms, force=True)
        time.sleep(interval)
    time.sleep(0.5)


def _print_animation_list(presets: ArmPosePresets) -> None:
    names = presets.list_animation_names()
    if not names:
        print("No saved animations.", flush=True)
        return
    print(f"Saved animations ({presets.path}):", flush=True)
    for name in names:
        frames = presets.get_animation(name)
        print(f"  {name} ({len(frames)} frames):", flush=True)
        for i, frame in enumerate(frames, start=1):
            print(f"    {i}: {_fmt_frame(frame)}", flush=True)


def main() -> int:
    try:
        envelope = ArmSafetyEnvelope.from_json(DEFAULT_LIMITS_PATH)
    except FileNotFoundError as e:
        print(e, flush=True)
        return 1

    servo = load_servo_cfg()
    parser = argparse.ArgumentParser(description="Arm jogger with multi-pose animation capture/playback")
    parser.add_argument("--port", default=str(servo.get("port") or ""))
    parser.add_argument("--baud", type=int, default=int(servo.get("baud", 115200)))
    parser.add_argument("--step", type=float, default=1.0, help="Degrees per jog tick while held")
    parser.add_argument("--limits", default=str(DEFAULT_LIMITS_PATH), help="captured_arm_limits.json path")
    parser.add_argument("--poses", default=str(DEFAULT_PRESETS_PATH), help="arm_pose_presets.json path")
    parser.add_argument(
        "--blend",
        type=float,
        default=DEFAULT_BLEND_SEC,
        help="Seconds to smooth between animation frames (0 = instant)",
    )
    parser.add_argument("--no-home", action="store_true", help="Do not move arms home on exit")
    args = parser.parse_args()

    if args.limits != str(DEFAULT_LIMITS_PATH):
        envelope = ArmSafetyEnvelope.from_json(args.limits)

    presets = ArmPosePresets.load_or_create_home(args.poses, home=envelope.homes)

    link = ArduinoServoLink(port=args.port, baud=args.baud)
    link.configure_servo_stream(send_hz=JOG_HZ, min_deg=0.02, quantum_deg=0.1)

    if not link.connect():
        print("Could not connect to ESP32.")
        return 1

    if not link.has_arm_firmware():
        banner = link.firmware_banner()
        if banner:
            for line in banner.splitlines():
                if line.startswith("FW "):
                    print(f"ESP32 reports: {line.strip()}", flush=True)
        print("ERROR: ESP32 does not report arm firmware (V -> HOME A0=...).", flush=True)
        hint = link.arm_firmware_hint()
        if hint:
            print(hint, flush=True)
        link.close(skip_home=True)
        return 1

    homes = envelope.homes
    arms = list(homes)
    step_idx = STEP_CHOICES.index(args.step) if args.step in STEP_CHOICES else 0
    held: dict[str, float] = {}
    last_jog_ts = 0.0
    last_print_ts = 0.0
    quit_requested = False
    last_clamped = False
    blend: PresetBlend | None = None
    blend_sec = max(0.0, args.blend)
    anim_names = presets.list_animation_names()
    anim_idx = -1
    homed_on_exit = False

    capture_mode = False
    capture_frames: list[tuple[float, float, float, float]] = []
    anim_playback: AnimationPlayback | None = None

    def _refresh_anim_names() -> list[str]:
        nonlocal anim_names, anim_idx
        anim_names = presets.list_animation_names()
        if anim_idx >= len(anim_names):
            anim_idx = -1
        return anim_names

    def _stop_playback() -> None:
        nonlocal blend, anim_playback
        blend = None
        anim_playback = None

    def _begin_blend_to_frame(
        playback: AnimationPlayback,
        frame_idx: int,
        *,
        label: str,
    ) -> None:
        nonlocal blend
        target = playback.frames[frame_idx]
        if blend_sec <= 0.0:
            arms[:] = list(target)
            link.write_arms(*arms, force=True)
            _sync_arms_from_link(link, arms)
            blend = None
            _on_animation_frame_done(playback, frame_idx)
            return
        blend = PresetBlend(
            start=tuple(arms),
            target=target,
            t0=time.time(),
            duration=blend_sec,
            name=label,
            frame_idx=frame_idx,
        )

    def _on_animation_frame_done(playback: AnimationPlayback, frame_idx: int) -> None:
        nonlocal anim_playback
        total = len(playback.frames)
        print(
            f"  frame {frame_idx + 1}/{total} {_fmt_frame(playback.frames[frame_idx])}",
            flush=True,
        )
        next_idx = frame_idx + 1
        playback.next_frame_idx = next_idx
        if next_idx < total:
            _begin_blend_to_frame(
                playback,
                next_idx,
                label=f"{playback.name} [{next_idx + 1}/{total}]",
            )
            return
        print(f"→ animation {playback.name!r} complete ({total} frames)", flush=True)
        _print_pose(arms, envelope)
        anim_playback = None

    def _start_animation_play(name: str) -> None:
        nonlocal anim_playback, anim_idx, blend
        held.clear()
        _stop_playback()
        frames = [envelope.clamp_arms(*f) for f in presets.get_animation(name)]
        anim_names_local = _refresh_anim_names()
        if name in anim_names_local:
            anim_idx = anim_names_local.index(name)
        anim_playback = AnimationPlayback(name=name, frames=frames, next_frame_idx=0)
        print(
            f"playing animation [{anim_idx + 1}/{len(anim_names_local)}] "
            f"{name!r} ({len(frames)} frames)",
            flush=True,
        )
        _begin_blend_to_frame(
            anim_playback,
            0,
            label=f"{name} [1/{len(frames)}]",
        )

    def _nav_animation(delta: int) -> None:
        names = _refresh_anim_names()
        if not names:
            print("No saved animations.", flush=True)
            return
        idx = anim_idx if anim_idx >= 0 else 0
        idx = (idx + delta) % len(names)
        _start_animation_play(names[idx])

    def _replay_animation() -> None:
        if anim_idx < 0 or anim_idx >= len(anim_names):
            print("No animation selected — use ,/. or N first.", flush=True)
            return
        _start_animation_play(anim_names[anim_idx])

    def _current_clamped_pose() -> tuple[float, float, float, float]:
        _sync_arms_from_link(link, arms)
        return envelope.clamp_arms(*arms)

    def _mark_capture_frame() -> None:
        frame = _current_clamped_pose()
        capture_frames.append(frame)
        print(
            f"  frame {len(capture_frames)} marked: {_fmt_frame(frame)}",
            flush=True,
        )

    def _finish_capture(keys: RawKeyReader) -> None:
        nonlocal capture_mode, capture_frames
        capture_mode = False
        if not capture_frames:
            print("capture finished — no frames marked", flush=True)
            return
        print(f"Captured {len(capture_frames)} frame(s):", flush=True)
        for i, frame in enumerate(capture_frames, start=1):
            print(f"  {i}: {_fmt_frame(frame)}", flush=True)
        raw = keys.read_line_prompt("animation name: ")
        frames_copy = list(capture_frames)
        capture_frames = []
        if not raw:
            print("save cancelled", flush=True)
            return
        try:
            key = presets.save_animation(raw, frames_copy)
            _refresh_anim_names()
            anim_idx = anim_names.index(key)
            print(f"saved animation {key!r} ({len(frames_copy)} frames)", flush=True)
        except ValueError as e:
            print(f"save failed: {e}", flush=True)

    def _discard_capture() -> None:
        nonlocal capture_mode, capture_frames
        capture_mode = False
        n = len(capture_frames)
        capture_frames = []
        print(f"capture discarded ({n} frame(s) cleared)", flush=True)

    def _exit_home() -> None:
        nonlocal homed_on_exit
        if args.no_home:
            return
        _stop_playback()
        held.clear()
        _sync_arms_from_link(link, arms)
        _smooth_home_arms(link, arms, envelope, homes, blend_sec)
        _print_pose(arms, envelope)
        homed_on_exit = True

    print(
        "Multi-pose animation jogger — sweep clamped to raise-dependent limits\n"
        "Right WASD: W/S=A0 raise, A/D=A2 sweep  |  Left IJKL: K=raise I=lower, J=in L=out (A3)\n"
        "B=capture  M=mark frame  C=finish & save  Q=discard capture (during capture)\n"
        "N=recall  ,/.=prev/next animation  R=replay  0=list  H=home  P=print  +/-=step  Q=quit"
    )
    link.write_arms(*envelope.clamp_arms(*arms), force=True)
    _sync_arms_from_link(link, arms)
    _print_pose(arms, envelope)

    jog_interval = 1.0 / JOG_HZ

    try:
        with RawKeyReader() as keys:
            while not quit_requested:
                now = time.time()
                for key in keys.drain_keys():
                    action = _map_key(key)
                    if action is None:
                        continue
                    if action == "quit":
                        if capture_mode:
                            _discard_capture()
                            continue
                        quit_requested = True
                        break
                    if capture_mode and action not in (
                        "mark_frame",
                        "finish_capture",
                        "quit",
                        "print_pose",
                        "step_up",
                        "step_down",
                    ) and action not in _JOG_ACTIONS:
                        if action == "start_capture":
                            print("already in capture mode (M=mark, C=finish, Q=discard)", flush=True)
                        continue
                    if action == "step_up":
                        step_idx = min(step_idx + 1, len(STEP_CHOICES) - 1)
                        print(f"step={STEP_CHOICES[step_idx]:.1f}", flush=True)
                        continue
                    if action == "step_down":
                        step_idx = max(step_idx - 1, 0)
                        print(f"step={STEP_CHOICES[step_idx]:.1f}", flush=True)
                        continue
                    if action == "print_pose":
                        _print_pose(arms, envelope)
                        continue
                    if action in ("anim_prev", "anim_next"):
                        _nav_animation(-1 if action == "anim_prev" else 1)
                        continue
                    if action == "replay_animation":
                        _replay_animation()
                        continue
                    if action == "home":
                        _stop_playback()
                        arms = list(homes)
                        arms = list(envelope.clamp_arms(*arms))
                        link.write_arms(*arms, force=True)
                        _sync_arms_from_link(link, arms)
                        anim_idx = -1
                        _print_pose(arms, envelope)
                        continue
                    if action == "list_animations":
                        _print_animation_list(presets)
                        continue
                    if action == "start_capture":
                        _stop_playback()
                        held.clear()
                        capture_mode = True
                        capture_frames = []
                        print(
                            "CAPTURE MODE — jog to each pose, M=mark frame, "
                            "C=finish & save, Q=discard",
                            flush=True,
                        )
                        continue
                    if action == "mark_frame":
                        if not capture_mode:
                            print("press B first to enter capture mode", flush=True)
                            continue
                        _mark_capture_frame()
                        continue
                    if action == "finish_capture":
                        if not capture_mode:
                            print("not in capture mode", flush=True)
                            continue
                        _finish_capture(keys)
                        continue
                    if action == "recall_animation":
                        held.clear()
                        raw = keys.read_line_prompt("animation name: ")
                        if not raw:
                            print("recall cancelled", flush=True)
                            continue
                        try:
                            key = normalize_pose_name(raw)
                            presets.get_animation(key)
                            _refresh_anim_names()
                            _start_animation_play(key)
                        except (KeyError, ValueError) as e:
                            print(e, flush=True)
                        continue
                    if action in _JOG_ACTIONS and blend is not None:
                        _stop_playback()
                        print("playback cancelled", flush=True)
                    if action in _OPPOSITE:
                        held.pop(_OPPOSITE[action], None)
                    held[action] = now

                step = STEP_CHOICES[step_idx]
                if now - last_jog_ts >= jog_interval:
                    if blend is not None:
                        done = _tick_blend(blend, now, arms, envelope)
                        link.write_arms(*arms, force=True)
                        _sync_arms_from_link(link, arms)
                        last_jog_ts = now
                        if done:
                            if anim_playback is not None:
                                _on_animation_frame_done(anim_playback, blend.frame_idx)
                            else:
                                print(f"→ {blend.name}", flush=True)
                                _print_pose(arms, envelope)
                                blend = None
                        elif now - last_print_ts > 0.25:
                            _print_pose(arms, envelope)
                            last_print_ts = now
                    elif _apply_held(arms, held, now, step, envelope):
                        before = tuple(arms)
                        link.write_arms(*arms, force=True)
                        _sync_arms_from_link(link, arms)
                        last_jog_ts = now
                        clamped = before != tuple(arms) and (
                            before[2] != arms[2] or before[3] != arms[3]
                        )
                        if clamped and not last_clamped:
                            print("  (sweep clamped to safety zone)", flush=True)
                        last_clamped = clamped
                        if now - last_print_ts > 0.25:
                            _print_pose(arms, envelope)
                            last_print_ts = now
                    else:
                        last_clamped = False

                time.sleep(POLL_SEC)
            if quit_requested and not args.no_home:
                _exit_home()
    except KeyboardInterrupt:
        print("\nCtrl+C — homing arms...", flush=True)
        _exit_home()
    finally:
        if args.no_home or homed_on_exit:
            link.close(skip_home=True)
        else:
            link.close(
                home_arm0=homes[0],
                home_arm1=homes[1],
                home_arm2=homes[2],
                home_arm3=homes[3],
            )
    print("Done.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
