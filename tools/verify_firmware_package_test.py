#!/usr/bin/env python3
"""Offline regression tests for firmware staging and archive verification."""

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


class FirmwarePackageVerificationTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory(prefix="bosun-package-verify-")
        self.root = Path(self.temporary.name)
        self.resources = self.root / "resources"
        (self.resources / "firmware" / "lib" / "captain").mkdir(parents=True)
        (self.resources / "firmware" / "lib" / "plugins").mkdir(parents=True)
        (self.resources / "lib" / "captain").mkdir(parents=True)
        (self.resources / "lib" / "plugins").mkdir(parents=True)
        (self.resources / "lib" / "vendor.mpy").write_bytes(b"vendor")
        (self.resources / "circuitpython.uf2").write_bytes(b"uf2")
        (self.resources / "firmware" / "code.py").write_bytes(b"code")
        self._write_pair("captain", "protocol", b"protocol source", b"compiled protocol")
        self._write_pair("plugins", "kemper", b"plugin source", b"compiled plugin")

    def tearDown(self):
        self.temporary.cleanup()

    def _write_pair(self, package, name, source_data, compiled_data):
        for base in (
            self.resources / "firmware" / "lib",
            self.resources / "lib",
        ):
            source = base / package / f"{name}.py"
            compiled = base / package / f"{name}.mpy"
            source.write_bytes(source_data)
            compiled.write_bytes(compiled_data)
            # A generated artifact is normally newer than its source.
            base = 1_700_000_000_000_000_000
            os.utime(source, ns=(base, base))
            os.utime(compiled, ns=(base + 1_000_000_000, base + 1_000_000_000))

    def _stage(self):
        staged = self.root / "assets"
        shutil.copytree(self.resources, staged)
        (staged / "public").mkdir()
        (staged / "public" / "index.html").write_text("frontend", encoding="utf-8")
        (staged / "tauri.conf.json").write_text("{}", encoding="utf-8")
        return staged

    def _archive(self, staged, name="app.apk"):
        archive = self.root / name
        with zipfile.ZipFile(archive, "w") as package:
            for path in staged.rglob("*"):
                if path.is_file():
                    package.write(path, "assets/" + path.relative_to(staged).as_posix())
        return archive

    def test_exact_directory_and_apk_inventory_pass_with_unrelated_assets(self):
        staged = self._stage()
        archive = self._archive(staged)

        expected_count = len(VERIFY._directory_inventory(self.resources))
        self.assertEqual(
            VERIFY.verify_directory(self.resources, staged), expected_count
        )
        self.assertEqual(
            VERIFY.verify_archive(self.resources, archive, "assets"), expected_count
        )

    def test_directory_rejects_missing_stale_and_unexpected_resource_files(self):
        mutations = (
            lambda root: (root / "firmware" / "lib" / "captain" / "protocol.mpy").unlink(),
            lambda root: (root / "firmware" / "code.py").write_bytes(b"stale"),
            lambda root: (root / "lib" / "unexpected.mpy").write_bytes(b"extra"),
        )
        for mutate in mutations:
            with self.subTest(mutation=mutate):
                staged = self._stage()
                mutate(staged)
                with self.assertRaises(VERIFY.VerificationError):
                    VERIFY.verify_directory(self.resources, staged)
                shutil.rmtree(staged)

    def test_apk_rejects_missing_stale_and_unexpected_resource_entries(self):
        staged = self._stage()
        archives = []
        with zipfile.ZipFile(self.root / "missing.apk", "w") as package:
            for path in staged.rglob("*"):
                relative = path.relative_to(staged).as_posix()
                if path.is_file() and relative != "firmware/lib/captain/protocol.mpy":
                    package.write(path, "assets/" + relative)
        archives.append(self.root / "missing.apk")

        code = staged / "firmware" / "code.py"
        original_code = code.read_bytes()
        code.write_bytes(b"stale")
        stale = self._archive(staged, "stale.apk")
        archives.append(stale)
        code.write_bytes(original_code)

        unexpected = staged / "firmware" / "unexpected.py"
        unexpected.write_bytes(b"extra")
        extra = self._archive(staged, "extra.apk")
        archives.append(extra)

        for archive in archives:
            with self.subTest(archive=archive.name), self.assertRaises(
                VERIFY.VerificationError
            ):
                VERIFY.verify_archive(self.resources, archive, "assets")

    def test_windows_portable_backslashes_are_normalized_without_aliases(self):
        archive = self.root / "portable.zip"
        with zipfile.ZipFile(archive, "w") as package:
            for path in self.resources.rglob("*"):
                if path.is_file():
                    relative = path.relative_to(self.resources).as_posix()
                    package.writestr(
                        "Bosun-portable\\" + relative.replace("/", "\\"),
                        path.read_bytes(),
                    )
            package.writestr("Bosun-portable\\README.txt", b"unrelated")

        expected_count = len(VERIFY._directory_inventory(self.resources))
        self.assertEqual(
            VERIFY.verify_archive(self.resources, archive, r"Bosun-portable"),
            expected_count,
        )

        collision = self.root / "collision.apk"
        with zipfile.ZipFile(collision, "w") as package:
            package.writestr("assets/firmware/code.py", b"one")
            # On Windows zipfile itself canonicalizes the backslashes and
            # warns about the duplicate; on POSIX our verifier performs that
            # canonicalization. Both platforms must reject the archive.
            with warnings.catch_warnings():
                warnings.simplefilter("ignore", UserWarning)
                package.writestr(r"assets\firmware\code.py", b"two")
        with self.assertRaisesRegex(VERIFY.VerificationError, "duplicate normalized"):
            VERIFY.verify_archive(self.resources, collision, "assets")

        traversal = self.root / "traversal.apk"
        with zipfile.ZipFile(traversal, "w") as package:
            package.writestr(r"assets\firmware\..\escape.py", b"escape")
        with self.assertRaisesRegex(VERIFY.VerificationError, "unsafe archive entry"):
            VERIFY.verify_archive(self.resources, traversal, "assets")

    def test_archive_rejects_collapsed_aliases_and_symlinks(self):
        staged = self._stage()
        aliases = (
            "assets/firmware/./code.py",
            "assets//firmware/code.py",
        )
        for index, alias in enumerate(aliases):
            archive = self._archive(staged, f"alias-{index}.apk")
            with zipfile.ZipFile(archive, "a") as package:
                package.writestr(alias, b"code")
            with self.subTest(alias=alias), self.assertRaises(
                VERIFY.VerificationError
            ):
                VERIFY.verify_archive(self.resources, archive, "assets")

        archive = self._archive(staged, "symlink.apk")
        link = zipfile.ZipInfo("assets/firmware/link.py")
        link.create_system = 3
        link.external_attr = (0o120777 << 16)
        with zipfile.ZipFile(archive, "a") as package:
            package.writestr(link, b"code")
        with self.assertRaisesRegex(VERIFY.VerificationError, "symlink archive"):
            VERIFY.verify_archive(self.resources, archive, "assets")

    def test_directory_and_archive_root_links_are_rejected_when_supported(self):
        staged = self._stage()
        archive = self._archive(staged)
        directory_link = self.root / "assets-link"
        archive_link = self.root / "apk-link"
        try:
            os.symlink(staged, directory_link, target_is_directory=True)
            os.symlink(archive, archive_link)
        except (OSError, NotImplementedError):
            self.skipTest("symlink creation is unavailable")
        with self.assertRaisesRegex(VERIFY.VerificationError, "directory is unsafe"):
            VERIFY.verify_directory(self.resources, directory_link)
        with self.assertRaisesRegex(VERIFY.VerificationError, "archive is unsafe"):
            VERIFY.verify_archive(self.resources, archive_link, "assets")

    def test_visibly_newer_source_rejects_preferred_stale_mpy(self):
        source = self.resources / "firmware" / "lib" / "captain" / "protocol.py"
        compiled = source.with_suffix(".mpy")
        base = 1_700_000_000_000_000_000
        os.utime(compiled, ns=(base, base))
        os.utime(source, ns=(base + 20_000_000_000, base + 20_000_000_000))

        with self.assertRaisesRegex(VERIFY.VerificationError, "visibly stale"):
            VERIFY.verify_directory(self.resources, self._stage())

    def test_small_checkout_timestamp_skew_does_not_false_positive(self):
        source = self.resources / "firmware" / "lib" / "captain" / "protocol.py"
        compiled = source.with_suffix(".mpy")
        base = 1_700_000_000_000_000_000
        os.utime(compiled, ns=(base, base))
        os.utime(source, ns=(base + 1_000_000_000, base + 1_000_000_000))

        VERIFY.verify_directory(self.resources, self._stage())


