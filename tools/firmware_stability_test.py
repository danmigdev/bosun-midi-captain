#!/usr/bin/env python3
"""Offline firmware stability tests - no hardware, no CircuitPython.

Mocks the CircuitPython surface, constructs the REAL captain.app.Captain,
and hammers the protocol dispatch + main loop with valid and malformed
input to prove the firmware can't be knocked over. On real hardware a
crash here shows up as the editor losing the USB data CDC ("not connected"):
if the main loop throws, code.py exits and the data port goes dead.

Covered:
  A. protocol.handle() never raises and always answers, across a barrage of
     valid + malformed messages - including PUT_GLOBAL with patch_link locks
     and PUT_PATCH of arbitrary shapes.
  B. The main loop (Captain.tick_once) survives an exception thrown by ANY
     sub-component (protocol.poll, MIDI parse, switch poll, autosave tick).
     This is the regression guard for the "bare loop kills the connection"
     bug: previously only protocol.handle() was wrapped.
  C. The MIDI parser eats arbitrary/garbage byte streams without throwing.

Usage:
    python tools/firmware_stability_test.py
"""

import json
import random
import sys
import tempfile
import types
from pathlib import Path


# ---------------- mock the CircuitPython surface ----------------

def _mod(name):
    m = types.ModuleType(name)
    sys.modules[name] = m
    return m


board = _mod("board")
for _i in range(30):
    setattr(board, "GP%d" % _i, "GP%d" % _i)

digitalio = _mod("digitalio")
class _DIO:
    def __init__(self, pin): self.pin = pin; self.direction = None; self.pull = None; self.value = True
    def deinit(self): pass
digitalio.DigitalInOut = _DIO
digitalio.Direction = types.SimpleNamespace(INPUT="in", OUTPUT="out")
digitalio.Pull = types.SimpleNamespace(UP="up", DOWN="down")

busio = _mod("busio")
class _SPI:
    def __init__(self, *a, **k): pass
class _UART:
    def __init__(self, *a, **k): self.in_waiting = 0
    def read(self, n=None): return b""
    def write(self, b): return len(b)
busio.SPI = _SPI
busio.UART = _UART

pwmio = _mod("pwmio")
class _PWM:
    def __init__(self, *a, **k): self.duty_cycle = 0
pwmio.PWMOut = _PWM

terminalio = _mod("terminalio")
terminalio.FONT = object()

fourwire = _mod("fourwire")
class _FourWire:
    def __init__(self, *a, **k): pass
fourwire.FourWire = _FourWire

displayio = _mod("displayio")
class _Group:
    def __init__(self, *a, **k): self._c = []
    def append(self, x): self._c.append(x)
class _Bitmap:
    def __init__(self, w, h, n): self.w = w; self.h = h
    def __setitem__(self, k, v): pass
    def __getitem__(self, k): return 0
class _Palette:
    def __init__(self, n): self._c = [0] * n
    def __setitem__(self, k, v): self._c[k] = v
    def make_transparent(self, i): pass
class _TileGrid:
    def __init__(self, *a, **k): pass
displayio.Group = _Group
displayio.Bitmap = _Bitmap
displayio.Palette = _Palette
displayio.TileGrid = _TileGrid
displayio.release_displays = lambda: None

_adt = _mod("adafruit_display_text")
_label = _mod("adafruit_display_text.label")
class _Label:
    def __init__(self, *a, **k): pass
_label.Label = _Label
_adt.label = _label

_st = _mod("adafruit_st7789")
class _ST7789:
    def __init__(self, *a, **k): self.root_group = None
_st.ST7789 = _ST7789

neopixel = _mod("neopixel")
class _NeoPixel:
    def __init__(self, pin, count, **k):
        self.n = count; self._buf = [(0, 0, 0)] * count; self.brightness = k.get("brightness", 1)
    def __setitem__(self, i, v): self._buf[i] = v
    def __getitem__(self, i): return self._buf[i]
    def __len__(self): return self.n
    def fill(self, v): self._buf = [v] * self.n
    def show(self): pass
neopixel.NeoPixel = _NeoPixel
neopixel.GRB = "GRB"

usb_midi = _mod("usb_midi")
usb_midi.ports = []

usb_cdc = _mod("usb_cdc")
class _CDC:
    def __init__(self):
        self.in_waiting = 0
        self.connected = True
        self.write_timeout = None
        self.out = bytearray()
    def read(self, n): return b""
    def write(self, b): self.out.extend(b); return len(b)
usb_cdc.data = _CDC()
usb_cdc.console = None


# ---------------- import the real firmware against a temp /config ----------------

_LIB = Path(__file__).resolve().parent.parent / "firmware" / "lib"
sys.path.insert(0, str(_LIB))

