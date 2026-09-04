"""
A fake pedal that drives a lively Stage View, for developing the kiosk
without real hardware.

TCP server on :9876 speaking the Bosun protocol. Answers the bootstrap
requests (DEVICE_INFO / MANIFEST / GLOBAL / LIST_PATCHES / GET_PATCH /
GET_CONTEXT) with a small Kemper-style profile, then loops forever
pushing realistic unsolicited traffic: rig changes, effect-block
toggles, tuner in/out, tempo drift.

    python tests/stage_demo_pedal.py            # then point the hub at
    python -m bosun_hub --target tcp://127.0.0.1:9876 --stage-dir ../../editor/dist-stage
"""

from __future__ import annotations

import json
import random
import socket
import socketserver
import threading
import time

FW = "0.6.0-demo"
HOST, PORT = "0.0.0.0", 9876

RIGS = [
    "Clean Fender Twin", "Vintage Deluxe Reverb", "Brit 800 Crunch",
    "Modern High Gain", "Ambient Swell Pad", "Acoustic Sim DI",
]
BLOCKS = ["A", "B", "C", "D", "X", "MOD", "DLY", "REV"]

# One Kemper-ish bank of 5 patches, switches 1-4 + A-D bound to blocks.
BINDINGS = [
    {"switch": "1", "mode": "latched", "label": "Boost",
     "led": {"on": "#ff3b3b"}, "actions": {},
     "_block": "B"},
    {"switch": "2", "mode": "latched", "label": "Drive",
     "led": {"on": "#f59e0b"}, "actions": {}, "_block": "A"},
    {"switch": "3", "mode": "latched", "label": "Chorus",
     "led": {"on": "#3b82f6"}, "actions": {}, "_block": "MOD"},
    {"switch": "4", "mode": "latched", "label": "Delay",
     "led": {"on": "#22c55e"}, "actions": {}, "_block": "DLY"},
    {"switch": "A", "mode": "latched", "label": "Reverb",
     "led": {"on": "#a855f7"}, "actions": {}, "_block": "REV"},
    {"switch": "B", "mode": "tap", "label": "Tuner",
     "led": {"on": "#64748b"}, "actions": {}},
    {"switch": "C", "mode": "tap", "label": "Tap", "led": {"on": "#eab308"}, "actions": {}},
]

state = {
    "bank": 1,
    "slot": 1,
    "bpm": 120,
    "tuner": "off",
    "blocks": {b: "off" for b in BLOCKS},
    "rig": RIGS[0],
}
_clients: list[socket.socket] = []
_lock = threading.Lock()


def context_payload() -> dict:
    ctx = {
        "kemper_rig_name": state["rig"],
        "kemper_bank": state["bank"],
        "kemper_rig_in_bank": state["slot"],
        "kemper_bpm": state["bpm"],
        "kemper_tuner": state["tuner"],
        "kemper_connected": "on",
    }
    if state["tuner"] == "on":
        ctx["kemper_tuner_note"] = random.choice(["E", "A", "D", "G", "B"])
        ctx["kemper_tuner_deviance"] = random.randint(7600, 8800)
    for b, v in state["blocks"].items():
        ctx["kemper_block_" + b] = v
    return ctx


def patch_payload() -> dict:
    return {
        "name": state["rig"],
        "bindings": [
            {k: v for k, v in b.items() if not k.startswith("_")} for b in BINDINGS
        ],
    }


def handle(msg: dict) -> dict | None:
    t, mid = msg.get("type", ""), msg.get("id", "")
    if t == "PING":
        return {"type": "ACK", "id": mid, "fw": FW}
    if t == "GET_DEVICE_INFO":
        return {"type": "DEVICE_INFO", "id": mid, "fw": FW, "device": "midi_captain_10",
                "current": {"bank": state["bank"], "slot": state["slot"]}}
    if t == "GET_GLOBAL":
        return {"type": "GLOBAL", "id": mid, "device": {
            "preset_navigation": {
                "switches": {"up": 0, "down": 0},
                "bank_colors": {"1": "#3b82f6"},
            }
        }}
    if t == "GET_MANIFEST":
        return {"type": "MANIFEST", "id": mid, "core_messages": {}, "plugins": {}}
    if t == "LIST_PATCHES":
        return {"type": "PATCH_LIST", "id": mid, "patches": [
            {"bank": 1, "slot": s, "name": RIGS[s - 1], "color": "#3b82f6"}
            for s in range(1, 6)
        ]}
    if t == "GET_PATCH":
        return {"type": "PATCH", "id": mid, "bank": msg.get("bank", 1),
                "slot": msg.get("slot", 1), "patch": patch_payload()}
    if t == "GET_CONTEXT":
        return {"type": "CONTEXT", "id": mid, "context": context_payload()}
    return {"type": "ACK", "id": mid}


