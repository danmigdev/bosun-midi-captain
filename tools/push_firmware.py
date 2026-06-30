#!/usr/bin/env python3
"""Push the firmware/ tree to a running Captain over USB CDC.

Uses the PUT_FILE_BEGIN/CHUNK/END protocol commands. Once the firmware has OTA
support installed, you never need to enter editing mode again - just:

    python tools/push_firmware.py --port COM4

Skips __pycache__/, .pyc, .tmp, .DS_Store.
Optional --files restricts to a specific subset (paths relative to firmware/).
Optional --reboot triggers a hard reset at the end.
"""
import argparse
import base64
import json
import os
import sys
import time
from pathlib import Path

import serial


CHUNK_SIZE = 256                              # bytes binary per chunk


def call(ser: serial.Serial, msg: dict, timeout: float = 5.0) -> dict:
    line = (json.dumps(msg) + "\n").encode()
    ser.write(line)
    deadline = time.monotonic() + timeout
    buf = bytearray()
    while time.monotonic() < deadline:
        chunk = ser.read(4096)
        if chunk:
            buf.extend(chunk)
            while b"\n" in buf:
                first, _, rest = bytes(buf).partition(b"\n")
                buf = bytearray(rest)
                first = first.strip()
                if not first:
                    continue
                try:
                    obj = json.loads(first)
                except json.JSONDecodeError:
                    continue
                if obj.get("id") == msg["id"]:
                    return obj
        time.sleep(0.01)
    raise TimeoutError(f"no response to {msg['type']}#{msg['id']}")


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

    resp = call(ser, {"type": "PUT_FILE_BEGIN", "id": _next_id(ids), "path": dst})
    if resp.get("type") != "ACK":
        raise RuntimeError(f"begin failed for {dst}: {resp}")

    sent = 0
    for offset in range(0, size, CHUNK_SIZE):
        chunk = data[offset:offset + CHUNK_SIZE]
        b64 = base64.b64encode(chunk).decode()
        resp = call(ser, {
            "type": "PUT_FILE_CHUNK",
            "id": _next_id(ids),
            "path": dst,
            "data_b64": b64,
        }, timeout=5)
        if resp.get("type") != "ACK":
            raise RuntimeError(f"chunk {offset} failed for {dst}: {resp}")
        sent += len(chunk)

    resp = call(ser, {"type": "PUT_FILE_END", "id": _next_id(ids), "path": dst})
    if resp.get("type") != "ACK":
        raise RuntimeError(f"end failed for {dst}: {resp}")

    elapsed = time.monotonic() - t0
    rate = (size / 1024) / elapsed if elapsed > 0 else 0
    print(f"  {dst:<45} {size:>6} B  {elapsed*1000:>4.0f} ms  {rate:>5.1f} KB/s")


def collect_files(root: Path, restrict: list[str] | None,
                  include_config: bool = False) -> list[tuple[Path, str]]:
    out: list[tuple[Path, str]] = []
    if restrict:
        for rel in restrict:
            src = root / rel
            if not src.is_file():
                print(f"WARN missing: {rel}", file=sys.stderr)
                continue
            out.append((src, "/" + rel.replace("\\", "/")))
        return out

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
            rel = src.relative_to(root).as_posix()
            out.append((src, "/" + rel))
    return sorted(out, key=lambda e: e[1])


def main():
    p = argparse.ArgumentParser(description=__doc__,
                                formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--port", required=True, help="Data CDC COM port (e.g. COM4)")
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

    files = collect_files(firmware, args.files, include_config=args.include_config)
    if not files:
        sys.exit("no files to push")

    ser = serial.Serial(args.port, 115200, timeout=0.1)
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
        push_file(ser, src, dst, ids)
        total_bytes += src.stat().st_size
    wall = time.monotonic() - t_start
    print(f"# Done: {total_bytes/1024:.1f} KB in {wall:.1f}s "
          f"({total_bytes/1024/wall:.1f} KB/s)")

    if args.reboot:
        print("# Rebooting...")
        try:
            call(ser, {"type": "REBOOT", "id": "reboot"}, timeout=1)
        except TimeoutError:
            pass  # firmware vanishes during reset - expected
    ser.close()


if __name__ == "__main__":
    main()
