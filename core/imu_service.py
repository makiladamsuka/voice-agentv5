"""ImuService: wraps ImuReader + HorizonTiltBias as a Blackboard writer.

Writes to BB: imu_pitch_deg, imu_roll_deg, imu_gyro_dps,
              imu_accel_trusted, imu_horizon_ok, imu_available.

If the BMI160 hardware is absent, imu_available stays False and the
service exits cleanly — no error propagation to other modules.
"""

from __future__ import annotations

import time
from pathlib import Path

try:
    import yaml
except ImportError:
    yaml = None

from core.blackboard import Blackboard

try:
    from imu_sensor import HorizonTiltBias, ImuReader, startup_level_calibrate
    _IMU_SENSOR_AVAILABLE = True
except ImportError:
    _IMU_SENSOR_AVAILABLE = False
    ImuReader = None  # type: ignore
    HorizonTiltBias = None  # type: ignore
    startup_level_calibrate = None  # type: ignore

APP_DIR = Path(__file__).resolve().parent.parent
DEFAULT_CONFIG_PATH = APP_DIR / "config.yaml"


def _load_yaml(path: Path) -> dict:
    if yaml is None or not path.exists():
        return {}
    with open(path, encoding="utf-8") as f:
        return yaml.safe_load(f) or {}


def _cfg(data, *keys, default=None):
    cur = data
    for key in keys:
        if not isinstance(cur, dict) or key not in cur:
            return default
        cur = cur[key]
    return cur


class ImuService:
    """Reads the BMI160 IMU in a background loop and publishes to the Blackboard."""

    def __init__(self, bb: Blackboard, config_path: Path = DEFAULT_CONFIG_PATH) -> None:
        self.bb = bb
        cfg = _load_yaml(config_path)
        imu = _cfg(cfg, "imu", default={}) or {}

        self.enabled: bool = bool(imu.get("enabled", False))
        self.bus: int = int(imu.get("i2c_bus", 1))
        self.address: int = int(imu.get("address", 0x69))
        self.sample_hz: float = float(imu.get("sample_hz", 100.0))
        self.roll_pitch_alpha: float = float(imu.get("roll_pitch_alpha", 0.02))
        _axis = imu.get("axis_remap")
        self.axis_remap = tuple(int(v) for v in _axis) if _axis else (-3, 2, -1)
        self.roll_offset: float = float(imu.get("roll_offset_deg", 0.0))
        self.pitch_offset: float = float(imu.get("pitch_offset_deg", 0.0))
        self.yaw_sign: float = float(imu.get("yaw_sign", 1.0))
        self.auto_level: bool = bool(imu.get("auto_level_on_start", True))
        self.auto_level_sec: float = float(imu.get("auto_level_sec", 2.0))
        self.auto_level_settle: float = float(imu.get("auto_level_settle_sec", 0.8))
        self.auto_level_warmup: float = float(imu.get("auto_level_warmup_sec", 0.3))
        self.auto_level_gyro_max: float = float(imu.get("auto_level_gyro_max_dps", 8.0))
        self.auto_level_min_samples: int = int(imu.get("auto_level_min_samples", 40))

        # HorizonTiltBias config
        self.horizon_gain: float = float(imu.get("horizon_tilt_gain", 1.0))
        self.horizon_sign: float = float(imu.get("horizon_tilt_sign", 1.0))
        self.horizon_smooth_hz: float = float(imu.get("horizon_pitch_smooth_hz", 4.0))
        self.horizon_max_bias: float = float(imu.get("horizon_max_bias_deg", 4.0))
        self.horizon_max_pitch: float = float(imu.get("horizon_max_pitch_deg", 25.0))
        self.horizon_gyro_max: float = float(imu.get("horizon_gyro_max_dps", 35.0))
        self.horizon_max_up: float = float(imu.get("horizon_max_up_from_center_deg", 2.0))
        self.horizon_max_down: float = float(imu.get("horizon_max_down_from_center_deg", 4.0))

        self._reader = None
        self._horizon = None

    # ─────────────────────────────────────────────────────────────────────────

    def run(self) -> None:
        """Main service loop. Exits if IMU is disabled or unavailable."""
        if not self.enabled or not _IMU_SENSOR_AVAILABLE:
            print("[ImuService] IMU disabled or imu_sensor not installed — skipping.")
            self.bb.write(imu_available=False)
            return

        try:
            self._reader = ImuReader(
                bus=self.bus,
                address=self.address,
                sample_hz=self.sample_hz,
                roll_pitch_alpha=self.roll_pitch_alpha,
                axis_remap=self.axis_remap,
                roll_offset_deg=self.roll_offset,
                pitch_offset_deg=self.pitch_offset,
                yaw_sign=self.yaw_sign,
            )
            self._reader.start()
        except Exception as exc:
            print(f"[ImuService] Hardware init failed: {exc}")
            self.bb.write(imu_available=False)
            return

        # Optional startup level calibration
        if self.auto_level and startup_level_calibrate is not None:
            try:
                print("[ImuService] Startup level calibration…")
                roll_off, pitch_off, residual, n = startup_level_calibrate(
                    self._reader,
                    duration_sec=self.auto_level_sec,
                    warmup_sec=self.auto_level_warmup,
                    max_gyro_dps=self.auto_level_gyro_max,
                    min_samples=self.auto_level_min_samples,
                )
                print(
                    f"[ImuService] Calibrated: roll_off={roll_off:+.2f}° "
                    f"pitch_off={pitch_off:+.2f}° residual={residual:+.2f}° n={n}"
                )
            except Exception as exc:
                print(f"[ImuService] Level calibration failed (non-fatal): {exc}")

        self._horizon = HorizonTiltBias(
            gain=self.horizon_gain,
            bias_sign=self.horizon_sign,
            smooth_hz=self.horizon_smooth_hz,
            max_bias_deg=self.horizon_max_bias,
            max_pitch_deg=self.horizon_max_pitch,
            max_up_from_center_deg=self.horizon_max_up,
            max_down_from_center_deg=self.horizon_max_down,
        )

        self.bb.write(imu_available=True)
        print("[ImuService] IMU online and publishing to Blackboard.")

        prev_ts = time.perf_counter()
        while self.bb.read("running")["running"]:
            sample = self._reader.latest()
            if sample is None:
                time.sleep(0.005)
                continue

            now = time.perf_counter()
            dt = max(0.001, min(0.1, now - prev_ts))
            prev_ts = now

            gyro_ok = sample.gyro_mag_dps < self.horizon_gyro_max
            self.bb.write(
                imu_pitch_deg=sample.pitch_deg,
                imu_roll_deg=sample.roll_deg,
                imu_gyro_dps=sample.gyro_mag_dps,
                imu_accel_trusted=sample.accel_trusted,
                imu_horizon_ok=gyro_ok,
            )
            time.sleep(0.008)  # ~120 Hz publish ceiling

        # Cleanup
        if self._reader is not None:
            self._reader.stop()
        self.bb.write(imu_available=False)
        print("[ImuService] Stopped.")
