"""EyeRenderer: BlockyEye animation + ST7735 SPI display output.

Reads from BB: emotion, emotion_intensity, face_norm_x, face_norm_y,
               face_roll_deg, face_detected, running
Writes to BB:  nothing
"""
from __future__ import annotations
import math, random, time
from pathlib import Path

try:
    import yaml
except ImportError:
    yaml = None

from core.blackboard import Blackboard

APP_DIR = Path(__file__).resolve().parent.parent
DEFAULT_CONFIG_PATH = APP_DIR / "config.yaml"

SCREEN_WIDTH, SCREEN_HEIGHT = 128, 160
EYE_COLOR = (255, 255, 255)
BG_COLOR = (0, 0, 0)
EYE_SIZE = 120
FLOOR_Y = SCREEN_HEIGHT - 5
EYE_BOUND_MARGIN = 8
BLINK_SPEED_MIN, BLINK_SPEED_MAX = 2.0, 3.5
LOOK_SIDE_OFFSET = 16.0
MAX_X_OFFSET, MAX_Y_OFFSET = 30, 22
FACE_ROLL_MULT, FACE_ROLL_MAX_DEG = 0.75, 10.0
EMOTION_CHANGE_COOLDOWN = 0.75

EMOTION_PRESETS = {
    "idle":                  {"scale_w":1.0,  "scale_h":1.0,  "top_lid":0.0,  "bottom_lid":0.0,  "lid_angle":0.0,  "mirror_angle":True},
    "happy":                 {"scale_w":1.10, "scale_h":0.84, "top_lid":0.0,  "bottom_lid":0.30, "lid_angle":-6.0, "mirror_angle":True},
    "sad":                   {"scale_w":0.98, "scale_h":0.96, "top_lid":0.12, "bottom_lid":0.0,  "lid_angle":-8.0, "mirror_angle":True, "pos":(0,4)},
    "surprised":             {"scale_w":0.98, "scale_h":1.12, "top_lid":0.0,  "bottom_lid":0.0,  "lid_angle":0.0,  "mirror_angle":True},
    "suspicious":            {"scale_w":1.06, "scale_h":0.74, "top_lid":0.38, "bottom_lid":0.35, "lid_angle":0.0,  "mirror_angle":True},
    "sleepy":                {"scale_w":1.04, "scale_h":0.88, "top_lid":0.56, "bottom_lid":0.0,  "lid_angle":0.0,  "mirror_angle":True},
    "looking_left_natural":  {"scale_w":1.02, "scale_h":0.98, "top_lid":0.0,  "bottom_lid":0.05, "lid_angle":-3.0, "mirror_angle":False},
    "looking_right_natural": {"scale_w":1.02, "scale_h":0.98, "top_lid":0.0,  "bottom_lid":0.05, "lid_angle":3.0,  "mirror_angle":False},
    "excited":               {"scale_w":1.14, "scale_h":0.80, "top_lid":0.0,  "bottom_lid":0.24, "lid_angle":0.0,  "mirror_angle":True},
    "calm":                  {"scale_w":1.03, "scale_h":0.90, "top_lid":0.16, "bottom_lid":0.12, "lid_angle":0.0,  "mirror_angle":True},
    "curious":               {"scale_w":1.02, "scale_h":1.03, "top_lid":0.0,  "bottom_lid":0.13, "lid_angle":4.0,  "mirror_angle":False},
    "attentive":             {"scale_w":1.08, "scale_h":1.06, "top_lid":0.0,  "bottom_lid":0.0,  "lid_angle":0.0,  "mirror_angle":True},
    "engaged":               {"scale_w":1.02, "scale_h":1.00, "top_lid":0.04, "bottom_lid":0.06, "lid_angle":5.0,  "mirror_angle":True},
    "thinking":              {"scale_w":1.0,  "scale_h":1.0,  "top_lid":0.0,  "bottom_lid":0.0,  "lid_angle":0.0,  "mirror_angle":True, "pos":(0,-2)},
    "warm":                  {"scale_w":1.06, "scale_h":1.00, "top_lid":0.0,  "bottom_lid":0.16, "lid_angle":2.0,  "mirror_angle":True, "pos":(0,-4)},
    "amused":                {"scale_w":1.00, "scale_h":0.98, "top_lid":0.0,  "bottom_lid":0.14, "lid_angle":3.0,  "mirror_angle":False},
    "playful":               {"scale_w":1.02, "scale_h":1.00, "top_lid":0.0,  "bottom_lid":0.06, "lid_angle":0.0,  "mirror_angle":False},
    "concentrating":         {"scale_w":0.96, "scale_h":0.84, "top_lid":0.16, "bottom_lid":0.08, "lid_angle":0.0,  "mirror_angle":True},
    "uncertain":             {"scale_w":0.98, "scale_h":0.96, "top_lid":0.08, "bottom_lid":0.04, "lid_angle":0.0,  "mirror_angle":True},
    "squint":                {"scale_w":1.0,  "scale_h":0.62, "top_lid":0.42, "bottom_lid":0.35, "lid_angle":0.0,  "mirror_angle":True},
    "afraid":                {"scale_w":0.92, "scale_h":1.12, "top_lid":0.0,  "bottom_lid":0.0,  "lid_angle":0.0,  "mirror_angle":True},
    "proud":                 {"scale_w":1.06, "scale_h":1.02, "top_lid":0.0,  "bottom_lid":0.0,  "lid_angle":-2.0, "mirror_angle":True},
    "remembering":           {"scale_w":1.04, "scale_h":1.03, "top_lid":0.02, "bottom_lid":0.0,  "lid_angle":0.0,  "mirror_angle":True},
    "awkward":               {"scale_w":0.96, "scale_h":0.93, "top_lid":0.10, "bottom_lid":0.10, "lid_angle":0.0,  "mirror_angle":True},
    "looking_left":          {"scale_w":1.02, "scale_h":0.98, "top_lid":0.0,  "bottom_lid":0.05, "lid_angle":-3.0, "mirror_angle":False},
    "looking_right":         {"scale_w":1.02, "scale_h":0.98, "top_lid":0.0,  "bottom_lid":0.05, "lid_angle":3.0,  "mirror_angle":False},
    "looking_left_happy":    {"scale_w":1.10, "scale_h":0.84, "top_lid":0.0,  "bottom_lid":0.30, "lid_angle":-6.0, "mirror_angle":False},
    "looking_right_happy":   {"scale_w":1.10, "scale_h":0.84, "top_lid":0.0,  "bottom_lid":0.30, "lid_angle":6.0,  "mirror_angle":False},
}


