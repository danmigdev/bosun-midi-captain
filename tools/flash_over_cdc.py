#!/usr/bin/env python3
"""Push changed firmware files to a connected pedal over the data CDC and reboot.

Drives the line-delimited JSON protocol (captain/protocol.py): PING to find the
data port, GET_DEVICE_INFO, PUT_FILE_BEGIN/CHUNK/END per file (atomic .tmp +
rename on the device), then REBOOT (microcontroller.reset -> boot.py runs, the
data CDC comes back). Re-reads DEVICE_INFO afterwards to confirm the version.

No editor / GUI needed. Safe: a wrong path only creates a stray file, and the
live file is replaced atomically.
"""

import base64
import json
import sys
import time

import serial
import serial.tools.list_ports as list_ports

REPO = r"C:\Users\danmigdev\Desktop\bosun"
FILES = [
    (REPO + r"\firmware\lib\captain\__init__.py", "/lib/captain/__init__.py"),
    (REPO + r"\firmware\lib\captain\plugin.py",    "/lib/captain/plugin.py"),
    (REPO + r"\firmware\lib\captain\bindings.py",  "/lib/captain/bindings.py"),
    (REPO + r"\firmware\lib\plugins\kemper.py",    "/lib/plugins/kemper.py"),
]
EXPECT_VERSION = "0.3.26"
CHUNK = 1024          # raw bytes per PUT_FILE_CHUNK
VID_ADAFRUIT = 0x239A


def cdc_ports():
    return [p.device for p in list_ports.comports() if p.vid == VID_ADAFRUIT]


def send(ser, obj):
    ser.write((json.dumps(obj) + "\n").encode())
    ser.flush()


def read_reply(ser, want_id, timeout=6.0):
    """Read JSON lines until one carries want_id, skipping EVENTs / noise."""
    deadline = time.time() + timeout
    while time.time() < deadline:
        line = ser.readline()
        if not line:
            continue
        line = line.strip()
        if not line:
            continue
        try:
            msg = json.loads(line)
        except ValueError:
            continue
        if msg.get("id") == want_id:
            return msg
    return None


_next = [0]
def nid():
    _next[0] += 1
    return _next[0]


def find_data_port():
    for dev in cdc_ports():
        try:
            ser = serial.Serial(dev, 115200, timeout=1)
        except Exception:
            continue
        try:
            time.sleep(0.3)
            ser.reset_input_buffer()
            i = nid()
            send(ser, {"type": "PING", "id": i})
            msg = read_reply(ser, i, timeout=3)
            if msg and msg.get("type") == "ACK":
                return ser, dev, msg.get("fw")
        except Exception:
            pass
        try:
            ser.close()
        except Exception:
            pass
    return None, None, None


def expect_ack(ser, obj, what):
    i = nid()
    obj = dict(obj, id=i)
    send(ser, obj)
    msg = read_reply(ser, i)
    if not msg or msg.get("type") != "ACK":
        raise RuntimeError("no ACK for %s: %r" % (what, msg))


def put_file(ser, local_path, dev_path):
    with open(local_path, "rb") as f:
        data = f.read()
    if data[:3] == b"\xef\xbb\xbf":          # strip UTF-8 BOM (CP rejects it)
        data = data[3:]
    print("  %s  (%d bytes)" % (dev_path, len(data)))
    expect_ack(ser, {"type": "PUT_FILE_BEGIN", "path": dev_path}, "BEGIN")
    for off in range(0, len(data), CHUNK):
        b64 = base64.b64encode(data[off:off + CHUNK]).decode()
        expect_ack(ser, {"type": "PUT_FILE_CHUNK", "path": dev_path, "data_b64": b64}, "CHUNK")
    expect_ack(ser, {"type": "PUT_FILE_END", "path": dev_path}, "END")
    print("    ok")


def device_info(ser):
    i = nid()
    send(ser, {"type": "GET_DEVICE_INFO", "id": i})
    return read_reply(ser, i)


def main():
    print("Scanning for the pedal data port...")
    ser, dev, fw = find_data_port()
    if ser is None:
        print("ERROR: no data CDC port responded to PING.")
        print("CDC ports seen:", cdc_ports() or "(none)")
        print("Make sure the pedal is connected and not held by the editor.")
        sys.exit(1)
    info = device_info(ser)
    print("Connected on %s  fw=%s  info=%r" % (dev, fw, info))

    print("\nUploading files:")
    for local, devp in FILES:
        put_file(ser, local, devp)

    print("\nRebooting the pedal...")
    expect_ack(ser, {"type": "REBOOT"}, "REBOOT")
    try:
        ser.close()
    except Exception:
        pass

    print("Waiting for the pedal to come back...")
    ser2 = None
    deadline = time.time() + 30
    while time.time() < deadline:
        time.sleep(1.5)
        ser2, dev2, fw2 = find_data_port()
        if ser2 is not None:
            break
    if ser2 is None:
        print("WARNING: pedal did not re-enumerate within 30 s.")
        print("It may need a USB power-cycle (boot.py runs only on hard reset).")
        sys.exit(2)
    info2 = device_info(ser2)
    fwv = (info2 or {}).get("fw")
    print("Back on %s  fw=%s  info=%r" % (dev2, fwv, info2))
    try:
        ser2.close()
    except Exception:
        pass

    if fwv == EXPECT_VERSION:
        print("\nSUCCESS: pedal now runs firmware %s (preset-nav fix installed)." % EXPECT_VERSION)
    else:
        print("\nNOTE: pedal reports fw=%r (expected %s). Check the upload." % (fwv, EXPECT_VERSION))
        sys.exit(3)


if __name__ == "__main__":
    main()
