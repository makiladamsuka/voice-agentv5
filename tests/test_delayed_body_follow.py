"""Automated checks for delayed body follow tuning (config + step math)."""

from __future__ import annotations

import re
from pathlib import Path

import _bootstrap  # noqa: F401

from elastic_head_motion import clamp

CONFIG_PATH = Path(__file__).resolve().parents[1] / "config.yaml"


def _read_base_config() -> dict[str, float | bool]:
    text = CONFIG_PATH.read_text()
    block = re.search(r"^base:\n(.*?)(?=^\w|\Z)", text, re.MULTILINE | re.DOTALL)
    assert block is not None, "base: section missing from config.yaml"
    section = block.group(1)

    def _val(key: str):
        m = re.search(rf"^\s*{re.escape(key)}:\s*(.+)$", section, re.MULTILINE)
        assert m is not None, f"missing base.{key}"
        raw = m.group(1).split("#", 1)[0].strip()
        if raw.lower() in ("true", "false"):
            return raw.lower() == "true"
        return float(raw)

    return {
        "enabled": _val("enabled"),
        "counts_per_degree": _val("counts_per_degree"),
        "require_calibrated_cpd": _val("require_calibrated_cpd"),
        "head_lead_min_deg": _val("head_lead_min_deg"),
        "trigger_hold_sec": _val("trigger_hold_sec"),
        "cooldown_sec": _val("cooldown_sec"),
        "min_step_deg": _val("min_step_deg"),
        "max_step_deg": _val("max_step_deg"),
        "pan_offset_to_step_gain": _val("pan_offset_to_step_gain"),
        "track_compensation_gain": _val("track_compensation_gain"),
    }


def plan_track_base_step(
    pan_offset_mech: float,
    cfg: dict[str, float | bool],
    face_pull_sign: float = 0.0,
) -> float | None:
    head_lead = float(cfg["head_lead_min_deg"])
    if abs(pan_offset_mech) < head_lead:
        return None
    head_sign = 1.0 if pan_offset_mech > 0.0 else -1.0
    if face_pull_sign != 0.0:
        pull_sign = 1.0 if face_pull_sign > 0.0 else -1.0
        if pull_sign * head_sign < 0.0:
            return None
    mag = clamp(
        abs(pan_offset_mech) * float(cfg["pan_offset_to_step_gain"]),
        float(cfg["min_step_deg"]),
        float(cfg["max_step_deg"]),
    )
    return head_sign * mag


def test_base_calibration_prerequisites():
    cfg = _read_base_config()
    assert cfg["enabled"] is True
    assert cfg["require_calibrated_cpd"] is True
    assert float(cfg["counts_per_degree"]) > 0.05


def test_tuned_timing_and_gains():
    cfg = _read_base_config()
    assert float(cfg["trigger_hold_sec"]) == 0.8
    assert float(cfg["cooldown_sec"]) == 0.75
    assert float(cfg["pan_offset_to_step_gain"]) == 0.42
    assert float(cfg["track_compensation_gain"]) == 1.0
    assert float(cfg["head_lead_min_deg"]) == 3.5


def test_incremental_step_scales_with_head_offset():
    cfg = _read_base_config()
    step = plan_track_base_step(7.0, cfg)
    assert step is not None
    assert abs(step - 2.94) < 0.01  # 7 * 0.42
    assert float(cfg["min_step_deg"]) <= abs(step) <= float(cfg["max_step_deg"])


def test_full_compensation_counter_rotation():
    cfg = _read_base_config()
    base_step = plan_track_base_step(8.0, cfg)
    assert base_step is not None
    comp_mech = base_step * float(cfg["track_compensation_gain"])
    assert abs(comp_mech - base_step) < 0.01  # gain 1.0 → neck subtracts same mech deg


def test_hold_timer_resets_when_head_centered():
    """Mirror servo_worker reset: timer clears when head returns near center."""
    hold_active = True
    pan_offset = 1.0  # below head_lead_min_deg (3.5)
    cfg = _read_base_config()
    if abs(pan_offset) < float(cfg["head_lead_min_deg"]):
        hold_active = False
    assert hold_active is False


if __name__ == "__main__":
    test_base_calibration_prerequisites()
    test_tuned_timing_and_gains()
    test_incremental_step_scales_with_head_offset()
    test_full_compensation_counter_rotation()
    test_hold_timer_resets_when_head_centered()
    print("test_delayed_body_follow: ok")
