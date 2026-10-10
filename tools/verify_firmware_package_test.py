#!/usr/bin/env python3
"""Offline regression tests for resource staging and archive verification."""

from __future__ import annotations

import importlib.util
import os
import shutil
import sys
import tempfile
import unittest
import warnings
import zipfile
from pathlib import Path


SCRIPT = Path(__file__).resolve().with_name("verify_firmware_package.py")
SPEC = importlib.util.spec_from_file_location(
    "verify_firmware_package_under_test", SCRIPT
)
VERIFY = importlib.util.module_from_spec(SPEC)
assert SPEC.loader is not None
sys.modules[SPEC.name] = VERIFY
SPEC.loader.exec_module(VERIFY)


class ResourcePackageVerificationTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory(prefix="bosun-package-verify-")
        self.root = Path(self.temporary.name)
        self.resources = self.root / "resources"
        for relative, data in (
            ("update/bosun-update.zip", b"verified native update"),
            ("installer/manifest.json", b"{}"),
            ("installer/loader.uf2", b"loader"),
            ("installer/storage.bin", b"storage"),
            ("pi/bosun-pi-setup.tar.gz", b"pi"),
        ):
            path = self.resources / relative
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(data)

    def tearDown(self):
        self.temporary.cleanup()

    def _stage(self):
        staged = self.root / "portable"
        shutil.copytree(self.resources, staged)
        (staged / "Bosun.exe").write_bytes(b"executable")
        (staged / "README.txt").write_text("unrelated", encoding="utf-8")
        return staged

    def _archive(self, staged, name="portable.zip"):
        archive = self.root / name
        with zipfile.ZipFile(archive, "w") as package:
            for path in staged.rglob("*"):
                if path.is_file():
                    package.write(path, "Bosun/" + path.relative_to(staged).as_posix())
        return archive

    def test_exact_directory_and_archive_inventory_pass_with_unrelated_files(self):
        staged = self._stage()
        archive = self._archive(staged)

        expected_count = len(VERIFY._directory_inventory(self.resources))
        self.assertEqual(expected_count, 5)
        self.assertEqual(VERIFY.verify_directory(self.resources, staged), expected_count)
        self.assertEqual(VERIFY.verify_archive(self.resources, archive, "Bosun"), expected_count)

    def test_directory_rejects_missing_stale_and_unexpected_resource_files(self):
        mutations = (
            lambda root: (root / "installer" / "loader.uf2").unlink(),
            lambda root: (root / "update" / "bosun-update.zip").write_bytes(b"obsolete"),
            lambda root: (root / "pi" / "unexpected.txt").write_bytes(b"extra"),
        )
        for mutate in mutations:
            with self.subTest(mutation=mutate):
                staged = self._stage()
                mutate(staged)
                with self.assertRaises(VERIFY.VerificationError):
                    VERIFY.verify_directory(self.resources, staged)
                shutil.rmtree(staged)

    def test_native_update_is_verified_in_staging_and_finished_archive(self):
        staged = self._stage()
        packaged_update = staged / "update" / "bosun-update.zip"
        for content in (None, b"obsolete native update"):
            with self.subTest(content=content):
                if content is None:
                    packaged_update.unlink()
                else:
                    packaged_update.write_bytes(content)
                with self.assertRaisesRegex(VERIFY.VerificationError, "update/bosun-update.zip"):
                    VERIFY.verify_directory(self.resources, staged)
                with self.assertRaisesRegex(VERIFY.VerificationError, "update/bosun-update.zip"):
                    VERIFY.verify_archive(self.resources, self._archive(staged), "Bosun")

    def test_tree_absent_from_the_source_is_unexpected_in_the_package(self):
        shutil.rmtree(self.resources / "pi")
        staged = self._stage()
        (staged / "pi").mkdir()
        (staged / "pi" / "bosun-pi-setup.tar.gz").write_bytes(b"obsolete checkout package")
        with self.assertRaisesRegex(VERIFY.VerificationError, "unexpected: pi/bosun-pi-setup.tar.gz"):
            VERIFY.verify_directory(self.resources, staged)
        with self.assertRaisesRegex(VERIFY.VerificationError, "unexpected: pi/bosun-pi-setup.tar.gz"):
            VERIFY.verify_archive(self.resources, self._archive(staged), "Bosun")

    def test_empty_source_resources_are_rejected(self):
        for tree in VERIFY.RESOURCE_TREES:
            shutil.rmtree(self.resources / tree)
        staged = self._stage()
        with self.assertRaisesRegex(VERIFY.VerificationError, "no update, installer or pi"):
            VERIFY.verify_directory(self.resources, staged)

    def test_windows_portable_backslashes_are_normalized_without_aliases(self):
        archive = self.root / "backslashes.zip"
        with zipfile.ZipFile(archive, "w") as package:
            for path in self.resources.rglob("*"):
                if path.is_file():
                    relative = path.relative_to(self.resources).as_posix()
                    package.writestr("Bosun-portable\\" + relative.replace("/", "\\"), path.read_bytes())
            package.writestr("Bosun-portable\\README.txt", b"unrelated")

        expected_count = len(VERIFY._directory_inventory(self.resources))
        self.assertEqual(VERIFY.verify_archive(self.resources, archive, r"Bosun-portable"), expected_count)

        collision = self.root / "collision.zip"
        with zipfile.ZipFile(collision, "w") as package:
            package.writestr("Bosun/update/bosun-update.zip", b"one")
            # On Windows zipfile itself canonicalizes the backslashes and
            # warns about the duplicate; on POSIX our verifier performs that
            # canonicalization. Both platforms must reject the archive.
            with warnings.catch_warnings():
                warnings.simplefilter("ignore", UserWarning)
                package.writestr(r"Bosun\update\bosun-update.zip", b"two")
        with self.assertRaisesRegex(VERIFY.VerificationError, "duplicate normalized"):
            VERIFY.verify_archive(self.resources, collision, "Bosun")

        traversal = self.root / "traversal.zip"
        with zipfile.ZipFile(traversal, "w") as package:
            package.writestr(r"Bosun\update\..\escape.zip", b"escape")
        with self.assertRaisesRegex(VERIFY.VerificationError, "unsafe archive entry"):
            VERIFY.verify_archive(self.resources, traversal, "Bosun")

    def test_archive_rejects_collapsed_aliases_and_symlinks(self):
        staged = self._stage()
        for index, alias in enumerate(("Bosun/update/./bosun-update.zip", "Bosun//update/bosun-update.zip")):
            archive = self._archive(staged, f"alias-{index}.zip")
            with zipfile.ZipFile(archive, "a") as package:
                package.writestr(alias, b"update")
            with self.subTest(alias=alias), self.assertRaises(VERIFY.VerificationError):
                VERIFY.verify_archive(self.resources, archive, "Bosun")

        archive = self._archive(staged, "symlink.zip")
        link = zipfile.ZipInfo("Bosun/update/link.zip")
        link.create_system = 3
        link.external_attr = (0o120777 << 16)
        with zipfile.ZipFile(archive, "a") as package:
            package.writestr(link, b"update")
        with self.assertRaisesRegex(VERIFY.VerificationError, "symlink archive"):
            VERIFY.verify_archive(self.resources, archive, "Bosun")

    def test_directory_and_archive_root_links_are_rejected_when_supported(self):
        staged = self._stage()
        archive = self._archive(staged)
        directory_link = self.root / "portable-link"
        archive_link = self.root / "zip-link"
        try:
            os.symlink(staged, directory_link, target_is_directory=True)
            os.symlink(archive, archive_link)
        except (OSError, NotImplementedError):
            self.skipTest("symlink creation is unavailable")
        with self.assertRaisesRegex(VERIFY.VerificationError, "directory is unsafe"):
            VERIFY.verify_directory(self.resources, directory_link)
        with self.assertRaisesRegex(VERIFY.VerificationError, "archive is unsafe"):
            VERIFY.verify_archive(self.resources, archive_link, "Bosun")


