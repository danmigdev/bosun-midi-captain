#!/usr/bin/env python3
"""Verify packaged Bosun firmware resources byte-for-byte.

The canonical packaging input is ``editor/src-tauri/resources`` after
``sync_firmware_resources.py`` has run.  Android additionally needs those
files copied into its generated ``assets`` directory.  This checker compares
the complete firmware/lib inventories plus ``circuitpython.uf2`` against
either that directory or a finished ZIP-compatible artifact such as an APK.
Unrelated Android assets (the frontend and tauri.conf.json) are ignored.
"""

from __future__ import annotations

import argparse
import hashlib
import os
import stat
import sys
import zipfile
from pathlib import Path, PurePosixPath


class VerificationError(RuntimeError):
    """The packaged resource inventory is missing, stale, or unsafe."""


RESOURCE_FILE = "circuitpython.uf2"
RESOURCE_TREES = ("firmware", "lib")
# Git/checkouts can give adjacent tracked files slightly different mtimes.
# Treat only a clearly newer source as evidence that its deploy-preferred
# compiled sibling was not regenerated.
COMPILED_STALE_TOLERANCE_NS = 5_000_000_000


def _sha256_stream(stream) -> str:
    digest = hashlib.sha256()
    while True:
        chunk = stream.read(1024 * 1024)
        if not chunk:
            return digest.hexdigest()
        digest.update(chunk)


def _is_link(path: Path) -> bool:
    is_junction = getattr(path, "is_junction", None)
    return path.is_symlink() or bool(is_junction and is_junction())


def _directory_inventory(root: Path) -> dict[str, str]:
    root = root.expanduser()
    if _is_link(root):
        raise VerificationError(f"resource directory is unsafe: {root}")
    try:
        root = root.resolve(strict=True)
    except OSError as exc:
        raise VerificationError(f"resource directory is missing: {root}") from exc
    if not root.is_dir() or _is_link(root):
        raise VerificationError(f"resource directory is unsafe: {root}")

    inventory: dict[str, str] = {}

    def add_file(path: Path, relative: PurePosixPath) -> None:
        if _is_link(path) or not path.is_file():
            raise VerificationError(f"packaged resource is unsafe: {path}")
        try:
            with path.open("rb") as stream:
                inventory[relative.as_posix()] = _sha256_stream(stream)
        except OSError as exc:
            raise VerificationError(f"cannot read packaged resource {path}: {exc}") from exc

    single = root / RESOURCE_FILE
    add_file(single, PurePosixPath(RESOURCE_FILE))
    for tree_name in RESOURCE_TREES:
        tree = root / tree_name
        if not tree.is_dir() or _is_link(tree):
            raise VerificationError(f"packaged resource tree is missing or unsafe: {tree}")
        for directory, dirnames, filenames in os.walk(tree, followlinks=False):
            base = Path(directory)
            for dirname in dirnames:
                candidate = base / dirname
                if _is_link(candidate):
                    raise VerificationError(f"packaged resource is unsafe: {candidate}")
            for filename in filenames:
                candidate = base / filename
                relative = PurePosixPath(tree_name) / PurePosixPath(
                    candidate.relative_to(tree).as_posix()
                )
                add_file(candidate, relative)
    return inventory


def _archive_inventory(archive: Path, prefix: str) -> dict[str, str]:
    archive = archive.expanduser()
    if _is_link(archive):
        raise VerificationError(f"package archive is unsafe: {archive}")
    try:
        archive = archive.resolve(strict=True)
    except OSError as exc:
        raise VerificationError(f"package archive is missing: {archive}") from exc
    if not archive.is_file() or _is_link(archive):
        raise VerificationError(f"package archive is unsafe: {archive}")

    normalized_prefix = prefix.replace("\\", "/").strip("/")
    prefix_parts = PurePosixPath(normalized_prefix).parts if normalized_prefix else ()
    if (
        any(part in ("", ".", "..") for part in prefix_parts)
        or (prefix_parts and prefix_parts[0].endswith(":"))
    ):
        raise VerificationError("archive prefix must be normalized and relative")

    inventory: dict[str, str] = {}
    seen_names: set[str] = set()
    try:
        with zipfile.ZipFile(archive) as package:
            for info in package.infolist():
                raw_name = info.filename
                # PowerShell Compress-Archive emits backslashes on Windows.
                # Normalize first so mixed separators cannot bypass traversal
                # checks or alias an already-seen entry.
                name = raw_name.replace("\\", "/")
                logical = PurePosixPath(name)
                canonical_name = logical.as_posix()
                if canonical_name in seen_names:
                    raise VerificationError(
                        f"duplicate normalized archive entry: {raw_name}"
                    )
                seen_names.add(canonical_name)
                # PurePosixPath deliberately collapses repeated separators and
                # `.` components. Reject those aliases rather than allowing a
                # later ZIP entry to overwrite an earlier inventory item.
                raw_parts = name.rstrip("/").split("/")
                if (
                    logical.is_absolute()
                    or any(part in ("", ".", "..") for part in raw_parts)
                    or (logical.parts and logical.parts[0].endswith(":"))
                ):
                    raise VerificationError(f"unsafe archive entry: {raw_name}")
                unix_mode = (info.external_attr >> 16) & 0xFFFF
                if stat.S_IFMT(unix_mode) == stat.S_IFLNK:
                    raise VerificationError(f"symlink archive entry is unsafe: {raw_name}")
                if (
                    info.is_dir()
                    or name.endswith("/")
                    or logical.parts[:len(prefix_parts)] != prefix_parts
                ):
                    continue
                relative_parts = logical.parts[len(prefix_parts):]
                if not relative_parts:
                    continue
                relative = PurePosixPath(*relative_parts)
                in_scope = (
                    relative.as_posix() == RESOURCE_FILE
                    or relative.parts[0] in RESOURCE_TREES
                )
                if not in_scope:
                    continue
                with package.open(info, "r") as stream:
                    inventory[relative.as_posix()] = _sha256_stream(stream)
    except (OSError, zipfile.BadZipFile) as exc:
        raise VerificationError(f"cannot read package archive {archive}: {exc}") from exc
    return inventory


