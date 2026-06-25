"""Head IMU orientation — tilt, pan (yaw), and rates from a head-mounted BMI160.

Built from scratch for localization experiments. Uses only:
  - low-level BMI160 reads (imu_sensor.Bmi160)
  - head mount frame (lib.head_imu_mount)

No encoder fusion, blackboard, or drift corrector.
"""

from __future__ import annotations

import math
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

try:
    import yaml
except ImportError:
    yaml = None

from lib.head_imu_mount import HeadMount, load_head_mount

try:
    from imu_sensor import Bmi160
except ImportError:
    Bmi160 = None  # type: ignore


def wrap_degrees(value: float) -> float:
    return (value + 180.0) % 360.0 - 180.0


def _accel_roll_pitch(forward_g: float, left_g: float, up_g: float) -> tuple[float, float]:
    pitch = math.degrees(math.atan2(-forward_g, math.sqrt(left_g * left_g + up_g * up_g)))
    roll = math.degrees(math.atan2(left_g, up_g))
    return roll, pitch


@dataclass(frozen=True)
class OrientSample:
    """One orientation sample in the head frame."""

    ts: float
    roll_deg: float
    pitch_deg: float
    yaw_deg: float
    gyro_roll_dps: float
    gyro_pitch_dps: float
    gyro_yaw_dps: float
    tilt_deg: float
    pan_deg: float
    delta_roll_deg: float
    delta_pitch_deg: float
    delta_yaw_deg: float
    delta_tilt_deg: float
    delta_pan_deg: float
    accel_trusted: bool

    def as_dict(self) -> dict:
        return {
            "ts": self.ts,
            "roll_deg": self.roll_deg,
            "pitch_deg": self.pitch_deg,
            "yaw_deg": self.yaw_deg,
            "tilt_deg": self.tilt_deg,
            "tilt_real_deg": self.tilt_deg,
            "roll_real_deg": self.roll_deg,
            "pan_deg": self.pan_deg,
            "gyro_roll_dps": self.gyro_roll_dps,
            "gyro_pitch_dps": self.gyro_pitch_dps,
            "gyro_yaw_dps": self.gyro_yaw_dps,
            "delta_roll_deg": self.delta_roll_deg,
            "delta_pitch_deg": self.delta_pitch_deg,
            "delta_yaw_deg": self.delta_yaw_deg,
            "delta_tilt_deg": self.delta_tilt_deg,
            "delta_pan_deg": self.delta_pan_deg,
            "accel_trusted": self.accel_trusted,
        }


