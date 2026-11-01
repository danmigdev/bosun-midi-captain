#!/usr/bin/python3 -I
"""Paste the hub's custom logo into the early boot pictures; --root supports fixture tests.

Runs as root from bosun-boot-logo.service. It decodes no image: it checks
BSPLASH1 sizes and headers and copies byte rows. Hub files belong to the
network-facing bosun account and are read as untrusted input; every write
stays inside the root-owned boot-splash directory.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
from pathlib import Path
import stat
import struct

PICTURES = ("splash.bin", "splash-45.bin", "splash-65.bin", "splash-80.bin")
SCREEN, LOGO, BOX = (640, 360), (400, 222), (120, 28)
RECORD = "record.json"
TEMPORARY = ".bosun-logo-"
ATTEMPTS = 3


def size(dimensions):
    return 16 + dimensions[0] * dimensions[1] * 3


def check(data, dimensions, name):
    if (len(data) != size(dimensions) or data[:8] != b"BSPLASH1"
            or struct.unpack("<II", data[8:16]) != dimensions):
        raise ValueError(f"{name} is not a {dimensions[0]}x{dimensions[1]} BSPLASH1 picture")


def digest(data):
    return None if data is None else hashlib.sha256(data).hexdigest()


def paste(picture, logo):
    """The picture with the logo box (icon and title) replaced row by row."""
    out = bytearray(picture)
    row = LOGO[0] * 3
    for y in range(LOGO[1]):
        start = 16 + ((BOX[1] + y) * SCREEN[0] + BOX[0]) * 3
        out[start:start + row] = logo[16 + y * row:16 + (y + 1) * row]
    return bytes(out)


def open_directory(path):
    return os.open(path, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC)


def check_owned(directory, path):
    info = os.fstat(directory)
    # geteuid() is root in the service; fixture tests run as their own user.
    if info.st_uid != os.geteuid() or info.st_mode & 0o022:
        raise ValueError(f"Refusing to write to {path}: it must be root-owned and "
                         "not writable by group or others")


def is_file(directory, name):
    try:
        return stat.S_ISREG(os.stat(name, dir_fd=directory, follow_symlinks=False).st_mode)
    except FileNotFoundError:
        return False


def read_file(directory, name, limit):
    """A regular file below a directory fd, or None when it is absent."""
    try:
        fd = os.open(name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK | os.O_CLOEXEC,
                     dir_fd=directory)
    except FileNotFoundError:
        return None
    try:
        info = os.fstat(fd)
        if not stat.S_ISREG(info.st_mode) or info.st_size > limit:
            raise ValueError(f"Refusing unexpected file {name}")
        chunks, total = [], 0
        while chunk := os.read(fd, min(1 << 16, limit + 1 - total)):
            chunks.append(chunk)
            total += len(chunk)
            if total > limit:
                raise ValueError(f"Refusing unexpected file {name}")
        return b"".join(chunks)
    finally:
        os.close(fd)


def write_file(directory, name, data):
    temporary = TEMPORARY + name
    fd = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_TRUNC | os.O_NOFOLLOW | os.O_CLOEXEC,
                 0o644, dir_fd=directory)
    try:
        try:
            os.fchmod(fd, 0o644)
            view = memoryview(data)
            while view:
                view = view[os.write(fd, view):]
            os.fsync(fd)
        finally:
            os.close(fd)
        os.rename(temporary, name, src_dir_fd=directory, dst_dir_fd=directory)
    except BaseException:
        try:
            os.unlink(temporary, dir_fd=directory)
        except FileNotFoundError:
            pass
        raise
    os.fsync(directory)


def read_logo(root):
    """The custom logo.bin, or None for the Bosun logo (both hub files must exist)."""
    try:
        hub = open_directory(root / "var/lib/bosun-hub/boot-logo")
    except FileNotFoundError:
        return None
    try:
        logo = read_file(hub, "logo.bin", size(LOGO))
        return logo if logo is not None and is_file(hub, "logo.png") else None
    finally:
        os.close(hub)


def apply(root, logo):
    path = root / "opt/bosun-hub/boot-splash"
    try:
        assets = open_directory(path)
    except FileNotFoundError:
        return "unavailable"
    defaults = None
    try:
        present = [name for name in PICTURES if is_file(assets, name)]
        if not present:
            return "unavailable"
        check_owned(assets, path)
        try:
            os.mkdir("default", 0o755, dir_fd=assets)
        except FileExistsError:
            pass
        # Pristine Bosun pictures, so "Use Bosun logo" can always restore them.
        defaults = os.open("default", os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC,
                           dir_fd=assets)
        check_owned(defaults, path / "default")
        for directory in (assets, defaults):
            for entry in os.listdir(directory):
                if entry.startswith(TEMPORARY):
                    os.unlink(entry, dir_fd=directory)
        saved = read_file(defaults, RECORD, 1 << 16)
        record = json.loads(saved) if saved is not None else {}
        if not isinstance(record, dict):
            raise ValueError("The boot logo record is invalid")
        entries, plan = {}, []
        for name in present:
            current = read_file(assets, name, size(SCREEN))
            check(current, SCREEN, name)
            entry = record.get(name) if isinstance(record.get(name), dict) else {}
            found = digest(current)
            if found == entry.get("default") or found not in (entry.get("found"), entry.get("written")):
                # Bosun's own picture: first run, installer refresh or trial commit.
                default = current
                if read_file(defaults, name, size(SCREEN)) != current:
                    write_file(defaults, name, current)
            else:
                default = read_file(defaults, name, size(SCREEN))
                if default is None or digest(default) != entry.get("default"):
                    raise ValueError(f"The saved Bosun picture for {name} is missing or changed; "
                                     "rerun install-boot-splash.sh")
            output = paste(default, logo) if logo is not None else default
            if output == current and entry.get("written") == found:
                found = entry.get("found", found)  # already recorded; no rewrite
            entries[name] = {"default": digest(default), "found": found, "written": digest(output)}
            plan.append((name, current, output))
        # Record every output before replacing a picture: after a crash at any
        # step the current pictures are still recognised, never saved as defaults.
        if entries != record:
            write_file(defaults, RECORD, json.dumps(entries, indent=2, sort_keys=True).encode())
        for name, current, output in plan:
            if output != current:
                write_file(assets, name, output)
        return "applied" if logo is not None else "default"
    finally:
        os.close(assets)
        if defaults is not None:
            os.close(defaults)


def write_status(root, status):
    directory = root / "run/bosun-boot-logo"
    directory.mkdir(parents=True, exist_ok=True, mode=0o755)
    temporary = directory / (TEMPORARY + "status.json")
    with temporary.open("w", encoding="utf-8") as out:
        json.dump(status, out)
        out.flush()
        os.fsync(out.fileno())
    temporary.chmod(0o644)
    temporary.replace(directory / "status.json")


def run(root):
    status = {"state": "error", "logo": None, "error": None}
    try:
        for _ in range(ATTEMPTS):
            logo = read_logo(root)
            status["logo"] = digest(logo)
            if logo is not None:
                check(logo, LOGO, "logo.bin")
            status["state"] = apply(root, logo)
            # Path events that arrive during a run are lost: catch up here.
            if digest(read_logo(root)) == status["logo"]:
                break
    except (OSError, ValueError) as error:
        status.update(state="error", error=str(error))
    write_status(root, status)
    return status


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, default=Path("/"))
    result = run(parser.parse_args().root)
    if result["state"] == "error":
        parser.exit(1, f"Bosun boot logo: {result['error']}\n")
