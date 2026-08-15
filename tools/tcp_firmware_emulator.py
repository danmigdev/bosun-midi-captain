"""
TCP firmware emulator for Android debugging.
Run on PC. The Android app connects via WiFi instead of USB serial.

Usage: python tcp_firmware_emulator.py
Listens on 0.0.0.0:9876. Responds to Bosun protocol messages exactly
like the MIDI Captain firmware does.
"""

import json
import socket
import socketserver

VERSION = "0.4.16"
DEVICE = "MIDI Captain (emulated over TCP)"
HOST = "0.0.0.0"
PORT = 9876


def handle_message(msg: dict) -> dict | None:
    t = msg.get("type", "")
    mid = msg.get("id", "")

    if t == "PING":
        return {"type": "ACK", "id": mid, "fw": VERSION}
    if t == "GET_DEVICE_INFO":
        return {"type": "DEVICE_INFO", "id": mid, "fw": VERSION,
                "device": DEVICE, "current": {"bank": 1, "slot": 1}}
    if t == "GET_GLOBAL":
        return {"type": "GLOBAL", "id": mid, "device": {}}
    if t == "LIST_PATCHES":
        return {"type": "PATCH_LIST", "id": mid, "patches": []}
    if t == "STATS":
        return {"type": "STATS", "id": mid, "uptime_ms": 0,
                "mem_free": 100000, "mem_alloc": 50000,
                "loop_iters": 0, "midi_rx_count": 0, "midi_tx_count": 0,
                "protocol_cmd_count": 0, "last_patch_switch_ms": 0,
                "current": {"bank": 1, "slot": 1}}
    if t == "GET_MANIFEST":
        return {
            "type": "MANIFEST", "id": mid,
            "core_messages": {
                "cc": {"label": "Control Change", "params": {
                    "channel": {"type": "int", "min": 1, "max": 16, "default": 1, "label": "Channel"},
                    "cc": {"type": "int", "min": 0, "max": 127, "default": 0, "label": "CC #"},
                    "value": {"type": "int", "min": 0, "max": 127, "default": 0, "label": "Value"}},
                    "summary": "CC {cc}={value} ch {channel}"},
                "pc": {"label": "Program Change", "params": {
                    "channel": {"type": "int", "min": 1, "max": 16, "default": 1, "label": "Channel"},
                    "program": {"type": "int", "min": 0, "max": 127, "default": 0, "label": "Program"}},
                    "summary": "PC {program} ch {channel}"},
                "note_on": {"label": "Note On", "params": {
                    "channel": {"type": "int", "min": 1, "max": 16, "default": 1, "label": "Channel"},
                    "note": {"type": "int", "min": 0, "max": 127, "default": 60, "label": "Note"},
                    "velocity": {"type": "int", "min": 0, "max": 127, "default": 100, "label": "Velocity"}},
                    "summary": "Note On {note} v{velocity} ch {channel}"},
                "note_off": {"label": "Note Off", "params": {
                    "channel": {"type": "int", "min": 1, "max": 16, "default": 1, "label": "Channel"},
                    "note": {"type": "int", "min": 0, "max": 127, "default": 60, "label": "Note"},
                    "velocity": {"type": "int", "min": 0, "max": 127, "default": 64, "label": "Velocity"}},
                    "summary": "Note Off {note} ch {channel}"},
                "delay": {"label": "Delay", "params": {
                    "ms": {"type": "int", "min": 0, "max": 5000, "default": 100, "label": "Milliseconds"}},
                    "summary": "Wait {ms}ms"},
                "captain_patch": {"label": "Switch Captain Patch", "params": {
                    "bank": {"type": "int", "min": 1, "max": 99, "default": 1, "label": "Bank"},
                    "slot": {"type": "int", "min": 1, "max": 10, "default": 1, "label": "Slot"}},
                    "summary": "-> Captain {bank}/{slot}"},
            },
            "plugins": {},
        }
    if t == "PUT_GLOBAL":
        return {"type": "ACK", "id": mid}
    if t == "SAVE_NOW":
        return {"type": "SAVED", "id": mid, "patches": []}
    if t == "GET_DIRTY":
        return {"type": "DIRTY", "id": mid, "patches": []}
    return {"type": "ACK", "id": mid}


class BosunHandler(socketserver.StreamRequestHandler):
    def handle(self):
        print(f"Connected: {self.client_address}")
        buf = bytearray()
        # Use raw socket recv() instead of rfile.read() to avoid buffering
        # issues. rfile blocks until buffer is full on some platforms.
        sock = self.request
        sock.settimeout(10.0)
        try:
            while True:
                try:
                    chunk = sock.recv(4096)
                except socket.timeout:
                    break
                except OSError:
                    break
                if not chunk:
                    break
                buf.extend(chunk)
                while b"\n" in buf:
                    pos = buf.index(b"\n")
                    line = bytes(buf[:pos]).rstrip(b"\r").decode("utf-8", errors="replace")
                    del buf[:pos + 1]
                    if not line.strip():
                        continue
                    print(f"RECV: {line[:150]}")
                    try:
                        msg = json.loads(line)
                        resp = handle_message(msg)
                        if resp:
                            data = (json.dumps(resp) + "\n").encode()
                            sock.sendall(data)
                            print(f"SEND: {json.dumps(resp)[:100]}")
                    except json.JSONDecodeError as e:
                        err = json.dumps({"type": "ERROR", "error": "bad_json", "detail": str(e)}) + "\n"
                        sock.sendall(err.encode())
                        print(f"SEND: {err.strip()}")
        except Exception as e:
            print(f"Disconnected: {e}")


if __name__ == "__main__":
    import socket
    hostname = socket.gethostname()
    local_ip = socket.gethostbyname(hostname)
    print(f"PC IP: {local_ip}")
    print(f"Listening on {HOST}:{PORT}")
    print(f"On Android, set TCP mode and connect to {local_ip}:{PORT}")
    with socketserver.ThreadingTCPServer((HOST, PORT), BosunHandler) as server:
        server.serve_forever()
