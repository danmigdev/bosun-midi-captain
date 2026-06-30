#!/usr/bin/env python3
"""Generate 5 Captain patches (Layout C: Ampero=patch select, Captain=effects)
and push them via the PROTOCOL (not OTA - these are patch JSONs, not code).

After running, the firmware persists each patch to /config/patches/<bank>/<slot>.json
(if autosave is enabled or via explicit SAVE_NOW).

Usage:
    python tools/seed_layout_c.py --port COM4
"""
import argparse
import json
import sys
import time

import serial


# ---- Effect slot allocation per Ampero preset (see plan doc) ----
# Same conceptual layout: A1=amp, A2=cab, A3=drive, A4=eq, A5/A6=variants,
# B1=reverb, B2=delay, B3=eq2, B4=flanger, B6=boost. Not all slots are
# active on every preset - see the plan doc for the actual fills.

CH = 16  # Ampero default channel (configurable; matches device.json ampero.din_out_channel)


def slot_toggle(slot, on=True):
    """Build an ampero_slot_toggle message."""
    return {"type": "ampero_slot_toggle", "channel": CH, "slot": slot,
            "value": "on" if on else "off"}


def ampero_patch(p):
    return {"type": "ampero_patch", "channel": CH, "patch": p}


def cc(num, value, channel=CH):
    return {"type": "cc", "channel": channel, "cc": num, "value": value}


def binding(switch, mode, label, led_on, action_messages, *,
            actions=None, led_off=None, auto_momentary=None):
    """Compact binding builder.
    `action_messages` is shorthand for actions['press'].messages for tap/momentary,
    or you can pass full `actions={key: {messages:[...]}}` for explicit control."""
    led = {"on": led_on}
    if led_off is not None:
        led["off"] = led_off
    b = {
        "switch": switch,
        "mode": mode,
        "label": label,
        "led": led,
    }
    if actions is not None:
        b["actions"] = actions
    else:
        # default: single 'press' action
        b["actions"] = {"press": {"messages": action_messages}}
    if auto_momentary is not None:
        b["auto_momentary"] = auto_momentary
    return b


# Universal switches (down=Tap Tempo, up=Tuner) reused across all patches.
def tap_tempo_bind():
    return binding("down", "tap", "Tap", "#f4cd7a",
                   [cc(76, 127)])  # CC 76 = Tap on Ampero


def tuner_bind():
    return binding("up", "tap", "Tuner", "#7aa8f4",
                   [cc(60, 127)])  # CC 60 = Tuner on Ampero


# Latched slot toggle: toggle_on -> slot ON, toggle_off -> slot OFF.
def slot_latched(switch, label, slot, led_on, led_off="#1a1a1a"):
    return binding(switch, "latched", label, led_on,
                   action_messages=None,
                   led_off=led_off,
                   actions={
                       "toggle_on":  {"messages": [slot_toggle(slot, True)]},
                       "toggle_off": {"messages": [slot_toggle(slot, False)]},
                   },
                   auto_momentary=False)


# Momentary slot toggle: press -> slot ON, release -> slot OFF.
def slot_momentary(switch, label, slot, led_on):
    return binding(switch, "momentary", label, led_on,
                   action_messages=None,
                   actions={
                       "press":   {"messages": [slot_toggle(slot, True)]},
                       "release": {"messages": [slot_toggle(slot, False)]},
                   })


# ----------------- PATCH BUILDERS -----------------

def acoustic_patch():
    return {
        "name": "Acoustic",
        "tft_color": "#88c4a8",
        "on_enter": {"messages": [ampero_patch("P01-1")]},
        "bindings": [
            slot_latched("1", "Comp",    "A3", "#88c4a8"),
            slot_latched("2", "Chorus",  "A5", "#9ad0e6"),
            slot_latched("3", "Delay",   "B2", "#d99b6f"),
            slot_latched("4", "Reverb",  "B1", "#bca0d6"),
            # 12-string harmonizer - momentary so you can "double" passages
            slot_momentary("A", "12-str", "A4", "#e8ce6f"),
            slot_latched("B", "Flanger", "B4", "#6fe8c4"),
            slot_latched("C", "Boost",   "B6", "#ef9b9b"),
            slot_latched("D", "EQ alt",  "A2", "#888888"),
            tuner_bind(),
            tap_tempo_bind(),
        ],
    }


def clean_patch():
    return {
        "name": "Clean",
        "tft_color": "#9ad0e6",
        "on_enter": {"messages": [ampero_patch("P01-2")]},
        "bindings": [
            slot_latched("1", "Comp",    "A3", "#88c4a8"),
            slot_latched("2", "Chorus",  "A5", "#9ad0e6"),
            slot_latched("3", "Delay",   "B2", "#d99b6f"),
            slot_latched("4", "Reverb",  "B1", "#bca0d6"),
            slot_latched("A", "Tremolo", "A6", "#e8ce6f"),
            slot_latched("B", "Flanger", "B4", "#6fe8c4"),
            slot_latched("C", "Boost",   "B6", "#ef9b9b"),
            slot_latched("D", "EQ alt",  "A4", "#888888"),
            tuner_bind(),
            tap_tempo_bind(),
        ],
    }


def crunch_patch():
    return {
        "name": "Crunch",
        "tft_color": "#d99b6f",
        "on_enter": {"messages": [ampero_patch("P01-3")]},
        "bindings": [
            slot_latched("1", "Drive",   "A3", "#d99b6f"),
            slot_latched("2", "EQ alt",  "A4", "#888888"),
            slot_latched("3", "Delay",   "B2", "#d99b6f"),
            slot_latched("4", "Reverb",  "B1", "#bca0d6"),
            slot_latched("A", "Comp",    "A5", "#88c4a8"),
            slot_latched("B", "Flanger", "B4", "#6fe8c4"),
            slot_latched("C", "Boost",   "B6", "#ef9b9b"),
            slot_latched("D", "Chorus",  "A6", "#9ad0e6"),
            tuner_bind(),
            tap_tempo_bind(),
        ],
    }


