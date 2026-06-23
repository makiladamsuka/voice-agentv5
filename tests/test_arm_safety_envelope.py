"""Unit tests for raise-dependent sweep safety envelope."""

from __future__ import annotations

import unittest
from pathlib import Path

import _bootstrap  # noqa: F401

from arm_safety_envelope import ArmSafetyEnvelope, DEFAULT_LIMITS_PATH


class TestArmSafetyEnvelope(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        if not DEFAULT_LIMITS_PATH.is_file():
            raise unittest.SkipTest(f"missing {DEFAULT_LIMITS_PATH}")
        cls.env = ArmSafetyEnvelope.from_json(DEFAULT_LIMITS_PATH)

    def test_homes_match_capture(self) -> None:
        self.assertEqual(self.env.homes, (47.0, 65.0, 64.0, 87.0))

    def test_right_sweep_at_low_raise(self) -> None:
        lo, hi = self.env.sweep_range(side="right", raise_deg=47.0)
        self.assertAlmostEqual(lo, 44.0)
        self.assertAlmostEqual(hi, 64.0)

    def test_right_sweep_at_high_raise(self) -> None:
        lo, hi = self.env.sweep_range(side="right", raise_deg=124.0)
        self.assertAlmostEqual(lo, 44.0)
        self.assertAlmostEqual(hi, 44.0)

    def test_left_sweep_at_home(self) -> None:
        lo, hi = self.env.sweep_range(side="left", raise_deg=65.0)
        self.assertAlmostEqual(lo, 87.0)
        self.assertAlmostEqual(hi, 102.0)

    def test_left_sweep_at_high_raise(self) -> None:
        lo, hi = self.env.sweep_range(side="left", raise_deg=6.0)
        self.assertAlmostEqual(lo, 70.0)
        self.assertAlmostEqual(hi, 70.0)

    def test_left_sweep_mid_raise(self) -> None:
        lo, hi = self.env.sweep_range(side="left", raise_deg=35.5)
        self.assertAlmostEqual(lo, 78.5)
        self.assertAlmostEqual(hi, 86.0)

    def test_left_sweep_decreases_as_raise_increases(self) -> None:
        _, hi_home = self.env.sweep_range(side="left", raise_deg=65.0)
        _, hi_mid = self.env.sweep_range(side="left", raise_deg=40.0)
        _, hi_high = self.env.sweep_range(side="left", raise_deg=6.0)
        self.assertGreater(hi_home, hi_mid)
        self.assertGreater(hi_mid, hi_high)

    def test_clamp_arms_at_home(self) -> None:
        self.assertEqual(self.env.clamp_arms(47.0, 65.0, 64.0, 87.0), (47.0, 65.0, 64.0, 87.0))

    def test_clamp_sweep_down_when_raise_increases(self) -> None:
        a0, a1, a2, a3 = self.env.clamp_arms(124.0, 6.0, 64.0, 87.0)
        self.assertAlmostEqual(a0, 124.0)
        self.assertAlmostEqual(a2, 44.0)
        self.assertAlmostEqual(a3, 70.0)

    def test_missing_json_raises(self) -> None:
        with self.assertRaises(FileNotFoundError):
            ArmSafetyEnvelope.from_json(Path("/nonexistent/captured_arm_limits.json"))


if __name__ == "__main__":
    unittest.main()
