#!/usr/bin/env python3
"""Tiny diagnostic: GET_PATCH and print the name field."""
import argparse, json, sys, time
import serial


def main():
    p = argparse.ArgumentParser()
    p.add_argument("--port", required=True)
    p.add_argument("--bank", type=int, default=1)
    p.add_argument("--slot", type=int, default=1)
    args = p.parse_args()

    ser = serial.Serial(args.port, 115200, timeout=0.2)
    time.sleep(0.5)
    ser.write(json.dumps({"type": "GET_PATCH", "id": "g",
                          "bank": args.bank, "slot": args.slot}).encode() + b"\n")
    deadline = time.monotonic() + 3
    buf = bytearray()
    while time.monotonic() < deadline:
        chunk = ser.read(4096)
        if chunk:
            buf.extend(chunk)
            while b"\n" in buf:
                first, _, rest = bytes(buf).partition(b"\n")
                buf = bytearray(rest)
                if not first.strip(): continue
                try:
                    obj = json.loads(first)
                except json.JSONDecodeError:
                    continue
                if obj.get("id") == "g":
                    if obj.get("type") == "PATCH":
                        print(f"name: {obj['patch'].get('name')}")
                        print(f"bindings: {len(obj['patch'].get('bindings', []))}")
                    else:
                        print(f"unexpected: {obj}")
                    ser.close()
                    return
        time.sleep(0.02)
    print("TIMEOUT", file=sys.stderr)
    ser.close()
    sys.exit(1)


if __name__ == "__main__":
    main()
