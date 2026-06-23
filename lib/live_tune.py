"""Live tuning parameters for debug dashboard (servo PID, smoothness, base steps)."""

from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any


@dataclass(frozen=True)
class TuneParam:
    key: str
    section: str
    label: str
    minimum: float
    maximum: float
    step: float


TUNE_PARAMS: tuple[TuneParam, ...] = (
    # Face tracking PID
    TuneParam("pan_pid_kp", "servo", "Pan Kp", 0.0, 2.0, 0.02),
    TuneParam("pan_pid_ki", "servo", "Pan Ki", 0.0, 0.5, 0.005),
    TuneParam("pan_pid_kd", "servo", "Pan Kd", 0.0, 1.5, 0.02),
    TuneParam("tilt_pid_kp", "servo", "Tilt Kp", 0.0, 2.0, 0.02),
    TuneParam("tilt_pid_ki", "servo", "Tilt Ki", 0.0, 0.5, 0.005),
    TuneParam("tilt_pid_kd", "servo", "Tilt Kd", 0.0, 1.0, 0.01),
    TuneParam("pid_integral_limit", "servo", "PID integral cap", 0.05, 2.0, 0.05),
    # Smoothness
    TuneParam("target_smooth_hz", "servo", "Target smooth Hz", 0.5, 15.0, 0.25),
    TuneParam("pan_track_smooth_hz", "servo", "Pan track smooth Hz", 0.5, 20.0, 0.25),
    TuneParam("tilt_smooth_hz", "servo", "Tilt smooth Hz", 0.5, 15.0, 0.25),
    TuneParam("wander_pan_smooth_hz", "servo", "Wander pan smooth Hz", 0.5, 15.0, 0.25),
    TuneParam("wander_tilt_smooth_hz", "servo", "Wander tilt smooth Hz", 0.5, 15.0, 0.25),
    TuneParam("face_alpha_x", "servo", "Face alpha X", 0.02, 0.8, 0.02),
    TuneParam("face_alpha_y", "servo", "Face alpha Y", 0.02, 0.8, 0.02),
    # Head step sizes
    TuneParam("pan_max_step_deg", "servo", "Track pan max step °", 0.1, 5.0, 0.05),
    TuneParam("wander_step_min_deg", "servo", "Wander head min step °", 1.0, 30.0, 0.5),
    TuneParam("wander_step_max_deg", "servo", "Wander head max step °", 2.0, 40.0, 0.5),
    # Base rotation step sizes
    TuneParam("min_step_deg", "base", "Base min step °", 0.5, 15.0, 0.5),
    TuneParam("max_step_deg", "base", "Base max step °", 1.0, 30.0, 0.5),
    TuneParam("wander_step_deg", "base", "Base wander step °", 1.0, 30.0, 0.5),
)

TUNE_KEYS: frozenset[str] = frozenset(p.key for p in TUNE_PARAMS)


def tune_schema_dicts() -> list[dict[str, Any]]:
    return [
        {
            "key": p.key,
            "section": p.section,
            "label": p.label,
            "min": p.minimum,
            "max": p.maximum,
            "step": p.step,
        }
        for p in TUNE_PARAMS
    ]


def _section_cfg(cfg: dict[str, Any], section: str) -> dict[str, Any]:
    block = cfg.get(section, {}) or {}
    return block if isinstance(block, dict) else {}


def _parse_config_float(raw: Any) -> float:
    """Parse a config scalar; tolerate inline comments glued to numbers."""
    if isinstance(raw, (int, float)):
        return float(raw)
    text = str(raw).strip()
    if "#" in text:
        text = text.split("#", 1)[0].strip()
    return float(text)


def sanitize_config(obj: Any) -> Any:
    """Fix YAML scalars where inline comments were parsed as part of the value."""
    if isinstance(obj, dict):
        return {k: sanitize_config(v) for k, v in obj.items()}
    if isinstance(obj, list):
        return [sanitize_config(v) for v in obj]
    if isinstance(obj, str):
        try:
            float(obj)
            return obj
        except ValueError:
            if "#" in obj:
                try:
                    return float(obj.split("#", 1)[0].strip())
                except ValueError:
                    pass
        return obj
    return obj