class BuildWiringTests(unittest.TestCase):
    def test_android_build_stages_only_the_frontend(self):
        text = SCRIPT.with_name("build-android.ps1").read_text(encoding="utf-8")
        stage = text.index('Get-SafeAndroidAssetDestination -Name "public"')
        cargo = text.index("$cargoExe build --release --target aarch64-linux-android")
        gradle = text.index("Invoke-NativeTool { & .\\gradlew @gradleArgs }")
        sign = text.index("$apkSigner sign")
        verify_signature = text.index("$apkSigner verify --verbose --print-certs")
        publish = text.index("Copy-Item -Force $apkUnsigned $apkOut")

        self.assertIn("Assert-NoReparsePathComponents -Root $tauriDir -Target $androidAssets", text)
        self.assertIn("Remove-Item -Recurse -Force -LiteralPath $androidPublicAssets", text)
        self.assertIn('[ValidateSet("public")]', text)
        self.assertLess(stage, cargo)
        self.assertLess(cargo, gradle)
        self.assertLess(gradle, sign)
        self.assertLess(sign, verify_signature)
        self.assertLess(verify_signature, publish)

    def test_portable_package_verifies_staging_and_archive_before_publish(self):
        text = SCRIPT.with_name("package-portable.ps1").read_text(encoding="utf-8")
        stage = text.index("Invoke-FirmwarePackageVerification -Directory $stageDir")
        compress = text.index("Compress-Archive -Path $stageDir")
        archive = text.index(
            "Invoke-FirmwarePackageVerification -Archive $zipTemp -Prefix $stageName"
        )
        publish = text.index("Move-Item -Force -LiteralPath $zipTemp")

        self.assertLess(stage, compress)
        self.assertLess(compress, archive)
        self.assertLess(archive, publish)
        self.assertIn("Get-SafeDistChildPath -Name $stageName", text)
        self.assertIn("Remove-Item -Recurse -Force -LiteralPath $stageDir", text)
        self.assertIn("Portable dist directory must not be a link or junction", text)

    def test_release_android_job_builds_without_firmware_resources(self):
        workflow = (
            SCRIPT.parent.parent / ".github" / "workflows" / "release.yml"
        ).read_text(encoding="utf-8")
        android_job = workflow[workflow.index("  android:\n"):]

        self.assertIn("platforms;android-36", android_job)
        self.assertIn("build-tools;34.0.0", android_job)
        self.assertIn('BUILD_TOOLS_VERSION: "34.0.0"', android_job)
        self.assertNotIn("editor/src-tauri/resources", android_job)
        self.assertNotIn(".uf2", android_job)

    def test_ci_and_release_gate_run_rust_backend_tests(self):
        workflows = SCRIPT.parent.parent / ".github" / "workflows"
        ci = (workflows / "ci.yml").read_text(encoding="utf-8")
        release = (workflows / "release.yml").read_text(encoding="utf-8")

        self.assertIn("name: Desktop backend (Rust unit tests)", ci)
        self.assertIn("run: cargo test --locked", ci)
        self.assertIn(
            "cargo test --locked --manifest-path editor/src-tauri/Cargo.toml",
            release,
        )


if __name__ == "__main__":
    unittest.main(verbosity=2)
