"""Offline validation of native RP2040 firmware update packages.

Nothing in this module opens a device or writes flash. Package digests establish
integrity, not publisher identity: the caller must select a trusted release.
"""

from __future__ import annotations

from dataclasses import dataclass
import hashlib
import io
import json
import os
from pathlib import Path
import re
import stat
import struct
import zipfile
import zlib


FLASH_BASE = 0x10000000
FLASH_BYTES = 8 * 1024 * 1024
STORAGE_BYTES = 4 * 1024 * 1024
STORAGE_OFFSET = FLASH_BYTES - STORAGE_BYTES
_UF2_LIMIT = STORAGE_OFFSET * 2
_MANIFEST_LIMIT = 4096
_PACKAGE_LIMIT = _UF2_LIMIT + 65536
_VERSION = re.compile(r"[0-9]+\.[0-9]+\.[0-9]+(?:[-+][0-9A-Za-z.-]+)?\Z")


class UpdatePackageError(ValueError):
    """The update package is invalid or cannot be installed safely."""


@dataclass(frozen=True)
class ValidatedPackage:
    manifest: dict
    firmware_uf2: bytes
    firmware_pages: dict[int, bytes]


def _fail(message: str):
    raise UpdatePackageError(message)


def _unique_json(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            _fail(f"Duplicate manifest key: {key}")
        result[key] = value
    return result


def validate_firmware_uf2(data: bytes) -> dict[int, bytes]:
    """Accept complete native images, excluding the reserved settings region."""
    if not data or len(data) % 512 or len(data) > _UF2_LIMIT:
        _fail("Invalid UF2 size")
    count = len(data) // 512
    pages = {}
    for index in range(count):
        block = data[index * 512:(index + 1) * 512]
        magic0, magic1, flags, address, size, number, total, family = struct.unpack_from("<8I", block)
        if (magic0, magic1, struct.unpack_from("<I", block, 508)[0]) != (
                0x0A324655, 0x9E5D5157, 0x0AB16F30):
            _fail("Invalid UF2 magic")
        if flags != 0x2000 or family != 0xE48BFF56:
            _fail("UF2 must target RP2040 flash with the family identifier")
        if size != 256 or address % 256 or number != index or total != count:
            _fail("Invalid UF2 payload, alignment, or block sequence")
        if not FLASH_BASE <= address < FLASH_BASE + STORAGE_OFFSET:
            _fail("UF2 overlaps settings or leaves the firmware flash region")
        if address in pages:
            _fail("Duplicate UF2 flash address")
        pages[address] = block[32:288]
    # A partial update could depend on stale instructions from another family.
    if sorted(pages) != list(range(FLASH_BASE, FLASH_BASE + count * 256, 256)):
        _fail("UF2 must contain a complete contiguous firmware image starting at flash base")
    if FLASH_BASE + 256 not in pages:
        _fail("UF2 has no RP2040 vector table")
    stack, reset = struct.unpack_from("<II", pages[FLASH_BASE + 256])
    if stack % 8 or not 0x20000000 < stack <= 0x20042000:
        _fail("Invalid RP2040 initial stack pointer")
    if not reset & 1 or (reset & ~255) not in pages or reset < FLASH_BASE + 256:
        _fail("Invalid RP2040 reset vector")
    return pages


def validate_update_package(path: Path) -> ValidatedPackage:
    """Validate a bounded ZIP containing exactly manifest.json and firmware.uf2."""
    path = Path(path)
    try:
        with path.open("rb") as source:
            if not stat.S_ISREG(os.fstat(source.fileno()).st_mode):
                _fail("Firmware package must be a regular file")
            raw = source.read(_PACKAGE_LIMIT + 1)
        if len(raw) > _PACKAGE_LIMIT:
            _fail("Firmware package is too large")
        with zipfile.ZipFile(io.BytesIO(raw)) as archive:
            entries = archive.infolist()
            if len(entries) != 2 or {e.filename for e in entries} != {"manifest.json", "firmware.uf2"}:
                _fail("Package must contain exactly manifest.json and firmware.uf2")
            for entry in entries:
                mode = entry.external_attr >> 16
                if (entry.orig_filename != entry.filename or entry.is_dir() or
                        entry.external_attr & 0x10 or entry.flag_bits & 1 or
                        (stat.S_IFMT(mode) not in (0, stat.S_IFREG)) or
                        entry.compress_type not in (zipfile.ZIP_STORED, zipfile.ZIP_DEFLATED)):
                    _fail("Unsupported ZIP entry type, encryption, or compression")
                limit = _MANIFEST_LIMIT if entry.filename == "manifest.json" else _UF2_LIMIT
                if not 0 < entry.file_size <= limit or entry.file_size > max(1, entry.compress_size) * 200:
                    _fail("ZIP entry exceeds its size or compression ratio limit")
            manifest = json.loads(archive.read("manifest.json").decode("utf-8"),
                                  object_pairs_hook=_unique_json)
            firmware = archive.read("firmware.uf2")
    except UpdatePackageError:
        raise
    except (OSError, ValueError, RuntimeError, zipfile.BadZipFile, NotImplementedError,
            EOFError, RecursionError, zlib.error) as error:
        raise UpdatePackageError(f"Cannot read firmware package: {error}") from error
    keys = {"schema", "board", "release", "family", "firmware_version", "flash_bytes", "firmware_sha256"}
    if not isinstance(manifest, dict) or set(manifest) != keys:
        _fail("Unsupported firmware manifest fields")
    if type(manifest["schema"]) is not int or manifest["schema"] != 1:
        _fail("Unsupported firmware manifest schema")
    if manifest["board"] != "midi-captain-rp2040" or manifest["family"] != "native":
        _fail("Package does not target native MIDI Captain RP2040 firmware")
    if type(manifest["flash_bytes"]) is not int or manifest["flash_bytes"] != FLASH_BYTES:
        _fail("Package has unsupported flash geometry")
    for key in ("release", "firmware_version"):
        value = manifest[key]
        if not isinstance(value, str) or len(value) > 80 or not _VERSION.fullmatch(value):
            _fail(f"Invalid {key}")
    digest = manifest["firmware_sha256"]
    if not isinstance(digest, str) or not re.fullmatch(r"[0-9a-f]{64}", digest):
        _fail("Invalid firmware SHA256")
    if hashlib.sha256(firmware).hexdigest() != digest:
        _fail("Firmware SHA256 mismatch")
    return ValidatedPackage(manifest, firmware, validate_firmware_uf2(firmware))
