#!/usr/bin/env python3
"""Synchronize the canonical firmware into the Tauri resource trees.

``firmware/`` is the only source of truth for Bosun firmware.  Tauri needs
two derived trees:

* ``resources/firmware`` is an exact mirror (excluding transient caches);
* ``resources/lib`` mirrors each Bosun-owned top-level entry from
  ``firmware/lib`` while preserving unrelated, vendored CircuitPython libs.

The command verifies every copied file by SHA-256.  It is deliberately safe
to run before every build: unchanged files and trees are left untouched.
"""

from __future__ import annotations

import argparse
import hashlib
import os
import shutil
import sys
import uuid
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import Iterable


class SyncError(RuntimeError):
    """A resource tree is unsafe, incomplete, or out of sync."""


IGNORED_DIRS = frozenset({"__pycache__"})
IGNORED_FILES = frozenset({".DS_Store", "Thumbs.db"})
IGNORED_SUFFIXES = (".pyc", ".tmp")


@dataclass(frozen=True)
class TreeSnapshot:
    directories: frozenset[PurePosixPath]
    files: dict[PurePosixPath, str]


@dataclass(frozen=True)
class SyncResult:
    changed: bool
    digest: str


def _ignored(name: str, *, directory: bool) -> bool:
    if directory:
        return name in IGNORED_DIRS
    return name in IGNORED_FILES or name.endswith(IGNORED_SUFFIXES)


def _is_link(path: Path) -> bool:
    """Treat Windows junctions like symlinks when Python can identify them."""

    is_junction = getattr(path, "is_junction", None)
    return path.is_symlink() or bool(is_junction and is_junction())


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        while True:
            chunk = stream.read(1024 * 1024)
            if not chunk:
                break
            digest.update(chunk)
    return digest.hexdigest()


def _snapshot(root: Path, *, reject_symlinks: bool = True) -> TreeSnapshot:
    """Return a deterministic, cache-free tree snapshot.

    Symlinks are rejected for sources and verified destinations.  Following a
    link here could copy or hash a file outside the repository resource tree.
    """

    if not root.is_dir() or _is_link(root):
        raise SyncError(f"directory is missing or unsafe: {root}")

    directories: set[PurePosixPath] = set()
    files: dict[PurePosixPath, str] = {}

    def visit(directory: Path, relative: PurePosixPath) -> None:
        try:
            entries = sorted(os.scandir(directory), key=lambda item: item.name.casefold())
        except OSError as exc:
            raise SyncError(f"cannot read {directory}: {exc}") from exc

        for entry in entries:
            rel = relative / entry.name
            if entry.is_symlink() or _is_link(Path(entry.path)):
                if reject_symlinks:
                    raise SyncError(f"symlink is not allowed in resource trees: {entry.path}")
                continue
            if entry.is_dir(follow_symlinks=False):
                if _ignored(entry.name, directory=True):
                    continue
                directories.add(rel)
                visit(Path(entry.path), rel)
            elif entry.is_file(follow_symlinks=False):
                if _ignored(entry.name, directory=False):
                    continue
                files[rel] = _sha256(Path(entry.path))
            else:
                raise SyncError(f"unsupported filesystem entry: {entry.path}")

    visit(root, PurePosixPath())
    return TreeSnapshot(frozenset(directories), files)


def _iter_ignored(root: Path) -> Iterable[Path]:
    if not root.exists():
        return ()
    found: list[Path] = []
    for directory, dirnames, filenames in os.walk(root, topdown=True, followlinks=False):
        base = Path(directory)
        for name in list(dirnames):
            if _ignored(name, directory=True):
                found.append(base / name)
                dirnames.remove(name)
        for name in filenames:
            if _ignored(name, directory=False):
                found.append(base / name)
    return found


def _remove_path(path: Path) -> None:
    if _is_link(path):
        if path.is_dir():
            path.rmdir()
        else:
            path.unlink()
    elif path.is_file():
        path.unlink()
    elif path.exists():
        shutil.rmtree(path)


def _prune_ignored(root: Path, *, check: bool) -> bool:
    ignored = list(_iter_ignored(root))
    if check and ignored:
        raise SyncError(f"cache/transient file is present in resources: {ignored[0]}")
    for path in sorted(ignored, key=lambda item: len(item.parts), reverse=True):
        _remove_path(path)
    return bool(ignored)


