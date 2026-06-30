"""One-shot helper: push a set of Kemper Player patches to the captain
over the bosun JSON protocol. Run AFTER killing the editor (or any other
host) that owns the COM port - the captain only accepts a single serial
listener at a time.

Usage:
    python tools/push_patches.py            # auto-detect, push, save
    python tools/push_patches.py COM7       # explicit port

What it does, in order:
    1. opens the captain's USB CDC serial port
    2. drains any noise lines waiting in the kernel buffer
    3. PINGs to confirm we're talking to a captain
    4. for each patch in PATCHES below: sends PUT_PATCH and waits for the
       matching ACK by `id`
    5. sends SAVE_NOW so the patches survive reboot
    6. sends LIST_PATCHES and prints the resulting summary

The script is intentionally non-clever: no asyncio, no threads. Each
JSON line is one request, each ACK is one response, and we wait for one
before sending the next. That makes failures easy to read on the
console.
"""

import json
import sys
import time

import serial
import serial.tools.list_ports


# ----------------------------------------------------------------------
#  Patch definitions
# ----------------------------------------------------------------------
#
# Spec (from the user):
#
#   acoustic, rig 1-1, c (switch 4), dly (switch up)
#   clean,    rig 1-2, x (switch 3), rev (switch up)
#   crunch,   rig 1-3, x (switch 3), rev (switch up)
#   heavy,    rig 1-4, d (switch 3), x (switch 4), rev (switch up)
#   lead,     rig 1-5, x (switch 3)
#   heavy,    rig 2-4, d (switch 3), x (switch 4), rev (switch up)
#
# Switch labels -> Kemper effect slots:
#   c   -> slot "C"
#   d   -> slot "D"
#   x   -> slot "X"
#   dly -> slot "Delay"
#   rev -> slot "Reverb"
#
# Each binding is "latched": one tap turns the slot on, another tap turns
# it off. The Kemper plugin's bidirectional auto-follow keeps the LED
# state in sync with the actual on/off the Player reports back, so the
# pedal stays accurate even if the user toggles the effect on the Kemper
# directly.

CHANNEL = 1  # MIDI channel for Kemper (default Bosun ships with)

# LED color per switch position - matches editor's defaultLedFor(...) so
# the colors look the same as fresh-create patches in the UI.
DEFAULT_LED = {
    "1": "#3a8eff",
    "2": "#f5dc34",
    "3": "#e54848",
    "4": "#3ecb6e",
    "up": "#00bcd4",
    "down": "#c08aff",
    "A": "#ff8a00",
    "B": "#00e5ff",
    "C": "#ff4081",
    "D": "#76ff03",
}

# Kemper rig 1..5 LED colors (Player "Bank-Farbcodes" chart, level III).
# Used as the patch's tft_color so the on-pedal TFT picks a colour that
# matches the rig position.
RIG_COLOR = {
    1: "#3a8eff",
    2: "#f5dc34",
    3: "#e54848",
    4: "#2a2a2a",
    5: "#3ecb6e",
}


def effect_binding(switch, label, slot):
    """Build a latched binding that toggles a Kemper effect slot."""
    return {
        "switch": switch,
        "mode": "latched",
        "label": label,
        "led": {"on": DEFAULT_LED.get(switch, "#888888"), "off": "#000000"},
        "actions": {
            "toggle_on": {
                "messages": [{
                    "type": "kemper_effect_toggle",
                    "slot": slot, "value": "on", "channel": CHANNEL,
                }],
            },
            "toggle_off": {
                "messages": [{
                    "type": "kemper_effect_toggle",
                    "slot": slot, "value": "off", "channel": CHANNEL,
                }],
            },
        },
    }


def make_patch(name, rig_bank, rig_in_bank, switch_specs):
    """Compose a full patch dict.

    switch_specs = list of (switch_name, label, kemper_slot) tuples.
    """
    return {
        "name": name,
        "tft_color": RIG_COLOR.get(rig_in_bank, "#666666"),
        "on_enter": {
            "messages": [{
                "type": "kemper_rig",
                "bank": rig_bank, "rig": rig_in_bank, "channel": CHANNEL,
            }],
        },
        "bindings": [effect_binding(sw, lbl, slot) for sw, lbl, slot in switch_specs],
    }


# (captain_bank, captain_slot, patch_body)
PATCHES = [
    (1, 1, make_patch("acoustic", 1, 1, [("4", "c", "C"), ("up", "dly", "Delay")])),
    (1, 2, make_patch("clean",    1, 2, [("3", "x", "X"), ("up", "rev", "Reverb")])),
    (1, 3, make_patch("crunch",   1, 3, [("3", "x", "X"), ("up", "rev", "Reverb")])),
    (1, 4, make_patch("heavy",    1, 4, [("3", "d", "D"), ("4", "x", "X"), ("up", "rev", "Reverb")])),
    (1, 5, make_patch("lead",     1, 5, [("3", "x", "X")])),
    (2, 4, make_patch("heavy",    2, 4, [("3", "d", "D"), ("4", "x", "X"), ("up", "rev", "Reverb")])),
]


# ----------------------------------------------------------------------
#  Transport
# ----------------------------------------------------------------------

