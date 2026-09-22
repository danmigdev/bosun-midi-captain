"""Offline package and FAT migration tests; never open or modify a device."""

import hashlib
import json
from pathlib import Path
import struct
import subprocess
import tempfile
import unittest
from unittest import mock
import warnings
import zipfile

from bosun_hub.update_package import (
    FLASH_BASE, FLASH_BYTES, STORAGE_BYTES, STORAGE_OFFSET, UpdatePackageError,
    build_native_storage, extract_circuitpython_config, validate_firmware_uf2,
    validate_update_package,
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


def directory_entry(alias, *, cluster, size=0, directory=False, name=None):
    assert len(alias) == 11
    entry = bytearray(32)
    entry[:11] = alias
    entry[11] = 0x10 if directory else 0x20
    entry[12] = 0x18
    struct.pack_into("<H", entry, 26, cluster)
    struct.pack_into("<I", entry, 28, size)
    if name is None:
        return bytes(entry)
    checksum = 0
    for value in alias:
        checksum = (((checksum & 1) << 7) + (checksum >> 1) + value) & 255
    encoded = name.encode("utf-16-le") + b"\0\0"
    encoded += b"\xff\xff" * ((-len(encoded) // 2) % 13)
    pieces = [encoded[i:i + 26] for i in range(0, len(encoded), 26)]
    result = bytearray()
    for index in range(len(pieces), 0, -1):
        long = bytearray(32)
        long[0] = index | (0x40 if index == len(pieces) else 0)
        long[11] = 0x0F
        long[13] = checksum
        long[1:11], long[14:26], long[28:32] = pieces[index - 1][:10], pieces[index - 1][10:22], pieces[index - 1][22:]
        result.extend(long)
    return bytes(result + entry)


def dots(current, parent):
    dot = bytearray(directory_entry(b".          ", cluster=current, directory=True))
    dotdot = bytearray(directory_entry(b"..         ", cluster=parent, directory=True))
    dot[12] = dotdot[12] = 0
    return bytes(dot + dotdot)


class FatImage:
    """Small FAT12 and FAT16 volumes embedded in an exact full-flash image."""

    def __init__(self, bits=12, *, copies=1):
        self.bits = bits
        self.offset = 1024 * 1024
        self.fat_sectors = 1 if bits == 12 else 24
        self.total = 128 if bits == 12 else 6000
        self.root_sector = 1 + copies * self.fat_sectors
        self.data_sector = self.root_sector + 2
        self.data = bytearray(FLASH_BYTES)
        boot = memoryview(self.data)[self.offset:self.offset + 512]
        boot[:11] = b"\xeb\x3c\x90MSDOS5.0"
        struct.pack_into("<HBHBHHBHHHII", boot, 11, 512, 1, 1, copies, 32,
                         self.total, 0xF8, self.fat_sectors, 63, 255, 0, 0)
        boot[510:] = b"\x55\xaa"
        self.set_fat(0, 0xFF8 if bits == 12 else 0xFFF8)
        self.set_fat(1, self.end)
        self.expected = {"active_profile.json": b'{ "id": "test" }\r\n',
                         "profiles/device.json": b'{ "name": "Kept", "future": 42 }\n',
                         "profiles/彩色設定.json": bytes(range(256)) * 3}
        for cluster in range(2, 9):
            self.set_fat(cluster, self.end)
        self.set_fat(6, 7)
        self.write_cluster(3, self.expected["active_profile.json"])
        self.write_cluster(5, self.expected["profiles/device.json"])
        self.write_cluster(6, self.expected["profiles/彩色設定.json"][:512])
        self.write_cluster(7, self.expected["profiles/彩色設定.json"][512:])
        self.write_cluster(8, b"print('firmware')\n")
        self.config = (dots(2, 0) +
                       directory_entry(b"ACTIVE~1JSO", name="active_profile.json", cluster=3,
                                       size=len(self.expected["active_profile.json"])) +
                       directory_entry(b"PROFILES   ", cluster=4, directory=True))
        self.profiles = (dots(4, 2) +
                         directory_entry(b"DEVICE~1JSO", name="device.json", cluster=5,
                                         size=len(self.expected["profiles/device.json"])) +
                         directory_entry(b"UNICOD~1JSO", name="彩色設定.json", cluster=6,
                                         size=len(self.expected["profiles/彩色設定.json"])))
        self.write_cluster(2, self.config)
        self.write_cluster(4, self.profiles)
        self.root = (directory_entry(b"CONFIG     ", directory=True, cluster=2) +
                     directory_entry(b"CODE    PY ", cluster=8, size=len(b"print('firmware')\n")))
        start = self.offset + self.root_sector * 512
        self.data[start:start + len(self.root)] = self.root
        if copies == 2:
            start = self.offset + 512
            self.data[start + self.fat_sectors * 512:start + self.fat_sectors * 1024] = self.data[start:start + self.fat_sectors * 512]

    @property
    def end(self):
        return 0xFFF if self.bits == 12 else 0xFFFF

    def set_fat(self, cluster, following):
        offset = self.offset + 512
        if self.bits == 16:
            struct.pack_into("<H", self.data, offset + cluster * 2, following)
        else:
            offset += cluster + cluster // 2
            original = struct.unpack_from("<H", self.data, offset)[0]
            value = (original & 0x000F) | (following << 4) if cluster & 1 else (original & 0xF000) | following
            struct.pack_into("<H", self.data, offset, value)

    def cluster_offset(self, cluster):
        return self.offset + (self.data_sector + cluster - 2) * 512

    def write_cluster(self, cluster, data):
        assert len(data) <= 512
        start = self.cluster_offset(cluster)
        self.data[start:start + 512] = data.ljust(512, b"\0")


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
                           ("family", "circuitpython"), ("flash_bytes", 2097152),
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


class FatExtractionTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.destination = self.root / "config"

    def assert_rejected(self, image):
        with self.assertRaises(UpdatePackageError):
            extract_circuitpython_config(bytes(image.data), self.destination)
        self.assertFalse(self.destination.exists())
        self.assertEqual(list(self.root.iterdir()), [])

    def test_fat12_and_fat16_preserve_exact_nested_unicode_files_and_empty_directories(self):
        for bits in (12, 16):
            with self.subTest(bits=bits):
                source = FatImage(bits)
                source.set_fat(9, source.end)
                source.write_cluster(9, dots(9, 2))
                source.write_cluster(2, source.config + directory_entry(b"EMPTY      ", directory=True, cluster=9))
                before = hashlib.sha256(source.data).hexdigest()
                dest = self.root / f"config{bits}"
                paths = extract_circuitpython_config(bytes(source.data), dest)
                self.assertEqual(set(paths), set(source.expected))
                self.assertTrue((dest / "empty").is_dir())
                for name, expected in source.expected.items():
                    self.assertEqual((dest / name).read_bytes(), expected)
                self.assertEqual(hashlib.sha256(source.data).hexdigest(), before)
                self.assertFalse((dest / "code.py").exists())

    def test_truncated_or_oversized_flash_missing_and_ambiguous_volume_rejected(self):
        source = FatImage()
        for data in (bytes(source.data[:-1]), bytes(source.data) + b"\0", bytes(FLASH_BYTES)):
            with self.subTest(length=len(data)), self.assertRaises(UpdatePackageError):
                extract_circuitpython_config(data, self.destination)
        length = source.total * 512
        source.data[source.offset + length:source.offset + 2 * length] = source.data[source.offset:source.offset + length]
        self.assert_rejected(source)

    def test_corrupt_bpb_and_table_copies_rejected(self):
        source = FatImage(copies=2)
        extract_circuitpython_config(bytes(source.data), self.destination)
        other = self.root / "rejected"
        source.data[source.offset + 1024] ^= 1
        with self.assertRaises(UpdatePackageError):
            extract_circuitpython_config(bytes(source.data), other)
        for field, value in ((11, 123), (14, 0), (17, 0), (19, 65535), (22, 0)):
            source = FatImage()
            struct.pack_into("<H", source.data, source.offset + field, value)
            with self.subTest(field=field), self.assertRaises(UpdatePackageError):
                extract_circuitpython_config(bytes(source.data), other)

    def test_live_chain_cycle_free_bad_reserved_and_cross_links_rejected(self):
        for first, following in ((7, 6), (6, 0), (6, 1), (6, 0xFF7), (6, 0xFF0),
                                 (6, 120), (3, 8), (2, 2), (5, 3)):
            with self.subTest(first=first, following=following):
                source = FatImage()
                source.set_fat(first, following)
                self.assert_rejected(source)

    def test_orphan_chains_preserved_in_backup_but_cycles_or_live_crosslinks_rejected(self):
        source = FatImage()
        source.set_fat(9, 10)
        source.set_fat(10, source.end)
        original = bytes(source.data)
        self.assertEqual(set(extract_circuitpython_config(original, self.destination)), set(source.expected))
        self.assertEqual(bytes(source.data), original)
        for first, following in ((10, 9), (10, 3), (10, 0), (10, 130)):
            source = FatImage()
            source.set_fat(9, 10)
            source.set_fat(10, source.end)
            source.set_fat(first, following)
            with self.subTest(following=following), self.assertRaises(UpdatePackageError):
                extract_circuitpython_config(bytes(source.data), self.root / "other")

    def test_short_and_long_chains_cannot_silently_truncate_files(self):
        for cluster, following in ((6, 0xFFF), (7, 8)):
            source = FatImage()
            source.set_fat(cluster, following)
            self.assert_rejected(source)

    def test_unsafe_lfn_duplicate_names_and_invalid_parent_are_rejected(self):
        for name in ("../escape", "foo\\bar.json", "CON.json", "trailing.", "trailing ", "foo:bar"):
            source = FatImage()
            source.write_cluster(4, dots(4, 2) + directory_entry(b"UNSAFE~1JSO", name=name, cluster=5, size=1))
            with self.subTest(name=name):
                self.assert_rejected(source)
        source = FatImage()
        source.write_cluster(2, source.config + directory_entry(b"PROFILES   ", cluster=9, directory=True))
        self.assert_rejected(source)
        source = FatImage()
        struct.pack_into("<H", source.data, source.cluster_offset(4) + 32 + 26, 3)
        self.assert_rejected(source)

    def test_lfn_checksum_order_padding_and_utf16_corruption_rejected(self):
        for offset, value in ((64, 0x43), (64 + 13, 0), (64 + 12, 1),
                              (64 + 26, 1), (64 + 30, 1), (96 + 1, 0x00)):
            source = FatImage()
            source.data[source.cluster_offset(2) + offset] = value
            with self.subTest(offset=offset):
                self.assert_rejected(source)

    def test_missing_config_empty_config_and_file_named_config_rejected(self):
        source = FatImage()
        start = source.offset + source.root_sector * 512
        source.data[start:start + 11] = b"OTHER      "
        self.assert_rejected(source)
        source = FatImage()
        source.write_cluster(2, dots(2, 0))
        self.assert_rejected(source)
        source = FatImage()
        source.data[start + 11] = 0x20
        struct.pack_into("<I", source.data, start + 28, 512)
        self.assert_rejected(source)

    def test_existing_destination_and_symlink_cannot_be_overwritten(self):
        self.destination.mkdir()
        keep = self.destination / "keep"
        keep.write_bytes(b"original")
        with self.assertRaises(UpdatePackageError):
            extract_circuitpython_config(bytes(FatImage().data), self.destination)
        self.assertEqual(keep.read_bytes(), b"original")
        alias = self.root / "alias"
        try:
            alias.symlink_to(self.destination, target_is_directory=True)
        except OSError:
            self.skipTest("Symlink creation is unavailable")
        with self.assertRaises(UpdatePackageError):
            extract_circuitpython_config(bytes(FatImage().data), alias / "config")
        self.assertEqual(keep.read_bytes(), b"original")


class StorageBuilderTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.config = self.root / "config"
        self.config.mkdir()
        self.builder = self.root / "bosun_storage_image"
        self.builder.write_bytes(b"installed executable fixture")
        self.output = self.root / "storage.bin"

    def fake_builder(self, args, **kwargs):
        self.assertEqual(args[:3], [str(self.builder), "--config-root", str(self.config)])
        self.assertEqual(args[3], "--output")
        self.assertNotIn("shell", kwargs)
        candidate = Path(args[4])
        candidate.write_bytes(b"\xff" * (STORAGE_BYTES - 512 * 1024) + b"littlefs" + b"\xff" * (512 * 1024 - 8))
        report = {"verified": True, "storage_bytes": STORAGE_BYTES, "block_bytes": 4096}
        return subprocess.CompletedProcess(args, 0, json.dumps(report), "")

    def test_installed_builder_verified_image_is_published(self):
        with mock.patch("bosun_hub.update_package.subprocess.run", side_effect=self.fake_builder):
            image = build_native_storage(self.config, self.output, builder=self.builder)
        self.assertEqual(self.output.read_bytes(), image)
        self.assertEqual(len(image), STORAGE_BYTES)

    def test_failure_invalid_report_or_short_image_never_published(self):
        def short_image(args, **kwargs):
            result = self.fake_builder(args, **kwargs)
            Path(args[4]).write_bytes(b"littlefs")
            return result

        results = [lambda *args, **kwargs: subprocess.CompletedProcess(args, 1, "", "unsupported plugin"),
                   lambda *args, **kwargs: subprocess.CompletedProcess(args, 0, "{}", ""),
                   lambda *args, **kwargs: subprocess.CompletedProcess(args, 0, "invalid json", ""), short_image]
        for result in results:
            with self.subTest(result=result), mock.patch("bosun_hub.update_package.subprocess.run", side_effect=result):
                with self.assertRaises(UpdatePackageError):
                    build_native_storage(self.config, self.output, builder=self.builder)
                self.assertFalse(self.output.exists())

    def test_timeout_does_not_publish_or_leave_candidate(self):
        with mock.patch("bosun_hub.update_package.subprocess.run", side_effect=subprocess.TimeoutExpired("builder", 120)):
            with self.assertRaises(UpdatePackageError):
                build_native_storage(self.config, self.output, builder=self.builder)
        self.assertEqual(set(self.root.iterdir()), {self.config, self.builder})

    def test_existing_output_or_untrusted_relative_builder_rejected_before_execution(self):
        with mock.patch("bosun_hub.update_package.subprocess.run") as run:
            with self.assertRaises(UpdatePackageError):
                build_native_storage(self.config, self.output, builder=Path("uploaded-builder"))
            self.output.write_bytes(b"original")
            with self.assertRaises(UpdatePackageError):
                build_native_storage(self.config, self.output, builder=self.builder)
            self.assertEqual(self.output.read_bytes(), b"original")
            run.assert_not_called()


if __name__ == "__main__":
    unittest.main()
