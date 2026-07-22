"""Tests for BaseMoveWatchdog using Blackboard IMU fields."""

from __future__ import annotations

import sys
from dataclasses import dataclass
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from base_safety import BaseMotionGate, BaseMoveWatchdog, BaseSafetyConfig
from core.blackboard import Blackboard


@dataclass
class _FakeStatus:
    encoder_count: int
    degrees: float
    counts_per_degree: float
    busy: bool


class _FakeLink:
    def __init__(self, degrees: float = 0.0, busy: bool = True):
        self.degrees = degrees
        self.busy = busy
        self.last_base_error = None
        self.stop_called = False

    def query_status(self):
        return _FakeStatus(0, self.degrees, 31.0, self.busy)

    def write_base_stop(self):
        self.stop_called = True


def test_watchdog_trips_on_encoder_runaway():
    bb = Blackboard()
    bb.write(imu_yaw_integral_deg=0.0)
    gate = BaseMotionGate(backoff_sec=1.0)
    link = _FakeLink(degrees=20.0, busy=True)
    wd = BaseMoveWatchdog(
        link=link,
        bb=bb,
        gate=gate,
        config=BaseSafetyConfig(
            encoder_runaway_margin_deg=2.0,
            min_gyro_runaway_deg=100.0,
            poll_interval_sec=0.0,
        ),
    )
    wd.start_move(commanded_deg=5.0, encoder_deg=0.0, pan_offset_deg=0.0)
    reason = wd.tick(pan_offset_deg=0.0, now=1.0)
    assert reason is not None
    assert "encoder runaway" in reason
    assert link.stop_called
    assert gate.allowed(now=1.0) is False
    assert bb.read("base_motion_allowed")["base_motion_allowed"] is False


def test_watchdog_finishes_when_move_completes():
    bb = Blackboard()
    bb.write(imu_yaw_integral_deg=2.0)
    gate = BaseMotionGate()
    link = _FakeLink(degrees=4.0, busy=False)
    wd = BaseMoveWatchdog(
        link=link,
        bb=bb,
        gate=gate,
        config=BaseSafetyConfig(poll_interval_sec=0.0),
    )
    wd.start_move(commanded_deg=5.0, encoder_deg=0.0, pan_offset_deg=0.0)
    reason = wd.tick(pan_offset_deg=0.0, now=1.0)
    assert reason is None
    assert wd.active is False


def test_yaw_state_blocks_overshoot_step():
    from base_yaw_controller import BaseYawState

    state = BaseYawState(max_yaw_deg=120.0)
    state.update(base_encoder_deg=115.0, head_pan_offset_deg=0.0)
    assert state.allow_base_step(10.0, 0.0) is False
    assert state.allow_base_step(-4.0, 0.0) is True