def find_port():
    """Best-effort autodetect. The captain shows up as a CircuitPython
    USB CDC serial port - the manufacturer string is usually 'Adafruit'
    or the VID/PID matches the Pico's. If we can't decide, we list the
    candidates and bail."""
    candidates = []
    for p in serial.tools.list_ports.comports():
        descr = (p.description or "").lower()
        manuf = (p.manufacturer or "").lower()
        if ("circuitpython" in descr
                or "adafruit" in manuf
                or "raspberry pi pico" in descr
                or (p.vid, p.pid) == (0x239A, 0x80F4)
                or (p.vid, p.pid) == (0x2E8A, 0x0005)):
            candidates.append(p.device)
    if len(candidates) == 1:
        return candidates[0]
    if len(candidates) > 1:
        print(f"multiple candidates: {candidates} - pass one explicitly", file=sys.stderr)
        sys.exit(2)
    # Fallback: ANY COM port. List them so the user can pick.
    others = [p.device for p in serial.tools.list_ports.comports()]
    print(f"no obvious captain port found. Available: {others}", file=sys.stderr)
    print(f"pass the right one as the first arg, e.g. `python tools/push_patches.py {others[0] if others else 'COMx'}`",
          file=sys.stderr)
    sys.exit(2)


def open_port(port):
    s = serial.Serial(port, baudrate=115200, timeout=0.5, write_timeout=2.0)
    # CircuitPython USB CDC doesn't care about baud, but pyserial wants
    # one anyway. The timeout above is the per-read timeout - we use it
    # to slice the inbound stream into JSON lines.
    return s


def drain(s, ms=200):
    """Read whatever's already in the kernel buffer and throw it away.
    The captain may have queued boot-time prints, beacon SYSEX, etc.
    before we attached."""
    end = time.monotonic() + ms / 1000.0
    while time.monotonic() < end:
        try:
            chunk = s.read(4096)
        except serial.SerialException:
            break
        if not chunk:
            time.sleep(0.01)


def send_line(s, obj):
    line = json.dumps(obj, separators=(",", ":")) + "\n"
    s.write(line.encode("utf-8"))
    s.flush()


def read_line(s, timeout_s=5.0):
    """Read the next \\n-terminated line. Tolerates split reads."""
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
    raise TimeoutError(f"no response within {timeout_s}s; got so far: {bytes(buf)!r}")


def await_response(s, expected_id, timeout_s=10.0, want_type=None):
    """Read lines until we see one with the matching id (and optional type).
    Anything else (events, beacons, errors with other ids) is forwarded
    to stdout so we can see what the firmware is up to."""
    end = time.monotonic() + timeout_s
    while time.monotonic() < end:
        remaining = max(0.5, end - time.monotonic())
        try:
            line = read_line(s, timeout_s=remaining)
        except TimeoutError as e:
            raise TimeoutError(f"waiting for id={expected_id}: {e}") from e
        line = line.strip()
        if not line:
            continue
        try:
            msg = json.loads(line)
        except json.JSONDecodeError:
            print(f"  (non-json line) {line}")
            continue
        if msg.get("id") != expected_id:
            t = msg.get("type") or "?"
            if t == "EVENT":
                continue   # spam from beacon / sensing
            print(f"  (unrelated {t}) {line}")
            continue
        if want_type and msg.get("type") != want_type:
            raise RuntimeError(f"expected {want_type}, got {msg}")
        if msg.get("type") == "ERROR":
            raise RuntimeError(f"firmware error: {msg}")
        return msg
    raise TimeoutError(f"id={expected_id} never came back within {timeout_s}s")


# ----------------------------------------------------------------------
#  Main
# ----------------------------------------------------------------------

def main():
    port = sys.argv[1] if len(sys.argv) > 1 else find_port()
    print(f"opening {port}")
    s = open_port(port)
    try:
        drain(s)
        # 1. PING
        send_line(s, {"type": "PING", "id": "ping-1"})
        ack = await_response(s, "ping-1", want_type="ACK")
        print(f"PING ok, firmware {ack.get('fw')}")

        # 2. push patches
        for i, (bank, slot, patch) in enumerate(PATCHES, start=1):
            msg_id = f"put-{i}"
            send_line(s, {"type": "PUT_PATCH", "id": msg_id, "bank": bank, "slot": slot, "patch": patch})
            try:
                await_response(s, msg_id, want_type="ACK", timeout_s=5.0)
                print(f"  [{bank:02d}/{slot:02d}] {patch['name']:9s} put OK")
            except (TimeoutError, RuntimeError) as e:
                print(f"  [{bank:02d}/{slot:02d}] {patch['name']:9s} FAILED: {e}", file=sys.stderr)
                raise

        # 3. SAVE_NOW (global - persists every dirty patch)
        send_line(s, {"type": "SAVE_NOW", "id": "save-1"})
        saved = await_response(s, "save-1", want_type="SAVED", timeout_s=15.0)
        print(f"SAVE_NOW: persisted {len(saved.get('patches', []))} patch(es)")

        # 4. confirmation via LIST_PATCHES
        send_line(s, {"type": "LIST_PATCHES", "id": "list-1"})
        lst = await_response(s, "list-1", want_type="PATCH_LIST", timeout_s=5.0)
        print("Patches on disk:")
        for p in lst.get("patches", []):
            print(f"  {p['bank']:02d}/{p['slot']:02d}  {p.get('name', ''):20s}  dirty={p.get('dirty')}")
    finally:
        s.close()
        print("port closed")


if __name__ == "__main__":
    main()