class HeadImuOrient:
    """Complementary tilt + gyro-integrated pan/yaw for a head-mounted IMU."""

    def __init__(
        self,
        *,
        mount: HeadMount | None = None,
        bus: int = 1,
        address: int = 0x69,
        sample_hz: float = 100.0,
        roll_pitch_alpha: float = 0.02,
        spin_gyro_threshold_dps: float = 80.0,
        device: object | None = None,
    ) -> None:
        self.mount = mount or load_head_mount()
        if device is not None:
            self._device = device
        elif Bmi160 is not None:
            self._device = Bmi160(bus=bus, address=address)
        else:
            raise RuntimeError("imu_sensor not available — install smbus2 on the Pi")
        self._sample_hz = max(1.0, sample_hz)
        self._alpha = roll_pitch_alpha
        self._spin_threshold = spin_gyro_threshold_dps
        self._roll_deg = 0.0
        self._pitch_deg = 0.0
        self._yaw_deg = 0.0
        self._roll_offset = 0.0
        self._pitch_offset = 0.0
        self._ref_roll = 0.0
        self._ref_pitch = 0.0
        self._ref_yaw = 0.0
        self._initialized = False
        self._opened = False
        self._leveled = False

    def open(self) -> None:
        if self._opened:
            return
        if hasattr(self._device, "open"):
            self._device.open()
        self._opened = True

    def close(self) -> None:
        if not self._opened:
            return
        if hasattr(self._device, "close"):
            self._device.close()
        self._opened = False

    def zero_reference(self) -> None:
        """Set current real pose as zero for delta readouts only."""
        self._ref_roll = self._roll_deg
        self._ref_pitch = self._pitch_deg
        self._ref_yaw = self._yaw_deg

    def lock_reference(self) -> tuple[float, float]:
        """Reset yaw integral and lock center reference; returns (yaw, tilt)."""
        self._yaw_deg = 0.0
        self._ref_yaw = 0.0
        self._ref_roll = self._roll_deg
        self._ref_pitch = self._pitch_deg
        return 0.0, self._pitch_deg

    def reset_yaw(self) -> None:
        self._yaw_deg = 0.0
        self._ref_yaw = 0.0

    def calibrate_level_stationary(
        self,
        *,
        duration_sec: float = 2.0,
        max_gyro_dps: float = 8.0,
        min_samples: int = 40,
    ) -> int:
        """Average roll/pitch while still; sets offsets so upright reads tilt ≈ 0°."""
        if not self._opened:
            raise RuntimeError("HeadImuOrient not open")
        pool: list[tuple[float, float]] = []
        deadline = time.time() + duration_sec
        prev = time.perf_counter()
        while time.time() < deadline:
            ax, ay, az, gx, gy, gz = self._device.read_raw()
            now = time.perf_counter()
            dt = max(0.0005, min(0.1, now - prev))
            prev = now
            _, _, _, trust_accel, g_fwd, g_left, g_up = self._update_filter(
                ax, ay, az, gx, gy, gz, dt
            )
            gyro_mag = math.sqrt(g_fwd * g_fwd + g_left * g_left + g_up * g_up)
            if trust_accel and gyro_mag <= max_gyro_dps:
                pool.append((self._roll_deg, self._pitch_deg))
            time.sleep(0.01)
        if len(pool) < min_samples:
            raise ValueError(
                f"level calibrate needs {min_samples} still samples, got {len(pool)}"
            )
        mean_roll = sum(r for r, _ in pool) / len(pool)
        mean_pitch = sum(p for _, p in pool) / len(pool)
        self._roll_offset = mean_roll
        self._pitch_offset = mean_pitch
        self._leveled = True
        return len(pool)

    def _update_filter(
        self,
        ax: float,
        ay: float,
        az: float,
        gx: float,
        gy: float,
        gz: float,
        dt: float,
    ) -> tuple[float, float, float, bool, float, float, float]:
        """Update filter state; return leveled roll, pitch, yaw_rate, trust, raw gyros."""
        fwd, left, up = self.mount.remap_vec((ax, ay, az))
        g_fwd, g_left, g_up = self.mount.remap_vec((gx, gy, gz))

        accel_roll, accel_pitch = _accel_roll_pitch(fwd, left, up)
        gyro_mag = math.sqrt(g_fwd * g_fwd + g_left * g_left + g_up * g_up)
        trust_accel = gyro_mag < self._spin_threshold
        alpha = self._alpha if trust_accel else 0.0

        if not self._initialized:
            self._roll_deg = accel_roll
            self._pitch_deg = accel_pitch
            self._initialized = True
        else:
            roll_gyro = self._roll_deg + g_fwd * dt
            pitch_gyro = self._pitch_deg + g_left * dt
            self._roll_deg = alpha * accel_roll + (1.0 - alpha) * roll_gyro
            self._pitch_deg = alpha * accel_pitch + (1.0 - alpha) * pitch_gyro

        yaw_rate = self.mount.signed_yaw_rate_dps(g_up)
        self._yaw_deg += yaw_rate * dt

        return (
            self._roll_deg,
            self._pitch_deg,
            yaw_rate,
            trust_accel,
            g_fwd,
            g_left,
            g_up,
        )

    def update_from_raw(
        self,
        ax: float,
        ay: float,
        az: float,
        gx: float,
        gy: float,
        gz: float,
        dt: float,
    ) -> OrientSample:
        """Process one raw sensor sample (for tests and offline replay)."""
        roll, pitch, yaw_rate, trust_accel, g_fwd, g_left, g_up = self._update_filter(
            ax, ay, az, gx, gy, gz, dt
        )

        d_roll = roll - self._ref_roll
        d_pitch = pitch - self._ref_pitch
        d_yaw = wrap_degrees(self._yaw_deg - self._ref_yaw)

        return OrientSample(
            ts=time.time(),
            roll_deg=roll,
            pitch_deg=pitch,
            yaw_deg=self._yaw_deg,
            gyro_roll_dps=g_fwd,
            gyro_pitch_dps=g_left,
            gyro_yaw_dps=yaw_rate,
            tilt_deg=pitch,
            pan_deg=self._yaw_deg,
            delta_roll_deg=d_roll,
            delta_pitch_deg=d_pitch,
            delta_yaw_deg=d_yaw,
            delta_tilt_deg=d_pitch,
            delta_pan_deg=d_yaw,
            accel_trusted=trust_accel,
        )

    def update(self, dt: float) -> OrientSample:
        if not self._opened:
            raise RuntimeError("HeadImuOrient not open")
        ax, ay, az, gx, gy, gz = self._device.read_raw()
        return self.update_from_raw(ax, ay, az, gx, gy, gz, dt)