def heavy_patch():
    """The hero patch - two harmonizers in momentary mode."""
    return {
        "name": "Heavy",
        "tft_color": "#ef5a5a",
        "on_enter": {"messages": [ampero_patch("P01-4")]},
        "bindings": [
            slot_latched("1", "Drive",   "A3", "#ef5a5a"),
            slot_latched("2", "EQ alt",  "B3", "#888888"),
            slot_latched("3", "Delay",   "B2", "#d99b6f"),
            slot_latched("4", "Reverb",  "B1", "#bca0d6"),
            # ⭐ Two momentary harmonizers - press-and-hold to engage
            slot_momentary("A", "Harm 1v", "A5", "#e8ce6f"),
            slot_momentary("B", "Harm 2v", "A6", "#f4cd5a"),
            slot_latched("C", "Boost",   "B6", "#ef9b9b"),
            slot_latched("D", "Flanger", "B4", "#6fe8c4"),
            tuner_bind(),
            tap_tempo_bind(),
        ],
    }


def lead_patch():
    return {
        "name": "Lead",
        "tft_color": "#f4cd7a",
        "on_enter": {"messages": [ampero_patch("P01-5")]},
        "bindings": [
            slot_latched("1", "Drive",   "A3", "#f4cd7a"),
            slot_latched("2", "EQ alt",  "A4", "#888888"),
            slot_latched("3", "Delay",   "B2", "#d99b6f"),
            slot_latched("4", "Reverb",  "B1", "#bca0d6"),
            slot_latched("A", "Comp",    "A5", "#88c4a8"),
            slot_latched("B", "Flanger", "B4", "#6fe8c4"),
            slot_latched("C", "Boost",   "B6", "#ef9b9b"),
            slot_latched("D", "Chorus",  "A6", "#9ad0e6"),
            tuner_bind(),
            tap_tempo_bind(),
        ],
    }


PATCHES = [
    (1, 1, acoustic_patch()),
    (1, 2, clean_patch()),
    (1, 3, crunch_patch()),
    (1, 4, heavy_patch()),
    (1, 5, lead_patch()),
]


# ----------------- MIDI LEARN for beta auto-follow -----------------
# When Ampero broadcasts PC=N on patch switch, Captain looks up the
# pc_to_patch table and switches Captain patch accordingly. PC numbers
# 0..4 map to slots 1..5 in bank 1 (Ampero P01-1..P01-5 use PC 0..4
# on Bank MSB 0 per the official Ampero CC spec).

MIDI_LEARN = {
    "pc_to_patch": [
        {"channel": CH, "bank_msb": 0, "pc": 0, "captain_patch": "01/01"},
        {"channel": CH, "bank_msb": 0, "pc": 1, "captain_patch": "01/02"},
        {"channel": CH, "bank_msb": 0, "pc": 2, "captain_patch": "01/03"},
        {"channel": CH, "bank_msb": 0, "pc": 3, "captain_patch": "01/04"},
        {"channel": CH, "bank_msb": 0, "pc": 4, "captain_patch": "01/05"},
    ]
}


# ----------------- CLIENT -----------------

def send_call(ser, msg, timeout=5.0):
    """Send a command, return the response dict matching its id."""
    ser.write((json.dumps(msg) + "\n").encode())
    deadline = time.monotonic() + timeout
    buf = bytearray()
    while time.monotonic() < deadline:
        chunk = ser.read(256)
        if chunk:
            buf.extend(chunk)
            while b"\n" in buf:
                line, _, rest = buf.partition(b"\n")
                buf = bytearray(rest)
                line = line.strip()
                if not line:
                    continue
                try:
                    obj = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if obj.get("id") == msg.get("id"):
                    return obj
    raise TimeoutError(f"no response to {msg.get('type')}#{msg.get('id')}")


def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--port", required=True)
    args = p.parse_args()

    ser = serial.Serial(args.port, 115200, timeout=0.1)
    time.sleep(0.5)
    # Drain stale
    while ser.in_waiting:
        ser.read(ser.in_waiting)

    nid = 0
    def next_id():
        nonlocal nid
        nid += 1
        return f"seed-{nid}"

    for bank, slot, patch in PATCHES:
        msg = {"type": "PUT_PATCH", "id": next_id(),
               "bank": bank, "slot": slot, "patch": patch}
        resp = send_call(ser, msg, timeout=4)
        ok = "OK" if resp.get("type") == "ACK" else f"ERR {resp}"
        print(f"  PUT_PATCH {bank:02d}/{slot:02d} '{patch['name']}': {ok}")

    print("\nWriting MIDI Learn table for beta auto-follow...")
    resp = send_call(ser, {"type": "PUT_MIDI_LEARN", "id": next_id(),
                           "table": MIDI_LEARN}, timeout=4)
    print(f"  PUT_MIDI_LEARN: {resp.get('type')}")

    print("\nSaving to flash...")
    resp = send_call(ser, {"type": "SAVE_NOW", "id": next_id()}, timeout=8)
    saved = resp.get("patches", [])
    print(f"  SAVED {len(saved)} patches")

    ser.close()
    print("\nDone. Switch to one of the new patches via:")
    print("  ampero footswitch → triggers PC broadcast → Captain auto-follows")
    print("Or from the editor's Patches page.")


if __name__ == "__main__":
    main()
