"""ByeWaveService: detects hand waves from the shared Blackboard camera frame
and plays bye-wave arm animations via the Blackboard -> ServoMixer path.

Uses stream_frame written by FaceTracker (no second camera instance).
Streams a hand-annotated MJPEG feed on a configurable port (default 8000).

config.yaml section:
    bye_wave:
      enabled: true
      port: 8000
      cooldown_sec: 10.0
      max_hands: 2
      presets_path: "tests/arm_pose_presets.json"
"""
from __future__ import annotations
import collections, json, pathlib, random, socket, socketserver, sys, threading, time
from http.server import BaseHTTPRequestHandler, HTTPServer
import cv2
from core.blackboard import Blackboard
from lib.hand_detector import HandDetector, HandDetection, draw_skeleton, draw_motion_trail

_latest_frame = None
_frame_lock = threading.Lock()


class _ThreadingHTTPServer(socketserver.ThreadingMixIn, HTTPServer):
    daemon_threads = True


class _MJPEGHandler(BaseHTTPRequestHandler):
    def do_GET(self):
        global _latest_frame
        if self.path == "/":
            self.send_response(200)
            self.send_header("Content-Type", "text/html")
            self.end_headers()
            host_ip = "localhost"
            try:
                s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
                s.connect(("8.8.8.8", 80))
                host_ip = s.getsockname()[0]
                s.close()
            except Exception:
                pass
            port = self.server.server_address[1]
            html = (
                "<!DOCTYPE html><html><head><meta charset='utf-8'>"
                "<title>Bye Wave Debug Stream</title>"
                "<style>"
                "body{background:#0f101a;color:#f1f5f9;font-family:sans-serif;"
                "text-align:center;padding:20px;margin:0}"
                "h1{color:#ff6a00;font-size:1.8rem;margin-bottom:10px}"
                ".container{max-width:1200px;margin:0 auto}"
                ".video-box{display:inline-block;border-radius:12px;overflow:hidden;"
                "border:2px solid #ff6a00;box-shadow:0 0 25px rgba(255,106,0,.2);margin-bottom:20px}"
                "img{display:block;max-width:100%;height:auto}"
                ".controls{background:#1a1d2e;padding:20px;border-radius:12px;"
                "border:2px solid #2a2f45;margin-top:20px}"
                ".control-group{margin:15px 0;text-align:left;max-width:600px;margin-left:auto;margin-right:auto}"
                ".control-group label{display:block;color:#94a3b8;font-size:0.9rem;margin-bottom:5px}"
                ".slider-container{display:flex;align-items:center;gap:15px}"
                "input[type=range]{flex:1;height:8px;border-radius:5px;background:#2a2f45;"
                "outline:none;-webkit-appearance:none}"
                "input[type=range]::-webkit-slider-thumb{-webkit-appearance:none;appearance:none;"
                "width:20px;height:20px;border-radius:50%;background:#ff6a00;cursor:pointer}"
                "input[type=range]::-moz-range-thumb{width:20px;height:20px;border-radius:50%;"
                "background:#ff6a00;cursor:pointer;border:none}"
                ".value-display{min-width:60px;text-align:right;font-weight:bold;color:#ff6a00;"
                "font-size:1.1rem}"
                ".info{color:#64748b;font-size:0.85rem;margin-top:5px}"
                "button{background:#ff6a00;color:white;border:none;padding:10px 20px;"
                "border-radius:6px;cursor:pointer;font-size:0.9rem;margin:5px}"
                "button:hover{background:#ff8533}"
                ".status{color:#10b981;font-size:0.85rem;margin-top:10px}"
                "</style>"
                "</head><body>"
                "<div class='container'>"
                "<h1>🤖 Bye Wave Debug Stream</h1>"
                "<p style='color:#94a3b8'>Hand near face triggers bye animation. Adjust speeds in real-time.</p>"
                "<div class='video-box'><img src='/stream'/></div>"
                "<div class='controls'>"
                "<h3 style='color:#ff6a00;margin-top:0'>⚙️ Speed Controls</h3>"
                
                "<div class='control-group'>"
                "<label>🔽 Vertical Speed (a0, a1 - shoulder/elbow up/down)</label>"
                "<div class='slider-container'>"
                "<input type='range' id='verticalSpeed' min='0.3' max='1.5' step='0.05' value='0.8'>"
                "<span class='value-display' id='verticalValue'>0.8x</span>"
                "</div>"
                "<div class='info'>Lower = SLOWER movements. Higher = faster. Default: 0.8x</div>"
                "</div>"
                
                "<div class='control-group'>"
                "<label>↔️ Horizontal Speed (a2, a3 - wrist/hand swap)</label>"
                "<div class='slider-container'>"
                "<input type='range' id='horizontalSpeed' min='0.3' max='2.0' step='0.05' value='1.0'>"
                "<span class='value-display' id='horizontalValue'>1.0x</span>"
                "</div>"
                "<div class='info'>Lower = SLOWER swaps. Higher = faster. Default: 1.0x</div>"
                "</div>"
                
                "<div class='control-group'>"
                "<label>✨ Smoothness (easing strength)</label>"
                "<div class='slider-container'>"
                "<input type='range' id='smoothness' min='1.0' max='5.0' step='0.5' value='3.0'>"
                "<span class='value-display' id='smoothnessValue'>3.0</span>"
                "</div>"
                "<div class='info'>Higher = MORE smooth/gradual. Lower = more direct/linear. Default: 3.0 (cubic)</div>"
                "</div>"
                
                "<button onclick='resetDefaults()'>Reset to Defaults</button>"
                "<button onclick='applyToConfig()'>Save to Config</button>"
                "<div class='status' id='status'></div>"
                "</div>"
                
                f"<p style='color:#555;font-size:.75rem;margin-top:20px'>Stream: http://{host_ip}:{port}/stream</p>"
                "</div>"
                
                "<script>"
                "const verticalSlider = document.getElementById('verticalSpeed');"
                "const horizontalSlider = document.getElementById('horizontalSpeed');"
                "const smoothnessSlider = document.getElementById('smoothness');"
                "const verticalValue = document.getElementById('verticalValue');"
                "const horizontalValue = document.getElementById('horizontalValue');"
                "const smoothnessValue = document.getElementById('smoothnessValue');"
                "const status = document.getElementById('status');"
                
                "verticalSlider.oninput = function(){"
                "  verticalValue.textContent = this.value + 'x';"
                "  updateSpeeds();"
                "};"
                
                "horizontalSlider.oninput = function(){"
                "  horizontalValue.textContent = this.value + 'x';"
                "  updateSpeeds();"
                "};"
                
                "smoothnessSlider.oninput = function(){"
                "  smoothnessValue.textContent = this.value;"
                "  updateSpeeds();"
                "};"
                
                "function updateSpeeds(){"
                "  fetch('/api/set_speeds', {"
                "    method: 'POST',"
                "    headers: {'Content-Type': 'application/json'},"
                "    body: JSON.stringify({"
                "      vertical_speed: parseFloat(verticalSlider.value),"
                "      horizontal_speed: parseFloat(horizontalSlider.value),"
                "      smoothness: parseFloat(smoothnessSlider.value)"
                "    })"
                "  }).then(r => r.json()).then(data => {"
                "    if(data.success){"
                "      status.textContent = '✓ Settings updated in real-time';"
                "      status.style.color = '#10b981';"
                "    }"
                "  }).catch(() => {"
                "    status.textContent = '✗ Failed to update settings';"
                "    status.style.color = '#ef4444';"
                "  });"
                "};"
                
                "function resetDefaults(){"
                "  verticalSlider.value = 0.8;"
                "  horizontalSlider.value = 1.0;"
                "  smoothnessSlider.value = 3.0;"
                "  verticalValue.textContent = '0.8x';"
                "  horizontalValue.textContent = '1.0x';"
                "  smoothnessValue.textContent = '3.0';"
                "  updateSpeeds();"
                "};"
                
                "function applyToConfig(){"
                "  fetch('/api/save_to_config', {"
                "    method: 'POST',"
                "    headers: {'Content-Type': 'application/json'},"
                "    body: JSON.stringify({"
                "      vertical_speed: parseFloat(verticalSlider.value),"
                "      horizontal_speed: parseFloat(horizontalSlider.value),"
                "      smoothness: parseFloat(smoothnessSlider.value)"
                "    })"
                "  }).then(r => r.json()).then(data => {"
                "    if(data.success){"
                "      status.textContent = '✓ Saved to config.yaml! Restart to persist.';"
                "      status.style.color = '#10b981';"
                "    }"
                "  }).catch(() => {"
                "    status.textContent = '✗ Failed to save to config';"
                "    status.style.color = '#ef4444';"
                "  });"
                "};"
                
                "// Load current speeds on page load"
                "fetch('/api/get_speeds').then(r => r.json()).then(data => {"
                "  verticalSlider.value = data.vertical_speed;"
                "  horizontalSlider.value = data.horizontal_speed;"
                "  smoothnessSlider.value = data.smoothness || 3.0;"
                "  verticalValue.textContent = data.vertical_speed + 'x';"
                "  horizontalValue.textContent = data.horizontal_speed + 'x';"
                "  smoothnessValue.textContent = (data.smoothness || 3.0);"
                "});"
                "</script>"
                "</body></html>"
            )
            self.wfile.write(html.encode("utf-8"))
            return
        elif self.path == "/stream":
            self.send_response(200)
            self.send_header("Age", "0")
            self.send_header("Cache-Control", "no-cache, private")
            self.send_header("Pragma", "no-cache")
            self.send_header("Content-Type", "multipart/x-mixed-replace; boundary=frame")
            self.end_headers()
            try:
                while True:
                    with _frame_lock:
                        frame = None if _latest_frame is None else _latest_frame.copy()
                    if frame is None:
                        time.sleep(0.04)
                        continue
                    _, encoded = cv2.imencode(".jpg", frame)
                    jpg = encoded.tobytes()
                    self.wfile.write(b"--frame\r\n")
                    self.wfile.write(b"Content-Type: image/jpeg\r\n")
                    self.wfile.write(f"Content-Length: {len(jpg)}\r\n\r\n".encode())
                    self.wfile.write(jpg)
                    self.wfile.write(b"\r\n")
                    time.sleep(0.04)
            except (BrokenPipeError, ConnectionResetError):
                return
            except Exception:
                return
        else:
            self.send_error(404)
    
    def do_POST(self):
        if self.path == "/api/set_speeds":
            content_length = int(self.headers.get('Content-Length', 0))
            body = self.rfile.read(content_length)
            try:
                data = json.loads(body)
                vertical = float(data.get('vertical_speed', 0.8))
                horizontal = float(data.get('horizontal_speed', 1.0))
                smoothness = float(data.get('smoothness', 3.0))
                # Update the bye_runner speeds via Blackboard
                self.server.bye_service._vertical_speed = vertical
                self.server.bye_service._horizontal_speed = horizontal
                self.server.bye_service._smoothness = smoothness
                if hasattr(self.server, 'bye_runner'):
                    self.server.bye_runner._vertical_speed = vertical
                    self.server.bye_runner._horizontal_speed = horizontal
                    self.server.bye_runner._smoothness = smoothness
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.end_headers()
                self.wfile.write(json.dumps({"success": True}).encode())
            except Exception as e:
                self.send_response(500)
                self.send_header("Content-Type", "application/json")
                self.end_headers()
                self.wfile.write(json.dumps({"success": False, "error": str(e)}).encode())
        elif self.path == "/api/get_speeds":
            try:
                vertical = self.server.bye_service._vertical_speed
                horizontal = self.server.bye_service._horizontal_speed
                smoothness = self.server.bye_service._smoothness
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.end_headers()
                self.wfile.write(json.dumps({
                    "vertical_speed": vertical,
                    "horizontal_speed": horizontal,
                    "smoothness": smoothness
                }).encode())
            except Exception as e:
                self.send_response(500)
                self.send_header("Content-Type", "application/json")
                self.end_headers()
                self.wfile.write(json.dumps({"error": str(e)}).encode())
        elif self.path == "/api/save_to_config":
            content_length = int(self.headers.get('Content-Length', 0))
            body = self.rfile.read(content_length)
            try:
                data = json.loads(body)
                vertical = float(data.get('vertical_speed', 0.8))
                horizontal = float(data.get('horizontal_speed', 1.0))
                smoothness = float(data.get('smoothness', 3.0))
                
                # Read current config
                config_path = pathlib.Path(__file__).resolve().parent.parent / "config.yaml"
                with open(config_path, 'r') as f:
                    config_text = f.read()
                
                # Use regex to update only the talk_gesture section values
                # This preserves all comments, formatting, and other sections
                import re
                
                # Update vertical_speed
                config_text = re.sub(
                    r'(talk_gesture:.*?vertical_speed:\s*)[0-9.]+',
                    f'\\g<1>{vertical}',
                    config_text,
                    flags=re.DOTALL
                )
                
                # Update horizontal_speed  
                config_text = re.sub(
                    r'(talk_gesture:.*?horizontal_speed:\s*)[0-9.]+',
                    f'\\g<1>{horizontal}',
                    config_text,
                    flags=re.DOTALL
                )
                
                # Update smoothness (add if doesn't exist)
                if 'smoothness:' in config_text:
                    config_text = re.sub(
                        r'(talk_gesture:.*?smoothness:\s*)[0-9.]+',
                        f'\\g<1>{smoothness}',
                        config_text,
                        flags=re.DOTALL
                    )
                else:
                    # Add smoothness after horizontal_speed
                    config_text = re.sub(
                        r'(talk_gesture:.*?horizontal_speed:\s*[0-9.]+[^\n]*\n)',
                        f'\\g<1>  smoothness: {smoothness}              # Easing power for smooth motion (1.0=linear, 3.0=cubic, 5.0=quintic)\n',
                        config_text,
                        flags=re.DOTALL
                    )
                
                # Write back
                with open(config_path, 'w') as f:
                    f.write(config_text)
                
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.end_headers()
                self.wfile.write(json.dumps({"success": True}).encode())
            except Exception as e:
                self.send_response(500)
                self.send_header("Content-Type", "application/json")
                self.end_headers()
                self.wfile.write(json.dumps({"success": False, "error": str(e)}).encode())
        else:
            self.send_error(404)

    def log_message(self, format, *args):
        pass


