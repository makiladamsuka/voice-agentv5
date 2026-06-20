"""Servo Loop — PID face tracking for head pan/tilt.

Reads vision data from Blackboard, computes PID corrections, and writes
servo_pan, servo_tilt, servo_mode, servo_target_pan, servo_target_tilt.

All config is loaded from config.yaml (servo section).
"""

import math
import random
import time
from pathlib import Path

try:
    import yaml
except ImportError:
    yaml = None

from core.blackboard import Blackboard

APP_DIR = Path(__file__).resolve().parent.parent
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


def clamp(v, lo, hi):
    return max(lo, min(hi, v))


def smooth_toward(current, target, hz, dt):
    dt = max(0.001, min(0.1, dt))
    alpha = 1.0 - math.exp(-hz * dt)
    return current + (target - current) * alpha


class PidAxis:
    def __init__(self, kp, ki, kd, integral_limit):
        self.kp = kp; self.ki = ki; self.kd = kd
        self.integral_limit = max(0.0, integral_limit)
        self.integral = 0.0
        self.prev_error = 0.0
        self.deriv_filtered = 0.0
        self.initialized = False

    def reset(self):
        self.integral = 0.0
        self.prev_error = 0.0
        self.deriv_filtered = 0.0
        self.initialized = False

    def soften(self, keep=0.35):
        self.integral *= clamp(keep, 0.0, 1.0)
        self.deriv_filtered = 0.0
        self.initialized = False

    def tick(self, error, dt):
        dt = max(0.001, dt)
        if abs(error) < 0.02:
            error = 0.0
        self.integral = clamp(self.integral + error * dt, -self.integral_limit, self.integral_limit)
        raw_d = 0.0 if not self.initialized else (error - self.prev_error) / dt
        self.deriv_filtered = self.deriv_filtered * 0.82 + raw_d * 0.18
        self.prev_error = error
        self.initialized = True
        return self.kp * error + self.ki * self.integral + self.kd * self.deriv_filtered


class TargetGlide:
    def __init__(self, smooth_hz=4.5):
        self.smooth_hz = max(0.1, smooth_hz)
        self.x = 0.0
        self.y = 0.0

    def soften(self): pass

    def tick(self, tx, ty, dt, alpha_scale=1.0):
        dt = max(0.001, min(0.05, dt))
        hz = self.smooth_hz * clamp(alpha_scale, 0.15, 1.25)
        alpha = 1.0 - math.exp(-hz * dt)
        self.x = clamp(self.x + (tx - self.x) * alpha, -1.0, 1.0)
        self.y = clamp(self.y + (ty - self.y) * alpha, -1.0, 1.0)
        return self.x, self.y


def _apply_deadzone(value, deadzone):
    if abs(value) <= deadzone:
        return 0.0
    sign = 1.0 if value > 0 else -1.0
    return sign * ((abs(value) - deadzone) / max(0.001, 1.0 - deadzone))


