#!/usr/bin/env python3
"""End-to-end autosave check.

1. Reads the current seed patch via GET_PATCH.
2. Changes its name to "soak-{timestamp}" via PUT_PATCH.
3. SAVE_NOW to flush.
4. Hard-reset the device via REPL on the console port.
5. After it comes back, GET_PATCH again; assert the name change survived.
"""
import argparse
import json
import sys
import time

import serial


def reader_step(ser, buf):
    chunk = ser.read(4096)
    if chunk:
        buf.extend(chunk)
    return buf


def call(ser, msg, timeout=2.0):
    """Send a command and wait for a response matching its id."""
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
        time.sleep(0.02)
    raise TimeoutError(f"no response to {msg['type']}")


def hard_reset(repl_port):
    ser = serial.Serial(repl_port, 115200, timeout=0.2)
    try:
        time.sleep(0.3)
        ser.write(b"\x03\x03")
        time.sleep(0.5)
        while ser.in_waiting:
            ser.read(ser.in_waiting); time.sleep(0.05)
        ser.write(b"\r\n"); time.sleep(0.3)
        while ser.in_waiting:
            ser.read(ser.in_waiting); time.sleep(0.05)
        ser.write(b"import microcontroller\r\n"); time.sleep(0.2)
        ser.write(b"microcontroller.reset()\r\n"); time.sleep(0.3)
    finally:
        try: ser.close()
        except Exception: pass


def wait_for_port(port, timeout=15):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        try:
            s = serial.Serial(port, 115200, timeout=0.2)
            s.close()
            return True
        except serial.SerialException:
            time.sleep(0.3)
    return False


def main():
    p = argparse.ArgumentParser()
    p.add_argument("--data-port", required=True)
    p.add_argument("--repl-port", required=True)
    args = p.parse_args()

    new_name = f"autosave-{int(time.time())}"
    print(f"== Phase 1: change patch name to '{new_name}' ==")
    ser = serial.Serial(args.data_port, 115200, timeout=0.2)
    time.sleep(0.5)

    # Enable autosave at runtime (was disabled by default in device.json)
    resp = call(ser, {"type": "GET_GLOBAL", "id": "g1"})
    device = resp["device"]
    device["autosave"] = {"enabled": True, "debounce_ms": 500}
    call(ser, {"type": "PUT_GLOBAL", "id": "g2", "device": device})
    print("  autosave enabled at runtime")

    resp = call(ser, {"type": "GET_PATCH", "id": "p1", "bank": 1, "slot": 1})
    patch = resp["patch"]
    original_name = patch.get("name")
    print(f"  original name: '{original_name}'")

    patch["name"] = new_name
    call(ser, {"type": "PUT_PATCH", "id": "p2", "bank": 1, "slot": 1, "patch": patch})
    print("  PUT_PATCH sent")

    resp = call(ser, {"type": "SAVE_NOW", "id": "p3"})
    saved = resp.get("patches", [])
    print(f"  SAVE_NOW result: {saved}")
    if not saved:
        print("  FAIL: SAVE_NOW reported nothing saved")
        sys.exit(1)

    ser.close()

    print("== Phase 2: hard reset ==")
    hard_reset(args.repl_port)
    time.sleep(5)
    print("  waiting for data port to come back...")
    if not wait_for_port(args.data_port, timeout=15):
        print("  FAIL: data port never came back")
        sys.exit(1)
    time.sleep(2)

    print("== Phase 3: verify change survived ==")
    ser = serial.Serial(args.data_port, 115200, timeout=0.2)
    time.sleep(0.5)
    resp = call(ser, {"type": "GET_PATCH", "id": "p4", "bank": 1, "slot": 1})
    after_name = resp["patch"].get("name")
    print(f"  name after reboot: '{after_name}'")
    ser.close()

    if after_name == new_name:
        print("PASS: change persisted across hard reset")
        sys.exit(0)
    else:
        print(f"FAIL: expected '{new_name}', got '{after_name}'")
        sys.exit(1)


if __name__ == "__main__":
    main()