def broadcast(obj: dict) -> None:
    data = (json.dumps(obj) + "\n").encode()
    with _lock:
        clients = list(_clients)
    for c in clients:
        try:
            c.sendall(data)
        except OSError:
            pass


class H(socketserver.StreamRequestHandler):
    def handle(self) -> None:
        sock = self.request
        sock.settimeout(2.0)
        with _lock:
            _clients.append(sock)
        print(f"client {self.client_address} connected", flush=True)
        buf = bytearray()
        try:
            while True:
                try:
                    chunk = sock.recv(4096)
                except socket.timeout:
                    continue  # idle is fine; a real close returns b""
                except OSError:
                    break
                if not chunk:
                    break
                buf.extend(chunk)
                while b"\n" in buf:
                    pos = buf.index(b"\n")
                    line = bytes(buf[:pos]).strip()
                    del buf[: pos + 1]
                    if not line:
                        continue
                    try:
                        resp = handle(json.loads(line))
                    except ValueError:
                        continue
                    if resp:
                        sock.sendall((json.dumps(resp) + "\n").encode())
        finally:
            with _lock:
                if sock in _clients:
                    _clients.remove(sock)
            print(f"client {self.client_address} gone", flush=True)


def driver() -> None:
    """Push lively unsolicited traffic forever."""
    time.sleep(2)
    while True:
        action = random.random()
        if action < 0.35:
            # toggle a random effect block
            b = random.choice(BLOCKS[:5] + ["MOD", "DLY", "REV"])
            state["blocks"][b] = "on" if state["blocks"][b] == "off" else "off"
            sw = next((x["switch"] for x in BINDINGS if x.get("_block") == b), None)
            broadcast({"type": "CONTEXT", "context": context_payload()})
            if sw:
                broadcast({"type": "EVENT", "event": "binding_fired", "switch": sw,
                           "action": "toggle_on" if state["blocks"][b] == "on" else "toggle_off"})
        elif action < 0.55:
            # rig change within the bank
            state["slot"] = random.randint(1, 5)
            state["rig"] = RIGS[state["slot"] - 1]
            state["blocks"] = {b: random.choice(["on", "off", "off"]) for b in BLOCKS}
            broadcast({"type": "EVENT", "event": "patch_switched",
                       "bank": state["bank"], "slot": state["slot"], "source": "midi_in"})
            broadcast({"type": "PATCH", "bank": state["bank"], "slot": state["slot"],
                       "patch": patch_payload()})
            broadcast({"type": "CONTEXT", "context": context_payload()})
        elif action < 0.70:
            # tuner in/out
            state["tuner"] = "on" if state["tuner"] == "off" else "off"
            broadcast({"type": "CONTEXT", "context": context_payload()})
        else:
            # tempo drift
            state["bpm"] = max(60, min(200, state["bpm"] + random.choice([-4, -2, 2, 4])))
            broadcast({"type": "CONTEXT", "context": context_payload()})

        # while the tuner is up, keep the needle moving
        if state["tuner"] == "on":
            for _ in range(6):
                time.sleep(0.4)
                broadcast({"type": "CONTEXT", "context": context_payload()})
        time.sleep(random.uniform(1.5, 3.5))


if __name__ == "__main__":
    threading.Thread(target=driver, daemon=True).start()
    print(f"stage demo pedal on {HOST}:{PORT}", flush=True)
    with socketserver.ThreadingTCPServer((HOST, PORT), H) as srv:
        srv.daemon_threads = True
        srv.serve_forever()
