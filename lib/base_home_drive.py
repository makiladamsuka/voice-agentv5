"""Drive base back to HOME using L/R spin (robottest style)."""

from __future__ import annotations

import math
import time
from typing import Callable, Protocol

from lib.person_memory import wrap_degrees


class EncQuery(Protocol):
    def __call__(self, fallback: float) -> tuple[float, int, bool, float]: ...


class ImuHomeQuery(Protocol):
    """Return (imu_from_home_deg, encoder_deg, base_busy, gyro_dps)."""

    def __call__(self) -> tuple[float, float, bool, float]: ...


class BaseLink(Protocol):
    def write_base_stop(self) -> bool: ...
    def zero_base(self) -> bool: ...
    def query_status(self): ...
    def write_base_spin_left(self) -> bool: ...
    def write_base_spin_right(self) -> bool: ...


def plate_step_toward_zero(error_deg: float, *, max_step: float) -> float:
    """
    Plate sign that drives a yaw error toward 0 (encoder or IMU base from HOME).

    With encoder_sign=-1: positive error → positive plate → encoder decreases.
    """
    if abs(error_deg) < 0.4:
        return 0.0
    mag = min(abs(error_deg), max_step)
    return math.copysign(mag, error_deg)


def spin_left_toward_zero(error_deg: float, *, positive_uses_left: bool) -> bool:
    """True = spin left, False = spin right, to drive error toward 0."""
    if abs(error_deg) < 0.05:
        return False
    plate_positive = error_deg > 0.0
    return plate_positive if positive_uses_left else not plate_positive


def _burst_sec(abs_err: float, *, fine_threshold: float, coarse_cap: float, fine_cap: float) -> float:
    if abs_err <= fine_threshold:
        return min(fine_cap, max(0.10, abs_err * 0.035))
    return min(coarse_cap, max(0.22, abs_err * 0.050))


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


def _run_home_spin_loop(
    link: BaseLink,
    base_cfg: dict,
    *,
    query_error: Callable[[], tuple[float, float, bool]],
    log: Callable[[str], None] | None,
    label: str,
) -> tuple[bool, float, float]:
    """Encoder-step homing (open-loop L/R until encoder delta reached)."""
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

    err, enc, busy = query_error()
    if abs(err) <= success_tol:
        link.zero_base()
        time.sleep(0.15)
        err, enc, _ = query_error()
        return True, err, enc

    _log(f"[HOME] {label} {err:+.1f}° → 0° …")

    stall_retries = 0
    for _attempt in range(22):
        err, enc, busy = query_error()
        if abs(err) <= success_tol:
            break
        if busy:
            _stop_and_wait(link)
            time.sleep(0.1)
            continue

        cap = fine_max if abs(err) <= fine_threshold else coarse_max
        margin = 0.94 if abs(err) > fine_threshold else 1.0
        step = plate_step_toward_zero(err * margin, max_step=cap)
        if abs(step) < 0.4:
            break

        tol = spin_tol if abs(step) <= fine_max + 0.5 else min(1.2, spin_tol + 0.3)
        step_stall = max(stall, abs(step) * 0.012 + 0.28)
        err_before = err
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
        err_after, enc_after, _ = query_error()
        tag = "OK" if ok else "FAIL"
        _log(
            f"[HOME] step {step:+.1f}° {tag} moved={moved:+.1f}° "
            f"{err_before:+.1f}°→{err_after:+.1f}° enc={enc_after:+.1f}° ({reason})"
        )
        err, enc = err_after, enc_after

        if abs(err) <= success_tol:
            break

        if err_before != 0.0 and (err_before * err_after) < 0 and abs(err_after) > success_tol:
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
            _stop_and_wait(link)
            time.sleep(0.15)
            small = plate_step_toward_zero(err, max_step=min(fine_max, abs(err) * 0.5))
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
                err, enc, _ = query_error()
                _log(
                    f"[HOME] retry {small:+.1f}° "
                    f"{'OK' if ok2 else 'FAIL'} moved={moved2:+.1f}° "
                    f"err={err:+.1f}° enc={enc:+.1f}° ({reason2})"
                )
                if abs(err) <= success_tol:
                    break
        else:
            stall_retries = 0

    _stop_and_wait(link)
    time.sleep(0.15)
    err, enc, _ = query_error()

    if abs(err) <= success_tol:
        link.zero_base()
        time.sleep(0.15)
        err, enc, _ = query_error()
        _log(f"[HOME] done {label}={err:+.1f}° enc={enc:+.1f}°")
        return True, err, enc

    _log(f"[HOME] failed — still {label} {err:+.1f}° enc={enc:+.1f}° (not zeroed)")
    return False, err, enc


