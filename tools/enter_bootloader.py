#!/usr/bin/env python3
"""Send a CircuitPython REPL the commands to drop into RP2 bootloader mode.

Used to avoid the dance of unplug-hold-FS1-replug. Works when CircuitPython
is already running on the device AND its REPL (primary CDC console) is enabled.
"""

import argparse
import sys
import time

import serial


def enter_bootloader(port: str) -> None:
    ser = serial.Serial(port, 115200, timeout=2)
    try:
        time.sleep(0.3)
        # Two Ctrl-C to interrupt anything currently running (code.py).
        ser.write(b"\x03\x03")
        time.sleep(0.4)
        # Drain whatever the device emitted on the interrupt.
        while ser.in_waiting:
            _ = ser.read(ser.in_waiting)
            time.sleep(0.1)
        # Absorb the first-byte-drop quirk of the REPL after an interrupt.
        ser.write(b"\r\n")
        time.sleep(0.3)
        while ser.in_waiting:
            _ = ser.read(ser.in_waiting)
            time.sleep(0.05)

        commands = [
            b"import microcontroller\r\n",
            b"microcontroller.on_next_reset(microcontroller.RunMode.BOOTLOADER)\r\n",
            b"microcontroller.reset()\r\n",
        ]
        for c in commands:
            ser.write(c)
            time.sleep(0.25)
        # Last reset() doesn't return - port will go away.
        time.sleep(0.6)
    finally:
        try:
            ser.close()
        except Exception:
            pass


def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--port", required=True)
    args = p.parse_args()
    enter_bootloader(args.port)
    print(f"reset command sent to {args.port}; RPI-RP2 drive should appear shortly")


if __name__ == "__main__":
    main()
