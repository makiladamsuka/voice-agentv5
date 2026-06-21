"""ImuService: wraps ImuReader + HorizonTiltBias as a Blackboard writer.

Writes to BB: imu_pitch_deg, imu_roll_deg, imu_gyro_dps, imu_gyro_z_dps,
              imu_yaw_integral_deg, imu_accel_trusted, imu_horizon_ok, imu_available.

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
from base_yaw_controller import (
    EncoderImuDriftCorrector,
    HeadYawFusion,
    decompose_yaw,
    resolve_fusion_yaw,
)
from lib.head_mech import signed_pan_mech_deg

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
        self.horizon_bias: float = float(imu.get("horizon_pitch_bias_deg", 0.0))
        self.horizon_smooth_hz: float = float(imu.get("horizon_pitch_smooth_hz", 4.0))
        self.horizon_max_bias: float = float(imu.get("horizon_max_bias_deg", 4.0))
        self.horizon_max_pitch: float = float(imu.get("horizon_max_pitch_deg", 25.0))
        self.horizon_gyro_max: float = float(imu.get("horizon_gyro_max_dps", 35.0))
        self.horizon_max_up: float = float(imu.get("horizon_max_up_from_center_deg", 2.0))
        self.horizon_max_down: float = float(imu.get("horizon_max_down_from_center_deg", 4.0))

        servo = _cfg(cfg, "servo", default={}) or {}
        self._servo_cfg = servo
        self._tilt_center = float(servo.get("tilt_center", 110.0))
        self._tilt_min = float(servo.get("tilt_min", 100.0))
        self._tilt_max = float(servo.get("tilt_max", 150.0))
        self._tilt_mechanical_scale = float(servo.get("tilt_mechanical_scale", 1.0))
        self._held_tilt_center = self._tilt_center

        self.drift_correction_enabled = bool(imu.get("drift_correction_enabled", True))
        self._fusion = HeadYawFusion(imu_yaw_sign=self.yaw_sign)
        self._drift = EncoderImuDriftCorrector(
            stationary_hold_sec=float(imu.get("drift_stationary_hold_sec", 0.35)),
            enc_stable_deg=float(imu.get("drift_enc_stable_deg", 0.2)),
            pan_stable_deg=float(imu.get("drift_pan_stable_deg", 0.2)),
            gyro_max_dps=float(imu.get("drift_gyro_max_dps", 6.0)),
        )
        self._fusion_initialized = False
        self._prev_base_enc: float | None = None

        self._reader = None
        self._horizon = None

    # ─────────────────────────────────────────────────────────────────────────

    def run(self) -> None:
        """Main service loop. Exits if IMU is disabled or unavailable."""
        if not self.enabled or not _IMU_SENSOR_AVAILABLE:
            print("[ImuService] IMU disabled or imu_sensor not installed — skipping.")
            self.bb.write(
                imu_available=False,
                imu_calibrated=True,
                base_fusion_resync_request=False,
            )
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
            self.bb.write(
                imu_available=False,
                imu_calibrated=True,
                base_fusion_resync_request=False,
            )
            return

        # Reset yaw integral at boot — world yaw reference starts at 0.
        self._reader.filter.reset_yaw_integral()

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

        self.bb.write(imu_calibrated=True)

        self._horizon = HorizonTiltBias(
            gain=self.horizon_gain,
            bias_sign=self.horizon_sign,
            smooth_hz=self.horizon_smooth_hz,
            max_bias_deg=self.horizon_max_bias,
            max_pitch_deg=self.horizon_max_pitch,
            max_up_from_center_deg=self.horizon_max_up,
            max_down_from_center_deg=self.horizon_max_down,
            mechanical_scale=self._tilt_mechanical_scale,
        )
        self._horizon_pitch_bias = self.horizon_bias
        self._horizon.reset()

        self.bb.write(
            imu_available=True,
            imu_effective_tilt_center=self._tilt_center,
        )
        print("[ImuService] IMU online and publishing to Blackboard.")

        prev_ts = time.perf_counter()
        while self.bb.read("running")["running"]:
            if self.bb.read("base_watchdog_reset")["base_watchdog_reset"]:
                self._reader.filter.reset_yaw_integral()
                self._fusion_initialized = False
                self._drift.reset_motion_tracking()
                self.bb.write(base_watchdog_reset=False)

            sample = self._reader.latest()
            if sample is None:
                time.sleep(0.005)
                continue

            now_mono = time.perf_counter()
            now = time.time()
            dt = max(0.001, min(0.1, now_mono - prev_ts))
            prev_ts = now_mono

            bb_state = self.bb.read(
                "yaw_reference_locked",
                "base_encoder_deg",
                "servo_pan",
                "base_fusion_resync_request",
                "imu_drift_reset_request",
            )
            pan_mech = signed_pan_mech_deg(float(bb_state["servo_pan"]), self._servo_cfg)
            base_enc = float(bb_state["base_encoder_deg"])
            raw_yaw = self._reader.filter.yaw_integral_deg() * self.yaw_sign

            if bb_state.get("base_fusion_resync_request"):
                self._fusion.reset_reference(
                    pan_mech_deg=pan_mech,
                    base_encoder_deg=base_enc,
                    imu_yaw_total_deg=raw_yaw,
                    now=now,
                    lock_startup=True,
                )
                self._drift.reset_motion_tracking()
                self._fusion_initialized = True
                self._prev_base_enc = base_enc
                self.bb.write(base_fusion_resync_request=False)

            if bb_state.get("imu_drift_reset_request"):
                self._drift.reset_motion_tracking()
                self.bb.write(imu_drift_reset_request=False)

            if bb_state.get("yaw_reference_locked") and not self._fusion_initialized:
                self._fusion.reset_reference(
                    pan_mech_deg=pan_mech,
                    base_encoder_deg=base_enc,
                    imu_yaw_total_deg=raw_yaw,
                    now=now,
                    lock_startup=True,
                )
                self._fusion_initialized = True

            yaw_out = raw_yaw
            drift_correction = 0.0
            stationary = False
            inferred_base = base_enc
            decomp = None

            if self.drift_correction_enabled and self._fusion_initialized:
                self._fusion.imu_yaw_total_deg = raw_yaw
                corrected, drift_correction, stationary = self._drift.update(
                    self._fusion,
                    imu_yaw_raw=raw_yaw,
                    base_encoder_deg=base_enc,
                    pan_mech_deg=pan_mech,
                    gyro_dps=sample.gyro_mag_dps,
                    now=now,
                )
                yaw_out, inferred_base = resolve_fusion_yaw(
                    self._fusion,
                    imu_yaw_corrected=corrected,
                    base_encoder_deg=base_enc,
                    pan_mech_deg=pan_mech,
                    prev_base_encoder_deg=self._prev_base_enc,
                    enc_stable_deg=self._drift.enc_stable_deg,
                )
                if stationary and abs(drift_correction) > 0.01:
                    self._reader.filter.set_yaw_integral_deg(yaw_out / self.yaw_sign)
                decomp = decompose_yaw(
                    self._fusion,
                    imu_yaw_total=yaw_out,
                    base_encoder_deg=base_enc,
                    pan_mech_deg=pan_mech,
                )
            elif self._fusion_initialized:
                decomp = decompose_yaw(
                    self._fusion,
                    imu_yaw_total=raw_yaw,
                    base_encoder_deg=base_enc,
                    pan_mech_deg=pan_mech,
                )

            self._prev_base_enc = base_enc
            self._fusion.imu_yaw_total_deg = yaw_out

            gyro_ok = sample.gyro_mag_dps < self.horizon_gyro_max
            pitch = sample.accel_pitch_deg if sample.accel_trusted else sample.pitch_deg
            pitch -= self._horizon_pitch_bias

            if gyro_ok and self._horizon is not None:
                self._held_tilt_center = self._horizon.effective_center(
                    self._tilt_center,
                    pitch,
                    dt,
                    self._tilt_min,
                    self._tilt_max,
                )
            self.bb.write(
                imu_pitch_deg=sample.pitch_deg,
                imu_roll_deg=sample.roll_deg,
                imu_gyro_dps=sample.gyro_mag_dps,
                imu_gyro_z_dps=sample.gyro_z_dps,
                imu_yaw_raw_deg=raw_yaw,
                imu_yaw_integral_deg=yaw_out,
                imu_drift_correction_deg=drift_correction,
                fusion_stationary=stationary,
                imu_inferred_base_deg=inferred_base,
                body_yaw_deg=decomp.body_yaw_deg if decomp else base_enc,
                head_yaw_on_body_deg=decomp.head_yaw_on_body_deg if decomp else 0.0,
                imu_yaw_rel_deg=decomp.imu_yaw_rel_deg if decomp else yaw_out,
                base_world_yaw_deg=(
                    decomp.world_head_yaw_deg if decomp else base_enc + pan_mech
                ),
                head_imu_vs_servo_delta_deg=(
                    decomp.head_imu_vs_servo_delta_deg if decomp else 0.0
                ),
                imu_accel_trusted=sample.accel_trusted,
                imu_horizon_ok=gyro_ok,
                imu_effective_tilt_center=self._held_tilt_center,
            )
            time.sleep(0.008)  # ~120 Hz publish ceiling

        # Cleanup
        if self._reader is not None:
            self._reader.stop()
        self.bb.write(imu_available=False)
        print("[ImuService] Stopped.")
