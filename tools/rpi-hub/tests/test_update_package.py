"""Offline update package tests; never open or modify a device."""

import hashlib
import json
from pathlib import Path
import struct
import tempfile
import unittest
import warnings
import zipfile

from bosun_hub.update_package import (
    FLASH_BASE, FLASH_BYTES, STORAGE_OFFSET, UpdatePackageError,
    validate_firmware_uf2, validate_update_package,
)


def uf2(pages=3):
    result = bytearray()
    for index in range(pages):
        payload = bytearray(b"\xa5" * 256)
        if index == 1:
            struct.pack_into("<II", payload, 0, 0x20042000, FLASH_BASE + 513)
        block = bytearray(512)
        struct.pack_into("<8I", block, 0, 0x0A324655, 0x9E5D5157, 0x2000,
                         FLASH_BASE + index * 256, 256, index, pages, 0xE48BFF56)
        block[32:288] = payload
        struct.pack_into("<I", block, 508, 0x0AB16F30)
        result.extend(block)
    return bytes(result)


def manifest(firmware):
    return {"schema": 1, "board": "midi-captain-rp2040", "release": "0.6.5",
            "family": "native", "firmware_version": "0.6.5-native",
            "flash_bytes": FLASH_BYTES, "firmware_sha256": hashlib.sha256(firmware).hexdigest()}


class PackageTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.package = self.root / "update.zip"

    def write_package(self, *, firmware=None, meta=None, entries=()):
        firmware = uf2() if firmware is None else firmware
        meta = manifest(firmware) if meta is None else meta
        with warnings.catch_warnings():
            warnings.simplefilter("ignore", UserWarning)
            with zipfile.ZipFile(self.package, "w", compression=zipfile.ZIP_DEFLATED) as archive:
                archive.writestr("manifest.json", meta if isinstance(meta, str) else json.dumps(meta))
                archive.writestr("firmware.uf2", firmware)
                for name, content in entries:
                    archive.writestr(name, content)
        return self.package

    def test_valid_package_returns_exact_firmware_and_flash_pages(self):
        result = validate_update_package(self.write_package())
        self.assertEqual(result.manifest, manifest(uf2()))
        self.assertEqual(result.firmware_uf2, uf2())
        self.assertEqual(sorted(result.firmware_pages), [FLASH_BASE + i * 256 for i in range(3)])

    def test_manifest_rejects_wrong_schema_board_family_flash_digest_and_versions(self):
        for key, value in (("schema", True), ("schema", 2), ("board", "pico"),
                           ("family", "other"), ("flash_bytes", 2097152),
                           ("firmware_sha256", "0" * 64), ("release", "latest"),
                           ("firmware_version", ["0.6.5-native"]), ("flash_bytes", "8388608")):
            with self.subTest(key=key, value=value):
                data = manifest(uf2())
                data[key] = value
                with self.assertRaises(UpdatePackageError):
                    validate_update_package(self.write_package(meta=data))

    def test_unknown_missing_and_duplicate_manifest_fields_rejected(self):
        extra = manifest(uf2()) | {"storage_offset": 0}
        missing = manifest(uf2())
        del missing["board"]
        duplicate = json.dumps(manifest(uf2())).replace('"schema": 1', '"schema": 1, "schema": 1')
        for data in (extra, missing, duplicate, "[]", "null", "{}"):
            with self.subTest(data=data), self.assertRaises(UpdatePackageError):
                validate_update_package(self.write_package(meta=data))

    def test_zip_rejects_extra_duplicate_traversal_absolute_and_symlink_entries(self):
        symlink = zipfile.ZipInfo("link")
        symlink.create_system = 3
        symlink.external_attr = 0o120777 << 16
        for name in ("extra.txt", "manifest.json", "../escape", "/absolute", "folder/../escape", symlink):
            with self.subTest(name=name), self.assertRaises(UpdatePackageError):
                validate_update_package(self.write_package(entries=[(name, "payload")]))
        self.assertFalse((self.root.parent / "escape").exists())

    def test_named_required_entry_symlink_is_rejected(self):
        with zipfile.ZipFile(self.package, "w") as archive:
            archive.writestr("manifest.json", json.dumps(manifest(uf2())))
            entry = zipfile.ZipInfo("firmware.uf2")
            entry.create_system = 3
            entry.external_attr = 0o120777 << 16
            archive.writestr(entry, uf2())
        with self.assertRaisesRegex(UpdatePackageError, "entry type"):
            validate_update_package(self.package)

    def test_zip_bomb_and_malformed_archive_rejected(self):
        with self.assertRaises(UpdatePackageError):
            validate_update_package(self.write_package(meta=" " * 5000))
        with self.assertRaises(UpdatePackageError):
            validate_update_package(self.write_package(firmware=b"0" * 1000000))
        self.package.write_bytes(b"not a zip")
        with self.assertRaises(UpdatePackageError):
            validate_update_package(self.package)

    def test_uf2_rejects_bad_header_sequence_addresses_and_settings_overlap(self):
        mutations = ((0, 0), (4, 0), (8, 0), (8, 0xA000), (12, FLASH_BASE + STORAGE_OFFSET),
                     (12, FLASH_BASE - 256), (12, FLASH_BASE + 1), (16, 128),
                     (20, 1), (24, 99), (28, 0x12345678), (508, 0),
                     (512 + 12, FLASH_BASE), (1024 + 12, FLASH_BASE + 1024),
                     (512 + 32, 0), (512 + 32, 0x20000004),
                     (512 + 36, FLASH_BASE + 512), (512 + 36, FLASH_BASE + 0x2001))
        for offset, value in mutations:
            with self.subTest(offset=offset, value=value):
                data = bytearray(uf2())
                struct.pack_into("<I", data, offset, value)
                with self.assertRaises(UpdatePackageError):
                    validate_firmware_uf2(bytes(data))
        for data in (b"", uf2()[:-1], uf2(1)):
            with self.assertRaises(UpdatePackageError):
                validate_firmware_uf2(data)


if __name__ == "__main__":
    unittest.main()