class HeadImuOrientReader:
    """Background sampler exposing the latest OrientSample."""

    def __init__(self, orient: HeadImuOrient) -> None:
        self._orient = orient
        self._lock = threading.Lock()
        self._latest: Optional[OrientSample] = None
        self._error: Optional[str] = None
        self._running = False
        self._thread: Optional[threading.Thread] = None

    @property
    def error(self) -> Optional[str]:
        return self._error

    def latest(self) -> Optional[OrientSample]:
        with self._lock:
            return self._latest

    def start(self) -> None:
        if self._running:
            return
        self._orient.open()
        self._running = True
        self._thread = threading.Thread(target=self._loop, name="head-imu-orient", daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._running = False
        if self._thread is not None:
            self._thread.join(timeout=1.5)
            self._thread = None
        self._orient.close()

    def zero_reference(self) -> None:
        self._orient.zero_reference()

    def lock_reference(self) -> tuple[float, float]:
        return self._orient.lock_reference()

    def reset_yaw(self) -> None:
        self._orient.reset_yaw()

    def _loop(self) -> None:
        interval = 1.0 / self._orient._sample_hz
        next_tick = time.perf_counter()
        prev = time.perf_counter()
        try:
            while self._running:
                now = time.perf_counter()
                sample = self._orient.update(now - prev)
                prev = now
                with self._lock:
                    self._latest = sample
                next_tick += interval
                sleep_for = next_tick - time.perf_counter()
                if sleep_for > 0:
                    time.sleep(sleep_for)
                else:
                    next_tick = time.perf_counter()
        except Exception as exc:
            self._error = str(exc)
            self._running = False


def load_imu_hw_config(config_path: Path | None = None) -> dict:
    """Load I2C + filter + startup level-cal settings from config imu section."""
    if config_path is None:
        config_path = Path(__file__).resolve().parent.parent / "config.yaml"
    defaults = {
        "bus": 1,
        "address": 0x69,
        "sample_hz": 100.0,
        "roll_pitch_alpha": 0.02,
        "auto_level_on_start": False,
        "auto_level_sec": 2.0,
        "auto_level_warmup_sec": 0.3,
        "auto_level_gyro_max_dps": 8.0,
        "auto_level_min_samples": 40,
    }
    if yaml is None or not config_path.exists():
        return defaults
    with open(config_path, encoding="utf-8") as f:
        cfg = yaml.safe_load(f) or {}
    imu = cfg.get("imu") or {}
    return {
        "bus": int(imu.get("i2c_bus", defaults["bus"])),
        "address": int(imu.get("address", defaults["address"])),
        "sample_hz": float(imu.get("sample_hz", defaults["sample_hz"])),
        "roll_pitch_alpha": float(imu.get("roll_pitch_alpha", defaults["roll_pitch_alpha"])),
        "auto_level_on_start": bool(imu.get("auto_level_on_start", defaults["auto_level_on_start"])),
        "auto_level_sec": float(imu.get("auto_level_sec", defaults["auto_level_sec"])),
        "auto_level_warmup_sec": float(imu.get("auto_level_warmup_sec", defaults["auto_level_warmup_sec"])),
        "auto_level_gyro_max_dps": float(
            imu.get("auto_level_gyro_max_dps", defaults["auto_level_gyro_max_dps"])
        ),
        "auto_level_min_samples": int(
            imu.get("auto_level_min_samples", defaults["auto_level_min_samples"])
        ),
    }
