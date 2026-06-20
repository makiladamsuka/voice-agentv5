"""Eye Renderer — drives the dual ST7735 TFT displays.

Reads `emotion` and `face_norm_x/y` from the Blackboard and renders
the BlockyEye animation on both SPI displays at ~24 fps.

If the displays are not available (non-Pi hardware), it runs silently
as a no-op so start_robot.py still works on a dev machine.
"""

import math
import random
import time
from pathlib import Path

from core.blackboard import Blackboard

# ── Display constants ────────────────────────────────────────────────────────
SCREEN_WIDTH  = 128
SCREEN_HEIGHT = 160
EYE_COLOR     = (255, 255, 255)
BG_COLOR      = (0, 0, 0)
EYE_SIZE      = 120
FLOOR_Y       = SCREEN_HEIGHT - 5
EYE_BOUND_MARGIN  = 8
MAX_X_OFFSET      = 30
MAX_Y_OFFSET      = 22
BLINK_SPEED_MIN   = 2.0
BLINK_SPEED_MAX   = 3.5
LOOK_SIDE_OFFSET  = 16.0
RENDER_FPS        = 24
EMOTION_CHANGE_COOLDOWN = 0.75

EMOTION_PRESETS = {
    "idle":                 {"scale_w": 1.0,  "scale_h": 1.0,  "top_lid": 0.0,  "bottom_lid": 0.0,  "lid_angle": 0.0,  "mirror_angle": True},
    "happy":                {"scale_w": 1.10, "scale_h": 0.84, "top_lid": 0.0,  "bottom_lid": 0.30, "lid_angle": -6.0, "mirror_angle": True},
    "sad":                  {"scale_w": 0.98, "scale_h": 0.96, "top_lid": 0.12, "bottom_lid": 0.0,  "lid_angle": -8.0, "mirror_angle": True, "pos": (0, 4)},
    "surprised":            {"scale_w": 0.98, "scale_h": 1.12, "top_lid": 0.0,  "bottom_lid": 0.0,  "lid_angle": 0.0,  "mirror_angle": True},
    "suspicious":           {"scale_w": 1.06, "scale_h": 0.74, "top_lid": 0.38, "bottom_lid": 0.35, "lid_angle": 0.0,  "mirror_angle": True},
    "sleepy":               {"scale_w": 1.04, "scale_h": 0.88, "top_lid": 0.56, "bottom_lid": 0.0,  "lid_angle": 0.0,  "mirror_angle": True},
    "looking_left_natural": {"scale_w": 1.02, "scale_h": 0.98, "top_lid": 0.0,  "bottom_lid": 0.05, "lid_angle": -3.0, "mirror_angle": False},
    "looking_right_natural":{"scale_w": 1.02, "scale_h": 0.98, "top_lid": 0.0,  "bottom_lid": 0.05, "lid_angle": 3.0,  "mirror_angle": False},
    "looking_left":         {"scale_w": 1.02, "scale_h": 0.98, "top_lid": 0.0,  "bottom_lid": 0.05, "lid_angle": -3.0, "mirror_angle": False},
    "looking_right":        {"scale_w": 1.02, "scale_h": 0.98, "top_lid": 0.0,  "bottom_lid": 0.05, "lid_angle": 3.0,  "mirror_angle": False},
    "excited":              {"scale_w": 1.14, "scale_h": 0.80, "top_lid": 0.0,  "bottom_lid": 0.24, "lid_angle": 0.0,  "mirror_angle": True},
    "calm":                 {"scale_w": 1.03, "scale_h": 0.90, "top_lid": 0.16, "bottom_lid": 0.12, "lid_angle": 0.0,  "mirror_angle": True},
    "curious":              {"scale_w": 1.02, "scale_h": 1.03, "top_lid": 0.0,  "bottom_lid": 0.13, "lid_angle": 4.0,  "mirror_angle": False},
    "afraid":               {"scale_w": 0.92, "scale_h": 1.12, "top_lid": 0.0,  "bottom_lid": 0.0,  "lid_angle": 0.0,  "mirror_angle": True},
    "thinking":             {"scale_w": 1.0,  "scale_h": 1.0,  "top_lid": 0.0,  "bottom_lid": 0.0,  "lid_angle": 0.0,  "mirror_angle": True, "pos": (0, -2)},
    "concentrating":        {"scale_w": 0.96, "scale_h": 0.84, "top_lid": 0.16, "bottom_lid": 0.08, "lid_angle": 0.0,  "mirror_angle": True, "pos": (0, -2)},
    "remembering":          {"scale_w": 1.04, "scale_h": 1.03, "top_lid": 0.02, "bottom_lid": 0.0,  "lid_angle": 0.0,  "mirror_angle": True, "pos": (0, -6)},
    "attentive":            {"scale_w": 1.08, "scale_h": 1.06, "top_lid": 0.0,  "bottom_lid": 0.0,  "lid_angle": 0.0,  "mirror_angle": True},
    "engaged":              {"scale_w": 1.02, "scale_h": 1.00, "top_lid": 0.04, "bottom_lid": 0.06, "lid_angle": 5.0,  "mirror_angle": True},
    "amused":               {"scale_w": 1.00, "scale_h": 0.98, "top_lid": 0.0,  "bottom_lid": 0.14, "lid_angle": 3.0,  "mirror_angle": False},
    "warm":                 {"scale_w": 1.06, "scale_h": 1.00, "top_lid": 0.0,  "bottom_lid": 0.16, "lid_angle": 2.0,  "mirror_angle": True, "pos": (0, -4)},
    "awkward":              {"scale_w": 0.96, "scale_h": 0.93, "top_lid": 0.10, "bottom_lid": 0.10, "lid_angle": 0.0,  "mirror_angle": True, "pos": (0, 8)},
    "proud":                {"scale_w": 1.06, "scale_h": 1.02, "top_lid": 0.0,  "bottom_lid": 0.0,  "lid_angle": -2.0, "mirror_angle": True, "pos": (0, -8)},
    "squint":               {"scale_w": 1.0,  "scale_h": 0.62, "top_lid": 0.42, "bottom_lid": 0.35, "lid_angle": 0.0,  "mirror_angle": True},
    "angry":                {"scale_w": 1.04, "scale_h": 0.82, "top_lid": 0.28, "bottom_lid": 0.0,  "lid_angle": 10.0, "mirror_angle": True},
    "nodding":              {"scale_w": 1.06, "scale_h": 1.00, "top_lid": 0.0,  "bottom_lid": 0.10, "lid_angle": 0.0,  "mirror_angle": True, "pos": (0, 4)},
    "apologetic":           {"scale_w": 0.98, "scale_h": 0.94, "top_lid": 0.10, "bottom_lid": 0.0,  "lid_angle": -5.0, "mirror_angle": True, "pos": (0, 3)},
    "cheerful":             {"scale_w": 1.10, "scale_h": 0.86, "top_lid": 0.0,  "bottom_lid": 0.26, "lid_angle": -4.0, "mirror_angle": True},
    "waiting":              {"scale_w": 1.02, "scale_h": 0.96, "top_lid": 0.08, "bottom_lid": 0.04, "lid_angle": 0.0,  "mirror_angle": True},
    "speaking":             {"scale_w": 1.04, "scale_h": 1.00, "top_lid": 0.0,  "bottom_lid": 0.08, "lid_angle": 2.0,  "mirror_angle": True},
    "listening":            {"scale_w": 1.06, "scale_h": 1.04, "top_lid": 0.0,  "bottom_lid": 0.0,  "lid_angle": 0.0,  "mirror_angle": True},
}
# Fallback to idle for any unknown emotion
_IDLE = EMOTION_PRESETS["idle"]