def _run_imu_spin_home(
    link: BaseLink,
    base_cfg: dict,
    *,
    query_imu_home: ImuHomeQuery,
    log: Callable[[str], None] | None,
    target_deg: float = 0.0,
    zero_on_success: bool = False,
    log_tag: str = "GOTO",
) -> tuple[bool, float, float]:
    """
    IMU closed-loop: L/R bursts until IMU base from HOME reaches target_deg.

    Does not use encoder-degree step targets (IMU and encoder diverge during motion).
    """
    def _log(msg: str) -> None:
        if log is not None:
            log(msg)

    def _err(imu_from_home: float) -> float:
        return wrap_degrees(imu_from_home - target_deg)

    success_tol = float(base_cfg.get("home_success_tolerance_deg", 1.5))
    stall = float(base_cfg.get("spin_stall_sec", base_cfg.get("home_stall_sec", 0.35)))
    fine_threshold = float(base_cfg.get("home_fine_threshold_deg", 8.0))
    positive_left = bool(base_cfg.get("spin_positive_uses_left", False))
    coarse_burst = float(base_cfg.get("home_imu_burst_sec", 1.6))
    fine_burst = float(base_cfg.get("home_imu_fine_burst_sec", 0.32))
    poll_sec = 1.0 / float(base_cfg.get("home_imu_poll_hz", 25.0))
    settle_sec = float(base_cfg.get("home_imu_settle_sec", 0.14))

    _stop_and_wait(link)
    time.sleep(settle_sec)

    imu_pos, enc, _busy, _gyro = query_imu_home()
    imu_err = _err(imu_pos)
    if abs(imu_err) <= success_tol:
        if zero_on_success:
            link.zero_base()
            time.sleep(0.15)
            imu_pos, enc, _, _ = query_imu_home()
        return True, imu_pos, enc

    _log(
        f"[{log_tag}] imu {imu_pos:+.1f}° → {target_deg:+.1f}° "
        f"(err {imu_err:+.1f}°, L/R on IMU) …"
    )

    stall_retries = 0
    for _attempt in range(36):
        imu_pos, enc, busy, gyro = query_imu_home()
        imu_err = _err(imu_pos)
        if abs(imu_err) <= success_tol:
            break
        if busy:
            _stop_and_wait(link)
            time.sleep(0.08)
            continue

        want_left = spin_left_toward_zero(imu_err, positive_uses_left=positive_left)
        burst = _burst_sec(
            abs(imu_err),
            fine_threshold=fine_threshold,
            coarse_cap=coarse_burst,
            fine_cap=fine_burst,
        )
        err_at_start = imu_err
        imu_at_start = imu_pos
        st0 = link.query_status()
        start_count = int(st0.encoder_count) if st0 is not None else 0
        last_count = start_count
        last_progress_ts = time.time()

        started = link.write_base_spin_left() if want_left else link.write_base_spin_right()
        if not started:
            break

        deadline = time.time() + burst
        stop_reason = "time"
        try:
            while time.time() < deadline:
                imu_pos, enc, busy, gyro = query_imu_home()
                imu_err = _err(imu_pos)
                if abs(imu_err) <= success_tol:
                    stop_reason = "target"
                    break
                if err_at_start != 0.0 and (err_at_start * imu_err) < 0:
                    stop_reason = "crossed"
                    break
                st = link.query_status()
                if st is not None:
                    moved = abs(int(st.encoder_count) - last_count)
                    if moved >= 4:
                        last_progress_ts = time.time()
                        last_count = int(st.encoder_count)
                if (time.time() - last_progress_ts) >= stall:
                    stop_reason = "stall"
                    break
                time.sleep(poll_sec)
        finally:
            link.write_base_stop()

        _stop_and_wait(link, timeout_sec=1.5)
        time.sleep(settle_sec)
        imu_after, enc_after, _, _ = query_imu_home()
        err_after = _err(imu_after)
        side = "L" if want_left else "R"
        _log(
            f"[{log_tag}] {side} {burst:.2f}s ({stop_reason})  "
            f"imu {imu_at_start:+.1f}°→{imu_after:+.1f}° "
            f"err {err_at_start:+.1f}°→{err_after:+.1f}° enc={enc_after:+.1f}°"
        )
        imu_err = err_after
        enc = enc_after

        if abs(imu_err) <= success_tol:
            break

        improved = abs(imu_err) < abs(err_at_start) - 0.35
        if improved:
            stall_retries = 0
        else:
            stall_retries += 1
            if stall_retries >= 4:
                break

    _stop_and_wait(link)
    time.sleep(settle_sec)
    imu_pos, enc, _, _ = query_imu_home()
    imu_err = _err(imu_pos)

    if abs(imu_err) <= success_tol:
        if zero_on_success:
            link.zero_base()
            time.sleep(0.15)
            imu_pos, enc, _, _ = query_imu_home()
        _log(
            f"[{log_tag}] done imu={imu_pos:+.1f}° "
            f"(target {target_deg:+.1f}°) enc={enc:+.1f}°"
        )
        return True, imu_pos, enc

    _log(
        f"[{log_tag}] failed — imu={imu_pos:+.1f}° "
        f"target {target_deg:+.1f}° enc={enc:+.1f}°"
    )
    return False, imu_pos, enc


