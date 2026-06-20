"""Life Force Emotion Engine.

Determines the robot's emotional state based on presence, distance, and activity.
Yields control if a conversation is active (voice agent takes over).
"""

import random
import time
from core.blackboard import Blackboard

# Constants from V5
CLOSE_FACE_AREA_RATIO = 0.05
FAR_FACE_AREA_RATIO = 0.018
NEAR_EXIT_RATIO = 0.041
FAR_EXIT_RATIO = 0.0225

NO_FACE_GRACE_SEC = 0.9
SOCIAL_MULTI_GRACE_SEC = 2.4

NO_PERSON_HOLD_MIN_SEC = 2.8
NO_PERSON_HOLD_MAX_SEC = 5.2
PERSON_HOLD_MIN_SEC = 1.1
PERSON_HOLD_MAX_SEC = 2.8

DIRECTION_TRIGGER_NORM_X = 0.25
DIRECTION_HOLD_MIN_SEC = 0.6
DIRECTION_HOLD_MAX_SEC = 1.2
DIRECTION_COOLDOWN_SEC = 0.9

EMOTION_HISTORY_LEN = 3


def classify_distance_zone(face_area_ratio, prev_zone):
    if prev_zone == "near" and face_area_ratio >= NEAR_EXIT_RATIO:
        return "near"
    if prev_zone == "far" and face_area_ratio <= FAR_EXIT_RATIO:
        return "far"
    if face_area_ratio >= CLOSE_FACE_AREA_RATIO:
        return "near"
    if face_area_ratio < FAR_FACE_AREA_RATIO:
        return "far"
    return "mid"


def weighted_pick(weights, fallback="idle"):
    total = 0.0
    cleaned = {}
    for name, w in weights.items():
        if w > 0.0:
            cleaned[name] = float(w)
            total += float(w)
    if total <= 0.0:
        return fallback
    r = random.uniform(0.0, total)
    acc = 0.0
    for name, w in cleaned.items():
        acc += w
        if r <= acc:
            return name
    return fallback


