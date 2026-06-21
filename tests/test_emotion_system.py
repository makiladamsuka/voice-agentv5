"""Tests for v4 emotion presets and surroundings controller."""

from __future__ import annotations

import time
import unittest

from core.emotion_presets import (
    EMOTION_INTENSITY,
    EMOTION_PRESETS,
    map_surroundings_emotion,
    resolve_emotion_name,
)
from core.surroundings_emotion import SurroundingsEmotionConfig, SurroundingsEmotionController


class TestEmotionPresets(unittest.TestCase):
    def test_v4_presets_present(self):
        for name in (
            "angry", "bored", "content", "curious_intense", "cheerful",
            "looking_left_cheerful", "looking_right_cheerful", "apologetic", "nodding",
        ):
            self.assertIn(name, EMOTION_PRESETS)
            self.assertIn(name, EMOTION_INTENSITY)

    def test_resolve_aliases(self):
        self.assertEqual(resolve_emotion_name("curious"), "curious_intense")
        self.assertEqual(resolve_emotion_name("calm"), "content")
        self.assertEqual(resolve_emotion_name("looking_left"), "looking_left_happy")
        self.assertEqual(resolve_emotion_name("afraid"), "uncertain")
        self.assertIsNone(resolve_emotion_name("not_a_real_emotion"))

    def test_map_surroundings(self):
        self.assertEqual(map_surroundings_emotion("calm"), "content")


class TestSurroundingsEmotion(unittest.TestCase):
    def test_directional_pick_when_face_off_center(self):
        ctrl = SurroundingsEmotionController(
            cfg=SurroundingsEmotionConfig(
                person_hold_min_sec=0.01,
                person_hold_max_sec=0.02,
                direction_trigger_norm_x=0.15,
            ),
        )
        ctrl.next_emotion_change_time = 0.0
        ctrl.direction_cooldown_until = 0.0
        now = time.time()
        picks = set()
        for _ in range(30):
            ctrl.next_emotion_change_time = 0.0
            pick = ctrl.tick(
                now=now,
                face_detected=True,
                face_area_ratio=0.03,
                face_norm_x=0.5,
                squint_hint=0.0,
                activity=0.5,
                wander_mode=False,
            )
            if pick:
                picks.add(pick)
        self.assertTrue(
            any(p.startswith("looking_") for p in picks),
            f"expected directional look in picks, got {picks}",
        )

    def test_far_zone_can_pick_squint(self):
        ctrl = SurroundingsEmotionController(
            cfg=SurroundingsEmotionConfig(
                person_hold_min_sec=0.01,
                person_hold_max_sec=0.02,
                far_face_area_ratio=0.02,
            ),
        )
        ctrl.next_emotion_change_time = 0.0
        picks = set()
        now = time.time()
        for _ in range(40):
            ctrl.next_emotion_change_time = 0.0
            pick = ctrl.tick(
                now=now,
                face_detected=True,
                face_area_ratio=0.01,
                face_norm_x=0.0,
                squint_hint=1.0,
                activity=0.2,
                wander_mode=False,
            )
            if pick:
                picks.add(pick)
        self.assertIn("squint", picks)


if __name__ == "__main__":
    unittest.main()
