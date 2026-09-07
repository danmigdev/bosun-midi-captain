"""Offline validation and settings migration for RP2040 firmware packages.

Nothing in this module opens a device or writes flash. Package digests establish
integrity, not publisher identity: the caller must select a trusted release.
The storage builder is installed with the hub, never taken from an upload.
"""

from __future__ import annotations

from dataclasses import dataclass
import hashlib
import io
import json
import os
from pathlib import Path
import re
import shutil
import stat
import struct
import subprocess
import tempfile
import zipfile
import zlib


FLASH_BASE = 0x10000000
FLASH_BYTES = 8 * 1024 * 1024
STORAGE_BYTES = 512 * 1024
STORAGE_OFFSET = FLASH_BYTES - STORAGE_BYTES
_UF2_LIMIT = STORAGE_OFFSET * 2
_MANIFEST_LIMIT = 4096
_PACKAGE_LIMIT = _UF2_LIMIT + 65536
_VERSION = re.compile(r"[0-9]+\.[0-9]+\.[0-9]+(?:[-+][0-9A-Za-z.-]+)?\Z")


class UpdatePackageError(ValueError):
    """The update or existing settings cannot be installed without data loss."""


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


def _u16(data: bytes, offset: int) -> int:
    return struct.unpack_from("<H", data, offset)[0]


def _u32(data: bytes, offset: int) -> int:
    return struct.unpack_from("<I", data, offset)[0]


def _safe_name(name: str) -> str:
    if (not name or name in (".", "..") or name[-1] in " ." or
            any(ord(c) < 32 or c in '<>:"/\\|?*' for c in name) or
            len(name.encode("utf-8")) > 255):
        _fail("FAT contains an unsupported or unsafe filename")
    # Keep extraction portable and prevent Windows device/alternate-stream paths.
    if name.split(".", 1)[0].upper() in {
            "CON", "PRN", "AUX", "NUL", *(f"COM{i}" for i in range(1, 10)),
            *(f"LPT{i}" for i in range(1, 10))}:
        _fail("FAT contains a reserved filename")
    return name


def _long_name(entries: list[bytes], short: bytes) -> str:
    count = entries[0][0] & 0x1F
    if not 1 <= count <= 20 or len(entries) != count:
        _fail("Incomplete FAT long filename")
    checksum = 0
    for value in short[:11]:
        checksum = (((checksum & 1) << 7) + (checksum >> 1) + value) & 255
    pieces = []
    for index, entry in enumerate(entries):
        expected = (count - index) | (0x40 if index == 0 else 0)
        if entry[0] != expected or entry[12] or entry[13] != checksum or _u16(entry, 26):
            _fail("Corrupt FAT long filename sequence or checksum")
        pieces.append(entry[1:11] + entry[14:26] + entry[28:32])
    units = struct.unpack("<" + "H" * (13 * count), b"".join(reversed(pieces)))
    if 0 in units:
        end = units.index(0)
        if any(value != 0xFFFF for value in units[end + 1:]):
            _fail("Corrupt FAT long filename padding")
        units = units[:end]
    if not units or len(units) > 255 or 0xFFFF in units:
        _fail("Invalid FAT long filename length")
    try:
        return _safe_name(struct.pack("<" + "H" * len(units), *units).decode("utf-16-le"))
    except UnicodeError as error:
        raise UpdatePackageError("Invalid UTF-16 FAT filename") from error


