import re

with open('e:/Git/voice-agentv5/core/bye_wave_service.py', 'r', encoding='utf-8') as f:
    content = f.read()

detector_class = '''class _HandNearFaceDetector:
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
'''

content = re.sub(r'class _WavingDetector:.*?class _ByeSequenceRunner:', detector_class + '\n\nclass _ByeSequenceRunner:', content, flags=re.DOTALL)

hud_old = '''        if seen:
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
            _draw_gauge(frame, bx + 3, by + 36, 71, state["intensity"])'''

hud_new = '''        if seen:
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
            _draw_gauge(frame, bx + 3, by + 36, 71, state["intensity"])'''

content = content.replace(hud_old, hud_new)

content = content.replace('waving_detector = _WavingDetector(wave_callback=bye_runner.trigger)', 'waving_detector = _HandNearFaceDetector(bb=self._bb, trigger_callback=bye_runner.trigger)')
content = content.replace('waving_detector.process(detections, now, cooldown_until)', 'waving_detector.process(detections, now, cooldown_until, frame.shape)')
content = content.replace('waving_detector.hand_states[hand.physical_side]["is_waving"]', 'waving_detector.hand_states[hand.physical_side]["is_near_face"]')

content = content.replace('4 swings triggers bye animation.', 'Hand near face triggers bye animation.')
content = content.replace('Running -- wave your hand to trigger a bye animation.', 'Running -- bring your hand near your face to trigger a bye animation.')

with open('e:/Git/voice-agentv5/core/bye_wave_service.py', 'w', encoding='utf-8') as f:
    f.write(content)
print('Done!')
