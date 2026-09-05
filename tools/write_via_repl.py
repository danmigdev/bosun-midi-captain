#!/usr/bin/env python3
"""Fail-closed recovery writer for a CircuitPython console.

This is the emergency path for a Captain whose normal OTA protocol no longer
starts. Normal deployments belong in ``push_firmware.py``. The writer uses
raw-REPL framing, stages and verifies the complete file, preserves the prior
live file until the replacement is verified, and always attempts to resume
``code.py``.
"""

import argparse
import base64
import binascii
import hashlib
import time
from pathlib import Path, PurePosixPath

import serial


CHUNK_SIZE = 64
READ_CHUNK_SIZE = 64
READ_TIMEOUT_S = 12.0
WRITE_TIMEOUT_S = 3.0
MAX_REPLY_BYTES = 64 * 1024


def validate_destination(destination):
    """Require one unambiguous absolute file path on CIRCUITPY."""
    if not isinstance(destination, str) or not destination:
        raise ValueError("destination must be a non-empty absolute path")
    if "\x00" in destination or "\r" in destination or "\n" in destination:
        raise ValueError("destination contains a control character")
    path = PurePosixPath(destination)
    if (not destination.startswith("/") or destination == "/" or
            str(path) != destination or path.name in ("", ".", "..") or
            ".." in path.parts):
        raise ValueError("destination must be a normalized absolute file path")
    if destination.endswith((".tmp", ".recovery")):
        raise ValueError("destination must not be a staging path")
    return destination


def read_until(port, suffix, timeout=READ_TIMEOUT_S):
    result = bytearray()
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        chunk = port.read(1024)
        if chunk:
            result.extend(chunk)
            if result.endswith(suffix):
                return bytes(result)
            if len(result) > MAX_REPLY_BYTES:
                raise RuntimeError("raw REPL reply exceeded safety limit")
        else:
            time.sleep(0.01)
    raise RuntimeError("timeout waiting for %r: %r" % (suffix, result[-300:]))


def write_all(port, payload):
    view = memoryview(payload)
    while view:
        written = port.write(view)
        if not isinstance(written, int) or written <= 0:
            raise RuntimeError("serial write made no progress")
        view = view[written:]


def execute_raw(port, code, timeout=READ_TIMEOUT_S, syntax_retries=0):
    """Run one raw-REPL command and return stdout after exact ACK framing."""
    for attempt in range(syntax_retries + 1):
        write_all(port, code.encode("utf-8"))
        write_all(port, b"\x04")
        # Raw REPL replies are ``OK + stdout + EOT + stderr + EOT + '>'``.
        # Waiting for only the final EOT/prompt also terminates promptly when
        # stderr is non-empty (where the two EOT bytes are not adjacent).
        reply = read_until(port, b"\x04>", timeout)
        valid_prefix = reply.startswith(b"OK")
        body = reply[2:-1] if valid_prefix else b""
        fields = body.split(b"\x04")
        valid_frame = len(fields) == 3 and fields[2] == b""
        stderr = fields[1] if valid_frame else b""
        if valid_prefix and valid_frame and not stderr:
            return fields[0]
        if b"SyntaxError" not in reply or attempt == syntax_retries:
            raise RuntimeError(
                "raw REPL command failed: " +
                reply[-500:].decode("utf-8", errors="replace")
            )
    raise AssertionError("unreachable")


def _remote_exists(port, path):
    parent, name = path.rsplit("/", 1)
    output = execute_raw(
        port,
        "import os\nprint('1' if %r in os.listdir(%r) else '0')" %
        (name, parent or "/"),
    )
    marker = output.strip()
    if marker == b"1":
        return True
    if marker == b"0":
        return False
    raise RuntimeError("invalid existence response for %s: %r" % (path, output))


def _remote_identity(port, path):
    """Return ``(size, sha256)`` while hashing exact readback on the host.

    CircuitPython 9.2.7 exposes ``hashlib.new`` but the MIDI Captain build has
    no SHA-256 algorithm enabled.  Asking the board to hash therefore fails
    before a recovery can verify its staging file.  Read back one bounded
    64-byte block per raw-REPL command and feed those bytes to CPython's SHA-256
    instead.  The board never allocates the complete file or digest state.
    """
    output = execute_raw(
        port,
        "import binascii,os,microcontroller\n"
        "microcontroller.watchdog.feed()\n"
        "print(os.stat(%r)[6])\n"
        "f=open(%r,'rb')" % (path, path),
    )
    try:
        size = int(output.strip())
    except ValueError as error:
        raise RuntimeError(
            "invalid identity response for %s: %r" % (path, output)
        ) from error
    if size < 0:
        raise RuntimeError("invalid negative size for %s: %d" % (path, size))

    digest = hashlib.sha256()
    offset = 0
    try:
        while offset < size:
            read_size = min(READ_CHUNK_SIZE, size - offset)
            output = execute_raw(
                port,
                "b=f.read(%d)\n"
                "microcontroller.watchdog.feed()\n"
                "print(binascii.hexlify(b).decode())" %
                read_size,
            )
            encoded = output.strip()
            try:
                chunk = binascii.unhexlify(encoded)
            except (ValueError, binascii.Error) as error:
                raise RuntimeError(
                    "invalid readback at %d for %s: %r" %
                    (offset, path, output)
                ) from error
            if len(chunk) != read_size:
                raise RuntimeError(
                    "short readback at %d for %s: expected %d, got %d" %
                    (offset, path, read_size, len(chunk))
                )
            digest.update(chunk)
            offset += len(chunk)
    except Exception:
        try:
            execute_raw(port, "f.close()\ndel f")
        except Exception:
            pass
        raise
    execute_raw(port, "f.close()\ndel f")
    return size, digest.hexdigest()