class BlockyEye:
    def __init__(self, x, y, is_left=True):
        self.base_x, self.base_y = x, y
        self.current_pos = [float(x), float(y)]
        self.target_pos  = [float(x), float(y)]
        self.vel_x = self.vel_y = 0.0
        self.base_w = self.base_h = float(EYE_SIZE)
        self.current_w = self.current_h = float(EYE_SIZE)
        self.target_w  = self.target_h  = float(EYE_SIZE)
        self.vel_w = self.vel_h = 0.0
        self.w = self.h = float(EYE_SIZE)
        self.current_rotation = self.target_rotation = 0.0
        self.rot_sensitivity = random.uniform(0.3, 0.5)
        self.rot_speed = random.uniform(0.15, 0.25)
        self.is_left = is_left
        self.blink_state = "IDLE"
        self.vy = 0
        self.blink_speed_mult = 1.0
        self.target_scale_w = self.scale_w = 1.0
        self.target_scale_h = self.scale_h = 1.0
        self.scale_w_vel = self.scale_h_vel = 0.0
        self.top_lid = self.bottom_lid = self.lid_angle = 0.0
        self.top_lid_vel = self.bottom_lid_vel = self.lid_angle_vel = 0.0
        self.target_top_lid = self.target_bottom_lid = self.target_lid_angle = 0.0
        self.current_emotion = "idle"
        self.last_emotion_change_time = 0.0
        self.pending_emotion = None
        self.pending_intensity = 1.0
        self.pending_apply_time = 0.0
        self.happy_phase = random.uniform(0.0, math.pi * 2)
        self.happy_burst_until = 0.0
        self.noise_t = random.uniform(0, 100)
        self.emotion_pos_bias_x = self.emotion_pos_bias_y = 0.0

    def start_blink(self, speed_mult=None):
        if self.blink_state == "IDLE":
            self.blink_state = "DROPPING"
            self.blink_speed_mult = speed_mult or random.uniform(BLINK_SPEED_MIN, BLINK_SPEED_MAX)
            self.vy = 40 * self.blink_speed_mult

    def set_emotion(self, name: str, intensity: float = 1.0, force: bool = False):
        preset = EMOTION_PRESETS.get(name, EMOTION_PRESETS.get("idle"))
        now = time.time()
        if (name != self.current_emotion and not force
                and (now - self.last_emotion_change_time) < EMOTION_CHANGE_COOLDOWN):
            self.pending_emotion = name
            self.pending_intensity = intensity
            self.pending_apply_time = self.last_emotion_change_time + EMOTION_CHANGE_COOLDOWN
            return
        if name == "happy" and self.current_emotion != "happy":
            self.happy_burst_until = now + 0.35
        if name != self.current_emotion:
            self.last_emotion_change_time = now
        self.pending_emotion = None
        self.current_emotion = name
        idle = _IDLE
        bias = preset.get("left_bias" if self.is_left else "right_bias", {})
        intensity = max(0.0, min(1.0, intensity))
        self.emotion_pos_bias_x = bias.get("pos_x", 0.0) * intensity
        self.emotion_pos_bias_y = bias.get("pos_y", 0.0) * intensity
        def _lerp(key): return idle[key] + (preset[key] - idle[key]) * intensity
        sw = _lerp("scale_w"); sh = _lerp("scale_h")
        tl = _lerp("top_lid"); bl = _lerp("bottom_lid"); la = _lerp("lid_angle")
        if bias:
            sw += bias.get("scale_w", 0.0) * intensity
            sh += bias.get("scale_h", 0.0) * intensity
            tl += bias.get("top_lid", 0.0) * intensity
            bl += bias.get("bottom_lid", 0.0) * intensity
            la += bias.get("lid_angle", 0.0) * intensity
        self.target_scale_w = sw; self.target_scale_h = sh
        self.target_top_lid = tl; self.target_bottom_lid = bl
        if preset.get("mirror_angle", True) and not self.is_left and abs(la) > 0:
            la = -la
        self.target_lid_angle = la

    def update(self):
        if self.pending_emotion and time.time() >= self.pending_apply_time:
            e, i = self.pending_emotion, self.pending_intensity
            self.pending_emotion = None
            self.set_emotion(e, i, force=True)
        if self.blink_state == "IDLE":
            t = time.time() + self.noise_t
            nx = math.sin(t*1.3)*0.2 + math.sin(t*0.7)*0.1
            ny = math.cos(t*1.1)*0.2 + math.cos(t*0.9)*0.1
            tx = self.target_pos[0] + nx
            ty = self.target_pos[1] + ny
            tl_t = self.target_top_lid
            bl_t = self.target_bottom_lid
            la_t = self.target_lid_angle
            if time.time() < self.happy_burst_until:
                ty -= 8.0
            if self.current_emotion == "happy":
                ht = time.time() * 6.0 + self.happy_phase
                ty -= 2.5 + math.sin(ht) * 2.0
                tx += math.sin(ht*1.7) * 1.2
            elif "looking_" in self.current_emotion:
                if "left" in self.current_emotion: tx -= LOOK_SIDE_OFFSET
                else: tx += LOOK_SIDE_OFFSET
            ep = EMOTION_PRESETS.get(self.current_emotion, _IDLE).get("pos", (0,0))
            tx += ep[0] + self.emotion_pos_bias_x
            ty += ep[1] + self.emotion_pos_bias_y
            dx = tx - self.current_pos[0]; dy = ty - self.current_pos[1]
            sy = 0.14 if dy < -1.0 else (0.38 if dy > 1.0 else 0.22)
            self.current_pos[0] += dx * 0.20; self.current_pos[1] += dy * sy
            self.vel_x = dx * 0.20; self.vel_y = dy * sy
            rel_x = self.current_pos[0] - self.base_x; rel_y = self.current_pos[1] - self.base_y
            look_rot = (rel_x*0.5 + rel_y*0.8) * self.rot_sensitivity
            if self.current_emotion == "happy":
                look_rot += math.sin(time.time()*8.0 + self.happy_phase) * 1.2
            self.current_rotation += (look_rot + self.target_rotation - self.current_rotation) * self.rot_speed
            t2 = time.time()
            bw = math.sin(t2*1.5+self.base_x)*1.5 + math.sin(t2*0.5)*1.0
            bh = math.cos(t2*1.8+self.base_y)*1.5 + math.cos(t2*0.6)*1.0
            msx = (dx*0.20)*2.5; msy = (dy*sy)*2.5
            if self.current_emotion == "surprised": msx *= 0.25; msy *= 0.25
            k = 0.30 if self.current_emotion == "surprised" else 0.12
            d = 0.52 if self.current_emotion == "surprised" else 0.7
            self.scale_w_vel = (self.scale_w_vel + (self.target_scale_w - self.scale_w)*k)*d
            self.scale_h_vel = (self.scale_h_vel + (self.target_scale_h - self.scale_h)*k)*d
            self.scale_w += self.scale_w_vel; self.scale_h += self.scale_h_vel
            self.top_lid_vel    = (self.top_lid_vel    + (tl_t - self.top_lid)*k)*d
            self.bottom_lid_vel = (self.bottom_lid_vel + (bl_t - self.bottom_lid)*k)*d
            self.lid_angle_vel  = (self.lid_angle_vel  + (la_t - self.lid_angle)*k)*d
            self.top_lid    += self.top_lid_vel
            self.bottom_lid += self.bottom_lid_vel
            self.lid_angle  += self.lid_angle_vel
            self.target_w = self.base_w*self.scale_w + bw + msx*0.5
            self.target_h = self.base_h*self.scale_h + bh - msy*0.2
        elif self.blink_state == "DROPPING":
            self.vy += 10 * self.blink_speed_mult
            self.current_pos[1] += self.vy
            self.current_w = self.base_w - 10; self.current_h = self.base_h + 20
            self.target_w = self.current_w; self.target_h = self.current_h
            if self.current_pos[1] + self.current_h//2 >= FLOOR_Y:
                self.current_pos[1] = FLOOR_Y - self.current_h//2
                self.blink_state = "SQUASHING"
        elif self.blink_state == "SQUASHING":
            self.current_h -= 65 * self.blink_speed_mult
            self.current_w += 40 * self.blink_speed_mult
            self.current_pos[1] = FLOOR_Y - self.current_h//2
            if self.current_h <= 22:
                self.current_h = 22; self.blink_state = "JUMPING"
        elif self.blink_state == "JUMPING":
            r = max(0.15, min(0.95, 0.85*self.blink_speed_mult))
            self.current_h += (self.base_h - self.current_h)*r
            self.current_w += (self.base_w - self.current_w)*r
            self.vel_x = (self.vel_x + (self.target_pos[0]-self.current_pos[0])*0.1)*0.8
            self.current_pos[0] += self.vel_x
            self.current_pos[1] += (self.target_pos[1]-self.current_pos[1])*0.8
            if abs(self.current_h-self.base_h)<5 and abs(self.current_pos[1]-self.target_pos[1])<5:
                self.current_h=self.base_h; self.current_w=self.base_w
                self.blink_state="IDLE"; self.vy=self.vel_x=self.vel_y=0
        if self.blink_state == "IDLE":
            k2=0.08; d2=0.90
            self.vel_w=(self.vel_w+(self.target_w-self.current_w)*k2)*d2; self.current_w+=self.vel_w
            self.vel_h=(self.vel_h+(self.target_h-self.current_h)*k2)*d2; self.current_h+=self.vel_h
        else:
            self.vel_w=self.vel_h=0
        self.w=self.current_w; self.h=self.current_h
        hw=max(2.0,self.w*0.5); hh=max(2.0,self.h*0.5)
        self.current_pos[0]=max(hw, min(SCREEN_WIDTH-hw,  self.current_pos[0]))
        self.current_pos[1]=max(hh, min(SCREEN_HEIGHT-hh, self.current_pos[1]))

    def draw(self, bg_image):
        from PIL import Image, ImageDraw
        dw=max(4,int(self.w)); dh=max(4,int(self.h))
        sz=int(max(self.base_w,self.base_h)*2.5)
        eye=Image.new("RGBA",(sz,sz),(0,0,0,0))
        drw=ImageDraw.Draw(eye)
        cr=min(int(min(self.base_w,self.base_h)*0.25), int(min(dw,dh)/2))
        ox=max(-1,min(1,(self.current_pos[0]-self.base_x)/30.0))
        oy=max(-1,min(1,(self.current_pos[1]-self.base_y)/20.0))
        cx2=sz/2; cy2=sz/2
        x0=cx2-dw/2; y0=cy2-dh/2; x1=cx2+dw/2; y1=cy2+dh/2
        sx=ox*dw*0.22; sy=oy*dh*0.18
        drw.ellipse([x0+sx, y0+sy, x1+sx, y1+sy], fill=EYE_COLOR)
        # Eyelids
        ap=int(min(8.0, 2.0+abs(self.lid_angle)*0.18))
        if self.top_lid>0.01:
            lh=max(1,int(dh*self.top_lid)); lw=int(dw+(ap+2)*4)
            lid=Image.new("RGBA",(lw,lh+14+ap*2),(*BG_COLOR,255))
            if abs(self.lid_angle)>0.1:
                lid=lid.rotate(self.lid_angle,resample=Image.BICUBIC,expand=True)
            eye.alpha_composite(lid,(int(cx2-lid.width/2), int(y0-ap*2)))
        if self.bottom_lid>0.01:
            lh=max(1,int(dh*self.bottom_lid)); lw=int(dw+(ap+2)*4)
            lid=Image.new("RGBA",(lw,lh+12+ap*2),(*BG_COLOR,255))
            if abs(self.lid_angle)>0.1:
                lid=lid.rotate(self.lid_angle,resample=Image.BICUBIC,expand=True)
            eye.alpha_composite(lid,(int(cx2-lid.width/2), int(y1+ap-lid.height+ap)))
        rotated=eye.rotate(self.current_rotation,resample=Image.BICUBIC,expand=False)
        bg_image.alpha_composite(rotated,(int(self.current_pos[0]-sz/2),int(self.current_pos[1]-sz/2)))