def _copy_tree(source: Path, destination: Path) -> None:
    """Create a filtered copy; destination must not already exist."""

    destination.mkdir(parents=True)

    def visit(src: Path, dst: Path) -> None:
        for entry in sorted(os.scandir(src), key=lambda item: item.name.casefold()):
            src_path = Path(entry.path)
            dst_path = dst / entry.name
            if entry.is_symlink() or _is_link(src_path):
                raise SyncError(f"symlink is not allowed in firmware sources: {src_path}")
            if entry.is_dir(follow_symlinks=False):
                if _ignored(entry.name, directory=True):
                    continue
                dst_path.mkdir()
                visit(src_path, dst_path)
            elif entry.is_file(follow_symlinks=False):
                if _ignored(entry.name, directory=False):
                    continue
                shutil.copy2(src_path, dst_path)
                if _sha256(src_path) != _sha256(dst_path):
                    raise SyncError(f"hash verification failed while copying {src_path}")
            else:
                raise SyncError(f"unsupported filesystem entry: {src_path}")

    visit(source, destination)


def _replace_tree(source: Path, destination: Path, *, check: bool) -> bool:
    expected = _snapshot(source)
    if destination.is_dir() and not _is_link(destination):
        actual = _snapshot(destination)
        ignored = list(_iter_ignored(destination))
        if expected == actual and not ignored:
            return False

    if check:
        raise SyncError(f"resource tree is out of sync: {source} -> {destination}")
    if destination.exists() and not destination.is_dir():
        raise SyncError(f"resource destination is not a directory: {destination}")
    if _is_link(destination):
        raise SyncError(f"resource destination must not be a symlink: {destination}")

    parent = destination.parent
    parent.mkdir(parents=True, exist_ok=True)
    stage = parent / f".{destination.name}.bosun-sync-{uuid.uuid4().hex}"
    backup = parent / f".{destination.name}.bosun-backup-{uuid.uuid4().hex}"
    _copy_tree(source, stage)
    if _snapshot(stage) != expected:
        _remove_path(stage)
        raise SyncError(f"staged resource verification failed: {destination}")

    moved_old = False
    try:
        if destination.exists():
            destination.rename(backup)
            moved_old = True
        stage.rename(destination)
    except Exception:
        if stage.exists():
            _remove_path(stage)
        if moved_old and backup.exists() and not destination.exists():
            backup.rename(destination)
        raise
    finally:
        if backup.exists() and destination.exists():
            _remove_path(backup)
    return True


def _copy_file(source: Path, destination: Path, *, check: bool) -> bool:
    expected_hash = _sha256(source)
    if destination.is_file() and not _is_link(destination):
        if _sha256(destination) == expected_hash:
            return False
    if check:
        raise SyncError(f"resource file is out of sync: {source} -> {destination}")
    if destination.exists() and destination.is_dir():
        raise SyncError(f"resource file destination is a directory: {destination}")
    if _is_link(destination):
        destination.unlink()
    destination.parent.mkdir(parents=True, exist_ok=True)
    temporary = destination.with_name(f".{destination.name}.bosun-sync-{uuid.uuid4().hex}.tmp")
    try:
        shutil.copy2(source, temporary)
        if _sha256(temporary) != expected_hash:
            raise SyncError(f"hash verification failed while copying {source}")
        os.replace(temporary, destination)
    finally:
        if temporary.exists():
            temporary.unlink()
    return True


