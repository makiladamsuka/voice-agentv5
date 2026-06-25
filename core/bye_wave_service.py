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
                "<title>Bye Wave Hand Stream</title>"
                "<style>body{background:#0f101a;color:#f1f5f9;font-family:sans-serif;"
                "text-align:center;padding:30px;margin:0}"
                "h1{color:#ff6a00;font-size:1.8rem}"
                ".box{display:inline-block;border-radius:12px;overflow:hidden;"
                "border:2px solid #ff6a00;box-shadow:0 0 25px rgba(255,106,0,.2)}"
                "img{display:block;max-width:100%;height:auto}</style></head>"
                "<body><h1>Wave Detector</h1>"
                "<p style='color:#94a3b8'>No height limit. 4 swings triggers bye animation.</p>"
                "<div class='box'><img src='/stream'/></div>"
                f"<p style='color:#555;font-size:.75rem'>http://{host_ip}:{port}/stream</p>"
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

    def log_message(self, format, *args):
        pass


class _WavingDetector:
    """Tracks palm history and fires wave_callback on a 4-swing bye-wave.
    No height gate. cooldown_until is passed in (read from Blackboard by caller).
    """
    def __init__(self, history_len=25, dead_zone_px=10, wave_callback=None):
        self.history_len = history_len
        self.dead_zone_px = dead_zone_px
        self.wave_callback = wave_callback
        self.announcement_end_time = 0.0
        self.announcement_hand = ""
        self.hand_states = {
            side: {
                "x_history": collections.deque(maxlen=history_len),
                "y_history": collections.deque(maxlen=history_len),
                "last_seen": 0.0,
                "is_waving": False,
                "reversals": 0,
                "amplitude": 0.0,
                "intensity": 0.0,
            }
            for side in ("Left", "Right")
        }

    def _detect_reversals(self, history):
        if len(history) < 6:
            return 0, 0.0
        reversals = 0
        anchor = history[0]
        direction = 0
        peaks = [history[0]]
        for val in history[1:]:
            diff = val - anchor
            if abs(diff) < self.dead_zone_px:
                continue
            new_dir = 1 if diff > 0 else -1
            if direction != 0 and new_dir != direction:
                reversals += 1
                peaks.append(anchor)
            direction = new_dir
            anchor = val
        return reversals, float(max(peaks) - min(peaks)) if peaks else 0.0

    def process(self, detections, now, cooldown_until):
        for side in ("Left", "Right"):
            if now - self.hand_states[side]["last_seen"] > 0.4:
                st = self.hand_states[side]
                st["x_history"].clear()
                st["y_history"].clear()
                st["is_waving"] = False
                st["reversals"] = 0
                st["amplitude"] = 0.0
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
            rev_x, amp_x = self._detect_reversals(list(state["x_history"]))
            state["reversals"] = rev_x
            state["amplitude"] = amp_x
            state["is_waving"] = rev_x >= 4 and amp_x >= 40.0
            state["intensity"] = min(1.0, amp_x / 160.0) * min(1.0, rev_x / 4.0)
            if rev_x >= 4 and amp_x >= 40.0 and now > cooldown_until:
                self.announcement_end_time = now + 3.0
                self.announcement_hand = side
                if self.wave_callback is not None:
                    self.wave_callback(side)


class _ByeSequenceRunner:
    """Plays a random bye animation by writing arm frames to the Blackboard.
    ServoMixer picks up arm_a0..arm_a3 and sends to ESP32 automatically.
    bye_wave_active=True on Blackboard pauses ArmController lean updates.
    """
    def __init__(self, bb, presets_path, cooldown_sec=10.0, on_complete=None, envelope=None):
        self._bb = bb
        self._presets_path = presets_path
        self._cooldown_sec = cooldown_sec
        self._on_complete = on_complete
        self._envelope = envelope
        self._lock = threading.Lock()
        self._thread = None
        self._running = False

    @property
    def is_running(self):
        return self._running

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
            self._bb.write(bye_wave_active=True)
            self._thread = threading.Thread(
                target=self._run_animation, args=(frames,), daemon=True, name="ByeAnimation"
            )
            self._thread.start()

    def _run_animation(self, frames):
        for f in frames:
            a0, a1, a2, a3 = f["a0"], f["a1"], f["a2"], f["a3"]
            if self._envelope is not None:
                a0, a1, a2, a3 = self._envelope.clamp_arms(a0, a1, a2, a3)
            self._bb.write(arm_a0=a0, arm_a1=a1, arm_a2=a2, arm_a3=a3)
            time.sleep(0.25)
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
              cooldown_until, fps, now):
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
            lbl = "WAVE ANYWHERE"
            if now < cooldown_until:
                lbl, col = "COOLDOWN...", (0, 165, 255)
            elif state["reversals"] >= 4:
                lbl, col = "TRIGGERED!", (255, 255, 0)
            elif state["reversals"] >= 3:
                lbl, col = f"SWINGS:{state['reversals']}/4", (0, 255, 255)
            cv2.putText(frame, lbl, (bx + 3, by + 22),
                        cv2.FONT_HERSHEY_SIMPLEX, 0.26, col, 1, cv2.LINE_AA)
            cv2.putText(frame, f"sw:{state['reversals']} {state['amplitude']:.0f}px",
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

        self._cooldown_sec = float(bw_cfg.get("cooldown_sec", 10.0))
        self._max_hands = int(bw_cfg.get("max_hands", 2))
        self._mjpeg_host = str(bw_cfg.get("host", "0.0.0.0"))
        self._mjpeg_port = int(bw_cfg.get("port", 8000))
        # FaceTracker swaps R/B when stream_swap_rb=true; undo that for CV/MediaPipe
        self._swap_rb: bool = bool(cam_cfg.get("stream_swap_rb", True))

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
        )
        waving_detector = _WavingDetector(wave_callback=bye_runner.trigger)

        def _on_complete() -> None:
            until = time.time() + self._cooldown_sec
            self._bb.write(bye_wave_cooldown_until=until)
            print(f"[ByeWaveService] Bye done -- cooldown {self._cooldown_sec:.0f}s active.")

        bye_runner._on_complete = _on_complete

        prev_time = time.time()
        fps = 0.0
        last_frame_token = -1

        print("[ByeWaveService] Running -- wave your hand to trigger a bye animation.")

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
                is_waving = waving_detector.hand_states[hand.physical_side]["is_waving"]
                draw_skeleton(annotated, hand, is_active=is_waving)
                st = waving_detector.hand_states[hand.physical_side]
                draw_motion_trail(
                    annotated, list(st["x_history"]), list(st["y_history"]),
                    is_active=is_waving,
                )

            cooldown_until = self._bb.read("bye_wave_cooldown_until")["bye_wave_cooldown_until"]
            waving_detector.process(detections, now, cooldown_until)

            fps_now = time.time()
            fps = 1.0 / max(fps_now - prev_time, 1e-6)
            prev_time = fps_now

            _draw_hud(
                annotated, waving_detector.hand_states,
                waving_detector.announcement_hand, waving_detector.announcement_end_time,
                cooldown_until, fps, now,
            )

            with _frame_lock:
                _latest_frame = annotated

        bye_runner.join(timeout=2.0)
        detector.close()
        server.shutdown()
        print("[ByeWaveService] Stopped.")
