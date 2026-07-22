"""Unit tests for base-turn arm lean math."""

from __future__ import annotations

import unittest

import _bootstrap  # noqa: F401

from lib.arm_base_lean import (
    apply_base_lean,
    lean_delta_per_spin,
    lean_direction,
    lean_magnitude_deg,
)


HOME = (47.0, 65.0, 64.0, 87.0)


class TestLeanMagnitude(unittest.TestCase):
    def test_zero_step(self) -> None:
        self.assertEqual(lean_magnitude_deg(0.0, step_delta_deg=4.0), 0.0)

    def test_scales_with_step(self) -> None:
        half = lean_magnitude_deg(4.0, step_delta_deg=4.0, ref_step_deg=8.0)
        self.assertAlmostEqual(half, 2.0)
        full = lean_magnitude_deg(16.0, step_delta_deg=4.0, ref_step_deg=8.0)
        self.assertAlmostEqual(full, 4.0)


class TestLeanDirection(unittest.TestCase):
    def test_right_turn_positive_step(self) -> None:
        self.assertEqual(lean_direction(10.0, turn_sign=1.0), 1.0)

    def test_left_turn_negative_step(self) -> None:
        self.assertEqual(lean_direction(-10.0, turn_sign=1.0), -1.0)


class TestLeanDeltaPerSpin(unittest.TestCase):
    def test_right_turn_delta_signs(self) -> None:
        d = lean_delta_per_spin(8.0, step_delta_deg=4.0, turn_sign=1.0)
        self.assertGreater(d[0], 0.0)
        self.assertLess(d[1], 0.0)
        self.assertLess(d[2], 0.0)
        self.assertEqual(d[3], 0.0)

    def test_left_turn_opposite(self) -> None:
        d = lean_delta_per_spin(-8.0, step_delta_deg=4.0, turn_sign=1.0)
        self.assertLess(d[0], 0.0)
        self.assertGreater(d[1], 0.0)
        self.assertGreater(d[2], 0.0)

    def test_accumulate_from_home(self) -> None:
        d = lean_delta_per_spin(8.0, step_delta_deg=4.0)
        pose = tuple(HOME[i] + d[i] for i in range(4))
        self.assertGreater(pose[0], HOME[0])
        self.assertLess(pose[1], HOME[1])


class TestApplyBaseLean(unittest.TestCase):
    def test_idle_returns_home(self) -> None:
        self.assertEqual(
            apply_base_lean(HOME, 0.0, max_delta_deg=4.0),
            HOME,
        )


if __name__ == "__main__":
    unittest.main()