def drive_base_to_encoder_zero(
    link: BaseLink,
    base_cfg: dict,
    *,
    query_enc: EncQuery,
    log: Callable[[str], None] | None = None,
) -> tuple[bool, float]:
    """Return (success, final_encoder_deg). Only zero_base() when within tolerance."""

    def query_error() -> tuple[float, float, bool]:
        enc, _, busy, _ = query_enc(0.0)
        return enc, enc, busy

    ok, _err, enc = _run_home_spin_loop(
        link,
        base_cfg,
        query_error=query_error,
        log=log,
        label="enc",
    )
    return ok, enc


def drive_base_to_imu_zero(
    link: BaseLink,
    base_cfg: dict,
    *,
    query_imu_home: ImuHomeQuery,
    log: Callable[[str], None] | None = None,
) -> tuple[bool, float]:
    """Return (success, final_imu_from_home_deg). L/R bursts until IMU base from HOME → 0."""

    ok, imu_pos, _enc = _run_imu_spin_home(
        link,
        base_cfg,
        query_imu_home=query_imu_home,
        log=log,
        target_deg=0.0,
        zero_on_success=True,
        log_tag="HOME",
    )
    return ok, imu_pos


def drive_base_to_imu_angle(
    link: BaseLink,
    base_cfg: dict,
    target_deg: float,
    *,
    query_imu_home: ImuHomeQuery,
    log: Callable[[str], None] | None = None,
) -> tuple[bool, float]:
    """Return (success, final_imu_from_home_deg). L/R bursts until IMU reaches target from HOME."""

    ok, imu_pos, _enc = _run_imu_spin_home(
        link,
        base_cfg,
        query_imu_home=query_imu_home,
        log=log,
        target_deg=float(target_deg),
        zero_on_success=False,
        log_tag="GOTO",
    )
    return ok, imu_pos
