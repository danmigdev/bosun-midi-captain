#!/usr/bin/env python3
"""Deterministic tests for the firmware-to-Tauri resource sync."""

from __future__ import annotations

import importlib.util
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path


SCRIPT = Path(__file__).resolve().with_name("sync_firmware_resources.py")
SPEC = importlib.util.spec_from_file_location("sync_firmware_resources_under_test", SCRIPT)
sync = importlib.util.module_from_spec(SPEC)
assert SPEC.loader is not None
sys.modules[SPEC.name] = sync
SPEC.loader.exec_module(sync)


class ResourceSyncTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="bosun-resource-sync-")
        self.root = Path(self.temp.name)
        self.firmware = self.root / "firmware"
        self.resources = self.root / "editor" / "src-tauri" / "resources"

        (self.firmware / "lib" / "captain").mkdir(parents=True)
        (self.firmware / "lib" / "plugins").mkdir()
        (self.firmware / "fonts").mkdir()
        (self.firmware / "boot.py").write_bytes(b"boot-v2\n")
        (self.firmware / "code.py").write_bytes(b"code-v2\n")
        (self.firmware / "fonts" / "display.pcf").write_bytes(b"font")
        (self.firmware / "lib" / "captain" / "app.py").write_bytes(b"large source")
        (self.firmware / "lib" / "captain" / "app.mpy").write_bytes(b"C\x06compiled")
        (self.firmware / "lib" / "captain" / "bindings.py").write_bytes(b"bindings-v2")
        (self.firmware / "lib" / "plugins" / "kemper.py").write_bytes(b"kemper-v2")

        (self.firmware / "__pycache__").mkdir()
        (self.firmware / "__pycache__" / "code.pyc").write_bytes(b"cache")
        (self.firmware / "scratch.tmp").write_bytes(b"partial")

        (self.resources / "firmware" / "lib" / "captain").mkdir(parents=True)
        (self.resources / "firmware" / "boot.py").write_bytes(b"boot-v1")
        (self.resources / "firmware" / "stale.py").write_bytes(b"stale")
        (self.resources / "firmware" / "lib" / "captain" / "stale.py").write_bytes(b"stale")

        (self.resources / "lib" / "captain").mkdir(parents=True)
        (self.resources / "lib" / "plugins").mkdir()
        (self.resources / "lib" / "captain" / "bindings.py").write_bytes(b"bindings-v1")
        (self.resources / "lib" / "captain" / "removed.py").write_bytes(b"stale")
        (self.resources / "lib" / "plugins" / "removed.py").write_bytes(b"stale")
        (self.resources / "lib" / "adafruit_display_text").mkdir()
        (self.resources / "lib" / "adafruit_display_text" / "label.mpy").write_bytes(b"vendor")
        (self.resources / "lib" / "neopixel.mpy").write_bytes(b"vendor-pixel")
        (self.resources / "lib" / "__pycache__").mkdir()
        (self.resources / "lib" / "__pycache__" / "old.pyc").write_bytes(b"cache")
        (self.resources / "circuitpython.uf2").write_bytes(b"uf2")

    def tearDown(self):
        self.temp.cleanup()

    def test_mirror_hashes_files_and_removes_stale_and_caches(self):
        result = sync.sync_repository(self.root)
        self.assertTrue(result.changed)
        mirror = self.resources / "firmware"
        self.assertEqual((mirror / "boot.py").read_bytes(), b"boot-v2\n")
        self.assertFalse((mirror / "stale.py").exists())
        self.assertFalse((mirror / "lib" / "captain" / "stale.py").exists())
        self.assertFalse((mirror / "__pycache__").exists())
        self.assertFalse((mirror / "scratch.tmp").exists())
        self.assertEqual(
            sync._snapshot(self.firmware),
            sync._snapshot(mirror),
        )

    def test_additive_lib_preserves_vendor_but_mirrors_bosun_namespaces(self):
        sync.sync_repository(self.root)
        resource_lib = self.resources / "lib"
        self.assertEqual((resource_lib / "captain" / "bindings.py").read_bytes(), b"bindings-v2")
        self.assertFalse((resource_lib / "captain" / "removed.py").exists())
        self.assertFalse((resource_lib / "plugins" / "removed.py").exists())
        self.assertEqual((resource_lib / "adafruit_display_text" / "label.mpy").read_bytes(), b"vendor")
        self.assertEqual((resource_lib / "neopixel.mpy").read_bytes(), b"vendor-pixel")
        self.assertFalse((resource_lib / "__pycache__").exists())

    def test_source_and_compiled_forms_are_mirrored_for_installer_selection(self):
        sync.sync_repository(self.root)
        captain = self.resources / "firmware" / "lib" / "captain"
        self.assertTrue((captain / "app.py").is_file())
        self.assertTrue((captain / "app.mpy").is_file())

        # The resource mirror remains reviewable and exact; deployment itself
        # must select the compiled sibling.  This is enforced in both the OTA
        # listing and initial-volume copy paths in installer.rs.
        installer = SCRIPT.parent.parent / "editor" / "src-tauri" / "src" / "installer.rs"
        text = installer.read_text(encoding="utf-8")
        self.assertIn("fn has_compiled_sibling", text)
        self.assertGreaterEqual(text.count("if has_compiled_sibling("), 2)

    def test_second_sync_is_a_true_noop(self):
        sync.sync_repository(self.root)
        tracked = self.resources / "firmware" / "boot.py"
        first_stat = tracked.stat()
        time.sleep(0.02)
        second = sync.sync_repository(self.root)
        second_stat = tracked.stat()
        self.assertFalse(second.changed)
        self.assertEqual(first_stat.st_mtime_ns, second_stat.st_mtime_ns)
        self.assertEqual(first_stat.st_ino, second_stat.st_ino)

    def test_check_detects_drift_without_repairing_it(self):
        sync.sync_repository(self.root)
        stale = self.resources / "firmware" / "boot.py"
        stale.write_bytes(b"tampered")
        with self.assertRaises(sync.SyncError):
            sync.sync_repository(self.root, check=True)
        self.assertEqual(stale.read_bytes(), b"tampered")
        sync.sync_repository(self.root)
        self.assertEqual(stale.read_bytes(), b"boot-v2\n")
        self.assertFalse(sync.sync_repository(self.root, check=True).changed)

    def test_check_does_not_create_a_missing_resource_root(self):
        other_root = self.root / "missing-resources-repo"
        (other_root / "firmware" / "lib").mkdir(parents=True)
        with self.assertRaises(sync.SyncError):
            sync.sync_repository(other_root, check=True)
        self.assertFalse((other_root / "editor").exists())

    def test_digest_covers_firmware_and_vendored_resources(self):
        first = sync.sync_repository(self.root).digest
        (self.firmware / "code.py").write_bytes(b"code-v3")
        second = sync.sync_repository(self.root).digest
        self.assertNotEqual(first, second)
        (self.resources / "lib" / "neopixel.mpy").write_bytes(b"vendor-pixel-v2")
        third = sync.sync_repository(self.root).digest
        self.assertNotEqual(second, third)

    def test_cli_writes_verified_digest_and_check_is_read_only(self):
        digest_file = self.root / "target" / "resources.sha256"
        completed = subprocess.run(
            [sys.executable, str(SCRIPT), "--repo-root", str(self.root), "--digest-file", str(digest_file)],
            check=False,
            capture_output=True,
            text=True,
        )
        self.assertEqual(completed.returncode, 0, completed.stderr)
        digest = digest_file.read_text(encoding="ascii").strip()
        self.assertRegex(digest, r"^[0-9a-f]{64}$")
        self.assertIn(f"RESOURCE_DIGEST={digest}", completed.stdout)

        checked = subprocess.run(
            [sys.executable, str(SCRIPT), "--repo-root", str(self.root), "--check"],
            check=False,
            capture_output=True,
            text=True,
        )
        self.assertEqual(checked.returncode, 0, checked.stderr)

    def test_source_symlinks_are_rejected_when_supported(self):
        link = self.firmware / "lib" / "captain" / "escape.py"
        outside = self.root / "outside.py"
        outside.write_bytes(b"outside")
        try:
            os.symlink(outside, link)
        except (OSError, NotImplementedError):
            self.skipTest("symlink creation is unavailable")
        with self.assertRaises(sync.SyncError):
            sync.sync_repository(self.root)