class EyeRenderer:
    """Renders BlockyEye animation on dual ST7735 TFT displays."""

    def __init__(self, bb: Blackboard):
        self.bb = bb
        self._disp_l = None
        self._disp_r = None
        self._displays_ok = False

    def _init_displays(self):
        try:
            import board, busio, digitalio
            from adafruit_rgb_display import st7735
            spi0 = board.SPI()
            self._disp_l = st7735.ST7735R(spi0, rotation=0, baudrate=24000000, bgr=True,
                cs=digitalio.DigitalInOut(board.CE1),
                dc=digitalio.DigitalInOut(board.D24),
                rst=digitalio.DigitalInOut(board.D25))
            spi1 = busio.SPI(clock=board.D21, MOSI=board.D20, MISO=board.D19)
            self._disp_r = st7735.ST7735R(spi1, rotation=0, baudrate=24000000, bgr=True,
                cs=digitalio.DigitalInOut(board.D18),
                dc=digitalio.DigitalInOut(board.D23),
                rst=digitalio.DigitalInOut(board.D27))
            self._displays_ok = True
            print("EyeRenderer: dual ST7735 displays initialized.")
        except Exception as e:
            print(f"EyeRenderer: displays unavailable ({e}). Running headless.")
            self._displays_ok = False

    def _send_to_display(self, disp, img):
        try:
            disp.image(img)
        except Exception:
            pass

    def run(self):
        print("EyeRenderer started.")
        self._init_displays()

        cx = SCREEN_WIDTH / 2
        cy = SCREEN_HEIGHT / 2
        left_eye  = BlockyEye(cx, cy, is_left=True)
        right_eye = BlockyEye(cx, cy, is_left=False)
        right_eye.noise_t         = left_eye.noise_t
        right_eye.rot_sensitivity = left_eye.rot_sensitivity
        right_eye.rot_speed       = left_eye.rot_speed
        right_eye.happy_phase     = left_eye.happy_phase

        left_eye.set_emotion("idle")
        right_eye.set_emotion("idle")

        interval = 1.0 / max(1.0, RENDER_FPS)
        next_blink = time.time() + random.uniform(3, 6)
        current_emotion = "idle"

        while self.bb.read("running")["running"]:
            try:
                now = time.time()
                state = self.bb.read("emotion", "emotion_intensity", "face_norm_x", "face_norm_y", "face_roll_deg")
                emotion   = state["emotion"]
                intensity = state["emotion_intensity"]
                nx = state["face_norm_x"]
                ny = state["face_norm_y"]
                nr = state["face_roll_deg"]

                if emotion != current_emotion:
                    left_eye.set_emotion(emotion, intensity)
                    right_eye.set_emotion(emotion, intensity)
                    current_emotion = emotion

                # Map face position to eye target offset
                tx = cx + nx * MAX_X_OFFSET
                ty = cy + ny * MAX_Y_OFFSET
                half_w = max(12.0, left_eye.base_w * 0.42)
                half_h = max(12.0, left_eye.base_h * 0.42)
                tx = max(half_w + EYE_BOUND_MARGIN, min(SCREEN_WIDTH  - half_w - EYE_BOUND_MARGIN, tx))
                ty = max(half_h + EYE_BOUND_MARGIN, min(SCREEN_HEIGHT - half_h - EYE_BOUND_MARGIN, ty))
                for eye in (left_eye, right_eye):
                    eye.target_pos[0] = tx
                    eye.target_pos[1] = ty
                    eye.target_rotation = nr

                # Sync blink
                if now >= next_blink:
                    sm = random.uniform(BLINK_SPEED_MIN, BLINK_SPEED_MAX)
                    for eye in (left_eye, right_eye):
                        eye.blink_state = "IDLE"; eye.vy = 0
                    left_eye.start_blink(sm); right_eye.start_blink(sm)
                    next_blink = now + random.uniform(3, 7)

                left_eye.update()
                right_eye.update()
                # Mirror blink phase from left → right
                right_eye.blink_state = left_eye.blink_state
                right_eye.vy = left_eye.vy

                if self._displays_ok:
                    from PIL import Image
                    for disp, eye in [(self._disp_l, left_eye), (self._disp_r, right_eye)]:
                        bg = Image.new("RGBA", (SCREEN_WIDTH, SCREEN_HEIGHT), (*BG_COLOR, 255))
                        eye.draw(bg)
                        self._send_to_display(disp, bg.convert("RGB"))

            except Exception as e:
                print(f"EyeRenderer error: {e}")

            time.sleep(interval)
