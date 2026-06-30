"""Map graph read/write helpers for kiosk 3D map and map-builder."""

from __future__ import annotations

import json
from pathlib import Path
from typing import TYPE_CHECKING, Any, Callable
from urllib.parse import parse_qs, urlparse

if TYPE_CHECKING:
    from voice.map_navigation import MapNavigator

DEFAULT_BUILDINGS = {
    "building_1": {
        "position": [-6, 0, 0],
        "size": [10, 10],
        "color": "#ffffff",
        "name": "Building 1",
    },
    "building_2": {
        "position": [6, 0, -2],
        "size": [10, 10],
        "color": "#a5f3fc",
        "name": "Building 2",
    },
}


def _floor_from_query(path: str) -> str:
    parsed = urlparse(path)
    params = parse_qs(parsed.query)
    floor_vals = params.get("floor", ["floor_1"])
    return floor_vals[0] if floor_vals else "floor_1"


def graph_path_for_floor(data_dir: Path, floor: str) -> Path:
    return data_dir / f"map_graph_{floor}.json"


def get_map_graph(data_dir: Path, path: str) -> dict[str, Any]:
    floor = _floor_from_query(path)
    file_path = graph_path_for_floor(data_dir, floor)
    if file_path.is_file():
        try:
            return json.loads(file_path.read_text(encoding="utf-8"))
        except Exception:
            pass
    return {"nodes": [], "edges": [], "buildings": DEFAULT_BUILDINGS}


def save_map_graph(
    data_dir: Path,
    path: str,
    body: bytes,
    on_saved: Callable[[str], None] | None = None,
) -> dict[str, Any]:
    floor = _floor_from_query(path)
    data_dir.mkdir(parents=True, exist_ok=True)
    file_path = graph_path_for_floor(data_dir, floor)
    payload = json.loads(body.decode("utf-8"))
    file_path.write_text(json.dumps(payload, indent=2), encoding="utf-8")
    if on_saved:
        on_saved(floor)
    return {"success": True, "floor": floor}


def reload_navigator(navigator: MapNavigator | None, floor: str) -> None:
    if navigator is None:
        return
    if floor in ("floor_1", "default") or navigator.graph_path.name.endswith(f"{floor}.json"):
        navigator.reload()
