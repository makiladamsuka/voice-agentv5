"""Named arm pose presets (A0–A3 degrees) stored in JSON.

Used by the safe arm jogger for save/recall. Later, map emotion names to pose
keys for idle/gesture arm positions, e.g.::

    EMOTION_ARM_POSES = {"idle": "home", "explaining": "explaining1"}
    arms = presets.get(EMOTION_ARM_POSES[emotion])
"""

from __future__ import annotations

import json
import re
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

DEFAULT_PRESETS_PATH = Path(__file__).resolve().parent / "tests" / "arm_pose_presets.json"
_POSE_KEY_RE = re.compile(r"^[a-z0-9_]+$")


def normalize_pose_name(raw: str) -> str:
    """Convert user input to a stable pose key (snake_case, alphanumeric)."""
    s = raw.strip().lower()
    s = re.sub(r"[^a-z0-9]+", "_", s)
    s = s.strip("_")
    if not s or not _POSE_KEY_RE.match(s):
        raise ValueError(f"invalid pose name {raw!r} (use letters, numbers, underscores)")
    return s


def _pose_dict(a0: float, a1: float, a2: float, a3: float) -> dict[str, float]:
    return {
        "a0": round(a0, 1),
        "a1": round(a1, 1),
        "a2": round(a2, 1),
        "a3": round(a3, 1),
    }


def _pose_tuple(entry: dict[str, Any]) -> tuple[float, float, float, float]:
    return (
        float(entry["a0"]),
        float(entry["a1"]),
        float(entry["a2"]),
        float(entry["a3"]),
    )


class ArmPosePresets:
    """Load/save named arm poses from a JSON file."""

    def __init__(self, path: Path, poses: dict[str, dict[str, float]]) -> None:
        self._path = path
        self._poses = poses

    @property
    def path(self) -> Path:
        return self._path

    @classmethod
    def load(cls, path: Path | str = DEFAULT_PRESETS_PATH) -> ArmPosePresets:
        p = Path(path)
        if not p.is_file():
            return cls(p, {})
        data = json.loads(p.read_text(encoding="utf-8"))
        poses = {k: dict(v) for k, v in data.get("poses", {}).items()}
        return cls(p, poses)

    @classmethod
    def load_or_create_home(
        cls,
        path: Path | str = DEFAULT_PRESETS_PATH,
        *,
        home: tuple[float, float, float, float],
    ) -> ArmPosePresets:
        store = cls.load(path)
        if not store._poses:
            store._poses["home"] = _pose_dict(*home)
            store._write()
        return store

    def list_names(self) -> list[str]:
        return sorted(self._poses.keys())

    def get(self, name: str) -> tuple[float, float, float, float]:
        key = normalize_pose_name(name) if name not in self._poses else name
        if key not in self._poses:
            known = ", ".join(self.list_names()) or "(none)"
            raise KeyError(f"unknown pose {name!r} — saved: {known}")
        return _pose_tuple(self._poses[key])

    def save(
        self,
        name: str,
        a0: float,
        a1: float,
        a2: float,
        a3: float,
        *,
        overwrite: bool = True,
    ) -> str:
        key = normalize_pose_name(name)
        if key in self._poses and not overwrite:
            raise ValueError(f"pose {key!r} already exists")
        self._poses[key] = _pose_dict(a0, a1, a2, a3)
        self._write()
        return key

    def delete(self, name: str) -> bool:
        key = normalize_pose_name(name) if name not in self._poses else name
        if key not in self._poses:
            return False
        del self._poses[key]
        self._write()
        return True

    def _write(self) -> None:
        self._path.parent.mkdir(parents=True, exist_ok=True)
        payload = {
            "version": 1,
            "updated_at": datetime.now(timezone.utc).isoformat(),
            "poses": self._poses,
        }
        text = json.dumps(payload, indent=2) + "\n"
        tmp = self._path.with_suffix(self._path.suffix + ".tmp")
        tmp.write_text(text, encoding="utf-8")
        tmp.replace(self._path)
