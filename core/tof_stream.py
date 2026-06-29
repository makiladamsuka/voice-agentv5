"""ToF serial line ingest with motion blanking (ESP32 stream listener)."""

from __future__ import annotations

import time
from typing import TYPE_CHECKING

from core.tof_state import FILTER_BANK, STATE, _TOF_RE

if TYPE_CHECKING:
    from core.blackboard import Blackboard
    from core.tof_state import TofState


class TofStreamHandler:
    """Accept TOF L=... lines when base is still; blank during/after spins."""

    def __init__(
        self,
        tof_state: TofState | None = None,
        bb: Blackboard | None = None,
        *,
        spin_settle_sec: float = 0.65,
        gyro_settle_dps: float = 8.0,
    ) -> None:
        self._state = tof_state if tof_state is not None else STATE
        self._bb = bb
        self.tof_spin_settle_sec = float(spin_settle_sec)
        self.tof_gyro_settle_dps = float(gyro_settle_dps)
        self._tof_ignore_until = 0.0
        self._last_base_busy = False

    def accept_tof_samples(self) -> bool:
        if time.time() < self._tof_ignore_until:
            return False
        if self._state.base_rotating:
            return False

        busy = False
        if self._bb is not None:
            busy = bool(self._bb.read("base_motion_busy")["base_motion_busy"])

        if busy:
            if not self._last_base_busy:
                self._state.set_base_rotating(True)
            self._last_base_busy = True
            return False

        if self._last_base_busy:
            self._last_base_busy = False
            self._tof_ignore_until = time.time() + self.tof_spin_settle_sec
            self.reset_after_motion()
            self._state.set_base_rotating(False)
            return False

        if self._bb is not None:
            gyro = float(self._bb.read("imu_gyro_dps")["imu_gyro_dps"])
            imu_ok = bool(self._bb.read("imu_available")["imu_available"])
            if imu_ok and gyro > self.tof_gyro_settle_dps:
                return False

        return True

    def handle_tof_line(self, line: str) -> None:
        if not self.accept_tof_samples():
            return
        m = _TOF_RE.search(line)
        if not m:
            return
        raw = [int(m.group(i)) for i in range(1, 4)]
        mm, vel, open_flags = FILTER_BANK.update_all(raw)
        self._state.update_sample(mm, vel, open_flags=open_flags)

    def reset_after_motion(self) -> None:
        self._state.reset_tracks()
        FILTER_BANK.reset()

    def on_spin_start(self) -> None:
        self._last_base_busy = True
        self._state.set_base_rotating(True)

    def on_spin_end(self) -> None:
        self._last_base_busy = False
        self._tof_ignore_until = time.time() + self.tof_spin_settle_sec
        self.reset_after_motion()
        self._state.set_base_rotating(False)
