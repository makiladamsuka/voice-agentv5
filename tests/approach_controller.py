"""Proximity-only base turns for the approach.py test harness."""

from __future__ import annotations

import math
import time
from pathlib import Path
from typing import TYPE_CHECKING, Optional

try:
    import yaml
except ImportError:
    yaml = None

from core.blackboard import Blackboard
from hardware.arduino_servo import (
    _PROX_CLEAR_RE,
    _PROX_DEPART_RE,
    _PROX_EVENT_RE,
    _ZONE_RE,
)
from base_spin_motion import expected_encoder_delta
from lib.head_mech import signed_pan_mech_deg

try:
    from tof_viz_server import STATE as TOF_STATE
except ImportError:
    TOF_STATE = None

if TYPE_CHECKING:
    from hardware.arduino_servo import ArduinoServoLink

APP_DIR = Path(__file__).resolve().parents[1]
DEFAULT_CONFIG_PATH = APP_DIR / "config.yaml"


def _load_yaml(path: Path) -> dict:
    if yaml is None or not path.exists():
        return {}
    with open(path, encoding="utf-8") as f:
        return yaml.safe_load(f) or {}


def _cfg(data, *keys, default=None):
    cur = data
    for key in keys:
        if not isinstance(cur, dict) or key not in cur:
            return default
        cur = cur[key]
    return cur


