"""Unit tests for arm pose preset storage."""

from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

import _bootstrap  # noqa: F401

from arm_pose_presets import ArmPosePresets, normalize_pose_name


class TestNormalizePoseName(unittest.TestCase):
    def test_snake_case(self) -> None:
        self.assertEqual(normalize_pose_name("Explaining 1"), "explaining_1")

    def test_rejects_empty(self) -> None:
        with self.assertRaises(ValueError):
            normalize_pose_name("   ")


class TestArmPosePresets(unittest.TestCase):
    def setUp(self) -> None:
        self._tmpdir = tempfile.TemporaryDirectory()
        self.path = Path(self._tmpdir.name) / "poses.json"

    def tearDown(self) -> None:
        self._tmpdir.cleanup()

    def test_save_reload_round_trip(self) -> None:
        store = ArmPosePresets.load(self.path)
        key = store.save("wave hello", 47.0, 65.0, 64.0, 87.0)
        self.assertEqual(key, "wave_hello")
        again = ArmPosePresets.load(self.path)
        self.assertEqual(again.get("wave_hello"), (47.0, 65.0, 64.0, 87.0))

    def test_get_normalizes_name(self) -> None:
        store = ArmPosePresets.load(self.path)
        store.save("explaining1", 80.0, 50.0, 58.0, 85.0)
        self.assertEqual(store.get("Explaining1"), (80.0, 50.0, 58.0, 85.0))

    def test_overwrite(self) -> None:
        store = ArmPosePresets.load(self.path)
        store.save("idle", 1.0, 2.0, 3.0, 4.0)
        store.save("idle", 10.0, 20.0, 30.0, 40.0)
        self.assertEqual(store.get("idle"), (10.0, 20.0, 30.0, 40.0))

    def test_missing_raises(self) -> None:
        store = ArmPosePresets.load(self.path)
        with self.assertRaises(KeyError):
            store.get("nope")

    def test_load_or_create_home(self) -> None:
        home = (47.0, 65.0, 64.0, 87.0)
        store = ArmPosePresets.load_or_create_home(self.path, home=home)
        self.assertEqual(store.get("home"), home)
        self.assertTrue(self.path.is_file())

    def test_delete(self) -> None:
        store = ArmPosePresets.load(self.path)
        store.save("tmp", 1.0, 2.0, 3.0, 4.0)
        self.assertTrue(store.delete("tmp"))
        with self.assertRaises(KeyError):
            store.get("tmp")

    def test_json_format(self) -> None:
        store = ArmPosePresets.load(self.path)
        store.save("home", 47.0, 65.0, 64.0, 87.0)
        data = json.loads(self.path.read_text(encoding="utf-8"))
        self.assertEqual(data["version"], 1)
        self.assertIn("updated_at", data)
        self.assertEqual(data["poses"]["home"]["a0"], 47.0)


if __name__ == "__main__":
    unittest.main()
