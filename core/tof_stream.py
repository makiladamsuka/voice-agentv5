"""ToF serial line ingest with motion blanking (ESP32 stream listener)."""

from __future__ import annotations

import time
from typing import TYPE_CHECKING

from core.tof_state import FILTER_BANK, STATE, SWAP_LEFT_RIGHT, _TOF_RE

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
        self._zone_streaks: dict[str, int] = {"L": 0, "C": 0, "R": 0}
        self._clear_streak = 0
        self._last_synth_zone = ""
        self._last_synth_ts = 0.0

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
        self._update_bb_from_sample(mm, vel)

    def _update_bb_from_sample(
        self, mm: list[int], vel: list[int | None]
    ) -> None:
        if self._bb is None:
            return
        idx_l = 2 if SWAP_LEFT_RIGHT else 0
        idx_r = 0 if SWAP_LEFT_RIGHT else 2
        zone_mm = {"L": mm[idx_l], "C": mm[1], "R": mm[idx_r]}
        zone_vel = {"L": vel[idx_l], "C": vel[1], "R": vel[idx_r]}

        for z in ("L", "C", "R"):
            d = zone_mm[z]
            if 80 <= d <= 1000:
                self._zone_streaks[z] = min(30, self._zone_streaks[z] + 1)
            else:
                self._zone_streaks[z] = 0

        zl = self._zone_streaks["L"] >= 2
        zc = self._zone_streaks["C"] >= 2
        zr = self._zone_streaks["R"] >= 2
        z_count = int(zl) + int(zc) + int(zr)
        surrounded = (zl and zr) or (z_count >= 3)
        too_close = any(0 < zone_mm[z] < 120 for z in ("L", "C", "R"))

        self._bb.write(
            prox_zone_left=zl,
            prox_zone_center=zc,
            prox_zone_right=zr,
            prox_zone_count=z_count,
            tof_surrounded=surrounded,
            tof_too_close_safety=too_close,
        )

        def _req_streak(d: int) -> int:
            return 1 if d <= 400 else (2 if d <= 700 else 3)
        side_candidates = [
            z for z in ("L", "R")
            if 120 <= zone_mm[z] <= 1000 and self._zone_streaks[z] >= _req_streak(zone_mm[z])
        ]
        if side_candidates:
            best_zone = min(side_candidates, key=lambda z: zone_mm[z])
        elif 120 <= zone_mm["C"] <= 1000 and self._zone_streaks["C"] >= _req_streak(zone_mm["C"]):
            best_zone = "C"
        else:
            best_zone = ""

        now = time.time()
        if best_zone:
            self._clear_streak = 0
            streak = self._zone_streaks[best_zone]
            req_s = _req_streak(zone_mm[best_zone])
            u_dist = max(0.0, min(1.0, (zone_mm[best_zone] - 120.0) / 880.0))
            retrigger_sec = 0.8 + u_dist * 1.4
            is_new_entry = (streak == req_s) or (best_zone != self._last_synth_zone)
            is_retrigger_due = (now - self._last_synth_ts) >= retrigger_sec
            if is_new_entry or is_retrigger_due:
                self._last_synth_zone = best_zone
                self._last_synth_ts = now
                v = float(zone_vel[best_zone] or 0.0)
                self._bb.write(
                    prox_approach_zone=best_zone,
                    prox_approach_velocity=v,
                    prox_approach_distance=int(zone_mm[best_zone]),
                    prox_approach_confidence=max(4, streak),
                    prox_approach_active=True,
                    prox_approach_ts=now,
                )
        else:
            self._clear_streak += 1
            if self._clear_streak >= 5:
                self._last_synth_zone = ""
                if self._bb.read("prox_approach_active")["prox_approach_active"]:
                    self._bb.write(
                        prox_approach_active=False,
                        prox_approach_zone="",
                        prox_approach_confidence=0,
                    )

    def reset_after_motion(self) -> None:
        self._state.reset_tracks()
        FILTER_BANK.reset()
        self._zone_streaks = {"L": 0, "C": 0, "R": 0}
        self._clear_streak = 0
        self._last_synth_zone = ""

    def on_spin_start(self) -> None:
        self._last_base_busy = True
        self._state.set_base_rotating(True)

    def on_spin_end(self) -> None:
        self._last_base_busy = False
        self._tof_ignore_until = time.time() + self.tof_spin_settle_sec
        self.reset_after_motion()
        self._state.set_base_rotating(False)
