"""
Android USB-CDC test bridge.
Run this on the PC to emulate the Bosun firmware protocol.
Use with a USB-CDC gadget or a physical serial loopback.

For testing with a real Android phone via USB gadget mode, you need:
- Linux with USB gadget (dummy_hcd / g_serial / configfs)
- Or a second Pico/RP2040 running CircuitPython with this script

This script opens a serial port and responds to the Bosun protocol
(PING -> ACK, GET_DEVICE_INFO -> DEVICE_INFO, etc.) exactly like the
real firmware does.
"""

import sys
import json
import time
import serial
import serial.tools.list_ports

VERSION = "0.4.16-test"
DEVICE = "MIDI Captain (emulated)"


def handle_message(msg: dict) -> dict | None:
    msg_type = msg.get("type", "")
    msg_id = msg.get("id", "")

    if msg_type == "PING":
        return {"type": "ACK", "id": msg_id, "fw": VERSION}

    if msg_type == "GET_DEVICE_INFO":
        return {
            "type": "DEVICE_INFO",
            "id": msg_id,
            "fw": VERSION,
            "device": DEVICE,
            "current": {"bank": 1, "slot": 1},
        }

    if msg_type == "GET_GLOBAL":
        return {"type": "GLOBAL", "id": msg_id, "device": {}}

    if msg_type == "LIST_PATCHES":
        return {"type": "PATCH_LIST", "id": msg_id, "patches": []}

    if msg_type == "STATS":
        return {
            "type": "STATS",
            "id": msg_id,
            "uptime_ms": 0,
            "mem_free": 100000,
            "mem_alloc": 50000,
            "loop_iters": 0,
            "midi_rx_count": 0,
            "midi_tx_count": 0,
            "protocol_cmd_count": 0,
            "last_patch_switch_ms": 0,
            "current": {"bank": 1, "slot": 1},
        }

    # Default: ACK any unknown message
    return {"type": "ACK", "id": msg_id}


def poll_serial(port: str, baud: int = 115200):
    """Read lines from serial, process as JSON, send responses."""
    ser = serial.Serial(port, baud, timeout=0.05)
    ser.dtr = True  # CircuitPython CDC needs DTR asserted
    ser.rts = True
    print(f"Emulator running on {port} at {baud} baud (DTR={ser.dtr})")
    print("Waiting for PING from Android...")

    buf = bytearray()
    while True:
        try:
            chunk = ser.read(4096)
            if chunk:
                buf.extend(chunk)
                while b"\n" in buf:
                    pos = buf.index(b"\n")
                    line = bytes(buf[:pos]).rstrip(b"\r").decode("utf-8", errors="replace")
                    del buf[: pos + 1]
                    if not line.strip():
                        continue
                    # Show what we received (raw bytes for debugging)
                    print(f"\nRECV ({len(line)} chars): {line[:200]}")
                    if len(line) > 200:
                        print(f"  ... truncated, total {len(line)} chars")

                    # Try parsing and responding
                    try:
                        msg = json.loads(line)
                        resp = handle_message(msg)
                        if resp:
                            resp_line = json.dumps(resp) + "\n"
                            ser.write(resp_line.encode("utf-8"))
                            ser.flush()
                            print(f"SEND: {resp_line.strip()}")
                    except json.JSONDecodeError as e:
                        err_resp = json.dumps({
                            "type": "ERROR",
                            "error": "bad_json",
                            "detail": str(e),
                        }) + "\n"
                        ser.write(err_resp.encode("utf-8"))
                        ser.flush()
                        print(f"SEND (error): {err_resp.strip()}")
            else:
                time.sleep(0.01)
        except KeyboardInterrupt:
            print("\nStopped.")
            break
        except Exception as e:
            print(f"Error: {e}")
            time.sleep(1)


def list_ports():
    """List available serial ports."""
    ports = serial.tools.list_ports.comports()
    for p in ports:
        extra = ""
        if p.vid is not None:
            extra += f" VID={p.vid:04x} PID={p.pid:04x}"
        print(f"  {p.device} - {p.description}{extra}")


if __name__ == "__main__":
    if len(sys.argv) < 2:
        print("Usage: python android_test_bridge.py <COM_PORT>")
        print("  e.g. python android_test_bridge.py COM5")
        print()
        print("Available ports:")
        list_ports()
        sys.exit(1)

    poll_serial(sys.argv[1])
