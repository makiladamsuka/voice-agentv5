"""Stall / partial progress handling for base spins."""

import time
import unittest
from unittest.mock import patch

from base_spin_motion import write_base_step_spin


class _FakeStatus:
    def __init__(self, degrees: float, encoder_count: int = 0):
        self.degrees = degrees
        self.encoder_count = encoder_count
        self.busy = False


class _StallLink:
    def __init__(self, start_deg: float = 0.0, partial_deg: float = 8.0):
        self.degrees = start_deg
        self.encoder_count = 0
        self._partial_deg = partial_deg
        self._advanced = False

    def query_status(self):
        return _FakeStatus(self.degrees, self.encoder_count)

    def write_base_spin_left(self) -> bool:
        return True

    def write_base_spin_right(self) -> bool:
        return True

    def write_base_stop(self) -> bool:
        return True


class StallPartialTests(unittest.TestCase):
    def test_stall_with_half_progress_counts_as_partial_success(self):
        link = _StallLink(start_deg=0.0, partial_deg=8.0)
        t0 = 1000.0
        ticks = [t0]

        def fake_time():
            return ticks[-1]

        def fake_sleep(_sec):
            if not link._advanced:
                link.degrees = link._partial_deg
                link.encoder_count = 10
                link._advanced = True
            ticks.append(ticks[-1] + 0.05)

        with patch("base_spin_motion.time.time", side_effect=fake_time), patch(
            "base_spin_motion.time.sleep", side_effect=fake_sleep
        ):
            ok, delta, reason = write_base_step_spin(
                link,
                15.0,
                tolerance_deg=1.5,
                timeout_sec=2.0,
                poll_hz=50.0,
                stall_sec=0.05,
                min_progress_counts=2,
            )

        self.assertTrue(ok)
        self.assertEqual(reason, "target_partial")
        self.assertGreaterEqual(abs(delta), 7.5)


if __name__ == "__main__":
    unittest.main()