class AndroidBuildWiringTests(unittest.TestCase):
    def test_android_build_stages_and_verifies_resources_then_checks_apk(self):
        text = SCRIPT.with_name("build-android.ps1").read_text(encoding="utf-8")
        stage = text.index("Invoke-FirmwarePackageVerification -Directory $androidAssets")
        cargo = text.index("$cargoExe build --release --target aarch64-linux-android")
        gradle = text.index("Invoke-NativeTool { & .\\gradlew @gradleArgs }")
        sign = text.index("$apkSigner sign")
        verify_signature = text.index("$apkSigner verify --verbose --print-certs")
        archive = text.index("Invoke-FirmwarePackageVerification -Archive $apkUnsigned")
        publish = text.index("Copy-Item -Force $apkUnsigned $apkOut")

        self.assertIn('foreach ($tree in @("firmware", "lib"))', text)
        self.assertIn('Get-SafeAndroidAssetDestination -Name "public"', text)
        self.assertIn('Get-SafeAndroidAssetDestination -Name $tree', text)
        self.assertIn(
            "Assert-NoReparsePathComponents -Root $tauriDir -Target $androidAssets",
            text,
        )
        self.assertIn('Remove-Item -Recurse -Force -LiteralPath $destination', text)
        self.assertIn('Get-SafeAndroidAssetDestination -Name "circuitpython.uf2"', text)
        self.assertLess(stage, cargo)
        self.assertLess(stage, gradle)
        self.assertLess(sign, verify_signature)
        self.assertLess(verify_signature, archive)
        self.assertLess(archive, publish)

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

    def test_release_android_provisions_vendor_libs_and_verifies_signed_apk(self):
        workflow = (
            SCRIPT.parent.parent / ".github" / "workflows" / "release.yml"
        ).read_text(encoding="utf-8")
        android_job = workflow[workflow.index("  android:\n"):]

        self.assertIn("platforms;android-36", android_job)
        self.assertIn("build-tools;34.0.0", android_job)
        self.assertIn('BUILD_TOOLS_VERSION: "34.0.0"', android_job)
        self.assertIn(
            'python tools/provision_adafruit_bundle.py --destination "$RES/lib"',
            android_job,
        )
        self.assertNotIn("Adafruit_CircuitPython_Bundle/releases/latest", android_job)
        verify = android_job.index("python tools/verify_firmware_package.py")
        copy_release = android_job.index('cp "$SIGNED_APK" release/bosun.apk')
        upload = android_job.index("- name: Upload APK to release")
        self.assertLess(copy_release, verify)
        self.assertLess(verify, upload)

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
