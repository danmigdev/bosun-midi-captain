#!/usr/bin/env python3
"""Drop into the CircuitPython REPL, send Ctrl-C to surface any traceback,
print whatever the device prints over the next few seconds."""
import argparse
import math
import sys
import time

import serial


WRITE_TIMEOUT_S = 2.0


def finite_nonnegative(value):
    seconds = float(value)
    if not math.isfinite(seconds) or seconds < 0:
        raise argparse.ArgumentTypeError("seconds must be finite and non-negative")
    return seconds


def write_all(port, payload):
    view = memoryview(payload)
    while view:
        written = port.write(view)
        if not isinstance(written, int) or written <= 0:
            raise RuntimeError("serial write made no progress")
        view = view[written:]


def resume_code(port):
    """Best-effort recovery if an error occurred after Ctrl-C."""
    try:
        write_all(port, b"\x03\x03\x02")
        time.sleep(0.2)
        write_all(port, b"\x04")
    except (OSError, RuntimeError, serial.SerialException):
        # A reset can immediately disconnect the USB CDC console.
        pass


def main():
    p = argparse.ArgumentParser()
    p.add_argument("--port", required=True)
    p.add_argument("--seconds", type=finite_nonnegative, default=3.0)
    args = p.parse_args()

    ser = serial.Serial(
        args.port, 115200, timeout=0.2, write_timeout=WRITE_TIMEOUT_S,
    )
    interrupted = False
    resumed = False
    try:
        time.sleep(0.2)
        # Soft-reset (Ctrl-D) to re-run boot.py+code.py so we see fresh output.
        write_all(ser, b"\x03\x03")
        interrupted = True
        time.sleep(0.4)
        write_all(ser, b"\x04")    # Ctrl-D = soft reset
        resumed = True
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
        if interrupted and not resumed:
            resume_code(ser)
        ser.close()


if __name__ == "__main__":
    main()