class BuildScriptWiringTests(unittest.TestCase):
    def test_resource_sync_precedes_android_rust_build(self):
        text = (SCRIPT.parent / "build-android.ps1").read_text(encoding="utf-8")
        self.assertIn("sync_firmware_resources.py", text)
        invocation = "Sync-FirmwareResources -DigestFile $resourceDigestBefore"
        self.assertLess(text.index(invocation), text.index("cargoExe build"))
        self.assertIn("resourceBuildStamp", text)
        self.assertIn("stampedDigest -ne $resourceDigest", text)
        self.assertGreater(
            text.index("Sync-FirmwareResources -DigestFile $resourceDigestFinal -Check"),
            text.index("apkSigner sign"),
        )
        self.assertLess(
            text.index("Sync-FirmwareResources -DigestFile $resourceDigestFinal -Check"),
            text.index("Copy-Item -Force $apkUnsigned $apkOut"),
        )

    def test_resource_sync_precedes_portable_tauri_build(self):
        text = (SCRIPT.parent / "package-portable.ps1").read_text(encoding="utf-8")
        self.assertIn("sync_firmware_resources.py", text)
        invocation = "Sync-FirmwareResources -DigestFile $resourceDigestBefore"
        self.assertLess(text.index(invocation), text.index("npx tauri build"))
        self.assertGreater(
            text.index("Sync-FirmwareResources -DigestFile $resourceDigestFinal -Check"),
            text.index("Compress-Archive"),
        )
        self.assertLess(
            text.index("Sync-FirmwareResources -DigestFile $resourceDigestFinal -Check"),
            text.index("Move-Item -Force -LiteralPath $zipTemp"),
        )

    def test_all_resource_producers_use_the_single_helper(self):
        for name in ("build-android.ps1", "package-portable.ps1", "download-assets.ps1", "bump-version.ps1"):
            with self.subTest(script=name):
                text = (SCRIPT.parent / name).read_text(encoding="utf-8")
                self.assertIn("sync_firmware_resources.py", text)
                if name in ("build-android.ps1", "package-portable.ps1"):
                    self.assertIn("Sync-FirmwareResources -DigestFile", text)
                else:
                    self.assertIn("& $pythonExe $syncScript --repo-root $repoRoot", text)

    def test_release_workflow_uses_shared_sync_and_invalidates_cargo_cache(self):
        workflow = SCRIPT.parent.parent / ".github" / "workflows" / "release.yml"
        text = workflow.read_text(encoding="utf-8")
        self.assertNotIn("cp -r firmware", text)
        self.assertEqual(text.count("python tools/sync_firmware_resources.py --repo-root ."), 3)
        self.assertGreaterEqual(text.count("uses: actions/setup-python@v6"), 3)
        # Desktop staging, its subsequently downloaded native update archive,
        # and Android staging each invalidate Cargo's resource fingerprint.
        self.assertEqual(text.count("touch editor/src-tauri/build.rs"), 3)
        build_jobs = text[text.index("  build:\n"):]
        vendor_copy = build_jobs.index("python tools/provision_adafruit_bundle.py")
        first_sync = build_jobs.index("python tools/sync_firmware_resources.py --repo-root .")
        self.assertLess(vendor_copy, first_sync)

    def test_fdroid_recipe_uses_shared_sync_and_stages_both_trees(self):
        recipe = SCRIPT.parent.parent / "docs" / "fdroid-com.bosun.app.yml"
        text = recipe.read_text(encoding="utf-8")
        self.assertNotIn("cp -r firmware editor/src-tauri/resources/firmware", text)
        self.assertIn("python3 tools/sync_firmware_resources.py --repo-root .", text)
        self.assertIn("python3 tools/provision_adafruit_bundle.py", text)
        self.assertIn(
            "cp -r src-tauri/resources/firmware src-tauri/gen/android/app/src/main/assets/firmware",
            text,
        )
        self.assertIn(
            "cp -r src-tauri/resources/lib src-tauri/gen/android/app/src/main/assets/lib",
            text,
        )


