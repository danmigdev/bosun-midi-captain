#!/usr/bin/env python3
"""Inspect, and optionally remove, exact CircuitPython staging files.

The Captain must be quiesced before this tool runs: stop every process that
owns either CDC interface. Removal is deliberately limited to absolute,
canonical ``.tmp``/``.recovery`` paths. The tool probes first, refuses
directories, disables auto-reload, verifies absence after removal, and then
soft-reboots back into ``code.py``.
"""

from __future__ import annotations

import argparse
import re
import time
from pathlib import PurePosixPath

import serial


_SAFE_PATH = re.compile(r"^/[A-Za-z0-9._/-]+$")
_SAFE_SUFFIXES = (".tmp", ".recovery")
_DIRECTORY_MODE = 0x4000


def validate_temp_path(value: str) -> str:
    """Return a canonical temporary path or fail closed."""
    if not isinstance(value, str) or not _SAFE_PATH.fullmatch(value):
        raise ValueError("path contains unsupported characters")
    path = PurePosixPath(value)
    if str(path) != value or value == "/" or ".." in path.parts:
        raise ValueError("path must be canonical and absolute")
    if not value.endswith(_SAFE_SUFFIXES):
        raise ValueError("only .tmp or .recovery staging files are allowed")
    return value


def _probe_source(paths: tuple[str, ...]) -> str:
    return (
        "import os,supervisor,microcontroller\n"
        "supervisor.runtime.autoreload=False\n"
        "microcontroller.watchdog.feed()\n"
        "for p in %r:\n"
        " try:\n"
        "  s=os.stat(p)\n"
        "  print('__BOSUN_FILE__|{}|{}|{}'.format(p,s[6],s[0]))\n"
        " except OSError:\n"
        "  print('__BOSUN_MISSING__|{}'.format(p))\n"
        " microcontroller.watchdog.feed()"
    ) % (paths,)


def _remove_source(paths: tuple[str, ...]) -> str:
    return (
        "import os,microcontroller\n"
        "for p in %r:\n"
        " microcontroller.watchdog.feed()\n"
        " os.remove(p)\n"
        " print('__BOSUN_REMOVED__|{}'.format(p))"
    ) % (paths,)


def parse_probe_reply(
    reply: bytes, expected: tuple[str, ...]
) -> dict[str, tuple[int, int] | None]:
    """Parse raw-REPL markers, requiring one unambiguous result per path."""
    text = reply.decode("utf-8", "replace")
    found: dict[str, tuple[int, int] | None] = {}
    for line in text.replace("\r", "\n").split("\n"):
        marker = line.find("__BOSUN_")
        if marker < 0:
            continue
        fields = line[marker:].split("|")
        if fields[0] == "__BOSUN_MISSING__" and len(fields) >= 2:
            path = fields[1]
            value = None
        elif fields[0] == "__BOSUN_FILE__" and len(fields) >= 4:
            path = fields[1]
            try:
                value = (int(fields[2]), int(fields[3]))
            except ValueError as exc:
                raise RuntimeError("invalid stat marker from Captain") from exc
        else:
            continue
        if path in found:
            raise RuntimeError("duplicate probe result for %s" % path)
        found[path] = value
    missing = set(expected) - set(found)
    unexpected = set(found) - set(expected)
    if missing or unexpected:
        raise RuntimeError(
            "incomplete probe (missing=%r unexpected=%r)" %
            (sorted(missing), sorted(unexpected))
        )
    return found


def removable_files(
    states: dict[str, tuple[int, int] | None]
) -> tuple[str, ...]:
    result = []
    for path, state in states.items():
        if state is None:
            continue
        _size, mode = state
        if mode & _DIRECTORY_MODE:
            raise RuntimeError("refusing to remove directory %s" % path)
        result.append(path)
    return tuple(result)


def _write_all(port, payload: bytes) -> None:
    view = memoryview(payload)
    while view:
        written = port.write(view)
        if not written:
            raise RuntimeError("serial write made no progress")
        view = view[written:]


def _read_until(port, suffix: bytes, timeout: float = 12.0) -> bytes:
    result = bytearray()
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        result.extend(port.read(1024))
        if result.endswith(suffix):
            return bytes(result)
    raise RuntimeError("timeout waiting for raw REPL: %r" % bytes(result[-300:]))


def _execute(port, source: str) -> bytes:
    _write_all(port, source.encode("utf-8"))
    _write_all(port, b"\x04")
    reply = _read_until(port, b">")
    if not reply.startswith(b"OK") or b"Traceback" in reply:
        raise RuntimeError(reply.decode("utf-8", "replace"))
    return reply


def inspect_or_remove(
    port_name: str, paths: tuple[str, ...], remove: bool
) -> None:
    with serial.Serial(port_name, 115200, timeout=0.1, write_timeout=3) as port:
        interrupted = False
        try:
            _write_all(port, b"\x03\x03")
            interrupted = True
            time.sleep(0.5)
            _write_all(port, b"\r\x01")
            _read_until(port, b">")

            before = parse_probe_reply(_execute(port, _probe_source(paths)), paths)
            for path, state in before.items():
                if state is None:
                    print("MISSING", path)
                else:
                    print("FOUND", path, state[0], "bytes")

            targets = removable_files(before)
            if remove and targets:
                reply = _execute(port, _remove_source(targets))
                for path in targets:
                    marker = ("__BOSUN_REMOVED__|" + path).encode("utf-8")
                    if marker not in reply:
                        raise RuntimeError(
                            "Captain did not confirm removal of %s" % path
                        )
                after = parse_probe_reply(
                    _execute(port, _probe_source(paths)), paths
                )
                remaining = [
                    path for path, state in after.items() if state is not None
                ]
                if remaining:
                    raise RuntimeError("staging files remain: %r" % remaining)
                for path in targets:
                    print("REMOVED", path)
        finally:
            if interrupted:
                # Explicitly return to friendly mode, then restart code.py.
                try:
                    _write_all(port, b"\r\x02")
                    time.sleep(0.2)
                    _write_all(port, b"\x04")
                except (OSError, RuntimeError, serial.SerialException):
                    pass


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--port", required=True)
    parser.add_argument("--path", action="append", required=True)
    parser.add_argument(
        "--remove", action="store_true",
        help="remove found files after the read-only probe (default: inspect only)",
    )
    args = parser.parse_args()
    try:
        paths = tuple(
            dict.fromkeys(validate_temp_path(path) for path in args.path)
        )
    except ValueError as exc:
        parser.error(str(exc))
    inspect_or_remove(args.port, paths, args.remove)


if __name__ == "__main__":
    main()
