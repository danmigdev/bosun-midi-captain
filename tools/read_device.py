"""Quick read of the active profile's device.json via the JSON protocol."""
import json
import sys
import time

import serial


def main():
    port = sys.argv[1] if len(sys.argv) > 1 else "COM4"
    s = serial.Serial(port, baudrate=115200, timeout=2.0)
    time.sleep(0.3)
    s.reset_input_buffer()
    s.write(b'{"type":"GET_GLOBAL","id":"g1"}\n')
    buf = b""
    end = time.time() + 5
    while time.time() < end:
        b = s.read(1)
        if not b:
            continue
        if b == b"\n":
            line = buf.decode("utf-8", errors="replace")
            try:
                m = json.loads(line)
            except Exception:
                buf = b""
                continue
            if m.get("id") == "g1":
                d = m.get("device", {})
                print("preset_navigation:", json.dumps(d.get("preset_navigation"), indent=2))
                print("kemper:", json.dumps(d.get("kemper"), indent=2))
                print("auto_momentary_on_hold:", d.get("auto_momentary_on_hold"))
                break
            buf = b""
        elif b != b"\r":
            buf += b
    s.close()


if __name__ == "__main__":
    main()
