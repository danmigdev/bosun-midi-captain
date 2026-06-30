#!/usr/bin/env python3
"""Quick recovery tool: write a file on the device via the REPL.

Used when the firmware is dead and OTA isn't available - sends a Python
snippet over the REPL that opens the target path and writes bytes."""
import argparse
import base64
import sys
import time
from pathlib import Path

import serial


def main():
    p = argparse.ArgumentParser()
    p.add_argument("--port", required=True)
    p.add_argument("--src", required=True, help="local file to push")
    p.add_argument("--dst", required=True, help="absolute path on device, e.g. /config/patches/01/01.json")
    args = p.parse_args()

    data = Path(args.src).read_bytes()
    b64 = base64.b64encode(data).decode()

    ser = serial.Serial(args.port, 115200, timeout=0.3)
    try:
        time.sleep(0.3)
        ser.write(b"\x03\x03")
        time.sleep(0.5)
        while ser.in_waiting:
            ser.read(ser.in_waiting); time.sleep(0.05)
        # Absorb the first-byte-drop.
        ser.write(b"\r\n")
        time.sleep(0.3)
        while ser.in_waiting:
            ser.read(ser.in_waiting); time.sleep(0.05)

        # Send the write in one paste-mode block.
        ser.write(b"\x05")                     # Ctrl-E enters paste mode
        time.sleep(0.3)
        script = (
            f"import binascii\n"
            f"with open({args.dst!r}, 'wb') as f:\n"
            f"    f.write(binascii.a2b_base64({b64!r}))\n"
            f"print('wrote', {args.dst!r})\n"
        )
        ser.write(script.encode())
        time.sleep(0.2)
        ser.write(b"\x04")                     # Ctrl-D ends paste mode
        time.sleep(1.5)

        captured = bytearray()
        deadline = time.monotonic() + 2.5
        while time.monotonic() < deadline:
            chunk = ser.read(4096)
            if chunk: captured.extend(chunk)
            time.sleep(0.05)
        text = captured.decode("utf-8", errors="replace").encode("ascii", errors="replace").decode("ascii")
        sys.stdout.write(text); sys.stdout.write("\n")
    finally:
        ser.close()


if __name__ == "__main__":
    main()