class _HandNearFaceDetector:
    """Tracks palm position and triggers when hand is near the face."""

    def __init__(self, bb, trigger_distance_px=110, trigger_callback=None):
        self._bb = bb
        self.trigger_distance_px = trigger_distance_px
        self.trigger_callback = trigger_callback
        self.announcement_end_time = 0.0
        self.announcement_hand = ""
        self.hand_states = {
            side: {
                "x_history": collections.deque(maxlen=25),
                "y_history": collections.deque(maxlen=25),
                "last_seen": 0.0,
                "is_near_face": False,
                "distance": 999.0,
                "intensity": 0.0,
            }
            for side in ("Left", "Right")
        }

    def process(self, detections, now, cooldown_until, frame_shape):
        fh, fw = frame_shape[:2]
        face_detected = self._bb.read("face_detected")["face_detected"]
        face_px = None
        if face_detected:
            face_norm_x = self._bb.read("face_norm_x")["face_norm_x"]
            face_norm_y = self._bb.read("face_norm_y")["face_norm_y"]
            face_px = (
                int((face_norm_x + 1.0) * 0.5 * fw),
                int((face_norm_y + 1.0) * 0.5 * fh)
            )

        for side in ("Left", "Right"):
            if now - self.hand_states[side]["last_seen"] > 0.4:
                st = self.hand_states[side]
                st["x_history"].clear()
                st["y_history"].clear()
                st["is_near_face"] = False
                st["distance"] = 999.0
                st["intensity"] = 0.0

        for hand in detections:
            side = hand.physical_side
            state = self.hand_states[side]
            state["last_seen"] = now
            px, py = hand.palm_center

            if hand.is_frontside:
                state["x_history"].append(px)
                state["y_history"].append(py)
            else:
                state["x_history"].clear()
                state["y_history"].clear()

            is_near = False
            dist = 999.0
            if face_px:
                dist = ((px - face_px[0])**2 + (py - face_px[1])**2)**0.5
                state["distance"] = dist
                if dist < self.trigger_distance_px:
                    is_near = True

            state["is_near_face"] = is_near
            if dist < self.trigger_distance_px * 2:
                state["intensity"] = max(0.0, 1.0 - (dist / (self.trigger_distance_px * 2)))
            else:
                state["intensity"] = 0.0

            if is_near and now > cooldown_until:
                self.announcement_end_time = now + 3.0
                self.announcement_hand = side
                if self.trigger_callback is not None:
                    self.trigger_callback(side)


