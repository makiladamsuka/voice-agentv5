"""Drive base encoder reading back to 0° using L/R spin (robottest style)."""

from __future__ import annotations

import math
import time
from typing import Callable, Protocol


class EncQuery(Protocol):
    def __call__(self, fallback: float) -> tuple[float, int, bool, float]: ...


class BaseLink(Protocol):
    def write_base_stop(self) -> bool: ...
    def zero_base(self) -> bool: ...
    def query_status(self): ...


def plate_step_toward_zero(encoder_deg: float, *, max_step: float) -> float:
    """
    Plate command that moves POS DEG toward 0 (verified on hardware).

    With encoder_sign=-1: positive enc → positive plate → encoder decreases.
    """
    if abs(encoder_deg) < 0.4:
        return 0.0
    mag = min(abs(encoder_deg), max_step)
    return math.copysign(mag, encoder_deg)


def _stop_and_wait(link: BaseLink, timeout_sec: float = 3.0) -> None:
    link.write_base_stop()
    deadline = time.time() + timeout_sec
    while time.time() < deadline:
        st = link.query_status()
        if st is not None and not st.busy:
            return
        time.sleep(0.05)
    link.write_base_stop()


def ensure_base_idle(link: BaseLink, timeout_sec: float = 3.0) -> None:
    _stop_and_wait(link, timeout_sec=timeout_sec)


def drive_base_to_encoder_zero(
    link: BaseLink,
    base_cfg: dict,
    *,
    query_enc: EncQuery,
    log: Callable[[str], None] | None = None,
) -> tuple[bool, float]:
    """
    Return (success, final_encoder_deg). Only zero_base() when within tolerance.
    """
    from base_spin_motion import write_base_step_spin

    def _log(msg: str) -> None:
        if log is not None:
            log(msg)

    success_tol = float(base_cfg.get("home_success_tolerance_deg", 1.5))
    spin_tol = float(base_cfg.get("home_spin_tolerance_deg", 0.8))
    timeout = float(base_cfg.get("home_timeout_sec", 6.0))
    stall = float(base_cfg.get("spin_stall_sec", base_cfg.get("home_stall_sec", 0.35)))
    coarse_max = min(float(base_cfg.get("home_step_deg", 35.0)), 22.0)
    fine_max = float(base_cfg.get("home_fine_step_deg", 6.0))
    fine_threshold = float(base_cfg.get("home_fine_threshold_deg", 8.0))
    encoder_sign = float(base_cfg.get("encoder_sign", -1.0))
    positive_left = bool(base_cfg.get("spin_positive_uses_left", False))

    _stop_and_wait(link)
    time.sleep(0.1)

    enc, _, _, _ = query_enc(0.0)
    if abs(enc) <= success_tol:
        link.zero_base()
        time.sleep(0.15)
        enc, _, _, _ = query_enc(0.0)
        return True, enc

    _log(f"[HOME] {enc:+.1f}° → 0° …")

    stall_retries = 0
    for attempt in range(20):
        enc, _, busy, _ = query_enc(enc)
        if abs(enc) <= success_tol:
            break
        if busy:
            _stop_and_wait(link)
            time.sleep(0.1)
            continue

        cap = fine_max if abs(enc) <= fine_threshold else coarse_max
        # Leave a little headroom on large moves to reduce overshoot.
        margin = 0.94 if abs(enc) > fine_threshold else 1.0
        step = plate_step_toward_zero(enc * margin, max_step=cap)
        if abs(step) < 0.4:
            break

        tol = spin_tol if abs(step) <= fine_max + 0.5 else min(1.2, spin_tol + 0.3)
        step_stall = max(stall, abs(step) * 0.012 + 0.28)
        enc_before = enc
        ok, moved, reason = write_base_step_spin(
            link,
            step,
            tolerance_deg=tol,
            timeout_sec=timeout,
            positive_uses_left=positive_left,
            encoder_sign=encoder_sign,
            stall_sec=step_stall,
        )
        _stop_and_wait(link, timeout_sec=2.0)
        time.sleep(0.12)
        enc_after, _, _, _ = query_enc(enc)
        tag = "OK" if ok else "FAIL"
        _log(
            f"[HOME] step {step:+.1f}° {tag} moved={moved:+.1f}° "
            f"{enc:+.1f}°→{enc_after:+.1f}° ({reason})"
        )
        enc = enc_after

        if abs(enc) <= success_tol:
            break

        # Overshot zero — next iteration corrects with opposite-sign step.
        if enc_before != 0.0 and (enc_before * enc_after) < 0 and abs(enc_after) > success_tol:
            stall_retries = 0
            continue

        if not ok:
            made_progress = abs(moved) >= min(2.5, abs(step) * 0.2)
            if made_progress:
                stall_retries = 0
            else:
                stall_retries += 1
            if stall_retries >= 3:
                break
            # Smaller step on stall
            _stop_and_wait(link)
            time.sleep(0.15)
            small = plate_step_toward_zero(enc, max_step=min(fine_max, abs(enc) * 0.5))
            if abs(small) >= 0.4:
                ok2, moved2, reason2 = write_base_step_spin(
                    link,
                    small,
                    tolerance_deg=spin_tol,
                    timeout_sec=timeout,
                    positive_uses_left=positive_left,
                    encoder_sign=encoder_sign,
                    stall_sec=stall,
                )
                enc = query_enc(enc)[0]
                _log(
                    f"[HOME] retry {small:+.1f}° "
                    f"{'OK' if ok2 else 'FAIL'} moved={moved2:+.1f}° "
                    f"enc={enc:+.1f}° ({reason2})"
                )
                if abs(enc) <= success_tol:
                    break
        else:
            stall_retries = 0

    _stop_and_wait(link)
    time.sleep(0.1)
    enc, _, _, _ = query_enc(enc)

    if abs(enc) <= success_tol:
        link.zero_base()
        time.sleep(0.15)
        enc, _, _, _ = query_enc(0.0)
        _log(f"[HOME] done enc={enc:+.1f}°")
        return True, enc

    _log(f"[HOME] failed — still {enc:+.1f}° (not zeroed)")
    return False, enc