@unittest.skipUnless(shutil.which("powershell.exe") or shutil.which("pwsh"), "PowerShell is unavailable")
class BumpVersionSmokeTests(unittest.TestCase):
    """Run the real release helper only inside a disposable fake checkout."""

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="bosun-version-fixture-")
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        fixtures = {
            "firmware/lib/captain/__init__.py": 'VERSION = "1.2.3"\n',
            "editor/package.json": '{"name": "bosun-editor", "version": "1.2.3", "dependencies": {"keep": "4.5.6"}}\n',
            "editor/package-lock.json": '{\n"name": "bosun-editor",\n"version": "1.2.3",\n"packages": {"": {\n"name": "bosun-editor",\n"version": "1.2.3"\n}}}\n',
            "editor/src-tauri/tauri.conf.json": '{"version": "1.2.3"}\n',
            "editor/src-tauri/Cargo.toml": '[package]\nname = "bosun-editor"\nversion = "1.2.3"\n[dependencies]\nkeep = { version = "4.5.6" }\n',
            "editor/src-tauri/Cargo.lock": '[[package]]\nname = "bosun-editor"\nversion = "1.2.3"\n[[package]]\nname = "keep"\nversion = "4.5.6"\n',
            "editor/src-tauri/android-version-code.txt": '31',
            "firmware-native/CMakeLists.txt": 'cmake_minimum_required(VERSION 3.20)\nif(BOSUN_PLATFORM STREQUAL "rp2040")\n    project(BosunNative VERSION 1.2.3 LANGUAGES C CXX ASM)\nelse()\n    project(BosunNative VERSION 1.2.3 LANGUAGES C)\nendif()\n',
            "firmware-native/include/bosun/protocol.h": '#define BOSUN_NATIVE_VERSION "1.2.3-native-experimental"\n#define BOSUN_PROTOCOL_RX_BYTES 26624u\n',
            "firmware-native/platform/rp2040/CMakeLists.txt": '    pico_set_program_name(${target} "Bosun Native MIDI Captain")\n    pico_set_program_version(${target} "1.2.3-native-experimental")\n',
        }
        for name, contents in fixtures.items():
            path = self.root / name
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(contents, encoding="utf-8")
        self.frozen_cp_bytes = (self.root / "firmware/lib/captain/__init__.py").read_bytes()
        (self.root / "tools").mkdir()
        for name in ("bump-version.ps1", "sync_firmware_resources.py"):
            shutil.copyfile(SCRIPT.parent / name, self.root / "tools" / name)

    def run_bump(self, version):
        return subprocess.run(
            [shutil.which("powershell.exe") or shutil.which("pwsh"), "-NoProfile", "-ExecutionPolicy", "Bypass",
             "-File", str(self.root / "tools/bump-version.ps1"), version],
            cwd=self.root, capture_output=True, text=True, timeout=30,
        )

    def assert_versions(self, version):
        native = (self.root / "firmware-native/CMakeLists.txt").read_text()
        self.assertEqual(re.findall(r"project\(BosunNative VERSION (\d+\.\d+\.\d+) LANGUAGES", native), [version, version])
        self.assertIn("cmake_minimum_required(VERSION 3.20)", native)
        self.assertIn(f'#define BOSUN_NATIVE_VERSION "{version}-native"',
                      (self.root / "firmware-native/include/bosun/protocol.h").read_text())
        self.assertIn(f'pico_set_program_version(${{target}} "{version}-native")',
                      (self.root / "firmware-native/platform/rp2040/CMakeLists.txt").read_text())
        for name in ("editor/package.json", "editor/src-tauri/tauri.conf.json"):
            self.assertEqual(json.loads((self.root / name).read_text())["version"], version)
        lock = json.loads((self.root / "editor/package-lock.json").read_text())
        self.assertEqual(lock["version"], version)
        self.assertEqual(lock["packages"][""]["version"], version)
        self.assertEqual(json.loads((self.root / "editor/package.json").read_text())["dependencies"]["keep"], "4.5.6")
        cargo = (self.root / "editor/src-tauri/Cargo.toml").read_text()
        self.assertIn(f'version = "{version}"', cargo)
        self.assertIn('keep = { version = "4.5.6" }', cargo)
        cargo_lock = (self.root / "editor/src-tauri/Cargo.lock").read_text()
        self.assertIn(f'name = "bosun-editor"\nversion = "{version}"', cargo_lock)
        self.assertIn('name = "keep"\nversion = "4.5.6"', cargo_lock)
        for name in ("firmware/lib/captain/__init__.py", "editor/src-tauri/resources/firmware/lib/captain/__init__.py",
                     "editor/src-tauri/resources/lib/captain/__init__.py"):
            self.assertEqual((self.root / name).read_bytes(), self.frozen_cp_bytes)

    def test_bump_advances_native_and_editor_but_preserves_frozen_cp_resources(self):
        result = self.run_bump("9.8.7")
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assert_versions("9.8.7")
        self.assertEqual((self.root / "editor/src-tauri/android-version-code.txt").read_text(), "32")

    def test_repeating_the_same_version_remains_valid(self):
        for _ in range(2):
            result = self.run_bump("1.2.3")
            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
            self.assert_versions("1.2.3")

    def test_missing_one_native_platform_version_cannot_silently_succeed(self):
        path = self.root / "firmware-native/CMakeLists.txt"
        contents = path.read_text().replace("    project(BosunNative VERSION 1.2.3 LANGUAGES C)\n", "")
        path.write_text(contents)
        result = self.run_bump("9.8.7")
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("Expected 2 version fields", result.stdout + result.stderr)
        self.assertEqual(path.read_text(), contents)


if __name__ == "__main__":
    unittest.main(verbosity=2)
