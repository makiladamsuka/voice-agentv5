"""Drive base back to HOME using L/R spin (robottest style)."""

from __future__ import annotations

import math
import time
from typing import Callable, Protocol

from base_yaw_controller import HeadingPid


class EncQuery(Protocol):
    def __call__(self, fallback: float) -> tuple[float, int, bool, float]: ...


class ImuHomeQuery(Protocol):
    """Return (imu_from_home_deg, encoder_deg, base_busy, gyro_dps)."""

    def __call__(self) -> tuple[float, float, bool, float]: ...


class BaseLink(Protocol):
    def write_base_stop(self) -> bool: ...
    def zero_base(self) -> bool: ...
    def query_status(self): ...


def plate_step_toward_zero(error_deg: float, *, max_step: float) -> float:
    """
    Plate command that drives a yaw error toward 0 (encoder or IMU base from HOME).

    With encoder_sign=-1: positive error → positive plate → encoder decreases.
    """
    if abs(error_deg) < 0.4:
        return 0.0
    mag = min(abs(error_deg), max_step)
    return math.copysign(mag, error_deg)


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
    use_pid: bool,
) -> tuple[bool, float, float]:
    """Shared closed-loop homing. Returns (success, final_error, final_enc)."""
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
    pid_kp = float(base_cfg.get("home_pid_kp", 0.92))
    pid_kd = float(base_cfg.get("home_pid_kd", 0.14))
    pid = HeadingPid(kp=pid_kp, kd=pid_kd)
    pid.reset()

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
    last_ts = time.time()
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
        min_step = min(2.0, fine_max * 0.45)

        now = time.time()
        dt = now - last_ts
        last_ts = now

        if use_pid:
            # PID output is opposite plate sign; negate to drive error toward 0.
            step = -pid.step(
                current_world_yaw_deg=err,
                target_world_yaw_deg=0.0,
                dt=dt,
                min_step_deg=min_step,
                max_step_deg=cap,
            )
            if abs(step) < 0.4:
                step = plate_step_toward_zero(err * margin, max_step=cap)
        else:
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
            pid.reset()
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
        use_pid=False,
    )
    return ok, enc


def drive_base_to_imu_zero(
    link: BaseLink,
    base_cfg: dict,
    *,
    query_imu_home: ImuHomeQuery,
    log: Callable[[str], None] | None = None,
) -> tuple[bool, float]:
    """Return (success, final_imu_from_home_deg). PID spin toward locked HOME IMU yaw."""

    def query_error() -> tuple[float, float, bool]:
        imu_err, enc, busy, _gyro = query_imu_home()
        return imu_err, enc, busy

    ok, imu_err, _enc = _run_home_spin_loop(
        link,
        base_cfg,
        query_error=query_error,
        log=log,
        label="imu",
        use_pid=True,
    )
    return ok, imu_err