class ApproachController:
    """Zone-step base turns on PROX approach events (wander-only subset)."""

    def __init__(
        self,
        bb: Blackboard,
        link: ArduinoServoLink,
        *,
        config_path: Path = DEFAULT_CONFIG_PATH,
    ) -> None:
        self.bb = bb
        self._link = link
        cfg = _load_yaml(config_path)
        s = _cfg(cfg, "servo", default={}) or {}
        b = _cfg(cfg, "base", default={}) or {}
        prox = _cfg(cfg, "proximity", default={}) or {}

        self._servo_cfg = s
        self.pan_center = float(s.get("pan_center", 100.0))
        self.tilt_center = float(s.get("tilt_center", 110.0))
        self.base_sign = float(b.get("sign", 1.0))
        self.max_yaw_deg = float(b.get("max_yaw_deg", 120.0))
        self.spin_tolerance_deg = float(b.get("spin_stop_tolerance_deg", 1.5))
        self.spin_timeout_sec = float(b.get("spin_timeout_sec", 12.0))
        self.spin_stall_sec = float(b.get("spin_stall_sec", 0.35))
        self.spin_positive_uses_left = bool(b.get("spin_positive_uses_left", False))
        self.encoder_sign = float(b.get("encoder_sign", 1.0))

        self.prox_enabled = bool(prox.get("enabled", True))
        self.prox_lockout_sec = float(prox.get("post_turn_lockout_sec", 2.0))
        self.prox_min_confidence = int(prox.get("min_confidence", 3))
        self.prox_max_turns = int(prox.get("max_turns_per_window", 2))
        self.prox_window_sec = float(prox.get("turn_window_sec", 30.0))
        self.prox_turn_step = float(prox.get("turn_step_deg", 35.0))
        self.prox_cooldown_sec = float(prox.get("cooldown_sec", 5.0))
        self.prox_post_motion_blanking_sec = float(
            prox.get("post_motion_blanking_sec", 1.5)
        )
        self._prox_swap_lr = bool(prox.get("swap_left_right", False))
        self.tof_turn_enabled = bool(prox.get("tof_turn_enabled", True))
        self.tof_min_bearing_deg = float(prox.get("tof_min_bearing_deg", 12.0))
        self.tof_min_dist_mm = int(prox.get("tof_min_dist_mm", 80))
        self.tof_max_dist_mm = int(prox.get("tof_max_dist_mm", 1600))
        self.tof_confirm_ticks = int(prox.get("tof_confirm_ticks", 3))
        self.tof_merge_radius_mm = float(prox.get("tof_merge_radius_mm", 400.0))
        self.tof_aim_tolerance_deg = float(prox.get("tof_aim_tolerance_deg", 5.0))
        self.tof_align_cooldown_sec = float(prox.get("tof_align_cooldown_sec", 1.0))
        self.tof_target_lock_sec = float(prox.get("tof_target_lock_sec", 2.5))
        self.clear_return_enabled = bool(prox.get("clear_return_enabled", True))
        self.clear_return_sec = float(prox.get("clear_return_sec", 2.5))
        self.clear_return_tolerance_deg = float(
            prox.get("clear_return_tolerance_deg", 6.0)
        )

        self._last_prox_ts = 0.0
        self._prox_turn_timestamps: list[float] = []
        self._last_base_motion_done_ts = 0.0
        self._last_prox_reaction_approach_ts = 0.0
        self._locked_track_id: int | None = None
        self._lock_track_until = 0.0
        self._locked_x_mm = 0.0
        self._locked_z_mm = 0.0
        self._bearing_stable_ticks = 0
        self._last_target_bearing = 0.0
        self._last_tof_reaction_ts = 0.0
        self._clear_since: float | None = None
        self._encoder_poll_hz = 2.0
        self._last_encoder_poll_ts = 0.0
        self._last_busy_check_ts = 0.0
        self.loop_hz = 20.0

        self.bb.write(servo_pan=self.pan_center, servo_tilt=self.tilt_center)
        self._hold_head_home()

    def _map_prox_zone(self, zone: str) -> str:
        if self._prox_swap_lr and zone in ("L", "R"):
            return "R" if zone == "L" else "L"
        return zone

    def handle_prox_line(self, line: str) -> None:
        if self.bb.read("base_motion_busy")["base_motion_busy"]:
            return

        m = _PROX_EVENT_RE.match(line)
        if m:
            self.bb.write(
                prox_approach_zone=self._map_prox_zone(m.group(1)),
                prox_approach_velocity=float(m.group(2)),
                prox_approach_distance=int(m.group(3)),
                prox_approach_confidence=int(m.group(4)),
                prox_approach_active=True,
                prox_approach_ts=time.time(),
            )
            return

        m = _PROX_DEPART_RE.match(line)
        if m:
            self.bb.write(
                prox_depart_zone=self._map_prox_zone(m.group(1)),
                prox_depart_active=True,
                prox_depart_ts=time.time(),
            )
            return

        if _PROX_CLEAR_RE.match(line):
            self.bb.write(
                prox_approach_active=False,
                prox_approach_zone="",
                prox_approach_confidence=0,
                prox_depart_active=False,
                prox_depart_zone="",
            )
            return

        m = _ZONE_RE.match(line)
        if m:
            zl = m.group(1) == "1"
            zc = m.group(2) == "1"
            zr = m.group(3) == "1"
            if self._prox_swap_lr:
                zl, zr = zr, zl
            self.bb.write(
                prox_zone_left=zl,
                prox_zone_center=zc,
                prox_zone_right=zr,
                prox_zone_count=int(zl) + int(zc) + int(zr),
            )

    def _pan_mech(self, pan_cmd: float) -> float:
        return signed_pan_mech_deg(pan_cmd, self._servo_cfg)

    def _hold_head_home(self) -> None:
        """Keep neck at config home — no counter-rotation during base turns."""
        self.bb.write(servo_pan=self.pan_center, servo_tilt=self.tilt_center)
        if self._link is not None and self._link.connected:
            self._link.write_angles(self.pan_center, self.tilt_center)

    def _publish_encoder(self, enc: float, pan: float, busy: bool) -> None:
        writes: dict = {
            "base_motion_busy": busy,
            "base_encoder_deg": enc,
            "base_encoder_synced": True,
        }
        if not self.bb.read("imu_available")["imu_available"]:
            pan_mech = self._pan_mech(pan)
            writes["base_world_yaw_deg"] = enc + pan_mech
            writes["body_yaw_deg"] = enc
            writes["head_yaw_on_body_deg"] = pan_mech
        self.bb.write(**writes)

    def _sync_encoder(self, pan: float) -> None:
        if self._link is None or not self._link.connected:
            return
        try:
            st = self._link.query_status()
            if st is not None:
                self._publish_encoder(st.degrees, pan, st.busy)
        except Exception:
            pass

    def _clip_step(self, step: float, encoder_deg: float) -> float:
        """Keep commanded spin within ±max_yaw_deg given encoder_sign convention."""
        if abs(step) < 0.05:
            return 0.0
        enc_delta = expected_encoder_delta(step, self.encoder_sign)
        projected = encoder_deg + enc_delta
        if abs(projected) <= self.max_yaw_deg:
            return step
        clamped = max(-self.max_yaw_deg, min(self.max_yaw_deg, projected))
        allowed_delta = clamped - encoder_deg
        sign = 1.0 if self.encoder_sign >= 0.0 else -1.0
        clipped = allowed_delta / sign
        if abs(clipped) < 0.5:
            return 0.0
        return clipped

    def _step_toward_bearing(self, bearing_deg: float, encoder_deg: float) -> Optional[float]:
        """Rotate base so forward (+Z) aligns with target bearing in body frame."""
        if abs(bearing_deg) < self.tof_aim_tolerance_deg:
            return None
        mag = min(abs(bearing_deg), self.prox_turn_step)
        step = math.copysign(mag, bearing_deg)
        return self._clip_step(step, encoder_deg)

    def _approach_gates_ok(
        self, now: float, state: dict, *, aligning: bool = False
    ) -> bool:
        if now < state.get("prox_post_turn_lockout_ts", 0.0):
            return False
        if (now - self._last_base_motion_done_ts) < self.prox_post_motion_blanking_sec:
            return False
        cooldown = self.tof_align_cooldown_sec if aligning else self.prox_cooldown_sec
        if (now - self._last_prox_ts) < cooldown:
            return False
        recent = [t for t in self._prox_turn_timestamps if now - t < self.prox_window_sec]
        return len(recent) < self.prox_max_turns

    def _live_tof_snapshot(self) -> dict:
        if TOF_STATE is None:
            return {}
        snap = TOF_STATE.snapshot()
        age = time.time() - float(snap.get("last_ts", 0.0) or 0.0)
        if age > 1.5:
            return {}
        return snap

    def _pick_target_track(self, snap: dict, now: float) -> dict | None:
        tracks = snap.get("tracks") or []
        if not tracks:
            primary = snap.get("primary_target")
            return primary if isinstance(primary, dict) else None

        if self._locked_track_id is not None and now < self._lock_track_until:
            for track in tracks:
                if track.get("id") == self._locked_track_id:
                    return track
            return min(
                tracks,
                key=lambda t: math.hypot(
                    float(t["x_mm"]) - self._locked_x_mm,
                    float(t["z_mm"]) - self._locked_z_mm,
                ),
            )

        primary = snap.get("primary_target")
        if isinstance(primary, dict):
            chosen = primary
        else:
            chosen = max(
                tracks,
                key=lambda t: (
                    {"human": 3, "uncertain": 2, "obstacle": 1}.get(t.get("kind", ""), 0),
                    float(t.get("confidence", 0)),
                ),
            )

        tid = chosen.get("id")
        if tid is not None:
            self._locked_track_id = int(tid)
            self._locked_x_mm = float(chosen.get("x_mm", 0.0))
            self._locked_z_mm = float(chosen.get("z_mm", 0.0))
            self._lock_track_until = now + self.tof_target_lock_sec
        return chosen

    def _target_bearing(self, target: dict) -> float:
        if "bearing_deg" in target:
            return float(target["bearing_deg"])
        return math.degrees(
            math.atan2(float(target["x_mm"]), float(target["z_mm"]))
        )

    def _step_to_front(self, body_yaw_deg: float, encoder_deg: float) -> Optional[float]:
        """Rotate base back to startup forward (0° body yaw)."""
        if abs(body_yaw_deg) < self.clear_return_tolerance_deg:
            return None
        mag = min(abs(body_yaw_deg), self.prox_turn_step)
        step = math.copysign(mag, -body_yaw_deg)
        return self._clip_step(step, encoder_deg)

    def _scene_clear(self, snap: dict) -> bool:
        """True when no person/obstacle in the trusted ToF range."""
        if not snap:
            return True
        tracks = snap.get("tracks") or []
        if not tracks:
            return True
        for track in tracks:
            dist = int(track.get("dist_mm", 0))
            if self.tof_min_dist_mm <= dist <= self.tof_max_dist_mm:
                return False
        return True

    def _plan_return_front_step(
        self, now: float, state: dict, body_yaw: float
    ) -> Optional[float]:
        if not self.clear_return_enabled:
            return None
        snap = self._live_tof_snapshot()
        if not self._scene_clear(snap):
            self._clear_since = None
            return None

        if abs(body_yaw) < self.clear_return_tolerance_deg:
            self._clear_since = None
            self._locked_track_id = None
            return None

        if self._clear_since is None:
            self._clear_since = now
            return None
        if (now - self._clear_since) < self.clear_return_sec:
            return None
        if not self._approach_gates_ok(now, state, aligning=True):
            return None

        enc = float(state.get("base_encoder_deg", 0.0))
        step = self._step_to_front(body_yaw, enc)
        if step is None or abs(step) < 0.5:
            return None

        self._locked_track_id = None
        self._record_approach_turn(now)
        return step

    def _body_yaw_deg(self, state: dict) -> float:
        imu = self.bb.read("imu_available", "body_yaw_deg")
        enc = float(state.get("base_encoder_deg", 0.0))
        if imu["imu_available"]:
            body = float(imu["body_yaw_deg"])
            # Head held at home during approach — encoder is forward offset.
            if abs(body) < 2.0 and abs(enc) > abs(body) + 3.0:
                return enc
            return body
        return enc

    def _plan_tof_step(self, now: float, state: dict) -> Optional[float]:
        if not self.tof_turn_enabled:
            return None
        snap = self._live_tof_snapshot()
        if snap and not self._scene_clear(snap):
            self._clear_since = None
        if not snap:
            self._bearing_stable_ticks = 0
            return None

        target = self._pick_target_track(snap, now)
        hits = snap.get("hits") or []
        if not target or not hits:
            self._bearing_stable_ticks = 0
            return None

        dist = int(target.get("dist_mm", 0))
        if dist < self.tof_min_dist_mm or dist > self.tof_max_dist_mm:
            return None

        bearing_deg = self._target_bearing(target)
        if abs(bearing_deg - self._last_target_bearing) < 4.0:
            self._bearing_stable_ticks += 1
        else:
            self._bearing_stable_ticks = 1
        self._last_target_bearing = bearing_deg

        if self._bearing_stable_ticks < self.tof_confirm_ticks:
            return None

        aligning = abs(bearing_deg) > self.tof_aim_tolerance_deg
        if not self._approach_gates_ok(now, state, aligning=aligning):
            return None

        enc = float(state.get("base_encoder_deg", 0.0))
        step = self._step_toward_bearing(bearing_deg, enc)
        if step is None or abs(step) < 0.5:
            return None

        self._last_tof_reaction_ts = now
        self._record_approach_turn(now)
        self._bearing_stable_ticks = 0
        return step

    def _record_approach_turn(self, now: float, approach_ts: float = 0.0) -> None:
        self._last_prox_ts = now
        recent = [t for t in self._prox_turn_timestamps if now - t < self.prox_window_sec]
        self._prox_turn_timestamps = recent + [now]
        if approach_ts > 0.0:
            self._last_prox_reaction_approach_ts = approach_ts
        lock = self.prox_lockout_sec
        if self._last_target_bearing and abs(self._last_target_bearing) > self.tof_aim_tolerance_deg:
            lock = min(lock, self.tof_align_cooldown_sec)
        self.bb.write(prox_post_turn_lockout_ts=now + lock)

    def _plan_proximity_step(self, now: float, state: dict) -> Optional[float]:
        if not self.prox_enabled or not state.get("prox_approach_active", False):
            return None
        approach_ts = float(state.get("prox_approach_ts", 0.0) or 0.0)
        if approach_ts > 0.0 and approach_ts == self._last_prox_reaction_approach_ts:
            return None
        if not self._approach_gates_ok(now, state):
            return None
        if state.get("prox_approach_confidence", 0) < self.prox_min_confidence:
            return None

        zone = state.get("prox_approach_zone", "")
        enc = float(state.get("base_encoder_deg", 0.0))

        snap = self._live_tof_snapshot()
        target = self._pick_target_track(snap, now) if snap else None
        if target:
            bearing_deg = self._target_bearing(target)
            aligning = abs(bearing_deg) > self.tof_aim_tolerance_deg
            if not self._approach_gates_ok(now, state, aligning=aligning):
                return None
            step = self._step_toward_bearing(bearing_deg, enc)
        elif zone == "L":
            step = self._step_toward_bearing(-self.prox_turn_step, enc)
        elif zone == "R":
            step = self._step_toward_bearing(self.prox_turn_step, enc)
        else:
            return None

        if step is None or abs(step) < 0.5:
            return None

        self._record_approach_turn(now, approach_ts)
        return step

    def _execute_base_step(self, step: float, pan: float, tilt: float, now: float) -> None:
        if self._link is None or not self._link.connected:
            return
        from base_spin_motion import write_base_step_spin

        try:
            self._hold_head_home()
            self._link.mute_tof()
            self.bb.write(base_motion_busy=True)
            ok, moved_deg, stop_reason = write_base_step_spin(
                self._link,
                step,
                tolerance_deg=self.spin_tolerance_deg,
                timeout_sec=self.spin_timeout_sec,
                positive_uses_left=self.spin_positive_uses_left,
                encoder_sign=self.encoder_sign,
                stall_sec=self.spin_stall_sec,
            )
            self._link.unmute_tof()
            time.sleep(0.5)
            self._hold_head_home()
            pan = self.pan_center
            tilt = self.tilt_center
            st = self._link.query_status()
            if st is not None:
                self._publish_encoder(st.degrees, pan, False)
                self._last_busy_check_ts = now
                tag = "OK" if ok else "FAIL"
                print(
                    f"[Approach] Base spin {step:+.1f}° {tag} "
                    f"moved={moved_deg:+.1f}° enc={st.degrees:+.1f}° ({stop_reason})"
                )
                self.bb.write(
                    base_fusion_resync_request=True,
                    imu_drift_reset_request=True,
                    base_last_spin_moved_deg=moved_deg,
                    base_last_spin_reason=stop_reason,
                )
            else:
                self.bb.write(base_motion_busy=False)
            self._last_base_motion_done_ts = time.time()
        except Exception as exc:
            print(f"[Approach] base step failed: {exc}")
            self.bb.write(base_motion_busy=False)

    def tick(self, now: float | None = None) -> None:
        if now is None:
            now = time.time()
        if self._link is not None:
            self._link._poll_prox_lines()

        pan = self.pan_center
        tilt = self.tilt_center
        state = self.bb.read(
            "base_motion_busy",
            "base_encoder_deg",
            "prox_approach_active",
            "prox_approach_zone",
            "prox_approach_confidence",
            "prox_approach_ts",
            "prox_post_turn_lockout_ts",
        )
        body_yaw = self._body_yaw_deg(state)

        if state["base_motion_busy"]:
            if (now - self._last_busy_check_ts) > 0.2:
                self._last_busy_check_ts = now
                if self._link is not None:
                    try:
                        st = self._link.query_status()
                        if st is not None:
                            self._publish_encoder(st.degrees, pan, st.busy)
                            if not st.busy:
                                self._last_base_motion_done_ts = now
                    except Exception:
                        self.bb.write(base_motion_busy=False)
            return

        if (now - self._last_encoder_poll_ts) > (1.0 / self._encoder_poll_hz):
            self._last_encoder_poll_ts = now
            self._sync_encoder(pan)

        step = self._plan_tof_step(now, state)
        source = "TOF"
        if step is None:
            step = self._plan_proximity_step(now, state)
            source = "PROX"
        if step is None:
            step = self._plan_return_front_step(now, state, body_yaw)
            source = "HOME"
        if step is not None:
            snap = self._live_tof_snapshot()
            target = self._pick_target_track(snap, now) if snap else None
            if source == "HOME":
                print(
                    f"[Approach] HOME front offset {body_yaw:+.0f}° "
                    f"→ step {step:+.1f}°"
                )
            elif target:
                tid = target.get("id", "?")
                ntracks = len(snap.get("tracks") or [])
                bearing = self._target_bearing(target)
                print(
                    f"[Approach] {source} track {tid}/{ntracks} "
                    f"bearing {bearing:+.0f}° → step {step:+.1f}°"
                )
            else:
                zone = state.get("prox_approach_zone", "?")
                print(f"[Approach] {source} {zone} → base step {step:+.1f}°")
            self._execute_base_step(step, pan, tilt, now)

    def run(self) -> None:
        print("[Approach] Controller running (ToF bearing + PROX turns).")
        delay = 1.0 / max(1.0, self.loop_hz)
        while self.bb.read("running")["running"]:
            self.tick()
            time.sleep(delay)
        print("[Approach] Controller stopped.")