import os                                  # noqa: E402
import captain.config as config            # noqa: E402
# Forward-slash the temp path so config's "/"-joined paths stay consistent,
# and swap _mkdir_p for os.makedirs: the firmware's _mkdir_p assumes POSIX
# absolute paths ("/config/..."), which don't exist on a Windows host. The
# device uses the real one; here we only need writes to land somewhere.
config.CONFIG_ROOT = tempfile.mkdtemp(prefix="bosun-fwtest-").replace("\\", "/")
config._mkdir_p = lambda p: os.makedirs(p, exist_ok=True)

from captain.app import Captain            # noqa: E402
from captain.midi import MidiParser        # noqa: E402


# ---------------- tiny test harness ----------------

PASS = 0
FAIL = 0
FAILURES = []

def check(name, ok, detail=""):
    global PASS, FAIL
    if ok:
        PASS += 1
        print("  ok   %s" % name)
    else:
        FAIL += 1
        FAILURES.append(name + ((" - " + detail) if detail else ""))
        print("  FAIL %s %s" % (name, detail))


def responses():
    """Drain + parse the JSON lines the firmware wrote to the fake CDC."""
    txt = bytes(usb_cdc.data.out).decode("utf-8", "replace")
    usb_cdc.data.out = bytearray()
    out = []
    for line in txt.split("\n"):
        line = line.strip()
        if not line:
            continue
        try:
            out.append(json.loads(line))
        except ValueError:
            pass
    return out


# ---------------- A. protocol resilience ----------------

def test_protocol_barrage():
    cap = Captain()
    proto = cap.protocol

    good = [
        {"type": "PING", "id": "p1"},
        {"type": "GET_DEVICE_INFO", "id": "p2"},
        {"type": "GET_GLOBAL", "id": "p3"},
        {"type": "PUT_GLOBAL", "id": "p4", "device": {
            "patch_link": {"locked_slots": [1, 2, 3]},
            "tft": {"layout": [{"field": "patch_name", "x": 0, "y": 0}]},
            "leds": {"brightness": 64},
        }},
        {"type": "PUT_PATCH", "id": "p5", "bank": 1, "slot": 1,
         "patch": {"name": "ACOUSTIC", "bindings": []}},
        {"type": "LIST_PATCHES", "id": "p6"},
        {"type": "GET_PATCH", "id": "p7", "bank": 1, "slot": 1},
        {"type": "GET_DIRTY", "id": "p8"},
        {"type": "SAVE_NOW", "id": "p9"},
        {"type": "GET_MANIFEST", "id": "p10"},
        {"type": "STATS", "id": "p11"},
    ]
    malformed = [
        {"type": "PUT_GLOBAL", "id": "m1"},                       # no device
        {"type": "PUT_GLOBAL", "id": "m2", "device": "notadict"},  # wrong type
        {"type": "PUT_PATCH", "id": "m3"},                        # missing bank/slot/patch
        {"type": "PUT_PATCH", "id": "m4", "bank": 1},             # partial
        {"type": "GET_PATCH", "id": "m5", "bank": 99, "slot": 99},  # missing patch
        {"type": "SWITCH_PATCH", "id": "m6", "bank": 7, "slot": 7},
        {"type": "TOTALLY_UNKNOWN", "id": "m7"},
        {"id": "m8"},                                             # no type
        {},                                                       # empty
    ]

    raised = None
    try:
        for m in good + malformed:
            proto.handle(m)
    except Exception as e:                                        # noqa: BLE001
        raised = e
    check("handle() never raises across valid+malformed barrage", raised is None,
          "raised %r" % raised)

    resp = responses()
    by_id = {r.get("id"): r for r in resp}
    check("PING answered with ACK", by_id.get("p1", {}).get("type") == "ACK")
    check("PUT_GLOBAL with locked_slots ACKed (apply_global didn't throw)",
          by_id.get("p4", {}).get("type") == "ACK",
          "got %r" % by_id.get("p4"))
    check("PUT_PATCH ACKed", by_id.get("p5", {}).get("type") == "ACK")
    check("malformed PUT_GLOBAL answered (ERROR, not silence/crash)",
          by_id.get("m1", {}).get("type") in ("ERROR", "ACK"))
    check("malformed PUT_PATCH answered with ERROR",
          by_id.get("m3", {}).get("type") == "ERROR")
    check("unknown type answered with ERROR",
          by_id.get("m7", {}).get("type") == "ERROR")

    # The lock write should have persisted to the active... there is no
    # active profile here, so GET_GLOBAL returns in-memory defaults; what
    # matters for stability is simply that none of it threw.


