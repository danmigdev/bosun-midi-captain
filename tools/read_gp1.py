#!/usr/bin/env python3
"""Send REPL commands to read GP1 and print the result over the next 3s."""
import argparse
import sys
import time

import serial


def main():
    p = argparse.ArgumentParser()
    p.add_argument("--port", required=True)
    args = p.parse_args()

    ser = serial.Serial(args.port, 115200, timeout=0.2)
    try:
        time.sleep(0.3)
        ser.write(b"\x03\x03")
        time.sleep(0.5)
        while ser.in_waiting:
            ser.read(ser.in_waiting)
            time.sleep(0.1)
        # Send a no-op first; CircuitPython's REPL sometimes drops the first
        # byte after a fresh interrupt.
        ser.write(b"\r\n")
        time.sleep(0.3)
        while ser.in_waiting:
            ser.read(ser.in_waiting)
            time.sleep(0.1)

        ser.write(b"import board, digitalio\r\n")
        time.sleep(0.2)
        ser.write(b"_p = digitalio.DigitalInOut(board.GP1)\r\n")
        time.sleep(0.2)
        ser.write(b"_p.direction = digitalio.Direction.INPUT\r\n")
        time.sleep(0.2)
        ser.write(b"_p.pull = digitalio.Pull.UP\r\n")
        time.sleep(0.2)
        ser.write(b"import time; time.sleep(0.1); print('GP1=', _p.value)\r\n")
        time.sleep(0.3)
        ser.write(b"_p.deinit()\r\n")
        time.sleep(0.3)

        deadline = time.monotonic() + 2.0
        captured = bytearray()
        while time.monotonic() < deadline:
            chunk = ser.read(4096)
            if chunk:
                captured.extend(chunk)
            time.sleep(0.05)
        text = captured.decode("utf-8", errors="replace").encode("ascii", errors="replace").decode("ascii")
        sys.stdout.write(text)
        sys.stdout.write("\n")
    finally:
        ser.close()


if __name__ == "__main__":
    main()