class ServoLoop:
    def __init__(self, bb: Blackboard, config_path: Path = DEFAULT_CONFIG_PATH):
        self.bb = bb
        cfg = _load_yaml(config_path)
        s = _cfg(cfg, "servo", default={}) or {}

        self.pan_min        = float(s.get("pan_min", 40.0))
        self.pan_max        = float(s.get("pan_max", 120.0))
        self.tilt_min       = float(s.get("tilt_min", 100.0))
        self.tilt_max       = float(s.get("tilt_max", 120.0))
        self.pan_center     = float(s.get("pan_center", (self.pan_min + self.pan_max) * 0.5))
        self.tilt_center    = float(s.get("tilt_center", (self.tilt_min + self.tilt_max) * 0.5))
        self.pan_range      = float(s.get("pan_track_range", 26.0))
        self.tilt_range     = float(s.get("tilt_track_range", 12.0))
        self.pan_sign       = float(s.get("pan_sign", 1.0))
        self.tilt_sign      = float(s.get("tilt_sign", -1.0))
        self.dz_x           = float(s.get("deadzone_x", 0.06))
        self.dz_y           = float(s.get("deadzone_y", 0.08))
        self.loop_hz        = float(s.get("loop_hz", 80.0))
        self.no_face_home   = float(s.get("no_face_home_sec", 0.8))
        self.face_alpha_x   = float(s.get("face_alpha_x", 0.20))
        self.face_alpha_y   = float(s.get("face_alpha_y", 0.14))
        self.predict_hold   = float(s.get("predict_hold_sec", 1.2))
        self.predict_edge   = float(s.get("predict_edge_norm", 0.55))
        self.predict_gain   = float(s.get("predict_gain", 0.72))
        self.lost_hold      = float(s.get("lost_search_hold_sec", 3.2))
        self.lost_vel_gain  = float(s.get("lost_search_velocity_gain", 0.45))
        self.body_alpha     = float(s.get("multi_face_track_servo_alpha", 0.34))
        self.multi_gain     = float(s.get("multi_face_track_gain", 0.62))
        self.wander_pan_amp = float(s.get("wander_pan_amp_deg", 20.0))
        self.wander_step_min= float(s.get("wander_step_min_deg", 3.0))
        self.wander_step_max= float(s.get("wander_step_max_deg", 8.0))
        self.wander_hold_min= float(s.get("wander_hold_min_sec", 0.6))
        self.wander_hold_max= float(s.get("wander_hold_max_sec", 2.2))
        self.wander_arrival = float(s.get("wander_arrival_deg", 1.4))
        self.wander_pan_hz  = float(s.get("wander_pan_smooth_hz", 4.8))
        self.wander_tilt_hz = float(s.get("wander_tilt_smooth_hz", 4.2))
        self.pan_hz         = float(s.get("pan_smooth_hz", 7.0))
        self.tilt_hz        = float(s.get("tilt_smooth_hz", 5.5))
        self.target_hz      = float(s.get("target_smooth_hz", 4.5))
        self.wander_tilt_step_max  = float(s.get("wander_tilt_step_max_deg", 2.5))
        self.wander_tilt_up_max    = float(s.get("wander_tilt_max_up_deg", 2.5))
        self.wander_tilt_down_max  = float(s.get("wander_tilt_max_down_deg", 2.5))
        self.wander_think_chance   = float(s.get("wander_thinking_hold_chance", 0.22))
        self.wander_think_min      = float(s.get("wander_thinking_hold_min_sec", 1.2))
        self.wander_think_max      = float(s.get("wander_thinking_hold_max_sec", 3.0))
        self.wander_long_chance    = float(s.get("wander_long_stare_chance", 0.06))

        pid_lim = float(s.get("pid_integral_limit", 0.40))
        self.pan_pid  = PidAxis(float(s.get("pan_pid_kp", 0.72)),  float(s.get("pan_pid_ki", 0.01)),  float(s.get("pan_pid_kd", 0.08)),  pid_lim)
        self.tilt_pid = PidAxis(float(s.get("tilt_pid_kp", 0.55)), float(s.get("tilt_pid_ki", 0.0)),  float(s.get("tilt_pid_kd", 0.06)), pid_lim)
        self.glide    = TargetGlide(self.target_hz)

        # wander state
        self._wander_goal_pan  = self.pan_center
        self._wander_goal_tilt = self.tilt_center
        self._wander_hold_until = 0.0

    def _new_wander_goal(self, current_pan, current_tilt, now):
        """Pick a new wander target near center."""
        direction = 1.0 if random.random() < 0.5 else -1.0
        step = random.uniform(self.wander_step_min, self.wander_step_max) * direction
        goal_pan = clamp(self.pan_center + step * (self.wander_pan_amp / max(self.wander_step_max, 1.0)),
                         self.pan_min, self.pan_max)
        tilt_step = random.uniform(-self.wander_tilt_step_max, self.wander_tilt_step_max)
        goal_tilt = clamp(self.tilt_center + tilt_step, self.tilt_min, self.tilt_max)

        if random.random() < self.wander_think_chance:
            hold = random.uniform(self.wander_think_min, self.wander_think_max)
        elif random.random() < self.wander_long_chance:
            hold = random.uniform(self.wander_hold_max, self.wander_hold_max * 2)
        else:
            hold = random.uniform(self.wander_hold_min, self.wander_hold_max)

        self._wander_goal_pan = goal_pan
        self._wander_goal_tilt = goal_tilt
        self._wander_hold_until = now + hold

    def run(self):
        print("ServoLoop started.")
        loop_delay = 1.0 / max(1.0, self.loop_hz)
        pan  = self.pan_center
        tilt = self.tilt_center
        servo_target_pan  = self.pan_center
        servo_target_tilt = self.tilt_center
        filtered_x = 0.0
        filtered_y = 0.0
        last_seen_face = 0.0
        last_mode = None
        last_track_kind = None
        last_face_nx = 0.0
        last_face_ny = 0.0
        last_face_vel_x = 0.0
        last_face_vel_y = 0.0
        last_norm_sample_x = 0.0
        last_norm_sample_ts = 0.0
        fast_vel_x = 0.0
        vision_fps = 10.0

        self._wander_goal_pan  = self.pan_center
        self._wander_goal_tilt = self.tilt_center

        while self.bb.read("running")["running"]:
            try:
                now = time.time()
                state = self.bb.read(
                    "face_detected", "face_norm_x", "face_norm_y",
                    "face_count", "body_detected", "track_kind",
                )
                face_seen  = state["face_detected"]
                norm_x     = state["face_norm_x"]
                norm_y     = state["face_norm_y"]
                face_count = state["face_count"]
                body_seen  = state["body_detected"]
                track_kind = state["track_kind"]

                if face_seen:
                    if track_kind != "body":
                        if last_norm_sample_ts > 0.0:
                            dt_n = max(1.0 / max(1.0, vision_fps), now - last_norm_sample_ts)
                            inst_vx = (norm_x - last_norm_sample_x) / dt_n
                            fast_vel_x += (inst_vx - fast_vel_x) * 0.35
                            last_face_vel_x = fast_vel_x
                            last_face_vel_y = (norm_y - last_face_ny) / dt_n
                        last_norm_sample_x = norm_x
                        last_norm_sample_ts = now
                    last_seen_face = now
                    last_face_nx = norm_x
                    last_face_ny = norm_y

                since = now - last_seen_face
                if face_seen:
                    mode = "track"
                elif since <= self.predict_hold:
                    mode = "predict"
                elif since <= self.lost_hold:
                    mode = "lost_search"
                else:
                    mode = "wander"

                if mode != last_mode:
                    if mode == "track":
                        self.pan_pid.soften(0.25)
                        self.tilt_pid.soften(0.25)
                        self.glide.soften()
                    elif mode == "wander":
                        self._new_wander_goal(pan, tilt, now)
                    last_mode = mode

                if mode == "track" and track_kind != last_track_kind:
                    self.pan_pid.soften(0.20)
                    self.tilt_pid.soften(0.20)
                    self.glide.soften()
                    last_track_kind = track_kind

                if mode == "track":
                    alpha = 0.30 if body_seen else (0.34 if face_count > 1 else 1.0)
                    gx, gy = self.glide.tick(norm_x, norm_y, loop_delay, alpha)
                    filtered_x += (gx - filtered_x) * self.face_alpha_x
                    filtered_y += (gy - filtered_y) * self.face_alpha_y
                    ex = _apply_deadzone(filtered_x, self.dz_x)
                    ey = _apply_deadzone(filtered_y, self.dz_y)
                    pc = clamp(self.pan_pid.tick(ex, loop_delay), -1.0, 1.0)
                    tc = clamp(self.tilt_pid.tick(ey, loop_delay), -1.0, 1.0)
                    if face_count > 1:
                        pc *= self.multi_gain
                        tc *= self.multi_gain
                    servo_target_pan  = clamp(self.pan_center  + pc * self.pan_range  * self.pan_sign,  self.pan_min,  self.pan_max)
                    servo_target_tilt = clamp(self.tilt_center + tc * self.tilt_range * self.tilt_sign, self.tilt_min, self.tilt_max)

                elif mode == "predict":
                    pred_x = clamp(last_face_nx + last_face_vel_x * since * self.lost_vel_gain, -1.0, 1.0)
                    pred_y = clamp(last_face_ny + last_face_vel_y * since * self.lost_vel_gain, -1.0, 1.0)
                    filtered_x += (pred_x - filtered_x) * self.face_alpha_x
                    filtered_y += (pred_y - filtered_y) * self.face_alpha_y
                    ex = _apply_deadzone(filtered_x * self.predict_gain, self.dz_x)
                    ey = _apply_deadzone(filtered_y * self.predict_gain, self.dz_y)
                    pc = clamp(self.pan_pid.tick(ex, loop_delay), -1.0, 1.0)
                    tc = clamp(self.tilt_pid.tick(ey, loop_delay), -0.45, 0.45)
                    servo_target_pan  = clamp(self.pan_center  + pc * self.pan_range  * self.pan_sign,  self.pan_min,  self.pan_max)
                    servo_target_tilt = clamp(self.tilt_center + tc * self.tilt_range * self.tilt_sign, self.tilt_min, self.tilt_max)

                elif mode == "lost_search":
                    norm_sign = 1.0 if last_face_nx > 0 else (-1.0 if last_face_nx < 0 else 0.0)
                    pan_bias = norm_sign * abs(last_face_vel_x) * self.lost_vel_gain * 0.5
                    servo_target_pan  = clamp(self.pan_center + pan_bias * self.pan_range * self.pan_sign, self.pan_min, self.pan_max)
                    servo_target_tilt = self.tilt_center

                else:  # wander
                    if now >= self._wander_hold_until or abs(pan - self._wander_goal_pan) < self.wander_arrival:
                        self._new_wander_goal(pan, tilt, now)
                    servo_target_pan  = self._wander_goal_pan
                    servo_target_tilt = self._wander_goal_tilt

                # Smooth toward target
                pan  = smooth_toward(pan,  servo_target_pan,  self.pan_hz,  loop_delay)
                tilt = smooth_toward(tilt, servo_target_tilt, self.tilt_hz, loop_delay)
                pan  = clamp(pan,  self.pan_min,  self.pan_max)
                tilt = clamp(tilt, self.tilt_min, self.tilt_max)

                self.bb.write(
                    servo_pan=pan,
                    servo_tilt=tilt,
                    servo_mode=mode,
                    servo_target_pan=servo_target_pan,
                    servo_target_tilt=servo_target_tilt,
                )

            except Exception as e:
                print(f"ServoLoop error: {e}")

            time.sleep(loop_delay)
