"""Tests for lib/motion_curves.py."""

import math
import unittest
from lib.motion_curves import (
    minjerk,
    minjerk_velocity,
    cartoon,
    ease_in_out,
    ease_out_cubic,
    biological_respiration,
    Trajectory,
)


class TestMotionCurves(unittest.TestCase):
    def test_minjerk_boundaries(self):
        self.assertEqual(minjerk(0.0), 0.0)
        self.assertEqual(minjerk(1.0), 1.0)
        self.assertEqual(minjerk(-0.5), 0.0)
        self.assertEqual(minjerk(1.5), 1.0)

    def test_minjerk_symmetry(self):
        self.assertAlmostEqual(minjerk(0.5), 0.5)
        for t in [0.1, 0.25, 0.4]:
            self.assertAlmostEqual(minjerk(t), 1.0 - minjerk(1.0 - t), places=5)

    def test_minjerk_monotonic(self):
        steps = 50
        prev = 0.0
        for i in range(1, steps + 1):
            t = i / steps
            val = minjerk(t)
            self.assertGreaterEqual(val, prev)
            prev = val

    def test_minjerk_velocity(self):
        self.assertEqual(minjerk_velocity(0.0), 0.0)
        self.assertEqual(minjerk_velocity(1.0), 0.0)
        self.assertAlmostEqual(minjerk_velocity(0.5), 1.875)

    def test_cartoon_anticipation_and_overshoot(self):
        self.assertEqual(cartoon(0.0), 0.0)
        self.assertEqual(cartoon(1.0), 1.0)

        # In anticipation phase (t ~ 0.06), output should dip slightly below 0
        self.assertLess(cartoon(0.06, anticipation=0.08), 0.0)

        # In overshoot phase (t ~ 0.75), output should exceed 1.0
        self.assertGreater(cartoon(0.75, overshoot=0.15), 1.0)

    def test_biological_respiration(self):
        period = 4.0
        # Over a full cycle, min should be around -1.0 and max around +1.0
        samples = [biological_respiration(t * 0.1, period=period) for t in range(41)]
        self.assertAlmostEqual(min(samples), -1.0, places=1)
        self.assertAlmostEqual(max(samples), 1.0, places=1)

        # Value should be bounded
        for s in samples:
            self.assertGreaterEqual(s, -1.0001)
            self.assertLessEqual(s, 1.0001)

    def test_trajectory_generator(self):
        traj = Trajectory(start=10.0, target=50.0, duration=1.0, curve="minjerk")
        self.assertEqual(traj.current, 10.0)
        self.assertFalse(traj.is_done)

        val, done = traj.tick(0.5)
        self.assertAlmostEqual(val, 30.0)
        self.assertFalse(done)

        val, done = traj.tick(0.5)
        self.assertAlmostEqual(val, 50.0)
        self.assertTrue(done)
        self.assertEqual(traj.current, 50.0)

        # Additional ticks should stay at target
        val, done = traj.tick(0.2)
        self.assertEqual(val, 50.0)
        self.assertTrue(done)


if __name__ == "__main__":
    unittest.main()
