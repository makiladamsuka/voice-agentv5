"""Shared base-motor helpers for Voice Agent V5 test scripts."""

from __future__ import annotations

import re
from pathlib import Path

from arduino_servo import ArduinoServoLink, BOOT_CPD

CONFIG_PATH = Path(__file__).resolve().parent / "config.yaml"
DEFAULT_MOVE_TIMEOUT = 15.0


def load_move_timeout() -> float:
    if CONFIG_PATH.exists():
        text = CONFIG_PATH.read_text(encoding="utf-8")
        match = re.search(r"^\s*move_timeout_sec:\s*([0-9]+\.?[0-9]*)\s*", text, re.MULTILINE)
        if match:
            return float(match.group(1))
    return DEFAULT_MOVE_TIMEOUT


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


def apply_config_cpd_to_nano(link: ArduinoServoLink) -> bool:
    cpd = load_counts_per_degree()
    if cpd <= BOOT_CPD + 0.05:
        print(
            "Config CPD not calibrated — run:\n"
            "  python tests/test_base_motor.py --calibrate-manual --degrees 90 --write-config"
        )
        return False
    if link.set_counts_per_degree(cpd):
        print(f"Applied CPD {cpd:.4f} from {CONFIG_PATH.name}")
        return True
    print(f"WARNING: failed to apply CPD {cpd:.4f} to ESP32")
    return False
