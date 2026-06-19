"""BMI160 head IMU reader for Raspberry Pi I2C (roll/pitch + gyro yaw validation)."""

from __future__ import annotations

import math
import threading
import time
from dataclasses import dataclass
from typing import Optional, Sequence

try:
    from smbus2 import SMBus
except ImportError:  # pragma: no cover - Pi runtime dependency
    SMBus = None  # type: ignore

CHIP_ID_REG = 0x00
CHIP_ID_EXPECTED = 0xD1
PMU_STATUS_REG = 0x03
PMU_NORMAL_BOTH = 0x14
GYR_DATA_REG = 0x0C
ACC_DATA_REG = 0x12
CMD_REG = 0x7E
ACC_CONF_REG = 0x40
ACC_RANGE_REG = 0x41
GYR_CONF_REG = 0x42
GYR_RANGE_REG = 0x43
SOFT_RESET_CMD = 0xB6
CMD_ACCEL_NORMAL = 0x11
CMD_GYRO_NORMAL = 0x15
ACC_CONF_100HZ = 0x28
GYR_CONF_100HZ = 0x28
ACC_RANGE_2G = 0x03
GYR_RANGE_250DPS = 0x03

ACC_LSB_PER_G = 16384.0
GYR_LSB_PER_DPS = 131.2
BMI160_ADDRESSES = (0x68, 0x69)


def probe_bmi160_address(bus: int, preferred: Optional[int] = None) -> int:
    """Return I2C address of a responding BMI160, or raise RuntimeError."""
    if SMBus is None:
        raise RuntimeError("smbus2 not installed; pip install smbus2")
    candidates: list[int] = []
    if preferred is not None:
        candidates.append(preferred)
    for addr in BMI160_ADDRESSES:
        if addr not in candidates:
            candidates.append(addr)
    with SMBus(bus) as i2c:
        for addr in candidates:
            try:
                chip = i2c.read_byte_data(addr, CHIP_ID_REG)
            except OSError:
                continue
            if chip == CHIP_ID_EXPECTED:
                return addr
    tried = ", ".join(f"0x{a:02X}" for a in candidates)
    raise RuntimeError(f"BMI160 not found on I2C bus {bus} (tried {tried})")


@dataclass(frozen=True)
class ImuSample:
    roll_deg: float
    pitch_deg: float
    accel_roll_deg: float
    accel_pitch_deg: float
    gyro_x_dps: float
    gyro_y_dps: float
    gyro_z_dps: float
    accel_x_g: float
    accel_y_g: float
    accel_z_g: float
    timestamp: float
    accel_trusted: bool = True

    @property
    def gyro_mag_dps(self) -> float:
        return math.sqrt(
            self.gyro_x_dps * self.gyro_x_dps
            + self.gyro_y_dps * self.gyro_y_dps
            + self.gyro_z_dps * self.gyro_z_dps
        )


def _read_i16(lo: int, hi: int) -> int:
    value = (hi << 8) | lo
    if value >= 0x8000:
        value -= 0x10000
    return value


def _remap_vec(vec: Sequence[float], axis_remap: Sequence[int]) -> tuple[float, float, float]:
    """Map sensor XYZ into filter frame [forward, left, up/yaw].

    axis_remap values use 1-based sensor axes: 1=+X, 2=+Y, 3=+Z; negate to flip.
    Example PCB (+X up, +Y left, +Z back): [-3, 2, 1]  (forward=-Z, left=+Y, up=+X).
  """
    out: list[float] = []
    for idx in axis_remap:
        sign = -1.0 if idx < 0 else 1.0
        axis = abs(int(idx))
        if axis in (1, 2, 3):
            out.append(vec[axis - 1] * sign)
        elif axis in (0, 1, 2):
            # Legacy 0-based: 0=+X, 1=+Y, 2=+Z
            out.append(vec[axis] * sign)
        else:
            out.append(0.0)
    while len(out) < 3:
        out.append(0.0)
    return out[0], out[1], out[2]


def _accel_roll_pitch(ax: float, ay: float, az: float) -> tuple[float, float]:
    pitch = math.degrees(math.atan2(-ax, math.sqrt(ay * ay + az * az)))
    roll = math.degrees(math.atan2(ay, az))
    return roll, pitch


