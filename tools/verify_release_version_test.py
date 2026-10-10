#!/usr/bin/env python3
"""Offline release-gate regressions using isolated source and UF2 fixtures."""
import hashlib
import importlib.util
import json
from pathlib import Path
import re
import shutil
import struct
import subprocess
import tempfile
import unittest
import zipfile

spec = importlib.util.spec_from_file_location("release_version", Path(__file__).with_name("verify-release-version.py"))
gate = importlib.util.module_from_spec(spec)
spec.loader.exec_module(gate)


class ReleaseVersionTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.sources = {
            "editor/package.json": '{"version":"1.2.3"}',
            "editor/src-tauri/tauri.conf.json": '{"version":"1.2.3"}',
            "editor/src-tauri/Cargo.toml": '[package]\nversion = "1.2.3"\n',
            "firmware/include/bosun/protocol.h": '#define BOSUN_NATIVE_VERSION "1.2.3-native"\n',
            "firmware/CMakeLists.txt": 'project(BosunNative VERSION 1.2.3 LANGUAGES C CXX ASM)\nproject(BosunNative VERSION 1.2.3 LANGUAGES C)\n',
            "firmware/platform/rp2040/CMakeLists.txt": 'pico_set_program_version(${target} "1.2.3-native")\n',
        }
        for path, contents in self.sources.items():
            destination = self.root / path
            destination.parent.mkdir(parents=True, exist_ok=True)
            destination.write_text(contents, encoding="utf-8")

    def package(self, *, release="1.2.3", firmware_version="1.2.3-native", embedded=b"1.2.3-native\0"):
        binary = bytearray()
        for index in range(3):
            block = bytearray(512)
            struct.pack_into("<8I", block, 0, 0x0A324655, 0x9E5D5157, 0x2000,
                             0x10000000 + index * 256, 256, index, 3, 0xE48BFF56)
            block[32:288] = b"\xa5" * 256
            if index == 1:
                struct.pack_into("<II", block, 32, 0x20042000, 0x10000201)
            if index == 2:
                block[64:64 + len(embedded)] = embedded
            struct.pack_into("<I", block, 508, 0x0AB16F30)
            binary.extend(block)
        manifest = {"schema": 1, "board": "midi-captain-rp2040", "release": release,
                    "family": "native", "firmware_version": firmware_version,
                    "flash_bytes": 8388608, "firmware_sha256": hashlib.sha256(binary).hexdigest()}
        path = self.root / "update.zip"
        with zipfile.ZipFile(path, "w") as archive:
            archive.writestr("manifest.json", json.dumps(manifest))
            archive.writestr("firmware.uf2", binary)
        return path

    def test_aligned_sources_tags_and_actual_uf2_identity(self):
        for tag in (None, "v1.2.3", "refs/tags/v1.2.3"):
            self.assertEqual(gate.verify(self.root, tag=tag, package=self.package()),
                             {"release": "1.2.3", "firmware_version": "1.2.3-native", "prerelease": "false"})

    def test_experimental_channel_is_explicit_and_unknown_channels_fail(self):
        path = self.root / "editor/package.json"
        path.write_text('{"version":"1.2.3","bosunReleaseChannel":"experimental"}')
        self.assertEqual(gate.verify(self.root)["prerelease"], "true")
        path.write_text('{"version":"1.2.3","bosunReleaseChannel":"typo"}')
        with self.assertRaises(ValueError):
            gate.verify(self.root)

    def test_each_version_touchpoint_must_match(self):
        for relative, original in self.sources.items():
            with self.subTest(path=relative):
                path = self.root / relative
                path.write_text(original.replace("1.2.3", "1.2.4"), encoding="utf-8")
                with self.assertRaises(ValueError):
                    gate.verify(self.root)
                path.write_text(original, encoding="utf-8")

    def test_editor_native_and_package_advance_together(self):
        for relative, original in self.sources.items():
            (self.root / relative).write_text(original.replace("1.2.3", "1.2.4"), encoding="utf-8")
        package = self.package(release="1.2.4", firmware_version="1.2.4-native", embedded=b"1.2.4-native\0")
        self.assertEqual(gate.verify(self.root, tag="v1.2.4", package=package),
                         {"release": "1.2.4", "firmware_version": "1.2.4-native", "prerelease": "false"})
        # A package built for the previous release must still be rejected.
        old_package = self.package()
        with self.assertRaises(ValueError):
            gate.verify(self.root, tag="v1.2.4", package=old_package)

    def test_missing_native_declaration_and_wrong_tag_fail(self):
        for tag in ("v1.2.4", "main", "v1.2.3\nrelease=untrusted"):
            with self.subTest(tag=tag), self.assertRaises(ValueError):
                gate.verify(self.root, tag=tag)
        (self.root / "firmware/CMakeLists.txt").write_text(
            "project(BosunNative VERSION 1.2.3 LANGUAGES C)\n", encoding="utf-8")
        with self.assertRaises(ValueError):
            gate.verify(self.root)

    def test_missing_wrong_manifest_and_old_embedded_firmware_fail(self):
        with self.assertRaises(ValueError):
            gate.verify(self.root, package=self.root / "missing.zip")
        for changes in ({"release": "1.2.4"}, {"firmware_version": "1.2.4-native"},
                        {"embedded": b"1.2.2-native\0"}, {"embedded": b"no version\0"},
                        {"embedded": b"1.2.3-native\0and 1.2.2-native\0"}):
            with self.subTest(changes=changes), self.assertRaises(ValueError):
                gate.verify(self.root, package=self.package(**changes))