def load_tune_defaults_from_config(cfg: dict[str, Any]) -> dict[str, float]:
    servo = _section_cfg(cfg, "servo")
    base = _section_cfg(cfg, "base")
    out: dict[str, float] = {}
    for spec in TUNE_PARAMS:
        src = servo if spec.section == "servo" else base
        if spec.key in src:
            out[spec.key] = _parse_config_float(src[spec.key])
    return out


def merge_tune_values(
    live: dict[str, Any] | None,
    *,
    servo_cfg: dict[str, Any],
    base_cfg: dict[str, Any],
) -> dict[str, float]:
    defaults = load_tune_defaults_from_config({"servo": servo_cfg, "base": base_cfg})
    merged = dict(defaults)
    if live:
        for key in TUNE_KEYS:
            if key in live:
                merged[key] = float(live[key])
    return merged


def apply_servo_tune(loop: Any, tune: dict[str, Any]) -> None:
    """Apply live tune dict to a ServoLoop instance."""
    float_keys = {
        "pan_pid_kp", "pan_pid_ki", "pan_pid_kd",
        "tilt_pid_kp", "tilt_pid_ki", "tilt_pid_kd",
        "pid_integral_limit",
        "target_smooth_hz", "pan_track_smooth_hz", "tilt_smooth_hz",
        "wander_pan_smooth_hz", "wander_tilt_smooth_hz",
        "face_alpha_x", "face_alpha_y",
        "pan_max_step_deg", "wander_step_min_deg", "wander_step_max_deg",
    }
    for key in float_keys:
        if key not in tune:
            continue
        val = float(tune[key])
        setattr(loop, key, val)

    if any(k in tune for k in ("pan_pid_kp", "pan_pid_ki", "pan_pid_kd", "pid_integral_limit")):
        loop._pan_pid.kp = loop.pan_pid_kp
        loop._pan_pid.ki = loop.pan_pid_ki
        loop._pan_pid.kd = loop.pan_pid_kd
        loop._pan_pid.integral_limit = loop.pid_integral_limit

    if any(k in tune for k in ("tilt_pid_kp", "tilt_pid_ki", "tilt_pid_kd", "pid_integral_limit")):
        loop._tilt_pid.kp = loop.tilt_pid_kp
        loop._tilt_pid.ki = loop.tilt_pid_ki
        loop._tilt_pid.kd = loop.tilt_pid_kd
        loop._tilt_pid.integral_limit = loop.pid_integral_limit

    if "target_smooth_hz" in tune:
        loop._target_glide.smooth_hz = loop.target_smooth_hz


def apply_base_tune(ctrl: Any, tune: dict[str, Any]) -> None:
    """Apply live tune dict to a BaseController instance."""
    for key in ("min_step_deg", "max_step_deg", "wander_step_deg"):
        if key not in tune:
            continue
        val = float(tune[key])
        if key == "min_step_deg":
            ctrl.min_step = val
        elif key == "max_step_deg":
            ctrl.max_step = val
        elif key == "wander_step_deg":
            ctrl.wander_base_step = val


def save_tune_to_config(path: Path, tune: dict[str, Any]) -> list[str]:
    """Write tuned values into config.yaml. Returns list of keys updated."""
    text = path.read_text(encoding="utf-8")
    updated: list[str] = []
    for spec in TUNE_PARAMS:
        if spec.key not in tune:
            continue
        val = float(tune[spec.key])
        formatted = f"{val:.4f}".rstrip("0").rstrip(".")
        pattern = rf"^(\s*{re.escape(spec.key)}:\s*)([^\n#]+)(.*)$"
        repl = rf"\g<1>{formatted} \g<3>"
        new_text, n = re.subn(pattern, repl, text, count=1, flags=re.MULTILINE)
        if not n:
            raise RuntimeError(f"Could not find {spec.section}.{spec.key} in {path}")
        text = new_text
        updated.append(spec.key)
    path.write_text(text, encoding="utf-8")
    return updated
