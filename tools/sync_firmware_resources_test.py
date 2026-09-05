#!/usr/bin/env python3
"""Deterministic tests for the firmware-to-Tauri resource sync."""

from __future__ import annotations

import importlib.util
import os
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
        self.assertEqual(text.count("touch editor/src-tauri/build.rs"), 2)
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


if __name__ == "__main__":
    unittest.main(verbosity=2)
