#!/usr/bin/env python3
"""Push the firmware/ tree to a running Captain over USB CDC or raw TCP.

Uses the PUT_FILE_BEGIN/CHUNK/END protocol commands. Once the firmware has OTA
support installed, you never need to enter editing mode again - just:

    python tools/push_firmware.py --port COM4
    python tools/push_firmware.py --port socket://192.168.1.91:9876

Skips __pycache__/, .pyc, .tmp, .DS_Store.
Optional --files restricts to a specific subset (paths relative to firmware/).
Optional --reboot triggers a hard reset at the end.
"""
import argparse
import base64
import json
import math
import os
import sys
import time
from pathlib import Path

import serial


# Keep the decoded allocation below 100 bytes.  The Captain normally drains
# USB MIDI in 256-byte bursts; making an OTA chunk another 256-byte allocation
# at the same time was enough to exhaust a fragmented RP2040 heap in the real
# Kemper setup (CircuitPython reported failure allocating 257 bytes).  96 raw
# bytes encode to exactly 128 base64 characters and remain fast enough over
# both direct CDC and the RPi raw-TCP bridge.
CHUNK_SIZE = 96                               # bytes binary per chunk
FILE_RETRIES = 3
WRITE_TIMEOUT_S = 2.0
_RX_BUFFER_ATTR = "_bosun_push_firmware_rx_buffer"


def open_transport(port: str, baudrate: int = 115200,
                   timeout: float = 0.1,
                   write_timeout: float = WRITE_TIMEOUT_S):
    """Open a local serial port or a pyserial URL transport.

    Keep the direct ``Serial`` path for COM/tty device names, preserving the
    behaviour existing deployment scripts rely on.  URL transports (notably
    ``socket://host:port`` exposed by bosun-hub) must go through pyserial's
    URL handler factory.
    """
    if (not isinstance(write_timeout, (int, float)) or
            not math.isfinite(write_timeout) or write_timeout <= 0):
        raise ValueError("write_timeout must be positive and finite")
    if "://" in port:
        return serial.serial_for_url(
            port, baudrate=baudrate, timeout=timeout,
            write_timeout=write_timeout,
        )
    return serial.Serial(
        port, baudrate, timeout=timeout, write_timeout=write_timeout,
    )


def _receive_buffer(ser: serial.Serial) -> bytearray:
    """Return the line-framing buffer owned by this transport.

    ``read()`` is allowed to return several lines and a prefix of the next
    line in one chunk.  The buffer therefore has to outlive an individual
    :func:`call`; otherwise returning as soon as its ACK is found discards the
    prefix and desynchronises the following request.
    """
    buf = getattr(ser, _RX_BUFFER_ATTR, None)
    if not isinstance(buf, bytearray):
        buf = bytearray()
        setattr(ser, _RX_BUFFER_ATTR, buf)
    return buf


def _write_all(ser: serial.Serial, data: bytes) -> None:
    """Write one complete JSON frame even on short serial/TCP writes."""
    view = memoryview(data)
    while view:
        written = ser.write(view)
        if not isinstance(written, int) or written <= 0:
            raise OSError("transport write made no progress")
        view = view[written:]


def _wait_for_response(ser: serial.Serial, msg: dict,
                       timeout: float) -> dict:
    """Wait for ``msg``'s response after its complete frame was written."""
    deadline = time.monotonic() + timeout
    buf = _receive_buffer(ser)
    while time.monotonic() < deadline:
        while b"\n" in buf:
            newline = buf.index(b"\n")
            first = bytes(buf[:newline]).strip()
            del buf[:newline + 1]
            if not first:
                continue
            try:
                obj = json.loads(first)
            except json.JSONDecodeError:
                continue
            if isinstance(obj, dict) and obj.get("id") == msg["id"]:
                return obj

        chunk = ser.read(4096)
        if chunk:
            buf.extend(chunk)
        time.sleep(0.01)
    raise TimeoutError(f"no response to {msg['type']}#{msg['id']}")


def call(ser: serial.Serial, msg: dict, timeout: float = 5.0) -> dict:
    line = (json.dumps(msg) + "\n").encode()
    _write_all(ser, line)
    return _wait_for_response(ser, msg, timeout)


def request_reboot(ser: serial.Serial, request_id: str = "reboot",
                   timeout: float = 1.0) -> dict | None:
    """Send a complete REBOOT frame and tolerate the resulting disconnect.

    Write errors intentionally propagate: unless :func:`_write_all` returned,
    the complete command is not known to have reached the Captain.  Once it
    did return, a timeout or transport disconnect while waiting for the ACK is
    expected because the USB device resets immediately.
    """
    msg = {"type": "REBOOT", "id": request_id}
    line = (json.dumps(msg) + "\n").encode()
    _write_all(ser, line)
    try:
        response = _wait_for_response(ser, msg, timeout)
    except (TimeoutError, OSError):
        return None
    if response.get("type") == "ERROR":
        raise RuntimeError(f"REBOOT failed: {response}")
    return response


