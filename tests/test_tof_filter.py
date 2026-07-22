"""Tests for ToF trusted-range filtering and averaged velocity."""

from __future__ import annotations

import time

from tof_filter import TofChannelFilter, TofFilterBank, MAX_TRUST_MM, MIN_TRUST_MM


def test_ceiling_readings_show_open():
    f = TofChannelFilter(hold_sec=0.0)
    s = f.update(2100, now=1.0)
    assert s.is_open
    assert s.display_mm == -1
    assert s.velocity_mm_s is None


def test_hold_through_brief_dropout():
    f = TofChannelFilter(hold_sec=0.5, avg_window=3)
    f.update(600, now=1.0)
    f.update(610, now=1.1)
    f.update(620, now=1.2)
    s = f.update(-1, now=1.25)
    assert not s.is_open
    assert s.display_mm > 0
    assert s.velocity_mm_s is None


def test_averaged_velocity_real_approach():
    f = TofChannelFilter(avg_window=3, hold_sec=0.0)
    t0 = 10.0
    f.update(1000, now=t0)
    f.update(950, now=t0 + 0.1)
    s = f.update(900, now=t0 + 0.2)
    assert not s.is_open
    assert s.velocity_mm_s is not None
    assert s.velocity_mm_s < -50


def test_ceiling_jitter_no_false_fast_approach():
    """2100↔1950 flicker should not produce huge negative velocity after filter."""
    f = TofChannelFilter(avg_window=5, hold_sec=0.4)
    t = 0.0
    for mm in (2100, -1, 2080, 2100, 1950, 2100, -1, 2050):
        s = f.update(mm, now=t)
        t += 0.1
    assert s.is_open or s.velocity_mm_s is None or s.velocity_mm_s > -100


def test_filter_bank_three_channels():
    bank = TofFilterBank(3)
    display, vel, open_f = bank.update_all([500, 2100, -1], now=1.0)
    assert display[0] > MIN_TRUST_MM
    assert open_f[1]
    assert display[1] == -1


def test_trusted_range_constants():
    assert MIN_TRUST_MM == 80
    assert MAX_TRUST_MM == 1800


if __name__ == "__main__":
    test_ceiling_readings_show_open()
    test_hold_through_brief_dropout()
    test_averaged_velocity_real_approach()
    test_ceiling_jitter_no_false_fast_approach()
    test_filter_bank_three_channels()
    test_trusted_range_constants()
    print("all tests passed")