class Bmi160:
    """Low-level BMI160 register access."""

    def __init__(self, bus: int = 1, address: int = 0x68):
        self._bus_num = bus
        self._address = address
        self._bus: Optional[SMBus] = None

    def open(self) -> None:
        if SMBus is None:
            raise RuntimeError("smbus2 not installed; pip install smbus2")
        self._bus = SMBus(self._bus_num)
        try:
            chip = self._read_u8(CHIP_ID_REG)
        except OSError:
            chip = None
        if chip != CHIP_ID_EXPECTED:
            resolved = probe_bmi160_address(self._bus_num, self._address)
            if resolved != self._address:
                print(f"BMI160 at 0x{resolved:02X} (configured 0x{self._address:02X})")
            self._address = resolved
            chip = self._read_u8(CHIP_ID_REG)
        if chip != CHIP_ID_EXPECTED:
            raise RuntimeError(
                f"BMI160 not found at 0x{self._address:02X} (CHIP_ID=0x{chip:02X}, expected 0x{CHIP_ID_EXPECTED:02X})"
            )
        self._init_sensors()

    def _init_sensors(self) -> None:
        """Bring accel + gyro out of suspend via the CMD register (not PWR_CTRL)."""
        self._write_u8(CMD_REG, SOFT_RESET_CMD)
        time.sleep(0.05)
        self._write_u8(CMD_REG, CMD_ACCEL_NORMAL)
        time.sleep(0.01)
        self._write_u8(CMD_REG, CMD_GYRO_NORMAL)
        time.sleep(0.08)
        self._write_u8(ACC_CONF_REG, ACC_CONF_100HZ)
        self._write_u8(ACC_RANGE_REG, ACC_RANGE_2G)
        self._write_u8(GYR_CONF_REG, GYR_CONF_100HZ)
        self._write_u8(GYR_RANGE_REG, GYR_RANGE_250DPS)
        time.sleep(0.01)
        pmu = self._read_u8(PMU_STATUS_REG)
        if pmu != PMU_NORMAL_BOTH:
            raise RuntimeError(
                f"BMI160 sensors did not enter normal mode (PMU_STATUS=0x{pmu:02X}, expected 0x{PMU_NORMAL_BOTH:02X})"
            )

    def close(self) -> None:
        if self._bus is not None:
            self._bus.close()
            self._bus = None

    def _read_u8(self, reg: int) -> int:
        if self._bus is None:
            raise RuntimeError("BMI160 bus not open")
        return self._bus.read_byte_data(self._address, reg)

    def _write_u8(self, reg: int, value: int) -> None:
        if self._bus is None:
            raise RuntimeError("BMI160 bus not open")
        self._bus.write_byte_data(self._address, reg, value & 0xFF)

    def read_raw(self) -> tuple[float, float, float, float, float, float]:
        if self._bus is None:
            raise RuntimeError("BMI160 bus not open")
        gyr = self._bus.read_i2c_block_data(self._address, GYR_DATA_REG, 6)
        acc = self._bus.read_i2c_block_data(self._address, ACC_DATA_REG, 6)
        gx = _read_i16(gyr[0], gyr[1]) / GYR_LSB_PER_DPS
        gy = _read_i16(gyr[2], gyr[3]) / GYR_LSB_PER_DPS
        gz = _read_i16(gyr[4], gyr[5]) / GYR_LSB_PER_DPS
        ax = _read_i16(acc[0], acc[1]) / ACC_LSB_PER_G
        ay = _read_i16(acc[2], acc[3]) / ACC_LSB_PER_G
        az = _read_i16(acc[4], acc[5]) / ACC_LSB_PER_G
        return ax, ay, az, gx, gy, gz


