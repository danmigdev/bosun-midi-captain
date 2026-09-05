#!/usr/bin/env python3
"""Offline tests for the pinned Adafruit CircuitPython bundle provisioner."""

from __future__ import annotations

import hashlib
import importlib.util
import json
import subprocess
import sys
import tempfile
import unittest
import zipfile
from pathlib import Path


SCRIPT = Path(__file__).resolve().with_name("provision_adafruit_bundle.py")
SPEC = importlib.util.spec_from_file_location("adafruit_provision_under_test", SCRIPT)
PROVISION = importlib.util.module_from_spec(SPEC)
assert SPEC.loader is not None
sys.modules[SPEC.name] = PROVISION
SPEC.loader.exec_module(PROVISION)


class AdafruitBundleProvisionTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory(prefix="bosun-adafruit-test-")
        self.root = Path(self.temporary.name)
        self.tag = "20260729"
        self.filename = f"adafruit-circuitpython-bundle-9.x-mpy-{self.tag}.zip"
        self.files = {
            "adafruit_display_text/__init__.mpy": b"C\x06\x00\x1finit",
            "adafruit_display_text/bitmap_label.mpy": b"C\x06\x00\x1fbitmap",
            "adafruit_display_text/label.mpy": b"C\x06\x00\x1flabel",
            "adafruit_display_text/outlined_label.mpy": b"C\x06\x00\x1foutlined",
            "adafruit_display_text/scrolling_label.mpy": b"C\x06\x00\x1fscroll",
            "adafruit_display_text/text_box.mpy": b"C\x06\x00\x1fbox",
            "adafruit_pixelbuf.mpy": b"C\x06\x00\x1fpixelbuf",
            "adafruit_st7789.mpy": b"C\x06\x00\x1fst7789",
            "neopixel.mpy": b"C\x06\x00\x1fneopixel",
        }
        self.archive = self.root / self.filename
        self.lock_path = self.root / "lock.json"
        self._write_fixture()

    def tearDown(self):
        self.temporary.cleanup()

    def _write_fixture(self):
        prefix = self.filename[:-4] + "/lib/"
        with zipfile.ZipFile(self.archive, "w") as package:
            for name, data in self.files.items():
                package.writestr(prefix + name, data)
        lock = {
            "schema": 1,
            "tag": self.tag,
            "circuitpython_major": 9,
            "bundle_series": "9.x",
            "filename": self.filename,
            "url": (
                "https://github.com/adafruit/Adafruit_CircuitPython_Bundle/"
                f"releases/download/{self.tag}/{self.filename}"
            ),
            "size": self.archive.stat().st_size,
            "sha256": hashlib.sha256(self.archive.read_bytes()).hexdigest(),
            "files": {
                name: hashlib.sha256(data).hexdigest()
                for name, data in self.files.items()
            },
        }
        self.lock_path.write_text(json.dumps(lock), encoding="utf-8")

    def test_installs_exact_nine_files_and_preserves_bosun_roots(self):
        destination = self.root / "lib"
        (destination / "captain").mkdir(parents=True)
        (destination / "captain" / "app.mpy").write_bytes(b"bosun")
        (destination / "adafruit_display_text").mkdir()
        (destination / "adafruit_display_text" / "stale.mpy").write_bytes(b"stale")

        lock = PROVISION.load_lock(self.lock_path)
        self.assertEqual(PROVISION.provision(self.archive, destination, lock), 9)
        self.assertEqual((destination / "captain" / "app.mpy").read_bytes(), b"bosun")
        self.assertFalse((destination / "adafruit_display_text" / "stale.mpy").exists())
        self.assertEqual(PROVISION._installed_files(destination, lock), self.files)

    def test_archive_hash_mismatch_fails_before_touching_destination(self):
        destination = self.root / "lib"
        destination.mkdir()
        sentinel = destination / "neopixel.mpy"
        sentinel.write_bytes(b"known-good")
        tampered = bytearray(self.archive.read_bytes())
        tampered[-1] ^= 0x01
        self.archive.write_bytes(tampered)

        with self.assertRaisesRegex(PROVISION.ProvisionError, "SHA-256 mismatch"):
            PROVISION.provision(
                self.archive, destination, PROVISION.load_lock(self.lock_path)
            )
        self.assertEqual(sentinel.read_bytes(), b"known-good")

    def test_wrong_mpy_abi_is_rejected_even_when_hashes_match(self):
        self.files["neopixel.mpy"] = b"M\x06\x00\x1fmicropython"
        self._write_fixture()
        with self.assertRaisesRegex(PROVISION.ProvisionError, "not CircuitPython mpy-v6"):
            PROVISION.provision(
                self.archive, self.root / "lib", PROVISION.load_lock(self.lock_path)
            )

    def test_cli_offline_archive_and_check_modes(self):
        destination = self.root / "lib"
        install = subprocess.run(
            [
                sys.executable, str(SCRIPT), "--lock", str(self.lock_path),
                "--archive", str(self.archive), "--destination", str(destination),
            ],
            capture_output=True, text=True, check=False,
        )
        self.assertEqual(install.returncode, 0, install.stderr)
        check = subprocess.run(
            [
                sys.executable, str(SCRIPT), "--lock", str(self.lock_path),
                "--check", "--destination", str(destination),
            ],
            capture_output=True, text=True, check=False,
        )
        self.assertEqual(check.returncode, 0, check.stderr)

    def test_production_lock_and_all_producers_are_pinned_to_one_helper(self):
        tools = SCRIPT.parent
        repo = tools.parent
        lock = PROVISION.load_lock(tools / "adafruit-bundle-lock.json")
        self.assertEqual(lock["tag"], "20260729")
        self.assertEqual(
            lock["sha256"],
            "88131525b851c216db17c864ee3a1081df3da4fe8511af39a77f71a7338d6539",
        )
        self.assertEqual(lock["size"], 17_204_092)
        self.assertEqual(len(lock["files"]), 9)
        self.assertEqual(
            set(lock["files"]),
            {
                "adafruit_display_text/__init__.mpy",
                "adafruit_display_text/bitmap_label.mpy",
                "adafruit_display_text/label.mpy",
                "adafruit_display_text/outlined_label.mpy",
                "adafruit_display_text/scrolling_label.mpy",
                "adafruit_display_text/text_box.mpy",
                "adafruit_pixelbuf.mpy",
                "adafruit_st7789.mpy",
                "neopixel.mpy",
            },
        )

        release = (repo / ".github" / "workflows" / "release.yml").read_text()
        download = (tools / "download-assets.ps1").read_text()
        fdroid = (repo / "docs" / "fdroid-com.bosun.app.yml").read_text()
        for name, text in (("release", release), ("download", download), ("fdroid", fdroid)):
            with self.subTest(producer=name):
                self.assertIn("provision_adafruit_bundle.py", text)
                self.assertNotIn("Adafruit_CircuitPython_Bundle/releases/latest", text)
        self.assertEqual(release.count("provision_adafruit_bundle.py"), 2)
        for build_script in ("build-android.ps1", "package-portable.ps1"):
            text = (tools / build_script).read_text()
            self.assertIn("provision_adafruit_bundle.py", text)
            self.assertIn("--check", text)


if __name__ == "__main__":
    unittest.main(verbosity=2)
