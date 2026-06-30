#!/usr/bin/env python3
"""Drop into the CircuitPython REPL, send Ctrl-C to surface any traceback,
print whatever the device prints over the next few seconds."""
import argparse
import sys
import time

import serial


def main():
    p = argparse.ArgumentParser()
    p.add_argument("--port", required=True)
    p.add_argument("--seconds", type=float, default=3.0)
    args = p.parse_args()

    ser = serial.Serial(args.port, 115200, timeout=0.2)
    try:
        time.sleep(0.2)
        # Soft-reset (Ctrl-D) to re-run boot.py+code.py so we see fresh output.
        ser.write(b"\x03\x03")
        time.sleep(0.4)
        ser.write(b"\x04")        # Ctrl-D = soft reset
        time.sleep(0.4)

        deadline = time.monotonic() + args.seconds
        captured = bytearray()
        while time.monotonic() < deadline:
            chunk = ser.read(4096)
            if chunk:
                captured.extend(chunk)
            time.sleep(0.05)
        text = captured.decode("utf-8", errors="replace")
        # Strip out non-ASCII so Windows cp1252 console doesn't choke
        text = text.encode("ascii", errors="replace").decode("ascii")
        sys.stdout.write(text)
        sys.stdout.write("\n")
    finally:
        ser.close()


if __name__ == "__main__":
    main()
