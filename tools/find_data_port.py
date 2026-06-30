#!/usr/bin/env python3
"""Find which COM port hosts the Captain JSON protocol.

Probes each candidate with a PING and checks whether the response is a valid
JSON ACK with the matching id. The data port responds; the REPL console does
not (it echoes the bytes but doesn't produce valid JSON)."""

import argparse
import json
import sys
import time

import serial


def probe(port: str, timeout: float = 1.5) -> dict | None:
    try:
        ser = serial.Serial(port, 115200, timeout=0.2)
    except serial.SerialException as e:
        return {"port": port, "ok": False, "err": str(e)}
    try:
        time.sleep(0.3)
        # Drain any stale bytes
        while ser.in_waiting:
            ser.read(ser.in_waiting)
        ser.write(b'{"type":"PING","id":"probe"}\n')
        buf = bytearray()
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            chunk = ser.read(256)
            if chunk:
                buf.extend(chunk)
                if b"\n" in buf:
                    for line in bytes(buf).split(b"\n"):
                        line = line.strip()
                        if not line:
                            continue
                        try:
                            obj = json.loads(line)
                        except json.JSONDecodeError:
                            continue
                        if obj.get("id") == "probe" and obj.get("type") == "ACK":
                            return {"port": port, "ok": True, "fw": obj.get("fw")}
            time.sleep(0.05)
        return {"port": port, "ok": False, "err": "no ACK"}
    finally:
        ser.close()


def main():
    p = argparse.ArgumentParser()
    p.add_argument("ports", nargs="+")
    p.add_argument("--timeout", type=float, default=1.5)
    args = p.parse_args()

    for port in args.ports:
        result = probe(port, args.timeout)
        if result["ok"]:
            print(f"DATA PORT: {port}  fw={result.get('fw')}")
            sys.exit(0)
        else:
            print(f"  {port}: {result.get('err')}")
    print("No data port responded to PING.")
    sys.exit(1)


if __name__ == "__main__":
    main()
