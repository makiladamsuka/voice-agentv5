"""Tests for core/motion_mixer.py."""

import unittest
from core.blackboard import Blackboard
from core.motion_mixer import MotionMixer


class TestMotionMixer(unittest.TestCase):
    def setUp(self):
        self.bb = Blackboard()
        self.mixer = MotionMixer(self.bb, breathing_tilt_amp=1.5, breathing_pan_amp=0.6)

    def test_idle_breathing_generates_deltas(self):
        deltas = self.mixer.compute_deltas(now=10.0)
        self.assertIn("breathing_delta_tilt", deltas)
        self.assertIn("breathing_delta_pan", deltas)
        self.assertIn("breathing_delta_base", deltas)
        # Should be non-zero at some point in time
        self.assertLessEqual(abs(deltas["breathing_delta_tilt"]), 1.5)
        self.assertLessEqual(abs(deltas["breathing_delta_pan"]), 0.6)

    def test_disabled_alive_motion_returns_zero(self):
        self.bb.write(motion_alive_enabled=False)
        deltas = self.mixer.compute_deltas(now=10.0)
        self.assertEqual(deltas["breathing_delta_tilt"], 0.0)
        self.assertEqual(deltas["breathing_delta_pan"], 0.0)
        self.assertEqual(deltas["breathing_delta_base"], 0.0)
        self.assertEqual(deltas["speech_wobble_delta_tilt"], 0.0)

    def test_speech_reactive_wobble(self):
        self.bb.write(agent_speaking=True, amplitude_fast=0.5)
        deltas = self.mixer.compute_deltas(now=10.0)
        # Tilt should show a downward nod offset
        self.assertLess(deltas["speech_wobble_delta_tilt"], 0.0)

    def test_base_sway_suppressed_when_busy(self):
        self.bb.write(base_motion_busy=True)
        deltas = self.mixer.compute_deltas(now=10.0)
        self.assertEqual(deltas["breathing_delta_base"], 0.0)

    def test_blended_pose(self):
        pan, tilt = self.mixer.get_blended_pose(80.0, 110.0)
        # Should stay close to commanded pose within breathing limits
        self.assertAlmostEqual(pan, 80.0, delta=2.0)
        self.assertAlmostEqual(tilt, 110.0, delta=2.0)

    def test_tick_writes_to_blackboard(self):
        self.mixer.tick(now=5.0)
        state = self.bb.read("breathing_delta_tilt", "breathing_delta_pan", "alive_blend_weight")
        self.assertIn("breathing_delta_tilt", state)
        self.assertGreater(state["alive_blend_weight"], 0.0)


if __name__ == "__main__":
    unittest.main()
