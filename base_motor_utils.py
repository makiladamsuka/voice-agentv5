"""Shared base-motor helpers for Voice Agent V5 test scripts."""

from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path

from arduino_servo import ArduinoServoLink, BOOT_CPD

CONFIG_PATH = Path(__file__).resolve().parent / "config.yaml"
DEFAULT_MOVE_TIMEOUT = 15.0

# Plate-degree B commands are scaled to encoder counts inside ArduinoServoLink using
# the calibration block in config.yaml (counts_per_degree, encoder_sign, command_scale).
# Change those only via tests/test_base_motor.py — not from face-tracking tuning.


@dataclass(frozen=True)
class BaseCalibration:
    counts_per_degree: float
    encoder_sign: float
    command_scale: float

    @property
    def is_calibrated(self) -> bool:
        return self.counts_per_degree > BOOT_CPD + 0.05


def load_base_calibration() -> BaseCalibration:
    return BaseCalibration(
        counts_per_degree=load_counts_per_degree(),
        encoder_sign=load_encoder_sign(),
        command_scale=load_command_scale(),
    )


def configure_base_link(link: ArduinoServoLink) -> BaseCalibration:
    """Apply config.yaml base calibration once at connect. All B commands use plate degrees."""
    cal = load_base_calibration()
    link.base_command_scale = cal.command_scale
    if cal.is_calibrated:
        link.set_counts_per_degree(cal.counts_per_degree)
        link.set_encoder_sign(cal.encoder_sign)
    return cal


def load_move_timeout() -> float:
    if CONFIG_PATH.exists():
        text = CONFIG_PATH.read_text(encoding="utf-8")
        match = re.search(r"^\s*move_timeout_sec:\s*([0-9]+\.?[0-9]*)\s*", text, re.MULTILINE)
        if match:
            return float(match.group(1))
    return DEFAULT_MOVE_TIMEOUT


def load_zero_on_start() -> bool:
    if CONFIG_PATH.exists():
        text = CONFIG_PATH.read_text(encoding="utf-8")
        match = re.search(r"^\s*zero_on_start:\s*(true|false)\s*(?:#.*)?$", text, re.MULTILINE | re.IGNORECASE)
        if match:
            return match.group(1).lower() == "true"
    return False


def load_counts_per_degree() -> float:
    if CONFIG_PATH.exists():
        text = CONFIG_PATH.read_text(encoding="utf-8")
        match = re.search(
            r"^\s*counts_per_degree:\s*([0-9]+\.?[0-9]*)\s*(?:#.*)?$",
            text,
            re.MULTILINE,
        )
        if match:
            return float(match.group(1))
    return BOOT_CPD


def load_encoder_sign() -> float:
    if CONFIG_PATH.exists():
        text = CONFIG_PATH.read_text(encoding="utf-8")
        match = re.search(
            r"^\s*encoder_sign:\s*(-?[0-9]+\.?[0-9]*)\s*(?:#.*)?$",
            text,
            re.MULTILINE,
        )
        if match:
            return -1.0 if float(match.group(1)) < 0.0 else 1.0
    return 1.0


def load_command_scale() -> float:
    if CONFIG_PATH.exists():
        text = CONFIG_PATH.read_text(encoding="utf-8")
        match = re.search(
            r"^\s*command_scale:\s*([0-9]+\.?[0-9]*)\s*(?:#.*)?$",
            text,
            re.MULTILINE,
        )
        if match:
            return max(0.01, min(2.0, float(match.group(1))))
    return 1.0


def scale_base_command_deg(deg: float) -> float:
    return deg * load_command_scale()


def correct_command_scale(commanded_plate_deg: float, actual_plate_deg: float) -> float:
    """Multiply current scale by commanded/actual from a measured move."""
    if commanded_plate_deg <= 0 or actual_plate_deg <= 0:
        raise ValueError("commanded and actual plate degrees must be positive")
    return load_command_scale() * (commanded_plate_deg / actual_plate_deg)


def write_command_scale_to_config(scale: float) -> None:
    scale = max(0.01, min(2.0, scale))
    text = CONFIG_PATH.read_text(encoding="utf-8")
    if re.search(r"^\s*command_scale:\s*", text, re.MULTILINE):
        updated = re.sub(
            r"^(\s*command_scale:\s*)([^\n#]+)(.*)$",
            rf"\g<1>{scale:.4f}\3",
            text,
            count=1,
            flags=re.MULTILINE,
        )
    else:
        updated = re.sub(
            r"^(\s*encoder_sign:\s*[^\n]+)$",
            rf"\1\n  command_scale: {scale:.4f}",
            text,
            count=1,
            flags=re.MULTILINE,
        )
    if updated == text:
        raise RuntimeError("Could not write base.command_scale in config.yaml")
    CONFIG_PATH.write_text(updated, encoding="utf-8")


def write_cpd_to_config(cpd: float) -> None:
    text = CONFIG_PATH.read_text(encoding="utf-8")
    updated = re.sub(
        r"^(\s*counts_per_degree:\s*)([^\n#]+)(.*)$",
        rf"\g<1>{cpd:.6f}  # set by --calibrate-manual\3",
        text,
        count=1,
        flags=re.MULTILINE,
    )
    if updated == text:
        raise RuntimeError("Could not find base.counts_per_degree in config.yaml")
    CONFIG_PATH.write_text(updated, encoding="utf-8")


def write_encoder_sign_to_config(sign: float) -> None:
    sign_val = -1.0 if sign < 0.0 else 1.0
    text = CONFIG_PATH.read_text(encoding="utf-8")
    if re.search(r"^\s*encoder_sign:\s*", text, re.MULTILINE):
        updated = re.sub(
            r"^(\s*encoder_sign:\s*)([^\n#]+)(.*)$",
            rf"\g<1>{sign_val:.0f}  # set by --calibrate-manual\3",
            text,
            count=1,
            flags=re.MULTILINE,
        )
    else:
        updated = re.sub(
            r"^(\s*counts_per_degree:\s*[^\n]+)$",
            rf"\1\n  encoder_sign: {sign_val:.0f}  # set by --calibrate-manual",
            text,
            count=1,
            flags=re.MULTILINE,
        )
    if updated == text:
        raise RuntimeError("Could not write base.encoder_sign in config.yaml")
    CONFIG_PATH.write_text(updated, encoding="utf-8")


def apply_config_cpd_to_nano(link: ArduinoServoLink) -> bool:
    return apply_base_calibration_to_nano(link)


def apply_base_calibration_to_nano(link: ArduinoServoLink) -> bool:
    cal = configure_base_link(link)
    if not cal.is_calibrated:
        print(
            "Config CPD not calibrated — run:\n"
            "  python tests/test_base_motor.py --calibrate-manual --degrees 90 --write-config"
        )
        return False
    print(
        f"Applied base cal: CPD {cal.counts_per_degree:.4f}, "
        f"encoder_sign {cal.encoder_sign:+.0f}, command_scale {cal.command_scale:.4f}"
    )
    return True
