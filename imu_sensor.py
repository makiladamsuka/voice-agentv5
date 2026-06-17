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
    gyro_x_dps: float
    gyro_y_dps: float
    gyro_z_dps: float
    accel_x_g: float
    accel_y_g: float
    accel_z_g: float
    timestamp: float


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
    ):
        self.roll_pitch_alpha = roll_pitch_alpha
        self.axis_remap = tuple(int(v) for v in axis_remap)
        self.spin_gyro_threshold_dps = spin_gyro_threshold_dps
        self.roll_offset_deg = 0.0
        self.pitch_offset_deg = 0.0
        self._roll_deg = 0.0
        self._pitch_deg = 0.0
        self._yaw_integral_deg = 0.0
        self._initialized = False

    def reset_yaw_integral(self) -> None:
        self._yaw_integral_deg = 0.0

    def yaw_integral_deg(self) -> float:
        return self._yaw_integral_deg

    def calibrate_level(self, samples: Sequence[ImuSample], min_samples: int = 40) -> None:
        if len(samples) < min_samples:
            raise ValueError(f"need at least {min_samples} stationary samples for level calibrate")
        self.roll_offset_deg = sum(s.roll_deg for s in samples) / len(samples)
        self.pitch_offset_deg = sum(s.pitch_deg for s in samples) / len(samples)

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
            gyro_x_dps=gx,
            gyro_y_dps=gy,
            gyro_z_dps=gz,
            accel_x_g=ax,
            accel_y_g=ay,
            accel_z_g=az,
            timestamp=now,
        )


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
    ):
        self._device = Bmi160(bus=bus, address=address)
        self._sample_hz = max(1.0, sample_hz)
        self._filter = ImuAttitudeFilter(
            roll_pitch_alpha=roll_pitch_alpha,
            axis_remap=axis_remap,
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