class _ByeSequenceRunner:
    """Plays a random bye animation by writing arm frames to the Blackboard.
    ServoMixer picks up arm_a0..arm_a3 and sends to ESP32 automatically.
    bye_wave_active=True on Blackboard pauses ArmController lean updates.
    Now with dual-speed interpolation for natural movements.
    """
    def __init__(self, bb, presets_path, cooldown_sec=10.0, on_complete=None, envelope=None,
                 vertical_speed=0.8, horizontal_speed=1.5, smoothness=3.0):
        self._bb = bb
        self._presets_path = presets_path
        self._cooldown_sec = cooldown_sec
        self._on_complete = on_complete
        self._envelope = envelope
        self._vertical_speed = vertical_speed  # Speed for a0, a1
        self._horizontal_speed = horizontal_speed  # Speed for a2, a3
        self._smoothness = smoothness  # Easing power (1.0=linear, 3.0=cubic, 5.0=quintic)
        self._lock = threading.Lock()
        self._thread = None
        self._running = False

    @property
    def is_running(self):
        return self._running
    
    def _ease_in_out(self, t):
        """Smooth ease-in-out function for continuous motion.
        
        Uses configurable easing power for adjustable smoothness.
        Input t should be in range [0, 1], output is also [0, 1].
        
        This prevents jerky starts/stops and creates fluid motion.
        Higher smoothness = more gradual ease, lower = more linear.
        """
        power = self._smoothness
        if t < 0.5:
            # Ease in (accelerate)
            return pow(2, power - 1) * pow(t, power)
        else:
            # Ease out (decelerate)
            return 1 - pow(-2 * t + 2, power) / pow(2, power - 1)

    def trigger(self, side):
        with self._lock:
            if self._running:
                return
            try:
                data = json.loads(self._presets_path.read_text(encoding="utf-8"))
                animations = data["animations"]
                key = random.choice(["bye1", "bye2", "bye3"])
                frames = animations[key]["frames"]
                if not frames:
                    raise ValueError(f"Animation '{key}' has no frames")
            except FileNotFoundError as exc:
                print(f"[ByeWaveService] ERROR: presets not found: {exc}", file=sys.stderr)
                return
            except (json.JSONDecodeError, KeyError, ValueError) as exc:
                print(f"[ByeWaveService] ERROR: bad presets data: {exc}", file=sys.stderr)
                return
            print(f"[ByeWaveService] Wave by {side} -- playing '{key}' ({len(frames)} frames)")
            self._running = True
            self._bb.write(bye_wave_active=True, bye_animation_name=key)
            self._thread = threading.Thread(
                target=self._run_animation, args=(frames,), daemon=True, name="ByeAnimation"
            )
            self._thread.start()

    def _run_animation(self, frames):
        """Play animation frames with smooth dual-speed interpolation.
        
        Speed values work intuitively:
        - Higher values (e.g., 2.0) = faster movements
        - Lower values (e.g., 0.5) = slower movements
        - 1.0 = normal speed
        
        Uses high-frequency updates (100Hz) for continuous smooth motion.
        """
        base_frame_duration = 0.25  # Base time to reach each frame
        poll_interval = 0.01  # 100 Hz update rate for smoother motion (was 0.02 = 50Hz)
        
        # Write animation state to Blackboard
        self._bb.write(
            bye_animation_playing=True,
            bye_animation_total_frames=len(frames),
            bye_animation_current_frame=0
        )
        
        for frame_idx, f in enumerate(frames):
            # Update current frame index
            self._bb.write(bye_animation_current_frame=frame_idx + 1)
            
            target_a0, target_a1, target_a2, target_a3 = f["a0"], f["a1"], f["a2"], f["a3"]
            
            # Apply safety envelope if available
            if self._envelope is not None:
                target_a0, target_a1, target_a2, target_a3 = self._envelope.clamp_arms(
                    target_a0, target_a1, target_a2, target_a3
                )
            
            # Get current arm positions
            current = self._bb.read("arm_a0", "arm_a1", "arm_a2", "arm_a3")
            start_a0 = current["arm_a0"]
            start_a1 = current["arm_a1"]
            start_a2 = current["arm_a2"]
            start_a3 = current["arm_a3"]
            
            # Calculate deltas
            delta_a0 = target_a0 - start_a0
            delta_a1 = target_a1 - start_a1
            delta_a2 = target_a2 - start_a2
            delta_a3 = target_a3 - start_a3
            
            # Calculate durations based on speed (INVERSE relationship for intuitive control)
            # Lower speed = longer duration = slower movement
            # Higher speed = shorter duration = faster movement
            vertical_duration = base_frame_duration / max(0.1, self._vertical_speed)
            horizontal_duration = base_frame_duration / max(0.1, self._horizontal_speed)
            
            # Use the longer duration as the frame duration to ensure smooth movement
            frame_duration = max(vertical_duration, horizontal_duration)
            
            # Smooth interpolation with dual speeds and easing
            start_time = time.time()
            
            while True:
                elapsed = time.time() - start_time
                if elapsed >= frame_duration:
                    break
                
                # Calculate progress for each motor group based on their respective durations
                vertical_t = min(1.0, elapsed / vertical_duration)
                horizontal_t = min(1.0, elapsed / horizontal_duration)
                
                # Apply ease-in-out for smoother acceleration/deceleration
                # This prevents jerky starts and stops
                vertical_progress = self._ease_in_out(vertical_t)
                horizontal_progress = self._ease_in_out(horizontal_t)
                
                # Interpolate with eased progress
                new_a0 = start_a0 + delta_a0 * vertical_progress
                new_a1 = start_a1 + delta_a1 * vertical_progress
                new_a2 = start_a2 + delta_a2 * horizontal_progress
                new_a3 = start_a3 + delta_a3 * horizontal_progress
                
                self._bb.write(arm_a0=new_a0, arm_a1=new_a1, arm_a2=new_a2, arm_a3=new_a3)
                time.sleep(poll_interval)
            
            # Ensure we reach the target
            self._bb.write(arm_a0=target_a0, arm_a1=target_a1, arm_a2=target_a2, arm_a3=target_a3)
        
        # Clear animation state
        self._bb.write(
            bye_animation_playing=False,
            bye_animation_current_frame=0
        )
        
        if self._on_complete is not None:
            self._on_complete()
        self._bb.write(bye_wave_active=False)
        with self._lock:
            self._running = False

    def join(self, timeout=2.0):
        if self._thread is not None:
            self._thread.join(timeout=timeout)