def _compare(expected: dict[str, str], actual: dict[str, str]) -> None:
    missing = sorted(expected.keys() - actual.keys())
    extra = sorted(actual.keys() - expected.keys())
    different = sorted(
        name for name in expected.keys() & actual.keys()
        if expected[name] != actual[name]
    )
    if not (missing or extra or different):
        return
    details = []
    if missing:
        details.append("missing: " + ", ".join(missing))
    if extra:
        details.append("unexpected: " + ", ".join(extra))
    if different:
        details.append("hash mismatch: " + ", ".join(different))
    raise VerificationError("packaged firmware differs from resources; " + "; ".join(details))


def _validate_compiled_siblings(resources: Path) -> None:
    """Fail when a production source plainly postdates its preferred .mpy.

    The installer intentionally omits a ``.py`` whenever the sibling ``.mpy``
    exists.  Hash equality cannot relate source to bytecode without the pinned
    compiler, but this catches the common local failure mode immediately and
    leaves exact compiler verification to ``build_firmware_mpy.py --check``.
    """

    firmware_lib = resources / "firmware" / "lib"
    for package_name in ("captain", "plugins"):
        package = firmware_lib / package_name
        if not package.is_dir() or _is_link(package):
            raise VerificationError(
                f"production firmware package is missing or unsafe: {package}"
            )
        for source in package.rglob("*.py"):
            if source.name == "__init__.py":
                continue
            compiled = source.with_suffix(".mpy")
            if not compiled.is_file() or _is_link(compiled):
                raise VerificationError(
                    f"compiled sibling is missing or unsafe: {compiled}"
                )
            try:
                source_mtime = source.stat().st_mtime_ns
                compiled_mtime = compiled.stat().st_mtime_ns
            except OSError as exc:
                raise VerificationError(
                    f"cannot inspect compiled sibling freshness for {source}: {exc}"
                ) from exc
            if source_mtime > compiled_mtime + COMPILED_STALE_TOLERANCE_NS:
                raise VerificationError(
                    f"compiled sibling is visibly stale: {compiled} is older than {source}; "
                    "rebuild with the pinned CircuitPython mpy-cross compiler"
                )


def verify_directory(resources: Path, packaged_root: Path) -> int:
    _validate_compiled_siblings(resources)
    expected = _directory_inventory(resources)
    actual = _directory_inventory(packaged_root)
    _compare(expected, actual)
    return len(expected)


def verify_archive(resources: Path, archive: Path, prefix: str) -> int:
    _validate_compiled_siblings(resources)
    expected = _directory_inventory(resources)
    actual = _archive_inventory(archive, prefix)
    _compare(expected, actual)
    return len(expected)


def _parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--resources", type=Path, required=True)
    target = parser.add_mutually_exclusive_group(required=True)
    target.add_argument("--directory", type=Path)
    target.add_argument("--archive", type=Path)
    parser.add_argument(
        "--prefix", default="",
        help="archive path containing circuitpython.uf2, firmware and lib",
    )
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = _parse_args(argv)
    try:
        if args.directory is not None:
            if args.prefix:
                raise VerificationError("--prefix is valid only with --archive")
            count = verify_directory(args.resources, args.directory)
        else:
            count = verify_archive(args.resources, args.archive, args.prefix)
    except (OSError, VerificationError) as exc:
        print(f"firmware package verification failed: {exc}", file=sys.stderr)
        return 1
    print(f"[ok  ] packaged firmware inventory and SHA-256 verified ({count} files)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
