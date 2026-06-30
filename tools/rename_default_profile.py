#!/usr/bin/env python3
"""One-shot: rename /config/profiles/default -> /config/profiles/ampero_ii_stage
on the device, and update active_profile.json accordingly.

Used to migrate devices that ran the old (pre-"no default") firmware.
Run once per device. Idempotent - does nothing if 'default' is already gone.
"""
import argparse
import sys
import time

import serial


SNIPPET = """import os
src = '/config/profiles/default'
dst = '/config/profiles/ampero_ii_stage'
try:
    os.stat(src)
    have_src = True
except OSError:
    have_src = False
try:
    os.stat(dst)
    have_dst = True
except OSError:
    have_dst = False
if not have_src:
    print('no-default-folder')
elif have_dst:
    print('target-already-exists')
else:
    os.rename(src, dst)
    print('renamed')
try:
    with open('/config/active_profile.json', 'w') as f:
        f.write('{"id": "ampero_ii_stage"}')
    print('active-updated')
except OSError as e:
    print('active-update-failed:', e)
"""


def main():
    p = argparse.ArgumentParser()
    p.add_argument("--port", required=True, help="REPL port (typically COM3)")
    args = p.parse_args()

    ser = serial.Serial(args.port, 115200, timeout=0.3)
    try:
        time.sleep(0.3)
        ser.write(b"\x03\x03")            # Ctrl-C twice
        time.sleep(0.5)
        while ser.in_waiting:
            ser.read(ser.in_waiting); time.sleep(0.05)
        ser.write(b"\r\n")                # absorb first-byte-drop
        time.sleep(0.3)
        while ser.in_waiting:
            ser.read(ser.in_waiting); time.sleep(0.05)

        ser.write(b"\x05")                # Ctrl-E -> paste mode
        time.sleep(0.3)
        ser.write(SNIPPET.encode())
        time.sleep(0.2)
        ser.write(b"\x04")                # Ctrl-D -> exit paste mode + run
        time.sleep(1.0)

        captured = bytearray()
        deadline = time.monotonic() + 3.0
        while time.monotonic() < deadline:
            chunk = ser.read(4096)
            if chunk:
                captured.extend(chunk)
            time.sleep(0.05)

        # Soft-reset so the firmware boots into the renamed profile.
        ser.write(b"\x04")
        time.sleep(0.5)

        text = captured.decode("utf-8", errors="replace")
        text = text.encode("ascii", errors="replace").decode("ascii")
        sys.stdout.write(text)
        sys.stdout.write("\n")
    finally:
        ser.close()


if __name__ == "__main__":
    main()
