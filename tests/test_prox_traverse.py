"""Tests for ToF walk-by traverse detection and base follow."""

import time
import unittest

from core.base_controller import BaseController
from core.blackboard import Blackboard
from lib.prox_traverse import ProxTraverseConfig, ProxTraverseTracker, match_traverse_direction


class _Link:
    pass


class TraverseMatchTests(unittest.TestCase):
    def test_lcr_within_window_is_l2r(self):
        t0 = 1000.0
        events = [("L", t0), ("C", t0 + 0.6), ("R", t0 + 1.2)]
        matched = match_traverse_direction(
            events,
            t0 + 1.2,
            window_sec=3.5,
            allow_skip_center=True,
            skip_center_max_gap_sec=1.2,
            min_zones=2,
        )
        self.assertEqual(matched, ("L2R", 3))

    def test_rcl_is_r2l(self):
        t0 = 2000.0
        events = [("R", t0), ("C", t0 + 0.5), ("L", t0 + 1.0)]
        matched = match_traverse_direction(
            events,
            t0 + 1.0,
            window_sec=3.5,
            allow_skip_center=True,
            skip_center_max_gap_sec=1.2,
            min_zones=2,
        )
        self.assertEqual(matched, ("R2L", 3))

    def test_lr_skip_center_when_fast(self):
        t0 = 3000.0
        events = [("L", t0), ("R", t0 + 0.8)]
        matched = match_traverse_direction(
            events,
            t0 + 0.8,
            window_sec=3.5,
            allow_skip_center=True,
            skip_center_max_gap_sec=1.2,
            min_zones=2,
        )
        self.assertEqual(matched, ("L2R", 2))

    def test_lr_skip_center_rejected_when_slow(self):
        t0 = 4000.0
        events = [("L", t0), ("R", t0 + 2.0)]
        matched = match_traverse_direction(
            events,
            t0 + 2.0,
            window_sec=3.5,
            allow_skip_center=True,
            skip_center_max_gap_sec=1.2,
            min_zones=2,
        )
        self.assertIsNone(matched)


class TraverseTrackerTests(unittest.TestCase):
    def _tracker(self) -> ProxTraverseTracker:
        return ProxTraverseTracker(
            config=ProxTraverseConfig(
                sequence_window_sec=3.5,
                idle_exit_sec=1.5,
                max_duration_sec=8.0,
            )
        )

    def test_sequence_activates_l2r(self):
        tr = self._tracker()
        t0 = 5000.0
        tr.update({"L": True, "C": False, "R": False}, t0)
        tr.update({"L": True, "C": True, "R": False}, t0 + 0.5)
        snap = tr.update({"L": False, "C": True, "R": True}, t0 + 1.0)
        self.assertTrue(snap.prox_traverse_active)
        self.assertEqual(snap.prox_traverse_dir, "L2R")
        self.assertEqual(snap.prox_traverse_zone, "R")

    def test_all_zones_at_start_rejected(self):
        tr = self._tracker()
        t0 = 6000.0
        tr.update({"L": True, "C": True, "R": True}, t0)
        snap = tr.update({"L": True, "C": True, "R": True}, t0 + 0.1)
        self.assertFalse(snap.prox_traverse_active)

    def test_idle_exit_clears_active(self):
        tr = self._tracker()
        t0 = 7000.0
        tr.update({"L": True, "C": False, "R": False}, t0)
        tr.update({"L": True, "C": True, "R": False}, t0 + 0.4)
        tr.update({"L": False, "C": True, "R": True}, t0 + 0.9)
        tr.update({"L": False, "C": False, "R": False}, t0 + 2.0)
        snap = tr.update({"L": False, "C": False, "R": False}, t0 + 3.7)
        self.assertFalse(snap.prox_traverse_active)


class TraverseBaseStepTests(unittest.TestCase):
    def _make_controller(self) -> tuple[Blackboard, BaseController]:
        bb = Blackboard()
        ctrl = BaseController(bb, _Link())
        ctrl.traverse_enabled = True
        ctrl.traverse_step_deg = 3.0
        ctrl.traverse_max_step_deg = 5.0
        ctrl.traverse_cooldown_sec = 0.55
        ctrl.traverse_comp_gain = 0.85
        ctrl.min_step = 2.0
        ctrl.max_step = 15.0
        ctrl.base_sign = 1.0
        return bb, ctrl

    def _state(self, bb: Blackboard, **extra) -> dict:
        base = bb.read(
            "servo_pan", "base_encoder_deg", "base_world_yaw_deg",
        )
        base.update({
            "face_detected": False,
            "body_detected": False,
            "servo_mode": "wander",
            "prox_traverse_active": True,
            "prox_traverse_dir": "L2R",
            "imu_available": False,
        })
        base.update(extra)
        return base

    def test_l2r_plans_positive_step(self):
        bb, ctrl = self._make_controller()
        bb.write(servo_pan=80.0, base_encoder_deg=0.0, base_world_yaw_deg=0.0, base_motion_allowed=True)
        now = time.time()
        step, source, _ = ctrl._plan_traverse_step(now, self._state(bb))
        self.assertEqual(source, "traverse")
        self.assertIsNotNone(step)
        self.assertGreater(step, 0.0)

    def test_r2l_plans_negative_step(self):
        bb, ctrl = self._make_controller()
        bb.write(servo_pan=80.0, base_encoder_deg=0.0, base_world_yaw_deg=0.0, base_motion_allowed=True)
        now = time.time()
        step, source, _ = ctrl._plan_traverse_step(
            now, self._state(bb, prox_traverse_dir="R2L"),
        )
        self.assertEqual(source, "traverse")
        self.assertIsNotNone(step)
        self.assertLess(step, 0.0)

    def test_cooldown_blocks_rapid_steps(self):
        bb, ctrl = self._make_controller()
        bb.write(servo_pan=80.0, base_encoder_deg=0.0, base_world_yaw_deg=0.0, base_motion_allowed=True)
        now = time.time()
        step1, _, _ = ctrl._plan_traverse_step(now, self._state(bb))
        step2, source2, _ = ctrl._plan_traverse_step(now + 0.1, self._state(bb))
        self.assertIsNotNone(step1)
        self.assertIsNone(step2)
        self.assertEqual(source2, "")


if __name__ == "__main__":
    unittest.main()