class ImuAttitudeFilter:
    """Complementary roll/pitch filter with level offsets and yaw integration."""

    def __init__(
        self,
        *,
        roll_pitch_alpha: float = 0.02,
        axis_remap: Sequence[int] = (0, 1, 2),
        spin_gyro_threshold_dps: float = 80.0,
        roll_offset_deg: float = 0.0,
        pitch_offset_deg: float = 0.0,
    ):
        self.roll_pitch_alpha = roll_pitch_alpha
        self.axis_remap = tuple(int(v) for v in axis_remap)
        self.spin_gyro_threshold_dps = spin_gyro_threshold_dps
        self.roll_offset_deg = roll_offset_deg
        self.pitch_offset_deg = pitch_offset_deg
        self._roll_deg = 0.0
        self._pitch_deg = 0.0
        self._yaw_integral_deg = 0.0
        self._initialized = False

    def reset_yaw_integral(self) -> None:
        self._yaw_integral_deg = 0.0

    def raw_roll_pitch_deg(self) -> tuple[float, float]:
        """Filter roll/pitch before level offsets (for startup calibration)."""
        return self._roll_deg, self._pitch_deg

    def yaw_integral_deg(self) -> float:
        return self._yaw_integral_deg

    def calibrate_level(
        self,
        samples: Sequence[ImuSample],
        min_samples: int = 40,
        *,
        incremental: bool = False,
        max_gyro_dps: Optional[float] = None,
        pitch_only: bool = False,
    ) -> int:
        """Set level offsets so the averaged pose reads ~0° pitch (and roll unless pitch_only).

        Returns the number of samples used. When max_gyro_dps is set, only low-motion
        samples are averaged (better for startup calibration at mechanical upright).
        """
        pool = list(samples)
        if max_gyro_dps is not None:
            still = [
                s
                for s in pool
                if s.gyro_mag_dps <= max_gyro_dps and s.accel_trusted
            ]
            if len(still) >= min_samples:
                pool = still
        if len(pool) < min_samples:
            raise ValueError(f"need at least {min_samples} samples for level calibrate (got {len(pool)})")
        mean_roll = sum(s.roll_deg for s in pool) / len(pool)
        mean_pitch = sum(s.pitch_deg for s in pool) / len(pool)
        if incremental:
            if not pitch_only:
                self.roll_offset_deg += mean_roll
            self.pitch_offset_deg += mean_pitch
        else:
            if not pitch_only:
                self.roll_offset_deg = mean_roll
            self.pitch_offset_deg = mean_pitch
        return len(pool)

    def calibrate_level_at_rest(
        self,
        raw_roll_pitch: Sequence[tuple[float, float]],
        min_samples: int = 40,
        *,
        pitch_only: bool = False,
    ) -> tuple[float, float]:
        """Set level offsets from raw filter angles averaged while head is still."""
        if len(raw_roll_pitch) < min_samples:
            raise ValueError(
                f"need at least {min_samples} stationary raw samples for level calibrate "
                f"(got {len(raw_roll_pitch)})"
            )
        rolls = [r for r, _ in raw_roll_pitch]
        pitches = [p for _, p in raw_roll_pitch]
        if not pitch_only:
            self.roll_offset_deg = sum(rolls) / len(rolls)
        self.pitch_offset_deg = sum(pitches) / len(pitches)
        return self.roll_offset_deg, self.pitch_offset_deg

    def update(self, ax: float, ay: float, az: float, gx: float, gy: float, gz: float, dt: float) -> ImuSample:
        ax, ay, az = _remap_vec((ax, ay, az), self.axis_remap)
        gx, gy, gz = _remap_vec((gx, gy, gz), self.axis_remap)

        accel_roll, accel_pitch = _accel_roll_pitch(ax, ay, az)
        gyro_mag = math.sqrt(gx * gx + gy * gy + gz * gz)
        trust_accel = gyro_mag < self.spin_gyro_threshold_dps
        alpha = self.roll_pitch_alpha if trust_accel else 0.0

        if not self._initialized:
            self._roll_deg = accel_roll
            self._pitch_deg = accel_pitch
            self._initialized = True
        else:
            dt = max(0.0005, min(0.1, dt))
            roll_gyro = self._roll_deg + gx * dt
            pitch_gyro = self._pitch_deg + gy * dt
            self._roll_deg = alpha * accel_roll + (1.0 - alpha) * roll_gyro
            self._pitch_deg = alpha * accel_pitch + (1.0 - alpha) * pitch_gyro

        self._yaw_integral_deg += gz * dt

        now = time.time()
        return ImuSample(
            roll_deg=self._roll_deg - self.roll_offset_deg,
            pitch_deg=self._pitch_deg - self.pitch_offset_deg,
            accel_roll_deg=accel_roll - self.roll_offset_deg,
            accel_pitch_deg=accel_pitch - self.pitch_offset_deg,
            gyro_x_dps=gx,
            gyro_y_dps=gy,
            gyro_z_dps=gz,
            accel_x_g=ax,
            accel_y_g=ay,
            accel_z_g=az,
            timestamp=now,
            accel_trusted=trust_accel,
        )


def startup_level_calibrate(
    reader: "ImuReader",
    *,
    duration_sec: float = 2.0,
    warmup_sec: float = 0.3,
    max_gyro_dps: float = 8.0,
    min_samples: int = 40,
    pitch_only: bool = False,
) -> tuple[float, float, float, int]:
    """Zero IMU pitch (and roll) at the current mechanical upright pose.

    Averages raw filter angles from still samples, then sets absolute offsets so
    horizon leveling starts from pitch ≈ 0°. Returns (roll_off, pitch_off, residual_pitch, n).
    """
    reader.filter.roll_offset_deg = 0.0
    reader.filter.pitch_offset_deg = 0.0
    if warmup_sec > 0:
        time.sleep(warmup_sec)
    raw_at_rest: list[tuple[float, float]] = []
    deadline = time.time() + duration_sec
    while time.time() < deadline:
        sample = reader.latest()
        if (
            sample is not None
            and sample.accel_trusted
            and sample.gyro_mag_dps <= max_gyro_dps
        ):
            raw_at_rest.append(reader.filter.raw_roll_pitch_deg())
        time.sleep(0.01)
    roll_off, pitch_off = reader.filter.calibrate_level_at_rest(
        raw_at_rest,
        min_samples=min_samples,
        pitch_only=pitch_only,
    )
    calibrated_at = time.time()
    latest = reader.latest()
    deadline = time.time() + 0.1
    while latest is not None and latest.timestamp < calibrated_at and time.time() < deadline:
        time.sleep(0.005)
        latest = reader.latest()
    residual_pitch = latest.pitch_deg if latest is not None else 0.0
    return roll_off, pitch_off, residual_pitch, len(raw_at_rest)