class EmotionEngine:
    def __init__(self, bb: Blackboard):
        self.bb = bb
        self.emotion_history = []
        self.distance_zone = "mid"
        self.no_person_next_emotion = "sleepy"
        self.next_emotion_change_time = time.time() + random.uniform(1.6, 3.0)
        self.direction_cooldown_until = 0.0
        
        self.last_seen_face_time = 0.0
        self.last_multi_face_time = 0.0
        
        self.prev_target_x = 0.0
        self.prev_target_y = 0.0
        self.prev_target_rot = 0.0

    def _push_emotion_history(self, emotion_name):
        self.emotion_history.append(emotion_name)
        if len(self.emotion_history) > EMOTION_HISTORY_LEN:
            self.emotion_history.pop(0)

    def _choose_no_person_emotion(self):
        base = self.no_person_next_emotion
        self.no_person_next_emotion = "idle" if self.no_person_next_emotion == "sleepy" else "sleepy"
        r = random.random()
        if r < 0.08:
            return "sad"
        if r < 0.14:
            return "calm"
        return base

    def _choose_person_emotion(self, zone, activity, squint_hint):
        current_emotion = self.bb.read("emotion")["emotion"]
        if zone == "near":
            weights = {
                "excited": 1.0, "happy": 0.55, "curious": 0.12, "calm": 0.12,
                "surprised": 0.09, "afraid": 0.06, "angry": 0.04,
            }
            if activity > 0.75:
                weights["surprised"] += 0.20
                weights["afraid"] += 0.08
            if activity < 0.25:
                weights["calm"] += 0.10
        elif zone == "far":
            weights = {
                "curious": 1.0, "happy": 0.25, "calm": 0.30, "squint": 0.22,
                "sad": 0.10, "sleepy": 0.08, "idle": 0.10,
            }
            if squint_hint > 0.5:
                weights["squint"] += 0.35
        else:
            weights = {
                "happy": 1.0, "excited": 0.22, "curious": 0.30, "calm": 0.28,
                "suspicious": 0.15, "surprised": 0.08, "angry": 0.04, "afraid": 0.03,
            }
            if activity > 0.8:
                weights["surprised"] += 0.20
            if activity < 0.2:
                weights["calm"] += 0.12

        for recent in self.emotion_history[-EMOTION_HISTORY_LEN:]:
            if recent in weights:
                weights[recent] *= 0.35
        if current_emotion in weights:
            weights[current_emotion] *= 0.45

        return weighted_pick(weights, fallback="happy")

    def run(self):
        print("Emotion engine started.")
        while self.bb.running:
            try:
                now = time.time()
                
                # Check if voice agent is driving emotion
                if self.bb.read("session_active")["session_active"]:
                    time.sleep(0.1)
                    continue

                state = self.bb.read(
                    "face_detected", "face_count", "body_detected", "track_kind",
                    "face_norm_x", "face_norm_y", "face_roll_deg", "face_area_ratio"
                )
                
                local_face_detected = state["face_detected"]
                local_face_count = state["face_count"]
                local_body_detected = state["body_detected"]
                local_target_kind = state["track_kind"]
                local_face_norm_x = state["face_norm_x"]
                local_face_norm_y = state["face_norm_y"]
                local_face_roll_deg = state["face_roll_deg"]
                local_face_area_ratio = state["face_area_ratio"]

                if local_face_detected:
                    self.last_seen_face_time = now
                if local_face_count > 1:
                    self.last_multi_face_time = now
                    
                person_present = (now - self.last_seen_face_time) <= NO_FACE_GRACE_SEC
                social_multi_present = (now - self.last_multi_face_time) <= SOCIAL_MULTI_GRACE_SEC

                # Activity heuristic
                dx_activity = abs(local_face_norm_x - self.prev_target_x)
                dy_activity = abs(local_face_norm_y - self.prev_target_y)
                dr_activity = abs(local_face_roll_deg - self.prev_target_rot) / 10.0
                activity = min(1.0, (dx_activity + dy_activity + dr_activity) / 3.0)
                
                self.prev_target_x = local_face_norm_x
                self.prev_target_y = local_face_norm_y
                self.prev_target_rot = local_face_roll_deg

                if now >= self.next_emotion_change_time:
                    if not person_present:
                        next_emotion = self._choose_no_person_emotion()
                        hold_sec = random.uniform(NO_PERSON_HOLD_MIN_SEC, NO_PERSON_HOLD_MAX_SEC)
                    elif local_body_detected or local_target_kind == "body":
                        next_emotion = weighted_pick({"attentive": 0.55, "calm": 0.30, "curious": 0.15}, fallback="attentive")
                        hold_sec = random.uniform(PERSON_HOLD_MIN_SEC, PERSON_HOLD_MAX_SEC)
                    elif local_target_kind == "center" or (social_multi_present and local_target_kind == "face"):
                        next_emotion = weighted_pick(
                            {"warm": 0.34, "engaged": 0.26, "attentive": 0.20, "happy": 0.12, "amused": 0.08},
                            fallback="warm"
                        )
                        hold_sec = random.uniform(2.6, 5.2)
                    elif local_target_kind == "multi" or local_face_count > 1 or social_multi_present:
                        if abs(local_face_norm_x) >= DIRECTION_TRIGGER_NORM_X:
                            next_emotion = weighted_pick(
                                {
                                    "warm": 0.34, "engaged": 0.28, "attentive": 0.18,
                                    "looking_left_natural" if local_face_norm_x > 0 else "looking_right_natural": 0.12,
                                    "amused": 0.08,
                                },
                                fallback="engaged"
                            )
                        else:
                            next_emotion = weighted_pick(
                                {"warm": 0.32, "engaged": 0.28, "attentive": 0.22, "happy": 0.10, "amused": 0.08},
                                fallback="engaged"
                            )
                        hold_sec = random.uniform(2.8, 5.8)
                        self.direction_cooldown_until = now + hold_sec + DIRECTION_COOLDOWN_SEC
                    else:
                        self.distance_zone = classify_distance_zone(local_face_area_ratio, self.distance_zone)
                        can_directional = now >= self.direction_cooldown_until and abs(local_face_norm_x) >= DIRECTION_TRIGGER_NORM_X
                        if can_directional and random.random() < (0.5 if self.distance_zone == "mid" else 0.35):
                            next_emotion = "looking_right" if local_face_norm_x > 0 else "looking_left"
                            hold_sec = random.uniform(DIRECTION_HOLD_MIN_SEC, DIRECTION_HOLD_MAX_SEC)
                            self.direction_cooldown_until = now + hold_sec + DIRECTION_COOLDOWN_SEC
                        else:
                            # Squint hint (1.0 if far away and randomly squinting)
                            squint_hint = 1.0 if local_face_area_ratio < FAR_FACE_AREA_RATIO and random.random() < 0.08 else 0.0
                            next_emotion = self._choose_person_emotion(self.distance_zone, activity, squint_hint)
                            hold_base = random.uniform(PERSON_HOLD_MIN_SEC, PERSON_HOLD_MAX_SEC)
                            hold_sec = max(PERSON_HOLD_MIN_SEC, hold_base * (1.12 - 0.42 * activity))

                    # Ensure we aren't overwriting an animation or conversation 
                    if not self.bb.read("session_active")["session_active"] and not self.bb.read("anim_active")["anim_active"]:
                        self.bb.write(emotion=next_emotion, emotion_source="life_force")
                        self._push_emotion_history(next_emotion)
                    
                    self.next_emotion_change_time = now + hold_sec
                    
            except Exception as e:
                print(f"Emotion engine error: {e}")
                
            time.sleep(0.1)  # 10Hz is plenty fast for emotions
