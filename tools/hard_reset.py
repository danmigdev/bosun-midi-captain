#!/usr/bin/env python3
"""Send a hard reset to a CircuitPython device via its REPL."""
import argparse
import sys
import time

import serial


def hard_reset(port: str) -> None:
    ser = serial.Serial(port, 115200, timeout=2)
    try:
        time.sleep(0.3)
        # Interrupt anything running so we land at the REPL prompt.
        ser.write(b"\x03\x03")
        time.sleep(0.5)
        while ser.in_waiting:
            _ = ser.read(ser.in_waiting)
            time.sleep(0.1)
        # The REPL occasionally drops the first byte after an interrupt;
        # send a bare newline to absorb that loss.
        ser.write(b"\r\n")
        time.sleep(0.3)
        while ser.in_waiting:
            _ = ser.read(ser.in_waiting)
            time.sleep(0.05)
        # microcontroller.reset() does a hard reset that re-runs boot.py.
        ser.write(b"import microcontroller\r\n")
        time.sleep(0.2)
        ser.write(b"microcontroller.reset()\r\n")
        time.sleep(0.5)
    finally:
        try:
            ser.close()
        except Exception:
            pass


def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--port", required=True)
    args = p.parse_args()
    hard_reset(args.port)
    print(f"hard reset sent to {args.port}")


if __name__ == "__main__":
    main()
