"""EyeRenderer: BlockyEye animation + ST7735 SPI display output.

Reads from BB: emotion, emotion_intensity, face_detected, running
Writes to BB:  nothing

Eyes stay fixed at screen center — no face-driven x/y offset or rotation.
Head servos handle looking; the display only shows emotion (lids, scale, blink).
"""
from __future__ import annotations
import math, random, time
from pathlib import Path

try:
    import yaml
except ImportError:
    yaml = None

from core.blackboard import Blackboard
from core.emotion_presets import EMOTION_PRESETS, resolve_emotion_name

APP_DIR = Path(__file__).resolve().parent.parent
DEFAULT_CONFIG_PATH = APP_DIR / "config.yaml"

SCREEN_WIDTH, SCREEN_HEIGHT = 128, 160
EYE_COLOR = (255, 255, 255)
BG_COLOR = (0, 0, 0)
EYE_SIZE = 120
FLOOR_Y = SCREEN_HEIGHT - 5
EMOTION_CHANGE_COOLDOWN = 0.75


def _load_yaml(path):
    if yaml is None or not path.exists():
        return {}
    with open(path, encoding="utf-8") as f:
        return yaml.safe_load(f) or {}


class BlockyEye:
    """Animated blocky eye widget."""

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

    def start_blink(self, speed_mult=None, blink_speed_min=2.0, blink_speed_max=3.5):
        if self.blink_state == "IDLE":
            self.blink_state = "DROPPING"
            self.blink_speed_mult = speed_mult if speed_mult is not None else random.uniform(blink_speed_min, blink_speed_max)
            self.vy = 40 * self.blink_speed_mult

    def set_emotion(self, name: str, intensity: float = 1.0, force: bool = False):
        resolved = resolve_emotion_name(name)
        if resolved is None:
            return
        now = time.time()
        if resolved != self.current_emotion and not force and (now - self.last_emotion_change_time) < EMOTION_CHANGE_COOLDOWN:
            self.pending_emotion = resolved; self.pending_intensity = intensity
            self.pending_apply_time = self.last_emotion_change_time + EMOTION_CHANGE_COOLDOWN
            return
        if resolved == "happy" and self.current_emotion != "happy":
            self.happy_burst_until = now + 0.35
        if resolved != self.current_emotion:
            self.last_emotion_change_time = now
        self.pending_emotion = None; self.current_emotion = resolved
        preset = EMOTION_PRESETS[resolved]; idle = EMOTION_PRESETS["idle"]
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
            tl = self.target_top_lid; bl = self.target_bottom_lid; la = self.target_lid_angle
            tx = self.base_x
            ty = self.base_y
            dx = tx - self.current_pos[0]; dy = ty - self.current_pos[1]
            self.current_pos[0] += dx * 0.20
            self.current_pos[1] += dy * 0.22
            self.current_rotation = 0.0
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
            self.top_lid = max(0.0, min(0.90, self.top_lid))
            self.bottom_lid = max(0.0, min(0.82, self.bottom_lid))
            self.lid_angle = max(-22.0, min(22.0, self.lid_angle))
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

    @staticmethod
    def _solid_lid_block(width: int, height: int, angle: float):
        """Opaque eyelid rectangle; bicubic rotate fringe is forced to solid black."""
        from PIL import Image
        lid = Image.new("RGBA", (max(1, width), max(1, height)), (*BG_COLOR, 255))
        if abs(angle) <= 0.1:
            return lid
        rotated = lid.rotate(angle, resample=Image.BICUBIC, expand=True)
        px = rotated.load()
        w, h = rotated.size
        for y in range(h):
            for x in range(w):
                if px[x, y][3] > 32:
                    px[x, y] = (*BG_COLOR, 255)
                else:
                    px[x, y] = (0, 0, 0, 0)
        return rotated

    def draw_eyelids(self, eye_img, x0: float, y0: float, x1: float, y1: float) -> None:
        """Eyelid masks with generous overdraw (ported from voice-agentv4)."""
        w = int(x1 - x0)
        h = int(y1 - y0)
        if w < 1 or h < 1:
            return

        if self.top_lid > 0.01:
            lid_h = int(h * self.top_lid)
            lid_src = self._solid_lid_block(int(w * 2.1), lid_h + 64, self.lid_angle)
            lid_x = int(x0 + w / 2 - lid_src.width / 2)
            lid_y = int(y0 - 32)
            eye_img.alpha_composite(lid_src, (lid_x, lid_y))

        if self.bottom_lid > 0.01:
            lid_h = int(h * self.bottom_lid)
            lid_src = self._solid_lid_block(int(w * 2.1), lid_h + 28, self.lid_angle)
            lid_x = int(x0 + w / 2 - lid_src.width / 2)
            lid_y = int(y1 + 13 - lid_src.height)
            eye_img.alpha_composite(lid_src, (lid_x, lid_y))

    def draw(self, bg_image):
        from PIL import Image, ImageDraw
        draw_w = max(6, min(int(self.w), SCREEN_WIDTH - 4))
        draw_h = max(6, min(int(self.h), SCREEN_HEIGHT - 4))
        eye_img_size = int(max(self.base_w, self.base_h) * 2.6)
        eye_img = Image.new("RGBA", (eye_img_size, eye_img_size), (0, 0, 0, 0))
        eye_draw = ImageDraw.Draw(eye_img)

        cx = eye_img_size / 2
        cy = eye_img_size / 2
        x0 = cx - draw_w / 2
        y0 = cy - draw_h / 2
        x1 = cx + draw_w / 2
        y1 = cy + draw_h / 2
        eye_draw.ellipse([x0, y0, x1, y1], fill=EYE_COLOR)
        self.draw_eyelids(eye_img, x0, y0, x1, y1)

        paste_x = int(self.current_pos[0] - eye_img_size / 2)
        paste_y = int(self.current_pos[1] - eye_img_size / 2)
        bg_image.alpha_composite(eye_img, (paste_x, paste_y))