def _draw_gauge(frame, x, y, width, score):
    h = 8
    cv2.rectangle(frame, (x, y), (x + width, y + h), (40, 40, 50), -1)
    fill = int(width * score)
    if fill > 0:
        color = (int(255 * score), int(255 * (1.0 - score * 0.2)), int(100 * (1.0 - score)))
        cv2.rectangle(frame, (x, y), (x + fill, y + h), color, -1)
    cv2.rectangle(frame, (x, y), (x + width, y + h), (120, 120, 140), 1)


def _draw_hud(frame, hand_states, announcement_hand, announcement_end_time,
              cooldown_until, fps, now, arm_positions=None, animation_state=None):
    fh, fw = frame.shape[:2]
    ov = frame.copy()
    cv2.rectangle(ov, (0, 0), (fw, 26), (15, 15, 20), -1)
    cv2.line(ov, (0, 26), (fw, 26), (255, 106, 0), 1)
    cv2.addWeighted(ov, 0.75, frame, 0.25, 0, frame)
    cv2.putText(frame, "HAND/BYE DETECTOR", (5, 17),
                cv2.FONT_HERSHEY_DUPLEX, 0.36, (255, 255, 255), 1, cv2.LINE_AA)
    cv2.putText(frame, f"FPS:{fps:.0f}", (fw - 52, 17),
                cv2.FONT_HERSHEY_SIMPLEX, 0.33, (0, 255, 120), 1, cv2.LINE_AA)
    rem = cooldown_until - now
    if rem > 0:
        cv2.putText(frame, f"COOLDOWN {rem:.1f}s", (fw // 2 - 42, 42),
                    cv2.FONT_HERSHEY_SIMPLEX, 0.33, (255, 106, 0), 1, cv2.LINE_AA)
    
    # Draw arm positions overlay (top right area)
    if arm_positions is not None:
        arm_x = fw - 130
        arm_y = 35
        arm_w, arm_h = 125, 70
        ov_arm = frame.copy()
        cv2.rectangle(ov_arm, (arm_x, arm_y), (arm_x + arm_w, arm_y + arm_h), (20, 20, 30), -1)
        cv2.rectangle(ov_arm, (arm_x, arm_y), (arm_x + arm_w, arm_y + arm_h), (0, 200, 255), 1)
        cv2.addWeighted(ov_arm, 0.8, frame, 0.2, 0, frame)
        
        cv2.putText(frame, "ARM POSITIONS", (arm_x + 3, arm_y + 12),
                    cv2.FONT_HERSHEY_SIMPLEX, 0.28, (0, 200, 255), 1, cv2.LINE_AA)
        
        a0 = arm_positions.get("arm_a0", 0.0)
        a1 = arm_positions.get("arm_a1", 0.0)
        a2 = arm_positions.get("arm_a2", 0.0)
        a3 = arm_positions.get("arm_a3", 0.0)
        
        cv2.putText(frame, f"a0:{a0:5.1f}  a1:{a1:5.1f}", (arm_x + 3, arm_y + 28),
                    cv2.FONT_HERSHEY_SIMPLEX, 0.26, (200, 200, 200), 1, cv2.LINE_AA)
        cv2.putText(frame, f"a2:{a2:5.1f}  a3:{a3:5.1f}", (arm_x + 3, arm_y + 43),
                    cv2.FONT_HERSHEY_SIMPLEX, 0.26, (200, 200, 200), 1, cv2.LINE_AA)
        
        # Show motor type labels
        cv2.putText(frame, "vertical(slow)", (arm_x + 3, arm_y + 55),
                    cv2.FONT_HERSHEY_SIMPLEX, 0.22, (150, 150, 150), 1, cv2.LINE_AA)
        cv2.putText(frame, "horizontal", (arm_x + 3, arm_y + 67),
                    cv2.FONT_HERSHEY_SIMPLEX, 0.22, (150, 150, 150), 1, cv2.LINE_AA)
    
    # Draw animation state overlay (center top)
    if animation_state is not None and animation_state.get("is_playing"):
        anim_name = animation_state.get("animation", "unknown")
        frame_idx = animation_state.get("frame", 0)
        total_frames = animation_state.get("total_frames", 0)
        
        msg = f"ANIM: {anim_name} [{frame_idx}/{total_frames}]"
        ts = cv2.getTextSize(msg, cv2.FONT_HERSHEY_SIMPLEX, 0.35, 1)[0]
        tx = max(0, (fw - ts[0]) // 2)
        ty = 50
        
        # Background box
        pad = 5
        ov_anim = frame.copy()
        cv2.rectangle(ov_anim, (tx - pad, ty - 15), (tx + ts[0] + pad, ty + 5), (20, 20, 30), -1)
        cv2.rectangle(ov_anim, (tx - pad, ty - 15), (tx + ts[0] + pad, ty + 5), (255, 200, 0), 1)
        cv2.addWeighted(ov_anim, 0.85, frame, 0.15, 0, frame)
        
        cv2.putText(frame, msg, (tx, ty), cv2.FONT_HERSHEY_SIMPLEX, 0.35,
                    (255, 200, 0), 1, cv2.LINE_AA)
    
    for side, bx in (("Left", 5), ("Right", fw - 82)):
        state = hand_states[side]
        seen = (now - state["last_seen"]) < 0.4
        by = fh - 48
        bw_box, bh_box = 77, 43
        ov2 = frame.copy()
        cv2.rectangle(ov2, (bx, by), (bx + bw_box, by + bh_box), (20, 20, 30), -1)
        cv2.rectangle(ov2, (bx, by), (bx + bw_box, by + bh_box), (255, 106, 0), 1)
        cv2.addWeighted(ov2, 0.8, frame, 0.2, 0, frame)
        cv2.putText(frame, f"{side[0]} HAND", (bx + 3, by + 11),
                    cv2.FONT_HERSHEY_SIMPLEX, 0.28, (255, 255, 255), 1, cv2.LINE_AA)
        if seen:
            col = (0, 255, 150)
            lbl = "TRACKING"
            if now < cooldown_until:
                lbl, col = "COOLDOWN...", (0, 165, 255)
            elif state["is_near_face"]:
                lbl, col = "TRIGGERED!", (255, 255, 0)
            elif state["distance"] < 180:
                lbl, col = "NEAR FACE", (0, 255, 255)
            cv2.putText(frame, lbl, (bx + 3, by + 22),
                        cv2.FONT_HERSHEY_SIMPLEX, 0.26, col, 1, cv2.LINE_AA)
            cv2.putText(frame, f"dist:{state['distance']:.0f}px",
                        (bx + 3, by + 32), cv2.FONT_HERSHEY_SIMPLEX, 0.24, (200, 200, 200), 1)
            _draw_gauge(frame, bx + 3, by + 36, 71, state["intensity"])
        else:
            cv2.putText(frame, "NO HAND", (bx + 3, by + 26),
                        cv2.FONT_HERSHEY_SIMPLEX, 0.26, (100, 100, 110), 1, cv2.LINE_AA)
    if now < announcement_end_time:
        msg = f"BYE WAVE! ({announcement_hand})"
        ts = cv2.getTextSize(msg, cv2.FONT_HERSHEY_DUPLEX, 0.5, 2)[0]
        tx = max(0, (fw - ts[0]) // 2)
        ty = fh // 2 + ts[1] // 2
        cv2.putText(frame, msg, (tx + 1, ty + 1), cv2.FONT_HERSHEY_DUPLEX, 0.5,
                    (0, 0, 0), 3, cv2.LINE_AA)
        cv2.putText(frame, msg, (tx, ty), cv2.FONT_HERSHEY_DUPLEX, 0.5,
                    (255, 255, 0), 2, cv2.LINE_AA)


class ByeWaveService:
    """Bye-wave gesture service for the main robot stack.

    Reads stream_frame from the Blackboard (written by FaceTracker) so it
    does not conflict with the existing Picamera2 instance.

    Arm animations write to arm_a0..arm_a3 on the Blackboard so ServoMixer
    sends them to the ESP32 through the normal path.
    """

    def __init__(self, bb: Blackboard, config: dict) -> None:
        self._bb = bb
        bw_cfg = config.get("bye_wave", {}) or {}
        arms_cfg = config.get("arms", {}) or {}
        cam_cfg = config.get("camera", {}) or {}
        talk_cfg = config.get("talk_gesture", {}) or {}

        self._cooldown_sec = float(bw_cfg.get("cooldown_sec", 10.0))
        self._max_hands = int(bw_cfg.get("max_hands", 2))
        self._mjpeg_host = str(bw_cfg.get("host", "0.0.0.0"))
        self._mjpeg_port = int(bw_cfg.get("port", 8000))
        # FaceTracker swaps R/B when stream_swap_rb=true; undo that for CV/MediaPipe
        self._swap_rb: bool = bool(cam_cfg.get("stream_swap_rb", True))
        
        # Read dual-speed settings from talk_gesture config
        self._vertical_speed = float(talk_cfg.get("vertical_speed", 0.8))
        self._horizontal_speed = float(talk_cfg.get("horizontal_speed", 1.5))
        self._smoothness = float(talk_cfg.get("smoothness", 3.0))

        app_dir = pathlib.Path(__file__).resolve().parent.parent
        raw = bw_cfg.get("presets_path", "tests/arm_pose_presets.json")
        self._presets_path = pathlib.Path(raw)
        if not self._presets_path.is_absolute():
            self._presets_path = app_dir / self._presets_path

        self._envelope = None
        try:
            from arm_safety_envelope import ArmSafetyEnvelope
            limits_raw = arms_cfg.get("limits_path", "tests/captured_arm_limits.json")
            limits_path = pathlib.Path(limits_raw)
            if not limits_path.is_absolute():
                limits_path = app_dir / limits_path
            self._envelope = ArmSafetyEnvelope.from_json(limits_path)
            print("[ByeWaveService] ArmSafetyEnvelope loaded.")
        except Exception as exc:
            print(f"[ByeWaveService] WARNING: safety envelope unavailable ({exc}).",
                  file=sys.stderr)

    def run(self) -> None:
        """Main loop. Blocks until bb.running is False."""
        global _latest_frame

        server = _ThreadingHTTPServer((self._mjpeg_host, self._mjpeg_port), _MJPEGHandler)
        # Attach references for API handlers
        server.bye_service = self
        
        srv_thread = threading.Thread(
            target=server.serve_forever, daemon=True, name="ByeWaveMJPEG"
        )
        srv_thread.start()
        try:
            local_ip = socket.gethostbyname(socket.gethostname())
        except Exception:
            local_ip = "localhost"
        print(f"[ByeWaveService] Hand stream -> http://{local_ip}:{self._mjpeg_port}/")

        detector = HandDetector(max_num_hands=self._max_hands)

        bye_runner = _ByeSequenceRunner(
            bb=self._bb,
            presets_path=self._presets_path,
            cooldown_sec=self._cooldown_sec,
            envelope=self._envelope,
            vertical_speed=self._vertical_speed,
            horizontal_speed=self._horizontal_speed,
            smoothness=self._smoothness,
        )
        # Attach runner to server for API access
        server.bye_runner = bye_runner
        
        waving_detector = _HandNearFaceDetector(bb=self._bb, trigger_callback=bye_runner.trigger)

        def _on_complete() -> None:
            until = time.time() + self._cooldown_sec
            self._bb.write(bye_wave_cooldown_until=until)
            print(f"[ByeWaveService] Bye done -- cooldown {self._cooldown_sec:.0f}s active.")

        bye_runner._on_complete = _on_complete

        # Initialize animation state variables on Blackboard
        self._bb.write(
            bye_animation_playing=False,
            bye_animation_name="",
            bye_animation_current_frame=0,
            bye_animation_total_frames=0
        )

        prev_time = time.time()
        fps = 0.0
        last_frame_token = -1

        print("[ByeWaveService] Running -- bring your hand near your face to trigger a bye animation.")

        while self._bb.read("running")["running"]:
            raw = self._bb.read("stream_frame")["stream_frame"]
            if raw is None:
                time.sleep(0.03)
                continue
            frame_token = id(raw)
            if frame_token == last_frame_token:
                time.sleep(0.02)
                continue
            last_frame_token = frame_token

            # Undo FaceTracker's R/B swap so OpenCV and MediaPipe get BGR
            if self._swap_rb:
                frame = cv2.cvtColor(raw, cv2.COLOR_RGB2BGR)
            else:
                frame = raw.copy()

            now = time.time()
            detections = detector.process(frame, mirrored=False)

            annotated = frame.copy()
            for hand in detections:
                is_waving = waving_detector.hand_states[hand.physical_side]["is_near_face"]
                draw_skeleton(annotated, hand, is_active=is_waving)
                st = waving_detector.hand_states[hand.physical_side]
                draw_motion_trail(
                    annotated, list(st["x_history"]), list(st["y_history"]),
                    is_active=is_waving,
                )

            cooldown_until = self._bb.read("bye_wave_cooldown_until")["bye_wave_cooldown_until"]
            waving_detector.process(detections, now, cooldown_until, frame.shape)

            # Read arm positions and animation state for overlay
            arm_data = self._bb.read("arm_a0", "arm_a1", "arm_a2", "arm_a3")
            anim_data = self._bb.read(
                "bye_animation_playing",
                "bye_animation_name", 
                "bye_animation_current_frame",
                "bye_animation_total_frames"
            )
            
            # Build animation state dict
            animation_state = None
            if anim_data.get("bye_animation_playing"):
                animation_state = {
                    "is_playing": True,
                    "animation": anim_data.get("bye_animation_name", "unknown"),
                    "frame": anim_data.get("bye_animation_current_frame", 0),
                    "total_frames": anim_data.get("bye_animation_total_frames", 0)
                }

            fps_now = time.time()
            fps = 1.0 / max(fps_now - prev_time, 1e-6)
            prev_time = fps_now

            _draw_hud(
                annotated, waving_detector.hand_states,
                waving_detector.announcement_hand, waving_detector.announcement_end_time,
                cooldown_until, fps, now,
                arm_positions=arm_data,
                animation_state=animation_state
            )

            with _frame_lock:
                _latest_frame = annotated

        bye_runner.join(timeout=2.0)
        detector.close()
        server.shutdown()
        print("[ByeWaveService] Stopped.")