class _FatVolume:
    def __init__(self, flash: bytes, offset: int):
        boot = flash[offset:offset + 512]
        sector = _u16(boot, 11)
        per_cluster = boot[13]
        reserved = _u16(boot, 14)
        fat_count = boot[16]
        root_entries = _u16(boot, 17)
        small_sectors, large_sectors = _u16(boot, 19), _u32(boot, 32)
        fat_sectors = _u16(boot, 22)
        if (sector not in (512, 1024, 2048, 4096) or not per_cluster or
                per_cluster & (per_cluster - 1) or sector * per_cluster > 32768 or
                not reserved or fat_count not in (1, 2) or not root_entries or not fat_sectors or
                bool(small_sectors) == bool(large_sectors) or boot[21] < 0xF0):
            _fail("Unsupported or corrupt FAT12/16 boot sector")
        total = small_sectors or large_sectors
        root_sectors = (root_entries * 32 + sector - 1) // sector
        self.data_sector = reserved + fat_count * fat_sectors + root_sectors
        self.cluster_count = (total - self.data_sector) // per_cluster
        if not 0 < self.cluster_count < 65525 or offset + total * sector > len(flash):
            _fail("FAT volume exceeds flash or requires FAT32")
        self.bits = 12 if self.cluster_count < 4085 else 16
        self.cluster_bytes = sector * per_cluster
        self.volume = flash[offset:offset + total * sector]
        fat_bytes = fat_sectors * sector
        self.table = self.volume[reserved * sector:reserved * sector + fat_bytes]
        if fat_bytes * 8 // self.bits < self.cluster_count + 2:
            _fail("FAT allocation table is too short")
        for copy in range(1, fat_count):
            start = (reserved + copy * fat_sectors) * sector
            if self.volume[start:start + fat_bytes] != self.table:
                _fail("FAT allocation table copies disagree")
        if (self.next(0) != ((0xF00 if self.bits == 12 else 0xFF00) | boot[21]) or
                self.next(1) & (0xFFF if self.bits == 12 else 0x3FFF) !=
                (0xFFF if self.bits == 12 else 0x3FFF)):
            _fail("Corrupt FAT reserved allocation entries")
        root_start = (reserved + fat_count * fat_sectors) * sector
        self.root = self.volume[root_start:root_start + root_entries * 32]
        self.data_offset = self.data_sector * sector
        self.used: set[int] = set()
        self.files: dict[str, bytes] = {}
        self.directories: set[str] = set()
        self.entries = 0

    def next(self, cluster: int) -> int:
        if self.bits == 16:
            return _u16(self.table, cluster * 2)
        value = _u16(self.table, cluster + cluster // 2)
        return value >> 4 if cluster & 1 else value & 0xFFF

    def chain(self, first: int) -> bytes:
        result = bytearray()
        cluster = first
        end = 0xFF8 if self.bits == 12 else 0xFFF8
        reserved = 0xFF0 if self.bits == 12 else 0xFFF0
        while True:
            if not 2 <= cluster < min(self.cluster_count + 2, reserved):
                _fail("FAT chain leaves the data region or references a free/reserved cluster")
            if cluster in self.used:
                _fail("FAT contains a cycle or cross-linked cluster")
            self.used.add(cluster)
            start = self.data_offset + (cluster - 2) * self.cluster_bytes
            result.extend(self.volume[start:start + self.cluster_bytes])
            cluster = self.next(cluster)
            if cluster >= end:
                return bytes(result)

    def walk(self, content: bytes, parent: str = "", cluster: int = 0,
             parent_cluster: int = 0, depth: int = 0):
        if depth > 32:
            _fail("FAT directory nesting exceeds the supported limit")
        names: set[str] = set()
        long_entries: list[bytes] = []
        dot_entries = set()
        for offset in range(0, len(content), 32):
            entry = content[offset:offset + 32]
            if entry[0] == 0:
                if long_entries:
                    _fail("Unterminated FAT long filename")
                break
            if entry[0] == 0xE5:
                if long_entries:
                    _fail("Orphan FAT long filename")
                continue
            attributes = entry[11]
            if attributes == 0x0F:
                long_entries.append(entry)
                if len(long_entries) > 20:
                    _fail("Too many FAT long filename entries")
                continue
            if attributes & 0xC0 or _u16(entry, 20):
                _fail("Unsupported FAT attributes or FAT32 cluster field")
            first = _u16(entry, 26)
            size = _u32(entry, 28)
            raw_name = entry[:11]
            if raw_name in (b".          ", b"..         "):
                expected = cluster if raw_name == b".          " else parent_cluster
                if not parent or long_entries or attributes != 0x10 or size or first != expected or raw_name in dot_entries:
                    _fail("Corrupt FAT directory parent entry")
                dot_entries.add(raw_name)
                continue
            if attributes & 0x08:
                if parent or attributes != 0x08 or first or size or long_entries:
                    _fail("Corrupt FAT volume label")
                continue
            try:
                short = (bytes([0xE5]) + raw_name[1:] if raw_name[0] == 0x05 else raw_name).decode("cp437")
                base, extension = short[:8].rstrip(" "), short[8:].rstrip(" ")
                if entry[12] & 0x08:
                    base = base.lower()
                if entry[12] & 0x10:
                    extension = extension.lower()
                short_name = _safe_name(base + ("." + extension if extension else ""))
                name = _long_name(long_entries, entry) if long_entries else short_name
            except UnicodeError as error:
                raise UpdatePackageError("Invalid FAT filename") from error
            long_entries = []
            # Both aliases participate in collision checks, as FAT is case-insensitive.
            aliases = {name.casefold(), short_name.casefold()}
            if aliases & names:
                _fail("Duplicate or ambiguous FAT filename")
            names.update(aliases)
            path = f"{parent}/{name}" if parent else name
            if len(path.encode("utf-8")) > 2048:
                _fail("FAT path is too long")
            self.entries += 1
            if self.entries > 20000:
                _fail("FAT contains too many directory entries")
            if attributes & 0x10:
                if size:
                    _fail("FAT directory has a nonzero file size")
                self.directories.add(path)
                self.walk(self.chain(first), path, first, cluster, depth + 1)
            else:
                if not size:
                    if first:
                        _fail("Empty FAT file has allocated clusters")
                    self.files[path] = b""
                else:
                    data = self.chain(first)
                    if not len(data) - self.cluster_bytes < size <= len(data):
                        _fail("FAT file size disagrees with its allocation chain")
                    self.files[path] = data[:size]
        if long_entries:
            _fail("Unterminated FAT long filename")
        if parent and dot_entries != {b".          ", b"..         "}:
            _fail("FAT directory has no valid parent entries")

    def validate_allocations(self):
        bad_cluster = 0xFF7 if self.bits == 12 else 0xFFF7
        end = 0xFF8 if self.bits == 12 else 0xFFF8
        orphaned = {cluster for cluster in range(2, self.cluster_count + 2)
                    if self.next(cluster) not in (0, bad_cluster) and cluster not in self.used}
        targets = set()
        for cluster in orphaned:
            following = self.next(cluster)
            if following >= end:
                continue
            if following not in orphaned or following in targets:
                _fail("Orphan FAT allocation overlaps live data or an invalid cluster")
            targets.add(following)
        # Historical file OTA can leave isolated chains after deleting firmware
        # files. They have no live filename and are retained in the full-flash
        # backup. Never interpret them as settings or permit cross-links/cycles.
        for first in sorted(orphaned - targets):
            self.chain(first)
        if not orphaned <= self.used:
            _fail("Orphan FAT allocation contains a cycle")


def _read_flash(source: bytes | Path) -> bytes:
    if isinstance(source, bytes):
        data = source
    else:
        try:
            with Path(source).open("rb") as handle:
                if not stat.S_ISREG(os.fstat(handle.fileno()).st_mode):
                    _fail("Flash backup must be a regular file")
                data = handle.read(FLASH_BYTES + 1)
        except OSError as error:
            raise UpdatePackageError(f"Cannot read flash backup: {error}") from error
    if len(data) != FLASH_BYTES:
        _fail("Flash backup must contain exactly 8 MiB")
    return data


def _new_path(path: Path) -> Path:
    """Reject symlinked ancestors; the output must be a new child of a real directory."""
    path = Path(os.path.abspath(path))
    if path.exists() or path.is_symlink():
        _fail("Output must not already exist")
    for ancestor in (path.parent, *path.parent.parents):
        if ancestor.is_symlink() or not ancestor.is_dir():
            _fail("Output parent must exist without symlinks")
    return path


def extract_circuitpython_config(full_flash: bytes | Path, destination: Path) -> list[str]:
    """Extract /config contents byte-for-byte into a new directory, or fail closed.

    The complete FAT directory and allocation graph is checked before publishing.
    No mounted volume, external extraction program, or uploaded code is used.
    """
    data = _read_flash(full_flash)
    candidates = []
    for offset in range(0, len(data) - 511, 512):
        sector = data[offset:offset + 512]
        if sector[0] in (0xEB, 0xE9) and sector[510:512] == b"\x55\xaa":
            try:
                candidates.append(_FatVolume(data, offset))
            except UpdatePackageError:
                continue
    if len(candidates) != 1:
        _fail("Flash backup must contain exactly one valid FAT12/16 volume")
    volume = candidates[0]
    volume.walk(volume.root)
    volume.validate_allocations()
    roots = [name for name in volume.directories if name.casefold() == "config"]
    if len(roots) != 1:
        _fail("CircuitPython backup has no unique /config directory")
    prefix = roots[0] + "/"
    directories = sorted(name[len(prefix):] for name in volume.directories if name.startswith(prefix))
    files = {name[len(prefix):]: content for name, content in volume.files.items() if name.startswith(prefix)}
    if not files:
        _fail("CircuitPython /config is empty")
    destination = _new_path(Path(destination))
    temporary = Path(tempfile.mkdtemp(prefix=".bosun-config-", dir=destination.parent))
    try:
        for name in directories:
            (temporary / name).mkdir(parents=True, exist_ok=False)
        for name, content in files.items():
            with (temporary / name).open("xb") as handle:
                handle.write(content)
                handle.flush()
                os.fsync(handle.fileno())
        # mkdir exclusively reserves the destination; never replace an existing path.
        destination.mkdir()
        try:
            for child in temporary.iterdir():
                child.rename(destination / child.name)
        except BaseException:
            shutil.rmtree(destination)
            raise
    except OSError as error:
        raise UpdatePackageError(f"Cannot publish extracted configuration: {error}") from error
    finally:
        shutil.rmtree(temporary)
    return sorted(files)


def build_native_storage(config_dir: Path, output: Path, *, builder: Path) -> bytes:
    """Run the trusted installed littlefs builder and verify its bounded result.

    The caller supplies its own configured executable path, never a package field.
    The native builder validates JSON and native limits, remounts the real littlefs
    image, checks all profiles, and compares every imported byte before success.
    """
    builder = Path(builder)
    if not builder.is_absolute() or not builder.is_file() or builder.is_symlink():
        _fail("Storage builder must be an installed absolute regular executable")
    if any(parent.is_symlink() for parent in builder.parents):
        _fail("Storage builder path must not contain symlinks")
    output = _new_path(Path(output))
    config_dir = Path(os.path.abspath(config_dir))
    if not config_dir.is_dir() or config_dir.is_symlink() or any(p.is_symlink() for p in config_dir.parents):
        _fail("Config directory must exist without symlinks")
    # Keep unverified output private and only publish after checking the report.
    with tempfile.TemporaryDirectory(prefix=".bosun-storage-", dir=output.parent) as temporary:
        candidate = Path(temporary) / "storage.bin"
        try:
            result = subprocess.run([str(builder), "--config-root", str(config_dir), "--output", str(candidate)],
                                    capture_output=True, text=True, timeout=120, check=False)
            if result.returncode:
                _fail("Native configuration migration failed: " + result.stderr.strip()[:2048])
            report = json.loads(result.stdout)
            if (not isinstance(report, dict) or report.get("verified") is not True or
                    report.get("storage_bytes") != STORAGE_BYTES or report.get("block_bytes") != 4096):
                _fail("Storage builder did not report a verified native image")
            if candidate.is_symlink() or not candidate.is_file():
                _fail("Storage builder did not produce a regular image")
            with candidate.open("rb") as handle:
                image = handle.read(STORAGE_BYTES + 1)
            if len(image) != STORAGE_BYTES or b"littlefs" not in image[:8192]:
                _fail("Storage builder produced an invalid image")
            with output.open("xb") as handle:
                handle.write(image)
                handle.flush()
                os.fsync(handle.fileno())
            return image
        except UpdatePackageError:
            raise
        except (OSError, ValueError, subprocess.TimeoutExpired) as error:
            raise UpdatePackageError(f"Cannot build native storage image: {error}") from error
