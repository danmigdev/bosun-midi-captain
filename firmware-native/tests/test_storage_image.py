"""Provision using real littlefs; reject unsafe/incompatible input before publishing."""
import argparse
import hashlib
import json
from pathlib import Path
import subprocess
import tempfile
import unittest


class StorageImage(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.base = Path(self.temporary.name)
        self.root = self.base / "config"
        self.profile = self.root / "profiles/test"
        (self.profile / "patches/01").mkdir(parents=True)
        (self.root / "active_profile.json").write_bytes(b'{ "id" : "test" }\r\n')
        (self.profile / "manifest.json").write_bytes(b'{"name":"Preserved","kind":"kemper_player"}\n')
        (self.profile / "device.json").write_bytes(b'{ "device_name" : "Captain", "future_setting": 42 }\r\n')
        (self.profile / "midi_learn.json").write_bytes(b'{"mappings":{}}')
        (self.profile / "patches/01/01.json").write_bytes(b'{"name":"CLEAN","bindings":[]}\n')
        self.output = self.base / "storage.bin"

    def run_builder(self, *, root=None, output=None, success=True):
        result = subprocess.run([str(ARGS.builder), "--config-root", str(root or self.root),
                                 "--output", str(output or self.output)],
                                capture_output=True, text=True, timeout=40)
        self.assertEqual(result.returncode == 0, success, (result.stdout, result.stderr))
        self.assertNotIn("AddressSanitizer", result.stderr)
        self.assertNotIn("runtime error:", result.stderr)
        if success:
            return json.loads(result.stdout)
        self.assertTrue(result.stderr, result)
        return result

    def assert_rejected(self, **arguments):
        result = self.run_builder(success=False, **arguments)
        self.assertFalse(self.output.exists())
        return result

    def snapshot(self):
        return {p.relative_to(self.root).as_posix(): hashlib.sha256(p.read_bytes()).hexdigest()
                for p in self.root.rglob("*") if p.is_file()}

    def test_real_backend_exact_readback_and_deterministic_image(self):
        original = self.snapshot()
        report = self.run_builder()
        self.assertEqual(report["storage_bytes"], 4 * 1024 * 1024)
        self.assertEqual(report["block_bytes"], 4096)
        self.assertEqual(report["files"], len(original))
        self.assertTrue(report["verified"])
        self.assertEqual(self.output.stat().st_size, 4 * 1024 * 1024)
        self.assertIn(b"littlefs", self.output.read_bytes()[3670016:3678208])
        second = self.base / "second.bin"
        self.run_builder(output=second)
        self.assertEqual(self.output.read_bytes(), second.read_bytes())
        self.assertEqual(self.snapshot(), original)

    def test_empty_factory_volume_is_verified_and_never_overwrites(self):
        command = [str(ARGS.builder), "--empty", "--output", str(self.output)]
        result = subprocess.run(command, capture_output=True, text=True, timeout=40)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertTrue(json.loads(result.stdout)["empty"])
        before = self.output.read_bytes()
        self.assertEqual(len(before), 4194304)
        self.assertIn(b"littlefs", before[3670016:3678208])
        again = subprocess.run(command, capture_output=True, text=True, timeout=40)
        self.assertNotEqual(again.returncode, 0)
        self.assertEqual(self.output.read_bytes(), before)

    def test_head_profile_and_shared_configuration_are_preserved(self):
        (self.profile / "manifest.json").write_text('{"kind":"kemper_head"}', encoding="utf-8")
        (self.profile / "device.json").write_text('{"kemper":{},"midi_channel":3}', encoding="utf-8")
        for bank in (99, 100, 125):
            directory = self.profile / f"patches/{bank:02d}"
            directory.mkdir()
            (directory / "05.json").write_text('{"name":"Performance"}', encoding="utf-8")
        original = self.snapshot()
        self.assertTrue(self.run_builder()["verified"])
        self.assertEqual(self.snapshot(), original)

    def test_all_625_performance_slots_fit_and_verify(self):
        (self.profile / "manifest.json").write_text('{"kind":"kemper_head"}')
        for bank in range(1, 126):
            directory = self.profile / f"patches/{bank:02d}"
            directory.mkdir(exist_ok=True)
            for slot in range(1, 6):
                (directory / f"{slot:02d}.json").write_text(json.dumps({
                    "name": f"Performance {bank} Slot {slot}",
                    "padding": "x" * 1800,
                    "on_enter": [{"type": "kemper_rig", "bank": bank, "slot": slot}],
                }))
        original = self.snapshot()
        report = self.run_builder()
        self.assertTrue(report["verified"])
        self.assertEqual(report["files"], len(original))
        self.assertEqual(self.snapshot(), original)

    def test_bank_range_and_canonical_paths(self):
        for bank in ("126", "001", "0100"):
            directory = self.profile / "patches" / bank
            directory.mkdir()
            self.assert_rejected()
            directory.rmdir()

    def test_existing_output_and_symlink_are_never_replaced(self):
        self.output.write_bytes(b"keep original")
        self.run_builder(success=False)
        self.assertEqual(self.output.read_bytes(), b"keep original")
        alias = self.base / "alias.bin"
        alias.symlink_to(self.output)
        self.run_builder(output=alias, success=False)
        self.assertTrue(alias.is_symlink())
        self.assertEqual(self.output.read_bytes(), b"keep original")

    def test_symlinked_file_directory_root_and_ancestor_rejected(self):
        target = self.base / "external.json"
        target.write_text("{}", encoding="utf-8")
        file = self.profile / "device.json"
        original = file.read_bytes()
        file.unlink()
        file.symlink_to(target)
        self.assert_rejected()
        file.unlink()
        file.write_bytes(original)
        alias = self.base / "root-link"
        alias.symlink_to(self.root, target_is_directory=True)
        self.assert_rejected(root=alias)
        ancestor = self.base / "ancestor-link"
        ancestor.symlink_to(self.base, target_is_directory=True)
        self.assert_rejected(root=ancestor / "config")
        (self.root / "profiles/linked").symlink_to(self.profile, target_is_directory=True)
        self.assert_rejected()
        self.assertEqual(target.read_text(), "{}")

    def test_traversal_and_symlinked_output_parent_rejected(self):
        self.assert_rejected(root=self.root / ".." / "config")
        self.assert_rejected(output=self.base / "config/../storage.bin")
        alias = self.base / "output-link"
        alias.symlink_to(self.base, target_is_directory=True)
        self.assert_rejected(output=alias / "storage.bin")

    def test_native_size_and_token_limits(self):
        device = self.profile / "device.json"
        device.write_text(json.dumps({"padding": "x" * 16384}), encoding="utf-8")
        self.assert_rejected()
        device.write_text(json.dumps({"padding": [0] * 1024}), encoding="utf-8")
        self.assert_rejected()
        device.write_text("{}", encoding="utf-8")
        (self.profile / "patches/01/01.json").write_text(json.dumps({"padding": "x" * 24576}), encoding="utf-8")
        self.assert_rejected()

    def test_invalid_json_plugin_and_active_profile_rejected(self):
        manifest = self.profile / "manifest.json"
        for contents in (b'{"kind":', b'[]', b'{"kind":"line6_helix"}'):
            manifest.write_bytes(contents)
            self.assert_rejected()
        manifest.write_text('{"kind":"kemper_player"}', encoding="utf-8")
        (self.root / "active_profile.json").write_text('{"id":"missing"}', encoding="utf-8")
        self.assert_rejected()
        (self.root / "active_profile.json").unlink()
        self.assert_rejected()

    def test_unknown_paths_and_noncanonical_coordinates_rejected(self):
        for path in (self.root / "extra.json", self.profile / "code.py",
                     self.profile / "patches/01/11.json", self.profile / "patches/01/1.json"):
            path.write_text("{}", encoding="utf-8")
            self.assert_rejected()
            path.unlink()

    def test_volume_full_leaves_no_output_and_does_not_change_input(self):
        for bank in range(1, 21):
            directory = self.profile / "patches" / f"{bank:02}"
            directory.mkdir(exist_ok=True)
            for slot in range(1, 11):
                (directory / f"{slot:02}.json").write_text(
                    json.dumps({"name": "Capacity", "padding": "x" * 22000}), encoding="utf-8")
        original = self.snapshot()
        result = self.assert_rejected()
        self.assertIn("littlefs import failed", result.stderr)
        self.assertEqual(self.snapshot(), original)


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--builder", type=Path, required=True)
    ARGS, remaining = parser.parse_known_args()
    unittest.main(argv=[__file__, *remaining])