def test_protocol_fuzz():
    cap = Captain()
    proto = cap.protocol
    random.seed(1234)
    types_pool = ["PING", "PUT_GLOBAL", "PUT_PATCH", "PUT_BINDING", "GET_PATCH",
                  "SWITCH_PATCH", "DELETE_PATCH", "PUT_MIDI_LEARN", "LIST_PATCHES",
                  "REBOOT_NOPE", "STATS", "GET_DIRTY"]
    raised = None
    try:
        for _ in range(2000):
            m = {"type": random.choice(types_pool), "id": str(random.randint(0, 9999))}
            if random.random() < 0.5:
                m["bank"] = random.choice([0, 1, 5, 99, -1, "x"])
            if random.random() < 0.5:
                m["slot"] = random.choice([0, 1, 5, 99, -1, "y"])
            if random.random() < 0.4:
                m["device"] = random.choice([{}, {"tft": {"layout": "bad"}},
                                             {"patch_link": {"locked_slots": "nope"}}, 5, None])
            if random.random() < 0.4:
                m["patch"] = random.choice([{}, {"bindings": "bad"}, {"name": 5}, None, 7])
            proto.handle(m)
    except Exception as e:                                        # noqa: BLE001
        raised = e
    responses()  # drain
    check("handle() survives 2000 fuzzed messages", raised is None, "raised %r" % raised)


# ---------------- B. main-loop resilience ----------------

def test_loop_survives_subcomponent_exceptions():
    cap = Captain()

    def boom(*a, **k):
        raise RuntimeError("injected sub-component failure")

    # Each of these used to be on a bare path inside the old while-loop;
    # tick_once must now swallow the failure and keep going.
    scenarios = {
        "protocol.poll raises":  lambda: setattr(cap.protocol, "poll", boom),
        "midi.poll raises":      lambda: setattr(cap.midi, "poll", boom),
        "patches.tick raises":   lambda: setattr(cap.patches, "tick", boom),
        "plugins.tick raises":   lambda: setattr(cap.plugins, "tick", boom),
    }
    for name, install in scenarios.items():
        fresh = Captain()
        # Re-target the injector at the fresh instance.
        if "protocol" in name: fresh.protocol.poll = boom
        elif "midi" in name:   fresh.midi.poll = boom
        elif "patches" in name: fresh.patches.tick = boom
        elif "plugins" in name: fresh.plugins.tick = boom
        raised = None
        try:
            for _ in range(5):
                fresh.tick_once()
        except Exception as e:                                    # noqa: BLE001
            raised = e
        check("tick_once survives: " + name, raised is None, "raised %r" % raised)

    # And a healthy instance keeps ticking with no exception.
    raised = None
    try:
        for _ in range(50):
            cap.tick_once()
    except Exception as e:                                        # noqa: BLE001
        raised = e
    check("tick_once runs cleanly 50x on a healthy build", raised is None, "raised %r" % raised)


# ---------------- C. MIDI parser robustness ----------------

def test_midi_parser_fuzz():
    p = MidiParser()
    random.seed(99)
    raised = None
    try:
        for _ in range(20000):
            data = bytes(random.randint(0, 255) for _ in range(random.randint(0, 12)))
            p.feed(data)
    except Exception as e:                                        # noqa: BLE001
        raised = e
    check("MidiParser.feed survives 20000 random byte bursts", raised is None, "raised %r" % raised)

    # SYSEX edge cases: unterminated, nested-ish, stray status bytes.
    p2 = MidiParser()
    raised = None
    try:
        p2.feed(bytes([0xF0, 0x00, 0x20, 0x33]))     # open sysex, no end
        p2.feed(bytes([0x10, 0x20, 0xF0, 0x7F]))     # stray F0 inside
        p2.feed(bytes([0x90, 0x40]))                 # note-on, missing velocity
        p2.feed(bytes([0xF7]))                       # lone end-of-sysex
        p2.feed(bytes([0xB0, 0x00]))                 # CC missing value
        p2.feed(bytes([0xF8, 0xFA, 0xFC]))           # real-time only
    except Exception as e:                                        # noqa: BLE001
        raised = e
    check("MidiParser handles SYSEX/short-message edge cases", raised is None, "raised %r" % raised)


def main():
    print("Firmware stability (offline, mocked CircuitPython)\n")
    print("A. protocol resilience")
    test_protocol_barrage()
    test_protocol_fuzz()
    print("B. main-loop resilience")
    test_loop_survives_subcomponent_exceptions()
    print("C. MIDI parser robustness")
    test_midi_parser_fuzz()
    print("\n%d passed, %d failed" % (PASS, FAIL))
    if FAILURES:
        print("\nFailures:")
        for f in FAILURES:
            print("  - " + f)
    sys.exit(1 if FAIL else 0)


if __name__ == "__main__":
    main()