def _sync_additive_lib(source: Path, destination: Path, *, check: bool) -> bool:
    """Mirror Bosun-owned roots, preserving unrelated third-party roots."""

    if not source.is_dir() or _is_link(source):
        raise SyncError(f"firmware lib directory is missing or unsafe: {source}")
    if destination.exists() and (not destination.is_dir() or _is_link(destination)):
        raise SyncError(f"resource lib destination is unsafe: {destination}")
    if not destination.exists():
        if check:
            raise SyncError(f"resource lib directory is missing: {destination}")
        destination.mkdir(parents=True)

    changed = _prune_ignored(destination, check=check)
    for entry in sorted(os.scandir(source), key=lambda item: item.name.casefold()):
        src = Path(entry.path)
        dst = destination / entry.name
        if entry.is_symlink() or _is_link(src):
            raise SyncError(f"symlink is not allowed in firmware sources: {src}")
        if entry.is_dir(follow_symlinks=False):
            if _ignored(entry.name, directory=True):
                continue
            changed = _replace_tree(src, dst, check=check) or changed
        elif entry.is_file(follow_symlinks=False):
            if _ignored(entry.name, directory=False):
                continue
            changed = _copy_file(src, dst, check=check) or changed
        else:
            raise SyncError(f"unsupported filesystem entry: {src}")
    return changed


def _resource_digest(resources: Path) -> str:
    snapshot = _snapshot(resources)
    digest = hashlib.sha256()
    for relative, file_hash in sorted(snapshot.files.items(), key=lambda item: item[0].as_posix()):
        encoded = relative.as_posix().encode("utf-8")
        digest.update(len(encoded).to_bytes(4, "big"))
        digest.update(encoded)
        digest.update(bytes.fromhex(file_hash))
    return digest.hexdigest()


def _write_digest(path: Path, digest: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.bosun-sync-{uuid.uuid4().hex}.tmp")
    try:
        temporary.write_text(digest + "\n", encoding="ascii")
        os.replace(temporary, path)
    finally:
        if temporary.exists():
            temporary.unlink()


def sync_repository(repo_root: Path, *, check: bool = False) -> SyncResult:
    root = repo_root.expanduser().resolve(strict=True)
    firmware = root / "firmware"
    resources = root / "editor" / "src-tauri" / "resources"
    if not firmware.is_dir() or _is_link(firmware):
        raise SyncError(f"firmware tree is missing or unsafe: {firmware}")
    if not (firmware / "lib").is_dir():
        raise SyncError(f"firmware/lib is missing: {firmware / 'lib'}")
    if not resources.exists():
        if check:
            raise SyncError(f"resource root is missing: {resources}")
        resources.mkdir(parents=True)
    if not resources.is_dir() or _is_link(resources):
        raise SyncError(f"resource root is not a safe directory: {resources}")

    changed = _replace_tree(firmware, resources / "firmware", check=check)
    changed = _sync_additive_lib(firmware / "lib", resources / "lib", check=check) or changed

    # Read the trees again after all writes.  This is both a copy-integrity
    # check and a guard against a source changing in the middle of a build.
    if _snapshot(firmware) != _snapshot(resources / "firmware"):
        raise SyncError("resources/firmware failed post-sync verification")
    for entry in os.scandir(firmware / "lib"):
        if _ignored(entry.name, directory=entry.is_dir(follow_symlinks=False)):
            continue
        source_entry = firmware / "lib" / entry.name
        resource_entry = resources / "lib" / entry.name
        if entry.is_dir(follow_symlinks=False):
            if _snapshot(source_entry) != _snapshot(resource_entry):
                raise SyncError(f"resources/lib/{entry.name} failed post-sync verification")
        elif _sha256(source_entry) != _sha256(resource_entry):
            raise SyncError(f"resources/lib/{entry.name} failed post-sync verification")

    return SyncResult(changed=changed, digest=_resource_digest(resources))


def _parse_args(argv: list[str] | None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--repo-root",
        type=Path,
        default=Path(__file__).resolve().parent.parent,
        help="repository root (default: parent of tools/)",
    )
    parser.add_argument(
        "--check",
        action="store_true",
        help="verify only; fail instead of changing stale resources",
    )
    parser.add_argument(
        "--digest-file",
        type=Path,
        help="write the verified complete-resource SHA-256 digest here",
    )
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = _parse_args(argv)
    try:
        result = sync_repository(args.repo_root, check=args.check)
        if args.digest_file:
            _write_digest(args.digest_file, result.digest)
    except (OSError, SyncError) as exc:
        print(f"resource sync failed: {exc}", file=sys.stderr)
        return 1

    state = "updated and verified" if result.changed else "already current"
    print(f"[ok  ] firmware resources {state}")
    print(f"RESOURCE_DIGEST={result.digest}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