class EyeRenderer:
    """Drives both ST7735 TFT displays with animated BlockyEye objects."""

    def __init__(self, bb: Blackboard, config_path: Path = DEFAULT_CONFIG_PATH) -> None:
        self.bb = bb
        cfg = _load_yaml(config_path)
        s = cfg.get("stream", {}) or {}
        e = cfg.get("eyes", {}) or {}

        self.render_fps = int(s.get("render_fps", 24))
        self.blink_speed_min = float(e.get("blink_speed_min", 3.2))
        self.blink_speed_max = float(e.get("blink_speed_max", 4.2))

    def run(self) -> None:
        from PIL import Image
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
        right_eye.noise_t = left_eye.noise_t
        right_eye.rot_sensitivity = left_eye.rot_sensitivity
        right_eye.rot_speed = left_eye.rot_speed
        right_eye.happy_phase = left_eye.happy_phase

        next_blink = time.time() + random.uniform(3, 6)
        delay = 1.0 / max(1, self.render_fps)
        current_emotion = "idle"

        while self.bb.read("running")["running"]:
            now = time.time()
            state = self.bb.read("emotion", "emotion_intensity", "running", "amplitude_fast", "amplitude_slow")
            emotion   = state["emotion"]
            intensity = state["emotion_intensity"]
            amp_fast  = state.get("amplitude_fast", 0.0)
            amp_slow  = state.get("amplitude_slow", 0.0)

            if emotion != current_emotion:
                left_eye.set_emotion(emotion, intensity)
                right_eye.set_emotion(emotion, intensity)
                current_emotion = emotion

            for eye in (left_eye, right_eye):
                eye.target_pos[0] = cx
                eye.target_pos[1] = cy
                eye.target_rotation = 0.0
                
                # ── Layer 2 Priority: Amplitude-Driven Animation ──
                # Enhance scale and lid openness slightly based on amplitude
                if amp_slow > 0.05:
                    eye.target_scale_h = min(1.3, eye.target_scale_h + amp_slow * 0.15)
                if amp_fast > 0.1:
                    eye.target_top_lid = max(0.0, eye.target_top_lid - amp_fast * 0.15)
                    eye.target_bottom_lid = max(0.0, eye.target_bottom_lid - amp_fast * 0.1)

            # Suppress blinks if amplitude is high (agent is speaking)
            if now >= next_blink and amp_fast < 0.2:
                speed = random.uniform(self.blink_speed_min, self.blink_speed_max)
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
                left_eye.start_blink(speed, self.blink_speed_min, self.blink_speed_max)
                right_eye.start_blink(speed, self.blink_speed_min, self.blink_speed_max)
                next_blink = now + random.uniform(3, 7)

            left_eye.update()
            right_eye.update()

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