def _next_id(seq: list[int]) -> str:
    seq[0] += 1
    return f"u{seq[0]}"


def push_file(ser: serial.Serial, src: Path, dst: str, ids: list[int]) -> None:
    data = src.read_bytes()
    # CircuitPython's json module chokes on UTF-8 BOMs that Windows tools
    # love to add. Strip it for any *.json / *.py to be safe.
    if data.startswith(b"\xef\xbb\xbf") and dst.endswith((".json", ".py")):
        data = data[3:]
    size = len(data)
    t0 = time.monotonic()

    resp = call(ser, {"type": "PUT_FILE_BEGIN", "id": _next_id(ids),
                      "path": dst, "size": size})
    if resp.get("type") != "ACK":
        raise RuntimeError(f"begin failed for {dst}: {resp}")
    if resp.get("size_check") is True and resp.get("size") != size:
        raise RuntimeError(
            f"begin size capability mismatch for {dst}: {resp}"
        )
    size_check = (resp.get("size_check") is True
                  and resp.get("size") == size)

    uncertain_offsets: list[int] = []
    for offset in range(0, size, CHUNK_SIZE):
        chunk = data[offset:offset + CHUNK_SIZE]
        b64 = base64.b64encode(chunk).decode()
        chunk_id = _next_id(ids)
        try:
            resp = call(ser, {
                "type": "PUT_FILE_CHUNK",
                "id": chunk_id,
                "path": dst,
                "data_b64": b64,
            }, timeout=5)
        except TimeoutError:
            # PUT_FILE_CHUNK is append-only in every deployed Captain
            # firmware: it has no offset and no request-id deduplication.  If
            # the firmware appended the bytes and only its ACK was lost,
            # resending this command would silently duplicate the chunk.
            # Send every append at most once and let PUT_FILE_END's exact-size
            # check decide whether an uncertain command actually arrived.  A
            # missing command produces size_mismatch and the outer transaction
            # retry safely starts again with a truncating PUT_FILE_BEGIN.
            if not size_check:
                # Legacy firmware ACKs BEGIN but ignores the `size` field and
                # installs any temporary file at END.  It cannot safely
                # resolve whether this append landed, so abort this attempt;
                # the outer retry starts with a truncating BEGIN.  Never send
                # the ambiguous individual chunk twice.
                raise RuntimeError(
                    f"chunk {offset} ACK uncertain and Captain did not "
                    "negotiate END size verification"
                )
            uncertain_offsets.append(offset)
            print(
                f"  WARN {dst}: no ACK for chunk at offset {offset}; "
                "continuing without unsafe resend",
                file=sys.stderr,
            )
            continue
        if resp.get("type") != "ACK":
            raise RuntimeError(f"chunk {offset} failed for {dst}: {resp}")

    resp = call(ser, {"type": "PUT_FILE_END", "id": _next_id(ids), "path": dst})
    if resp.get("type") != "ACK":
        raise RuntimeError(f"end failed for {dst}: {resp}")

    if uncertain_offsets:
        rendered = ", ".join(map(str, uncertain_offsets))
        print(
            f"  {dst}: device size check accepted uncertain chunk "
            f"offset(s) {rendered}",
            file=sys.stderr,
        )

    elapsed = time.monotonic() - t0
    rate = (size / 1024) / elapsed if elapsed > 0 else 0
    print(f"  {dst:<45} {size:>6} B  {elapsed*1000:>4.0f} ms  {rate:>5.1f} KB/s")


def push_file_with_retries(ser: serial.Serial, src: Path, dst: str,
                           ids: list[int], retries: int = FILE_RETRIES,
                           retry_delay: float = 0.5) -> None:
    """Upload one file, restarting only when end-to-end validation fails.

    A chunk whose response times out is deliberately *not* retried by
    :func:`push_file`; the firmware's final expected-size check resolves that
    ambiguity without risking a duplicate append.  This transaction-level
    retry remains necessary when a command itself was lost (or the firmware
    reported an explicit error).
    """
    if retries < 1:
        raise ValueError("retries must be at least 1")
    for attempt in range(1, retries + 1):
        try:
            push_file(ser, src, dst, ids)
            return
        except (TimeoutError, RuntimeError) as exc:
            if attempt == retries:
                raise
            print(
                f"  WARN {dst}: {exc}; retrying file "
                f"({attempt + 1}/{retries})",
                file=sys.stderr,
            )
            if retry_delay:
                time.sleep(retry_delay)