class ImuReader:
    """Background IMU sampling thread."""

    def __init__(
        self,
        *,
        bus: int = 1,
        address: int = 0x68,
        sample_hz: float = 100.0,
        roll_pitch_alpha: float = 0.02,
        axis_remap: Sequence[int] = (0, 1, 2),
        roll_offset_deg: float = 0.0,
        pitch_offset_deg: float = 0.0,
    ):
        self._device = Bmi160(bus=bus, address=address)
        self._sample_hz = max(1.0, sample_hz)
        self._filter = ImuAttitudeFilter(
            roll_pitch_alpha=roll_pitch_alpha,
            axis_remap=axis_remap,
            roll_offset_deg=roll_offset_deg,
            pitch_offset_deg=pitch_offset_deg,
        )
        self._lock = threading.Lock()
        self._latest: Optional[ImuSample] = None
        self._thread: Optional[threading.Thread] = None
        self._running = False
        self._error: Optional[str] = None

    @property
    def filter(self) -> ImuAttitudeFilter:
        return self._filter

    @property
    def error(self) -> Optional[str]:
        return self._error

    def latest(self) -> Optional[ImuSample]:
        with self._lock:
            return self._latest

    def start(self) -> None:
        if self._running:
            return
        self._device.open()
        self._running = True
        self._thread = threading.Thread(target=self._loop, name="imu-reader", daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._running = False
        if self._thread is not None:
            self._thread.join(timeout=1.5)
            self._thread = None
        self._device.close()

    def _loop(self) -> None:
        interval = 1.0 / self._sample_hz
        next_tick = time.perf_counter()
        prev_ts = time.perf_counter()
        try:
            while self._running:
                ax, ay, az, gx, gy, gz = self._device.read_raw()
                now = time.perf_counter()
                dt = now - prev_ts
                prev_ts = now
                sample = self._filter.update(ax, ay, az, gx, gy, gz, dt)
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


class HorizonTiltBias:
    """Low-pass IMU pitch and map it to a bounded dynamic servo tilt center."""

    def __init__(
        self,
        *,
        gain: float = 1.0,
        bias_sign: float = 1.0,
        smooth_hz: float = 4.0,
        max_bias_deg: float = 4.0,
        max_pitch_deg: float = 25.0,
        max_up_from_center_deg: float = 2.0,
        max_down_from_center_deg: float = 4.0,
    ):
        self.gain = gain
        self.bias_sign = 1.0 if bias_sign >= 0.0 else -1.0
        self._smooth_hz = max(0.1, smooth_hz)
        self._max_bias_deg = max(0.0, max_bias_deg)
        self._max_pitch_deg = max(1.0, max_pitch_deg)
        self._max_up_from_center_deg = max(0.0, max_up_from_center_deg)
        self._max_down_from_center_deg = max(0.0, max_down_from_center_deg)
        self._pitch_smooth = 0.0
        self._initialized = False

    def reset(self) -> None:
        self._pitch_smooth = 0.0
        self._initialized = False

    def update(self, pitch_deg: float, dt: float) -> float:
        dt = max(0.0005, min(0.2, dt))
        alpha = 1.0 - math.exp(-dt * self._smooth_hz)
        pitch_deg = max(-self._max_pitch_deg, min(self._max_pitch_deg, pitch_deg))
        if not self._initialized:
            self._pitch_smooth = pitch_deg
            self._initialized = True
        else:
            self._pitch_smooth += (pitch_deg - self._pitch_smooth) * alpha
        return self._pitch_smooth

    def tilt_center(
        self,
        base_center: float,
        pitch_deg: float,
        tilt_min: float,
        tilt_max: float,
    ) -> float:
        bias = pitch_deg * self.gain * self.bias_sign
        bias = max(-self._max_bias_deg, min(self._max_bias_deg, bias))
        value = base_center - bias
        lo = max(tilt_min, base_center - self._max_down_from_center_deg)
        hi = min(tilt_max, base_center + self._max_up_from_center_deg)
        return max(lo, min(hi, value))

    def effective_center(
        self,
        base_center: float,
        pitch_deg: float,
        dt: float,
        tilt_min: float,
        tilt_max: float,
    ) -> float:
        smooth_pitch = self.update(pitch_deg, dt)
        return self.tilt_center(base_center, smooth_pitch, tilt_min, tilt_max)
