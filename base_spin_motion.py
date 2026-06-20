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


def write_base_step_spin(
    link: SpinLink,
    plate_deg: float,
    *,
    tolerance_deg: float = 1.5,
    timeout_sec: float = 12.0,
    poll_hz: float = 25.0,
    positive_uses_left: bool = True,
) -> tuple[bool, float]:
    """
    Spin base until encoder delta reaches plate_deg (same units as POS DEG).

    Returns (success, encoder_delta_deg).
    """
    if abs(plate_deg) < 0.05:
        return True, 0.0

    st0 = link.query_status()
    if st0 is None:
        return False, 0.0

    start_deg = st0.degrees
    want_left = plate_deg > 0 if positive_uses_left else plate_deg < 0
    started = link.write_base_spin_left() if want_left else link.write_base_spin_right()
    if not started:
        return False, 0.0

    deadline = time.time() + max(1.0, timeout_sec)
    poll = 1.0 / max(5.0, poll_hz)
    delta = 0.0
    ok = False

    try:
        while time.time() < deadline:
            st = link.query_status()
            if st is not None:
                delta = st.degrees - start_deg
                if abs(delta) >= abs(plate_deg) - tolerance_deg:
                    ok = True
                    break
            time.sleep(poll)
    finally:
        link.write_base_stop()
        time.sleep(0.05)

    st1 = link.query_status()
    if st1 is not None:
        delta = st1.degrees - start_deg
        if not ok:
            ok = abs(delta) >= abs(plate_deg) * 0.35
    return ok, delta
