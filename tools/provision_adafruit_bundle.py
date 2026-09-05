#!/usr/bin/env python3
"""Install Bosun's SHA-pinned CircuitPython 9 Adafruit dependencies."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import shutil
import stat
import sys
import tempfile
import urllib.request
import uuid
import zipfile
from pathlib import Path, PurePosixPath


LOCK_DEFAULT = Path(__file__).resolve().with_name("adafruit-bundle-lock.json")
VENDOR_ROOTS = (
    "adafruit_display_text",
    "adafruit_pixelbuf.mpy",
    "adafruit_st7789.mpy",
    "neopixel.mpy",
)


class ProvisionError(RuntimeError):
    pass


def _sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        while chunk := stream.read(1024 * 1024):
            digest.update(chunk)
    return digest.hexdigest()


def _is_link(path: Path) -> bool:
    is_junction = getattr(path, "is_junction", None)
    return path.is_symlink() or bool(is_junction and is_junction())


def load_lock(path: Path) -> dict:
    try:
        lock = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise ProvisionError(f"cannot read bundle lock {path}: {exc}") from exc
    tag = lock.get("tag")
    series = lock.get("bundle_series")
    filename = lock.get("filename")
    expected_filename = f"adafruit-circuitpython-bundle-{series}-mpy-{tag}.zip"
    expected_url = (
        "https://github.com/adafruit/Adafruit_CircuitPython_Bundle/"
        f"releases/download/{tag}/{expected_filename}"
    )
    if (
        lock.get("schema") != 1
        or lock.get("circuitpython_major") != 9
        or series != "9.x"
        or not isinstance(tag, str)
        or not re.fullmatch(r"20\d{6}", tag)
        or filename != expected_filename
        or lock.get("url") != expected_url
        or not isinstance(lock.get("size"), int)
        or lock["size"] <= 0
        or not re.fullmatch(r"[0-9a-f]{64}", str(lock.get("sha256", "")))
    ):
        raise ProvisionError("bundle lock identity is invalid or not CircuitPython 9.x")
    files = lock.get("files")
    if not isinstance(files, dict) or len(files) != 9:
        raise ProvisionError("bundle lock must contain exactly nine vendor files")
    for name, digest in files.items():
        logical = PurePosixPath(name)
        if (
            logical.is_absolute()
            or any(part in ("", ".", "..") for part in name.split("/"))
            or logical.parts[0] not in VENDOR_ROOTS
            or not re.fullmatch(r"[0-9a-f]{64}", str(digest))
        ):
            raise ProvisionError(f"invalid locked vendor entry: {name}")
    expected_roots = {PurePosixPath(name).parts[0] for name in files}
    if expected_roots != set(VENDOR_ROOTS):
        raise ProvisionError("bundle lock does not cover the exact Bosun vendor roots")
    return lock


def _read_locked_files(archive: Path, lock: dict) -> dict[str, bytes]:
    if _is_link(archive) or not archive.is_file():
        raise ProvisionError(f"Adafruit bundle archive is missing or unsafe: {archive}")
    if archive.stat().st_size != lock["size"]:
        raise ProvisionError("Adafruit bundle size mismatch")
    if _sha256_file(archive) != lock["sha256"]:
        raise ProvisionError("Adafruit bundle SHA-256 mismatch")
    prefix = lock["filename"][:-4] + "/lib/"
    result: dict[str, bytes] = {}
    try:
        with zipfile.ZipFile(archive) as package:
            entries: dict[str, zipfile.ZipInfo] = {}
            for info in package.infolist():
                normalized = info.filename.replace("\\", "/")
                if normalized in entries:
                    raise ProvisionError(f"duplicate bundle entry: {normalized}")
                entries[normalized] = info
            for relative, expected_hash in lock["files"].items():
                name = prefix + relative
                info = entries.get(name)
                if info is None or info.is_dir():
                    raise ProvisionError(f"locked vendor file is missing: {relative}")
                unix_mode = (info.external_attr >> 16) & 0xFFFF
                if stat.S_IFMT(unix_mode) == stat.S_IFLNK:
                    raise ProvisionError(f"locked vendor file is a symlink: {relative}")
                data = package.read(info)
                if _sha256_bytes(data) != expected_hash:
                    raise ProvisionError(f"locked vendor file hash mismatch: {relative}")
                if len(data) < 4 or data[0] != ord("C") or data[1] != 6:
                    raise ProvisionError(
                        f"vendor bytecode is not CircuitPython mpy-v6: {relative}"
                    )
                result[relative] = data
    except (OSError, zipfile.BadZipFile) as exc:
        raise ProvisionError(f"cannot read Adafruit bundle {archive}: {exc}") from exc
    return result


def _installed_files(destination: Path, lock: dict) -> dict[str, bytes]:
    result = {}
    display_root = destination / "adafruit_display_text"
    actual_display = set()
    if display_root.is_dir() and not _is_link(display_root):
        for directory, dirnames, filenames in os.walk(display_root, followlinks=False):
            base = Path(directory)
            for name in dirnames:
                if _is_link(base / name):
                    raise ProvisionError("installed vendor directory contains a link")
            for name in filenames:
                path = base / name
                if _is_link(path) or not path.is_file():
                    raise ProvisionError("installed vendor directory contains an unsafe file")
                actual_display.add(path.relative_to(destination).as_posix())
    expected_display = {
        name for name in lock["files"] if name.startswith("adafruit_display_text/")
    }
    if actual_display != expected_display:
        raise ProvisionError("installed adafruit_display_text inventory is not exact")
    for relative, expected_hash in lock["files"].items():
        path = destination / Path(*PurePosixPath(relative).parts)
        if _is_link(path) or not path.is_file():
            raise ProvisionError(f"installed vendor file is missing or unsafe: {relative}")
        data = path.read_bytes()
        if _sha256_bytes(data) != expected_hash:
            raise ProvisionError(f"installed vendor file hash mismatch: {relative}")
        result[relative] = data
    return result


def provision(archive: Path, destination: Path, lock: dict) -> int:
    files = _read_locked_files(archive, lock)
    destination.mkdir(parents=True, exist_ok=True)
    if not destination.is_dir() or _is_link(destination):
        raise ProvisionError(f"vendor destination is unsafe: {destination}")
    parent = destination.parent
    stage = parent / f".{destination.name}.adafruit-stage-{uuid.uuid4().hex}"
    backups: dict[Path, Path] = {}
    installed: list[Path] = []
    committed = False
    try:
        for relative, data in files.items():
            output = stage / Path(*PurePosixPath(relative).parts)
            output.parent.mkdir(parents=True, exist_ok=True)
            output.write_bytes(data)
        for root_name in VENDOR_ROOTS:
            target = destination / root_name
            source = stage / root_name
            if _is_link(target):
                raise ProvisionError(f"vendor target is a link or junction: {target}")
            if target.exists():
                backup = parent / f".{destination.name}-{root_name}.backup-{uuid.uuid4().hex}"
                target.rename(backup)
                backups[target] = backup
            source.rename(target)
            installed.append(target)
        _installed_files(destination, lock)
        committed = True
    except Exception:
        for target in reversed(installed):
            if target.is_dir():
                shutil.rmtree(target)
            elif target.exists():
                target.unlink()
        for target, backup in backups.items():
            if backup.exists() and not target.exists():
                backup.rename(target)
        raise
    finally:
        if stage.exists():
            shutil.rmtree(stage)
        if committed:
            for backup in backups.values():
                if backup.is_dir():
                    shutil.rmtree(backup)
                elif backup.exists():
                    backup.unlink()
    return len(files)


def _download(lock: dict, output: Path) -> None:
    request = urllib.request.Request(lock["url"], headers={"User-Agent": "Bosun-assets/1"})
    try:
        total = 0
        with urllib.request.urlopen(request, timeout=60) as response, output.open("wb") as stream:
            while chunk := response.read(1024 * 1024):
                total += len(chunk)
                if total > lock["size"]:
                    raise ProvisionError("downloaded Adafruit bundle exceeds locked size")
                stream.write(chunk)
        if total != lock["size"]:
            raise ProvisionError("downloaded Adafruit bundle size mismatch")
    except OSError as exc:
        raise ProvisionError(f"cannot download pinned Adafruit bundle: {exc}") from exc


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--destination", type=Path, required=True)
    parser.add_argument("--lock", type=Path, default=LOCK_DEFAULT)
    parser.add_argument("--archive", type=Path, help="use a local archive (offline/testing)")
    parser.add_argument("--check", action="store_true", help="verify installed files only")
    args = parser.parse_args(argv)
    try:
        lock = load_lock(args.lock)
        if args.check:
            if args.archive:
                raise ProvisionError("--archive cannot be combined with --check")
            count = len(_installed_files(args.destination, lock))
        elif args.archive:
            count = provision(args.archive, args.destination, lock)
        else:
            with tempfile.TemporaryDirectory(prefix="bosun-adafruit-") as temporary:
                archive = Path(temporary) / lock["filename"]
                _download(lock, archive)
                count = provision(archive, args.destination, lock)
    except (OSError, ProvisionError) as exc:
        print(f"Adafruit bundle provisioning failed: {exc}", file=sys.stderr)
        return 1
    print(
        f"[ok  ] pinned Adafruit CircuitPython {lock['bundle_series']} "
        f"bundle {lock['tag']} verified ({count} files)"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
