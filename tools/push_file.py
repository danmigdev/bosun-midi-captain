"""Push a single source file to the captain via PUT_FILE_BEGIN /
_CHUNK / _END and reboot it. Used to OTA a firmware tweak without
re-flashing the whole CircuitPython filesystem.

Usage:
    python tools/push_file.py <local-path> <firmware-path> [COMx]

Example:
    python tools/push_file.py \\
        editor/src-tauri/resources/firmware/lib/plugins/kemper.py \\
        /lib/plugins/kemper.py
"""

import base64
import json
import sys
import time

import serial

CHUNK = 1024


def send_line(s, obj):
    line = json.dumps(obj, separators=(",", ":")) + "\n"
    s.write(line.encode("utf-8"))
    s.flush()


def read_line(s, timeout_s=10.0):
    end = time.monotonic() + timeout_s
    buf = bytearray()
    while time.monotonic() < end:
        b = s.read(1)
        if not b:
            continue
        if b == b"\n":
            return buf.decode("utf-8", errors="replace")
        if b == b"\r":
            continue
        buf.extend(b)
    raise TimeoutError(f"no response within {timeout_s}s")


def await_ack(s, expected_id, timeout_s=10.0):
    end = time.monotonic() + timeout_s
    while time.monotonic() < end:
        remaining = max(0.5, end - time.monotonic())
        line = read_line(s, remaining).strip()
        if not line:
            continue
        try:
            msg = json.loads(line)
        except json.JSONDecodeError:
            continue
        if msg.get("id") != expected_id:
            continue
        if msg.get("type") == "ERROR":
            raise RuntimeError(f"firmware error: {msg}")
        if msg.get("type") == "ACK":
            return msg
    raise TimeoutError(f"id={expected_id} never came back within {timeout_s}s")


def main():
    if len(sys.argv) < 3:
        print(__doc__, file=sys.stderr)
        sys.exit(2)
    local_path = sys.argv[1]
    firmware_path = sys.argv[2]
    port = sys.argv[3] if len(sys.argv) > 3 else "COM4"

    with open(local_path, "rb") as f:
        data = f.read()
    print(f"opening {port}, pushing {len(data)} bytes to {firmware_path}")

    s = serial.Serial(port, baudrate=115200, timeout=2.0)
    try:
        time.sleep(0.3)
        s.reset_input_buffer()

        send_line(s, {"type": "PUT_FILE_BEGIN", "id": "b1", "path": firmware_path})
        await_ack(s, "b1")
        print("BEGIN ok")

        n_chunks = (len(data) + CHUNK - 1) // CHUNK
        for i in range(n_chunks):
            slice_ = data[i * CHUNK:(i + 1) * CHUNK]
            b64 = base64.b64encode(slice_).decode("ascii")
            send_line(s, {"type": "PUT_FILE_CHUNK", "id": f"c{i}", "path": firmware_path, "data_b64": b64})
            await_ack(s, f"c{i}")
            print(f"  chunk {i+1}/{n_chunks} ({len(slice_)} bytes) ok")

        send_line(s, {"type": "PUT_FILE_END", "id": "e1", "path": firmware_path})
        await_ack(s, "e1")
        print("END ok")

        # Reboot so the new module takes effect.
        send_line(s, {"type": "REBOOT", "id": "r1"})
        # Don't wait for ACK - REBOOT severs the link immediately.
        print("REBOOT sent. Wait ~5s before reconnecting.")
    finally:
        s.close()


if __name__ == "__main__":
    main()
