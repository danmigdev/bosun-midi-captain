#!/usr/bin/env python3
"""Offline release-gate regressions using isolated source and UF2 fixtures."""
import hashlib
import importlib.util
import json
from pathlib import Path
import struct
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
            "firmware-native/include/bosun/protocol.h": '#define BOSUN_NATIVE_VERSION "1.2.3-native"\n',
            "firmware-native/CMakeLists.txt": 'project(BosunNative VERSION 1.2.3 LANGUAGES C CXX ASM)\nproject(BosunNative VERSION 1.2.3 LANGUAGES C)\n',
            "firmware-native/platform/rp2040/CMakeLists.txt": 'pico_set_program_version(${target} "1.2.3-native")\n',
        }
        for path, contents in self.sources.items():
            destination = self.root / path
            destination.parent.mkdir(parents=True, exist_ok=True)
            destination.write_text(contents, encoding="utf-8")
        self.archived_cp = self.root / "firmware/lib/captain/__init__.py"
        self.archived_cp.parent.mkdir(parents=True)
        self.archived_cp.write_text('VERSION = "0.6.5"\n', encoding="utf-8")

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

    def test_editor_native_and_package_advance_without_reversioning_archived_cp(self):
        archived = self.archived_cp.read_bytes()
        for relative, original in self.sources.items():
            (self.root / relative).write_text(original.replace("1.2.3", "1.2.4"), encoding="utf-8")
        package = self.package(release="1.2.4", firmware_version="1.2.4-native", embedded=b"1.2.4-native\0")
        self.assertEqual(gate.verify(self.root, tag="v1.2.4", package=package),
                         {"release": "1.2.4", "firmware_version": "1.2.4-native", "prerelease": "false"})
        self.assertEqual(self.archived_cp.read_bytes(), archived)
        # Retirement must not weaken validation of the firmware actually shipped.
        old_package = self.package()
        with self.assertRaises(ValueError):
            gate.verify(self.root, tag="v1.2.4", package=old_package)

    def test_missing_native_declaration_and_wrong_tag_fail(self):
        for tag in ("v1.2.4", "main", "v1.2.3\nrelease=untrusted"):
            with self.subTest(tag=tag), self.assertRaises(ValueError):
                gate.verify(self.root, tag=tag)
        (self.root / "firmware-native/CMakeLists.txt").write_text(
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


if __name__ == "__main__":
    unittest.main(verbosity=2)