def _resume_code(port):
    """Best-effort exit from either raw/friendly REPL followed by soft reset."""
    try:
        write_all(port, b"\x03\x03\x02")
        time.sleep(0.2)
        write_all(port, b"\x04")
    except (OSError, RuntimeError, serial.SerialException):
        # A successful reset can make the USB CDC endpoint disappear at once.
        pass


def install(port, data, destination, remove_source_sibling=False):
    """Install one byte string and return only after exact verification."""
    destination = validate_destination(destination)
    if remove_source_sibling and not destination.endswith(".mpy"):
        raise ValueError("--remove-source-sibling requires a .mpy destination")

    staged = destination + ".recovery"
    backup = destination + ".backup.recovery"
    expected = (len(data), hashlib.sha256(data).hexdigest())
    source_sibling = destination[:-4] + ".py" \
        if remove_source_sibling else None
    interrupted = False

    try:
        write_all(port, b"\x03\x03")
        interrupted = True
        time.sleep(0.5)
        if hasattr(port, "reset_input_buffer"):
            port.reset_input_buffer()
        write_all(port, b"\r\x01")
        prompt = read_until(port, b">")
        if b"raw REPL" not in prompt:
            raise RuntimeError("CircuitPython raw REPL prompt not confirmed: %r" %
                               prompt[-300:])

        execute_raw(
            port,
            "import binascii,os,supervisor,microcontroller\n"
            "supervisor.runtime.autoreload=False\n"
            "microcontroller.watchdog.feed()",
        )

        # A previous backup may be the only known-good copy after an interrupted
        # recovery. Never overwrite it automatically.
        if _remote_exists(port, backup):
            raise RuntimeError(
                "recovery backup already exists; inspect it before retrying: " +
                backup
            )

        execute_raw(port, "f=open(%r,'wb')\nf.close()" % staged)
        for offset in range(0, len(data), CHUNK_SIZE):
            chunk = data[offset:offset + CHUNK_SIZE]
            encoded = base64.b64encode(chunk).decode("ascii")
            execute_raw(
                port,
                "microcontroller.watchdog.feed()\n"
                "f=open(%r,'ab')\n"
                "n=f.write(binascii.a2b_base64(%r))\n"
                "f.close()\n"
                "if n != %d: raise OSError('short staged write')" %
                (staged, encoded, len(chunk)),
                syntax_retries=4,
            )

        staged_identity = _remote_identity(port, staged)
        if staged_identity != expected:
            raise RuntimeError(
                "staged identity mismatch: expected %r, got %r" %
                (expected, staged_identity)
            )

        had_live = _remote_exists(port, destination)
        # CircuitPython/FAT cannot replace an existing name portably. Move the
        # live file aside first, but roll it back within the same device-side
        # command if installing the verified staging file fails.
        execute_raw(
            port,
            (("os.rename(%r,%r)\n" % (destination, backup)) if had_live else "") +
            "try:\n"
            " os.rename(%r,%r)\n" % (staged, destination) +
            "except Exception:\n" +
            ((" os.rename(%r,%r)\n" % (backup, destination)) if had_live else " pass\n") +
            " raise",
        )

        installed_identity = _remote_identity(port, destination)
        if installed_identity != expected:
            # A positive mismatch is safe to roll back. A transport/framing
            # failure above deliberately leaves the backup untouched instead.
            execute_raw(
                port,
                "try:\n os.remove(%r)\nexcept OSError:\n pass\n" % destination +
                (("os.rename(%r,%r)" % (backup, destination))
                 if had_live else ""),
            )
            raise RuntimeError(
                "installed identity mismatch: expected %r, got %r" %
                (expected, installed_identity)
            )

        if source_sibling is not None:
            parent, name = source_sibling.rsplit("/", 1)
            execute_raw(
                port,
                "try:\n os.remove(%r)\nexcept OSError:\n pass" % source_sibling,
            )
            output = execute_raw(
                port,
                "print('1' if %r in os.listdir(%r) else '0')" %
                (name, parent or "/"),
            )
            if output.strip() != b"0":
                raise RuntimeError("stale source remains: " + source_sibling)

        if had_live:
            execute_raw(port, "os.remove(%r)" % backup)
    finally:
        if interrupted:
            _resume_code(port)

    return expected


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--port", required=True)
    parser.add_argument("--src", required=True, help="local file to push")
    parser.add_argument(
        "--dst", required=True,
        help="normalized absolute path on CIRCUITPY, e.g. /lib/captain/app.mpy",
    )
    parser.add_argument(
        "--remove-source-sibling", action="store_true",
        help="after verifying a .mpy, remove its stale .py sibling",
    )
    args = parser.parse_args()

    try:
        destination = validate_destination(args.dst)
    except ValueError as error:
        parser.error(str(error))
    if args.remove_source_sibling and not destination.endswith(".mpy"):
        parser.error("--remove-source-sibling requires a .mpy destination")
    data = Path(args.src).read_bytes()

    with serial.Serial(
        args.port, 115200, timeout=0.1, write_timeout=WRITE_TIMEOUT_S,
    ) as port:
        size, digest = install(
            port, data, destination,
            remove_source_sibling=args.remove_source_sibling,
        )
    print("wrote", destination, size, "bytes sha256=" + digest)


if __name__ == "__main__":
    main()