def _load_yaml(path):
    if yaml is None or not path.exists():
        return {}
    with open(path, encoding="utf-8") as f:
        return yaml.safe_load(f) or {}


class BlockyEye:
    """Animated blocky eye widget (extracted verbatim from face_tracking_head.py)."""

    def __init__(self, x, y, scale=1.0, is_left=True):
        self.base_x, self.base_y = x, y
        self.current_pos = [float(x), float(y)]
        self.target_pos  = [float(x), float(y)]
        self.vel_x = self.vel_y = 0.0
        self.base_w = self.base_h = EYE_SIZE * scale
        self.current_w = self.target_w = self.base_w
        self.current_h = self.target_h = self.base_h
        self.vel_w = self.vel_h = 0.0
        self.w = self.base_w; self.h = self.base_h
        self.current_rotation = self.target_rotation = 0.0
        self.rot_sensitivity = random.uniform(0.3, 0.5)
        self.rot_speed = random.uniform(0.15, 0.25)
        self.is_left = is_left
        self.blink_state = "IDLE"; self.vy = 0
        self.blink_speed_mult = 1.0
        self.target_scale_w = self.scale_w = 1.0
        self.target_scale_h = self.scale_h = 1.0
        self.scale_w_vel = self.scale_h_vel = 0.0
        self.top_lid = self.bottom_lid = self.lid_angle = 0.0
        self.top_lid_vel = self.bottom_lid_vel = self.lid_angle_vel = 0.0
        self.target_top_lid = self.target_bottom_lid = self.target_lid_angle = 0.0
        self.current_emotion = "idle"
        self.last_emotion_change_time = 0.0
        self.pending_emotion = None; self.pending_intensity = 1.0
        self.pending_apply_time = 0.0
        self.happy_phase = random.uniform(0.0, math.pi * 2)
        self.happy_burst_until = 0.0
        self.noise_t = random.uniform(0, 100)
        self.emotion_pos_bias_x = self.emotion_pos_bias_y = 0.0

    def start_blink(self, speed_mult=None):
        if self.blink_state == "IDLE":
            self.blink_state = "DROPPING"
            self.blink_speed_mult = speed_mult if speed_mult is not None else random.uniform(BLINK_SPEED_MIN, BLINK_SPEED_MAX)
            self.vy = 40 * self.blink_speed_mult

    def set_emotion(self, name: str, intensity: float = 1.0, force: bool = False):
        if name not in EMOTION_PRESETS:
            return
        now = time.time()
        if name != self.current_emotion and not force and (now - self.last_emotion_change_time) < EMOTION_CHANGE_COOLDOWN:
            self.pending_emotion = name; self.pending_intensity = intensity
            self.pending_apply_time = self.last_emotion_change_time + EMOTION_CHANGE_COOLDOWN
            return
        if name == "happy" and self.current_emotion != "happy":
            self.happy_burst_until = now + 0.35
        if name != self.current_emotion:
            self.last_emotion_change_time = now
        self.pending_emotion = None; self.current_emotion = name
        preset = EMOTION_PRESETS[name]; idle = EMOTION_PRESETS["idle"]
        side = preset.get("left_bias", {}) if self.is_left else preset.get("right_bias", {})
        intensity = max(0.0, min(1.0, intensity))
        self.emotion_pos_bias_x = side.get("pos_x", 0.0) * intensity
        self.emotion_pos_bias_y = side.get("pos_y", 0.0) * intensity
        self.target_scale_w  = idle["scale_w"]  + (preset["scale_w"]  - idle["scale_w"])  * intensity
        self.target_scale_h  = idle["scale_h"]  + (preset["scale_h"]  - idle["scale_h"])  * intensity
        self.target_top_lid  = idle["top_lid"]  + (preset["top_lid"]  - idle["top_lid"])  * intensity
        self.target_bottom_lid = idle["bottom_lid"] + (preset["bottom_lid"] - idle["bottom_lid"]) * intensity
        lid = idle["lid_angle"] + (preset["lid_angle"] - idle["lid_angle"]) * intensity
        if side:
            self.target_scale_w  += side.get("scale_w", 0.0) * intensity
            self.target_scale_h  += side.get("scale_h", 0.0) * intensity
            self.target_top_lid  += side.get("top_lid", 0.0) * intensity
            self.target_bottom_lid += side.get("bottom_lid", 0.0) * intensity
            lid += side.get("lid_angle", 0.0) * intensity
        if preset.get("mirror_angle", True) and not self.is_left and abs(lid) > 0:
            lid = -lid
        self.target_lid_angle = lid

    def update(self):
        if self.pending_emotion is not None and time.time() >= self.pending_apply_time:
            e, i = self.pending_emotion, self.pending_intensity
            self.pending_emotion = None
            self.set_emotion(e, i, force=True)

        if self.blink_state == "IDLE":
            t = time.time() + self.noise_t
            tx = self.target_pos[0] + math.sin(t*1.3)*0.2 + math.sin(t*0.7)*0.1
            ty = self.target_pos[1] + math.cos(t*1.1)*0.2 + math.cos(t*0.9)*0.1
            tl = self.target_top_lid; bl = self.target_bottom_lid; la = self.target_lid_angle
            if time.time() < self.happy_burst_until:
                ty -= 8.0
            if self.current_emotion == "happy":
                ht = time.time()*6.0 + self.happy_phase
                ty -= 2.5 + math.sin(ht)*2.0; tx += math.sin(ht*1.7)*1.2
            elif "looking_" in self.current_emotion and "left" in self.current_emotion:
                tx -= LOOK_SIDE_OFFSET
            elif "looking_" in self.current_emotion and "right" in self.current_emotion:
                tx += LOOK_SIDE_OFFSET
            ep = EMOTION_PRESETS[self.current_emotion].get("pos", (0,0))
            tx += ep[0] + self.emotion_pos_bias_x; ty += ep[1] + self.emotion_pos_bias_y
            dx = tx - self.current_pos[0]; dy = ty - self.current_pos[1]
            sy = 0.14 if dy < -1.0 else (0.38 if dy > 1.0 else 0.22)
            self.current_pos[0] += dx*0.20; self.current_pos[1] += dy*sy
            rx = self.current_pos[0]-self.base_x; ry = self.current_pos[1]-self.base_y
            lr = (rx*0.5+ry*0.8)*self.rot_sensitivity
            if self.current_emotion=="happy": lr += math.sin(time.time()*8.0+self.happy_phase)*1.2
            self.current_rotation += (lr+self.target_rotation-self.current_rotation)*self.rot_speed
            t2 = time.time()
            bw = math.sin(t2*1.5+self.base_x)*1.5 + math.sin(t2*0.5)*1.0
            bh = math.cos(t2*1.8+self.base_y)*1.5 + math.cos(t2*0.6)*1.0
            k=0.12; d=0.7
            if self.current_emotion=="surprised": k=0.30; d=0.52
            self.scale_w_vel=(self.scale_w_vel+(self.target_scale_w-self.scale_w)*k)*d; self.scale_w+=self.scale_w_vel
            self.scale_h_vel=(self.scale_h_vel+(self.target_scale_h-self.scale_h)*k)*d; self.scale_h+=self.scale_h_vel
            self.top_lid_vel=(self.top_lid_vel+(tl-self.top_lid)*k)*d; self.top_lid+=self.top_lid_vel
            self.bottom_lid_vel=(self.bottom_lid_vel+(bl-self.bottom_lid)*k)*d; self.bottom_lid+=self.bottom_lid_vel
            self.lid_angle_vel=(self.lid_angle_vel+(la-self.lid_angle)*k)*d; self.lid_angle+=self.lid_angle_vel
            self.target_w=self.base_w*self.scale_w+bw; self.target_h=self.base_h*self.scale_h+bh
        elif self.blink_state=="DROPPING":
            self.vy+=10*self.blink_speed_mult; self.current_pos[1]+=self.vy
            self.current_w=self.base_w-10; self.current_h=self.base_h+20
            self.target_w=self.current_w; self.target_h=self.current_h
            if self.current_pos[1]+self.current_h//2>=FLOOR_Y:
                self.current_pos[1]=FLOOR_Y-self.current_h//2; self.blink_state="SQUASHING"
        elif self.blink_state=="SQUASHING":
            self.current_h-=65*self.blink_speed_mult; self.current_w+=40*self.blink_speed_mult
            self.current_pos[1]=FLOOR_Y-self.current_h//2
            if self.current_h<=22: self.current_h=22; self.blink_state="JUMPING"
        elif self.blink_state=="JUMPING":
            r=max(0.15,min(0.95,0.85*self.blink_speed_mult))
            self.current_h+=(self.base_h-self.current_h)*r; self.current_w+=(self.base_w-self.current_w)*r
            self.current_pos[0]+=(self.target_pos[0]-self.current_pos[0])*0.8
            if abs(self.current_h-self.base_h)<5:
                self.current_h=self.base_h; self.current_w=self.base_w; self.blink_state="IDLE"; self.vy=0
        if self.blink_state=="IDLE":
            k=0.08; d=0.90
            self.vel_w=(self.vel_w+(self.target_w-self.current_w)*k)*d; self.current_w+=self.vel_w
            self.vel_h=(self.vel_h+(self.target_h-self.current_h)*k)*d; self.current_h+=self.vel_h
        else:
            self.vel_w=self.vel_h=0
        self.w=self.current_w; self.h=self.current_h
        hw=max(2.0,self.w*0.5); hh=max(2.0,self.h*0.5)
        self.current_pos[0]=max(hw,min(SCREEN_WIDTH-hw,self.current_pos[0]))
        self.current_pos[1]=max(hh,min(SCREEN_HEIGHT-hh,self.current_pos[1]))

    def draw(self, bg_image):
        from PIL import Image, ImageDraw
        dw=max(4,int(self.w)); dh=max(4,int(self.h))
        sz=int(max(self.base_w,self.base_h)*2.5)
        eye=Image.new("RGBA",(sz,sz),(0,0,0,0)); ed=ImageDraw.Draw(eye)
        br=min(int(min(self.base_w,self.base_h)*0.25),int(min(dw,dh)//2))
        ox=max(-1,min(1,(self.current_pos[0]-self.base_x)/30.0))
        oy=max(-1,min(1,(self.current_pos[1]-self.base_y)/20.0))
        cx=cy=sz/2; x0=cx-dw/2; y0=cy-dh/2; x1=cx+dw/2; y1=cy+dh/2
        sx=x0+(x1-x0)/2+ox*(dw*0.22); sy=y0+(y1-y0)/2+oy*(dh*0.18)
        ed.ellipse([sx-dw/2,sy-dh/2,sx+dw/2,sy+dh/2],fill=EYE_COLOR)
        # eyelids
        for which,ratio,ypos in [("top",self.top_lid,y0),("bot",self.bottom_lid,y1)]:
            if ratio>0.01:
                lh=max(1,int(dh*ratio)); lw=int(dw+28); lh2=int(lh+14)
                lid=Image.new("RGBA",(lw,lh2),(*BG_COLOR,255))
                if abs(self.lid_angle)>0.1:
                    lid=lid.rotate(self.lid_angle,resample=Image.BICUBIC,expand=True)
                lx=int(cx-lid.width/2)
                ly=int(y0-6) if which=="top" else int(y1+6-lid.height)
                eye.alpha_composite(lid,(lx,ly))
        rot=eye.rotate(self.current_rotation,resample=Image.BICUBIC,expand=False)
        px=int(self.current_pos[0]-sz/2); py=int(self.current_pos[1]-sz/2)
        bg_image.alpha_composite(rot,(px,py))


class EyeRenderer:
    """Drives both ST7735 TFT displays with animated BlockyEye objects."""

    def __init__(self, bb: Blackboard, config_path: Path = DEFAULT_CONFIG_PATH) -> None:
        self.bb = bb
        cfg = _load_yaml(config_path)
        s = cfg.get("stream", {}) or {}
        self.render_fps = int(s.get("render_fps", 24))

    def run(self) -> None:
        from PIL import Image
        # Display init (graceful fallback if hardware absent)
        disp_l = disp_r = None
        try:
            import board, busio, digitalio
            from adafruit_rgb_display import st7735
            spi0 = board.SPI()
            disp_l = st7735.ST7735R(spi0,rotation=0,baudrate=24000000,bgr=True,
                cs=digitalio.DigitalInOut(board.CE1),
                dc=digitalio.DigitalInOut(board.D24),
                rst=digitalio.DigitalInOut(board.D25))
            spi1 = busio.SPI(clock=board.D21,MOSI=board.D20,MISO=board.D19)
            disp_r = st7735.ST7735R(spi1,rotation=0,baudrate=24000000,bgr=True,
                cs=digitalio.DigitalInOut(board.D18),
                dc=digitalio.DigitalInOut(board.D23),
                rst=digitalio.DigitalInOut(board.D27))
            print("[EyeRenderer] Displays initialized.")
        except Exception as e:
            print(f"[EyeRenderer] Display init failed (headless mode): {e}")

        cx = SCREEN_WIDTH / 2; cy = SCREEN_HEIGHT / 2
        left_eye  = BlockyEye(cx, cy, is_left=True)
        right_eye = BlockyEye(cx, cy, is_left=False)
        # Sync dynamics so both eyes look identical
        right_eye.noise_t = left_eye.noise_t
        right_eye.rot_sensitivity = left_eye.rot_sensitivity
        right_eye.rot_speed = left_eye.rot_speed
        right_eye.happy_phase = left_eye.happy_phase

        next_blink = time.time() + random.uniform(3, 6)
        delay = 1.0 / max(1, self.render_fps)
        current_emotion = "idle"

        while self.bb.read("running")["running"]:
            now = time.time()
            state = self.bb.read("emotion","emotion_intensity","face_detected",
                                 "face_norm_x","face_norm_y","face_roll_deg")
            emotion   = state["emotion"]
            intensity = state["emotion_intensity"]
            norm_x    = state["face_norm_x"]
            norm_y    = state["face_norm_y"]
            face_roll = state["face_roll_deg"]

            # Apply emotion change
            if emotion != current_emotion:
                left_eye.set_emotion(emotion, intensity)
                right_eye.set_emotion(emotion, intensity)
                current_emotion = emotion

            # Eye target position from face
            tx = norm_x * MAX_X_OFFSET
            ty = norm_y * MAX_Y_OFFSET
            roll = max(-FACE_ROLL_MAX_DEG, min(FACE_ROLL_MAX_DEG, face_roll * FACE_ROLL_MULT))

            for eye in (left_eye, right_eye):
                half_w = max(12.0, eye.base_w * 0.42)
                half_h = max(12.0, eye.base_h * 0.42)
                eye.target_pos[0] = max(half_w + EYE_BOUND_MARGIN,
                                        min(SCREEN_WIDTH  - half_w - EYE_BOUND_MARGIN, cx + tx))
                eye.target_pos[1] = max(half_h + EYE_BOUND_MARGIN,
                                        min(SCREEN_HEIGHT - half_h - EYE_BOUND_MARGIN, cy + ty))
                eye.target_rotation = roll

            # Blink
            if now >= next_blink:
                speed = random.uniform(BLINK_SPEED_MIN, BLINK_SPEED_MAX)
                # Align blink start conditions so both displays animate the same phase
                avg_y = (left_eye.current_pos[1] + right_eye.current_pos[1]) * 0.5
                avg_w = (left_eye.current_w + right_eye.current_w) * 0.5
                avg_h = (left_eye.current_h + right_eye.current_h) * 0.5
                for eye in (left_eye, right_eye):
                    eye.blink_state = "IDLE"
                    eye.vy = 0
                    eye.current_pos[1] = avg_y
                    eye.current_w = avg_w
                    eye.current_h = avg_h
                    eye.w = avg_w
                    eye.h = avg_h
                left_eye.start_blink(speed)
                right_eye.start_blink(speed)
                next_blink = now + random.uniform(3, 7)

            # Update physics
            left_eye.update()
            right_eye.update()

            # Render to displays
            if disp_l is not None or disp_r is not None:
                try:
                    bg_l = Image.new("RGBA", (SCREEN_WIDTH, SCREEN_HEIGHT), (*BG_COLOR, 255))
                    bg_r = Image.new("RGBA", (SCREEN_WIDTH, SCREEN_HEIGHT), (*BG_COLOR, 255))
                    left_eye.draw(bg_l); right_eye.draw(bg_r)
                    if disp_l: disp_l.image(bg_l.convert("RGB"))
                    if disp_r: disp_r.image(bg_r.convert("RGB"))
                except Exception as e:
                    print(f"[EyeRenderer] Display error: {e}")

            time.sleep(delay)

        print("[EyeRenderer] Stopped.")