@unittest.skipUnless(shutil.which("powershell.exe") or shutil.which("pwsh"), "PowerShell is unavailable")
class BumpVersionSmokeTests(unittest.TestCase):
    """Run the real release helper only inside a disposable fake checkout."""

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="bosun-version-fixture-")
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        fixtures = {
            "editor/package.json": '{"name": "bosun-editor", "version": "1.2.3", "dependencies": {"keep": "4.5.6"}}\n',
            "editor/package-lock.json": '{\n"name": "bosun-editor",\n"version": "1.2.3",\n"packages": {"": {\n"name": "bosun-editor",\n"version": "1.2.3"\n}}}\n',
            "editor/src-tauri/tauri.conf.json": '{"version": "1.2.3"}\n',
            "editor/src-tauri/Cargo.toml": '[package]\nname = "bosun-editor"\nversion = "1.2.3"\n[dependencies]\nkeep = { version = "4.5.6" }\n',
            "editor/src-tauri/Cargo.lock": '[[package]]\nname = "bosun-editor"\nversion = "1.2.3"\n[[package]]\nname = "keep"\nversion = "4.5.6"\n',
            "editor/src-tauri/android-version-code.txt": '31',
            "firmware/CMakeLists.txt": 'cmake_minimum_required(VERSION 3.20)\nif(BOSUN_PLATFORM STREQUAL "rp2040")\n    project(BosunNative VERSION 1.2.3 LANGUAGES C CXX ASM)\nelse()\n    project(BosunNative VERSION 1.2.3 LANGUAGES C)\nendif()\n',
            "firmware/include/bosun/protocol.h": '#define BOSUN_NATIVE_VERSION "1.2.3-native-experimental"\n#define BOSUN_PROTOCOL_RX_BYTES 26624u\n',
            "firmware/platform/rp2040/CMakeLists.txt": '    pico_set_program_name(${target} "Bosun Native MIDI Captain")\n    pico_set_program_version(${target} "1.2.3-native-experimental")\n',
        }
        for name, contents in fixtures.items():
            path = self.root / name
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(contents, encoding="utf-8")
        (self.root / "tools").mkdir()
        shutil.copyfile(Path(__file__).with_name("bump-version.ps1"), self.root / "tools/bump-version.ps1")

    def run_bump(self, version):
        return subprocess.run(
            [shutil.which("powershell.exe") or shutil.which("pwsh"), "-NoProfile", "-ExecutionPolicy", "Bypass",
             "-File", str(self.root / "tools/bump-version.ps1"), version],
            cwd=self.root, capture_output=True, text=True, timeout=30,
        )

    def assert_versions(self, version):
        native = (self.root / "firmware/CMakeLists.txt").read_text()
        self.assertEqual(re.findall(r"project\(BosunNative VERSION (\d+\.\d+\.\d+) LANGUAGES", native), [version, version])
        self.assertIn("cmake_minimum_required(VERSION 3.20)", native)
        self.assertIn(f'#define BOSUN_NATIVE_VERSION "{version}-native"',
                      (self.root / "firmware/include/bosun/protocol.h").read_text())
        self.assertIn(f'pico_set_program_version(${{target}} "{version}-native")',
                      (self.root / "firmware/platform/rp2040/CMakeLists.txt").read_text())
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

    def test_bump_advances_native_editor_and_android_version_code(self):
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
        path = self.root / "firmware/CMakeLists.txt"
        contents = path.read_text().replace("    project(BosunNative VERSION 1.2.3 LANGUAGES C)\n", "")
        path.write_text(contents)
        result = self.run_bump("9.8.7")
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("Expected 2 version fields", result.stdout + result.stderr)
        self.assertEqual(path.read_text(), contents)


if __name__ == "__main__":
    unittest.main(verbosity=2)
