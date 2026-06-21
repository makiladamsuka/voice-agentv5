"""Open-loop L/R base spin moves (robottest style) with encoder stop."""

from __future__ import annotations

import time
from typing import TYPE_CHECKING, Protocol

if TYPE_CHECKING:
    from arduino_servo import ArduinoServoLink


class SpinLink(Protocol):
    def query_status(self): ...
    def write_base_spin_left(self) -> bool: ...
    def write_base_spin_right(self) -> bool: ...
    def write_base_stop(self) -> bool: ...


def _scaled_timeout(plate_deg: float, timeout_sec: float) -> float:
    """Small steps get short deadlines so a stuck encoder cannot spin for seconds."""
    mag = abs(plate_deg)
    step_budget = max(0.8, mag * 0.18 + 0.45)
    return min(timeout_sec, step_budget)


def write_base_step_spin(
    link: SpinLink,
    plate_deg: float,
    *,
    tolerance_deg: float = 1.5,
    timeout_sec: float = 12.0,
    poll_hz: float = 25.0,
    positive_uses_left: bool = True,
    stall_sec: float = 0.28,
    min_progress_counts: int = 4,
    on_poll=None,
) -> tuple[bool, float, str]:
    """
    Spin base until encoder delta reaches plate_deg (same units as POS DEG).

    Returns (success, encoder_delta_deg, stop_reason).
    stop_reason: target | stall | wrong_dir | timeout | no_start | zero
    """
    if abs(plate_deg) < 0.05:
        return True, 0.0, "zero"

    st0 = link.query_status()
    if st0 is None:
        return False, 0.0, "no_start"

    start_deg = st0.degrees
    start_count = st0.encoder_count
    want_left = plate_deg > 0 if positive_uses_left else plate_deg < 0
    started = link.write_base_spin_left() if want_left else link.write_base_spin_right()
    if not started:
        return False, 0.0, "no_start"

    deadline = time.time() + _scaled_timeout(plate_deg, timeout_sec)
    poll = 1.0 / max(5.0, poll_hz)
    delta = 0.0
    ok = False
    reason = "timeout"
    spin_start = time.time()
    last_progress_ts = spin_start
    last_count = start_count

    try:
        while time.time() < deadline:
            st = link.query_status()
            if st is not None:
                delta = st.degrees - start_deg
                abs_delta = abs(delta)
                count_moved = abs(st.encoder_count - last_count)
                if count_moved >= min_progress_counts:
                    last_progress_ts = time.time()
                    last_count = st.encoder_count

                if abs_delta >= abs(plate_deg) - tolerance_deg:
                    ok = True
                    reason = "target"
                    break

                elapsed = time.time() - spin_start
                if elapsed > 0.22:
                    if plate_deg > 0 and delta < -0.25:
                        reason = "wrong_dir"
                        break
                    if plate_deg < 0 and delta > 0.25:
                        reason = "wrong_dir"
                        break

                if elapsed > 0.18 and (time.time() - last_progress_ts) >= stall_sec:
                    reason = "stall"
                    break
            if on_poll is not None:
                on_poll()
            time.sleep(poll)
    finally:
        link.write_base_stop()
        time.sleep(0.05)

    st1 = link.query_status()
    if st1 is not None:
        delta = st1.degrees - start_deg
        if not ok and reason == "timeout":
            if abs(delta) >= abs(plate_deg) * 0.35:
                ok = True
                reason = "target_partial"
    return ok, delta, reason