def collect_files(root: Path, restrict: list[str] | None,
                  include_config: bool = False) -> list[tuple[Path, str]]:
    """Collect firmware files in a dependency-safe installation order.

    Every PUT_FILE_END commits one file independently, so a multi-file OTA is
    not atomic.  Install leaf/core modules first, the Captain application
    root after them, and CircuitPython's entry point last.  This prevents the
    common partial-deploy failure where a new ``app.mpy`` imports an old core
    module before that dependency has been replaced.  A power loss can still
    leave a mixed version; rerunning the same complete file set is the
    recovery path.
    """

    def install_key(entry: tuple[Path, str]) -> tuple[int, str]:
        destination = entry[1].replace("\\", "/").lower()
        if destination in ("/lib/captain_ota.py", "/lib/captain_ota.mpy"):
            priority = 0
        elif destination in ("/code.py", "/code.mpy"):
            priority = 3
        elif destination in (
            "/lib/captain/app.py", "/lib/captain/app.mpy",
        ):
            priority = 2
        else:
            priority = 1
        return priority, destination

    out: list[tuple[Path, str]] = []
    if restrict:
        missing = [rel for rel in restrict if not (root / rel).is_file()]
        if missing:
            rendered = ", ".join(missing)
            raise FileNotFoundError(f"requested firmware file not found: {rendered}")
        for rel in restrict:
            src = root / rel
            out.append((src, "/" + rel.replace("\\", "/")))
        # Python's sort is stable: retain the caller's order among files in
        # the same dependency tier while still moving roots to the end.
        return sorted(out, key=lambda entry: install_key(entry)[0])

    for dirpath, _, files in os.walk(root):
        if "__pycache__" in dirpath:
            continue
        rel_dir = Path(dirpath).relative_to(root).as_posix()
        # Skip config/ by default - it's user-editable state (patches,
        # device settings, midi_learn) stored on the device's flash and
        # owned by the editor, not by the source tree. Pushing it would
        # clobber what the user has saved. Use --include-config for
        # fresh-install / factory-reset.
        if not include_config and (rel_dir == "config" or rel_dir.startswith("config/")):
            continue
        for fname in files:
            if fname.endswith((".pyc", ".tmp", ".DS_Store")):
                continue
            src = Path(dirpath) / fname
            # CircuitPython prefers source when both forms exist. Large modules
            # are precompiled specifically because they exceed the device heap.
            if fname.endswith(".py") and src.with_suffix(".mpy").is_file():
                continue
            rel = src.relative_to(root).as_posix()
            out.append((src, "/" + rel))
    return sorted(out, key=install_key)


def main():
    p = argparse.ArgumentParser(description=__doc__,
                                formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument(
        "--port", required=True,
        help="Data CDC port or pyserial URL (e.g. COM4 or "
             "socket://192.168.1.91:9876)",
    )
    p.add_argument("--firmware",
                   default=str(Path(__file__).resolve().parent.parent / "firmware"),
                   help="firmware/ directory (default: ../firmware)")
    p.add_argument("--files", nargs="*", default=None,
                   help="optional subset, paths relative to firmware/")
    p.add_argument("--reboot", action="store_true",
                   help="send REBOOT after the last file")
    p.add_argument("--no-reboot", dest="reboot", action="store_false",
                   help="skip the reboot (default: reboot)")
    p.add_argument("--include-config", action="store_true",
                   help="also push firmware/config/* (overwrites user patches "
                        "and settings - use only for fresh install)")
    p.set_defaults(reboot=True)
    args = p.parse_args()

    firmware = Path(args.firmware).resolve()
    if not firmware.is_dir():
        sys.exit(f"firmware tree not found: {firmware}")

    try:
        files = collect_files(firmware, args.files,
                              include_config=args.include_config)
    except FileNotFoundError as exc:
        sys.exit(str(exc))
    if not files:
        sys.exit("no files to push")

    ser = open_transport(args.port)
    try:
        time.sleep(0.5)

        # Sanity ping
        try:
            resp = call(ser, {"type": "PING", "id": "ping"}, timeout=2)
            if resp.get("type") != "ACK":
                sys.exit(f"firmware did not ACK PING: {resp}")
        except TimeoutError:
            sys.exit("firmware not responding to PING - wrong port?")

        print(f"# Pushing {len(files)} files to {args.port}")
        ids = [0]
        total_bytes = 0
        t_start = time.monotonic()
        for src, dst in files:
            push_file_with_retries(ser, src, dst, ids)
            total_bytes += src.stat().st_size
        wall = time.monotonic() - t_start
        print(f"# Done: {total_bytes/1024:.1f} KB in {wall:.1f}s "
              f"({total_bytes/1024/wall:.1f} KB/s)")

        if args.reboot:
            print("# Rebooting...")
            request_reboot(ser)
    finally:
        ser.close()


if __name__ == "__main__":
    main()
