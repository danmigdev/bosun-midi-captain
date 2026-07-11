#!/usr/bin/env python3
"""Endurance / leak soak test - no hardware, no CircuitPython.

Builds the REAL captain.app.Captain against a mocked CircuitPython surface,
seeds a realistic Kemper profile (patches with bindings + on_enter/on_exit,
preset navigation, two enabled expression jacks), then drives a compressed
"hours of gigging" workload for many iterations:

  - footswitch presses/releases (FSM debounce, latched toggles, long-press),
  - inbound MIDI: rig-change PCs, effect-block on/off, tuner on/off (drives the
    Kemper bidirectional publish cache + display context + LED mirroring),
  - continuous expression-pedal sweeps (the run_message dispatch path),
  - editor protocol traffic (STATS, GET_PATCH, PUT_BINDING) over a fake CDC,
  - occasional malformed MIDI / protocol input.

A fake monotonic clock is advanced ~20 ms per iteration, so the default
60k iterations simulate ~20 minutes and `--long` (600k) simulates ~3.3 hours.

It then asserts the firmware is LEAK-FREE and UNBREAKABLE over that run:
  - no exception ever escaped a tick or an injection,
  - the live Python object count did not grow unbounded,
  - every long-lived dict/list/set the engine and the Kemper plugin keep
    (bank trackers, marquee list, dirty set, block-state / published caches,
    binding index) stayed bounded - none grows per-event.

Usage:
    python tools/soak_test.py [--long] [--iters N]
"""

import gc
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
    def __init__(self, *a, **k):
        # A label wide enough that a scrolling patch_name registers in _scroll,
        # so the marquee path is exercised (and watched for leaks).
        self.x = 0
        self._ap = k.get("anchor_point")
        self.anchor_point = k.get("anchor_point")
        self.anchored_position = k.get("anchored_position")
    @property
    def bounding_box(self): return (0, 0, 400, 20)
_label.Label = _Label
_adt.label = _label

_bf = _mod("adafruit_bitmap_font")
_bfm = _mod("adafruit_bitmap_font.bitmap_font")
_bfm.load_font = lambda p: terminalio.FONT
_bf.bitmap_font = _bfm

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

# Feedable CDC so we can inject editor protocol commands mid-run.
usb_cdc = _mod("usb_cdc")
class _CDC:
    def __init__(self):
        self.connected = True
        self.write_timeout = None
        self.out = bytearray()
        self._in = bytearray()
    @property
    def in_waiting(self): return len(self._in)
    def read(self, n): chunk = bytes(self._in[:n]); del self._in[:n]; return chunk
    def write(self, b): self.out.extend(b); return len(b)
    def feed(self, obj): self._in.extend((json.dumps(obj) + "\n").encode())
usb_cdc.data = _CDC()
usb_cdc.console = None

# analogio so the expression jacks are live.
analogio = _mod("analogio")
class _AnalogIn:
    registry = {}
    def __init__(self, pin): self.pin = pin; self.value = 0; _AnalogIn.registry[pin] = self
    def deinit(self): pass
analogio.AnalogIn = _AnalogIn

# CircuitPython-only gc members so Captain.stats() runs fully (incl.
# expression.stats()); values are placeholders - leak detection uses
# len(gc.get_objects()), not these.
if not hasattr(gc, "mem_free"):
    gc.mem_free = lambda: 100000
if not hasattr(gc, "mem_alloc"):
    gc.mem_alloc = lambda: 100000


# ---------------- import the real firmware against a temp /config ----------------

_LIB = Path(__file__).resolve().parent.parent / "firmware" / "lib"
sys.path.insert(0, str(_LIB))

import os                                       # noqa: E402
import captain.config as config                 # noqa: E402
config.CONFIG_ROOT = tempfile.mkdtemp(prefix="bosun-soak-").replace("\\", "/")
config._mkdir_p = lambda p: os.makedirs(p, exist_ok=True)


def _seed_profile():
    config.create_profile("soak", "Soak", "kemper_player")
    dev = config.load_device()
    dev["kemper"] = {"enabled": True, "midi_channel": 1,
                     "auto_follow_effects": True, "auto_follow_rig": True}
    dev["midi_channel"] = 1
    dev["expression"] = [
        {"jack": 1, "enabled": True, "invert": False,
         "calibration": {"min": 0, "max": 65535}, "curve": "linear",
         "message": {"type": "cc", "channel": 1, "cc": 11, "value": 0}},
        {"jack": 2, "enabled": True, "invert": False,
         "calibration": {"min": 0, "max": 65535}, "curve": "linear",
         "message": {"type": "kemper_wah", "channel": 1, "value": 0}},
    ]
    dev["preset_navigation"] = {
        "switches": {"1": 1, "2": 2, "3": 3, "4": 4},
        "bank_colors": {"1": "#3355ff"}, "dim_factor": 4,
    }
    dev["tft"]["layout"] = [
        {"field": "patch_name", "x": 0, "y": 0, "size": 5,
         "color": "#ffffff", "scroll": True},
        {"field": "kemper_rig", "x": 0, "y": 120, "size": 5, "color": "#6fd99b"},
    ]
    config.save_device(dev)
    for slot in range(1, 6):
        config.save_patch(1, slot, {
            "name": "LONG RIG NAME %d THAT OVERFLOWS" % slot,
            "bindings": [
                {"switch": "A", "mode": "latched",
                 "actions": {
                     "toggle_on":  {"messages": [{"type": "kemper_effect_toggle", "slot": "A", "value": "on",  "channel": 1}]},
                     "toggle_off": {"messages": [{"type": "kemper_effect_toggle", "slot": "A", "value": "off", "channel": 1}]}},
                 "led": {"on": "#ff0000", "off": "#330000"}},
                {"switch": "B", "mode": "tap",
                 "actions": {"press": {"messages": [{"type": "kemper_tap_tempo", "channel": 1}]}}},
            ],
            "on_enter": {"messages": [{"type": "kemper_rig", "bank": 1, "rig": slot, "channel": 1}]},
            "on_exit":  {"messages": [{"type": "cc", "channel": 1, "cc": 50, "value": slot}]},
        })


_seed_profile()

from captain.app import Captain                 # noqa: E402
import plugins.kemper as kemper                  # noqa: E402


# ---------------- harness ----------------

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


def _sizes(cap):
    """Snapshot of every long-lived structure that could leak per-event."""
    bidir = getattr(kemper, "_BIDIR_STATE", {})
    return {
        "objects":       len(gc.get_objects()),
        "bank_msb":      len(cap._last_bank_msb),
        "bank_lsb":      len(cap._last_bank_lsb),
        "scroll":        len(cap.display._scroll),
        "dirty":         len(cap.patches.dirty_ids()),
        "binding_index": len(cap._binding_index),
        "mode_index":    len(cap._mode_index),
        "block_state":   len(getattr(kemper, "_BLOCK_STATE", {})),
        "published":     len(bidir.get("published", {})),
        "cdc_in":        len(usb_cdc.data._in),
    }


def run_soak(iters):
    global _now
    cap = Captain()
    # Captain.__init__ ran discover() against the device's absolute
    # "/lib/plugins" path, which does not exist on the host - so no plugin
    # registered. Re-run discovery against the real repo path so the Kemper
    # bidirectional path (the most cache-heavy, most leak-prone code) is
    # actually exercised by the inbound-MIDI workload below.
    cap.plugins.discover(str(_LIB / "plugins"))
    clock = [0]
    cap._now_ms = lambda: clock[0]              # deterministic, fast-forwardable

    random.seed(4321)
    switch_names = [sw.name for sw in cap.switches.switches]
    pressed = {}                                # name -> release_at_ms
    injection_failures = []
    tick_failures = []

    def inject(fn, label):
        try:
            fn()
        except Exception as e:                  # noqa: BLE001
            injection_failures.append("%s: %r" % (label, e))

    WARMUP = min(3000, iters // 4)
    baseline = None
    peak = {}
    exp_pins = ["GP27", "GP28"]

    for i in range(iters):
        clock[0] += 20                          # ~20 ms per loop -> hours over the run
        now = clock[0]

        # Expression sweep: slow triangle on both jacks -> continuous
        # run_message dispatch (cc + kemper_wah).
        tri = i % 256
        val = tri if tri < 128 else 255 - tri
        for pin in exp_pins:
            a = _AnalogIn.registry.get(pin)
            if a is not None:
                a.value = int(val / 255 * 65535)

        # Footswitch activity: occasionally press a switch, and release it a
        # little later, so latched toggles / long-press windows all fire.
        if i % 6 == 0:
            name = random.choice(switch_names)
            for sw in cap.switches.switches:
                if sw.name == name:
                    sw.io.value = False         # LOW = pressed
                    pressed[name] = now + random.choice([60, 120, 700])
                    break
        for name, rel in list(pressed.items()):
            if now >= rel:
                for sw in cap.switches.switches:
                    if sw.name == name:
                        sw.io.value = True
                        break
                del pressed[name]

        # Inbound MIDI from the "Player": rig changes, block on/off, tuner,
        # and a dash of garbage. Called the same way tick_once would, wrapped
        # so a bug surfaces as a counted failure rather than a crash.
        if i % 3 == 0:
            # Uniform across all branches (i%9 under an i%3 gate only ever
            # yields 0/3/6, which would silently skip the tuner and bank paths).
            r = random.randint(0, 8)
            if r == 0:
                inject(lambda: cap._handle_midi_in("usb", 1, 0xC0, [random.randint(0, 124)]), "pc")
            elif r == 1:
                inject(lambda: cap._handle_midi_in("usb", 1, 0xB0, [31, 127]), "tuner_on")
            elif r == 2:
                inject(lambda: cap._handle_midi_in("usb", 1, 0xB0, [31, 0]), "tuner_off")
            elif r == 3:
                inject(lambda: cap._handle_midi_in("usb", 1, 0xB0, [17, random.randint(0, 127)]), "block_cc")
            elif r == 4:
                inject(lambda: cap._handle_midi_in("usb", 1, 0xB0, [0, random.randint(0, 3)]), "bank_msb")
            elif r == 5:
                inject(lambda: cap._handle_midi_in("usb", 1, 0xB0, [32, random.randint(0, 3)]), "bank_lsb")
            else:
                inject(lambda: cap._handle_midi_in("usb", 1, 0xF0, [random.randint(0, 255) for _ in range(6)]), "garbage")

        # Editor protocol traffic over the fake CDC.
        if i % 11 == 0:
            usb_cdc.data.feed({"type": "STATS", "id": "s%d" % i})
        elif i % 17 == 0:
            usb_cdc.data.feed({"type": "GET_PATCH", "id": "g%d" % i, "bank": 1, "slot": random.randint(1, 5)})
        elif i % 23 == 0:
            usb_cdc.data.feed({"type": "PUT_BINDING", "id": "b%d" % i, "bank": 1, "slot": 1,
                               "binding": {"switch": "C", "mode": "tap",
                                           "actions": {"press": {"messages": [{"type": "cc", "channel": 1, "cc": 80, "value": i % 128}]}}}})

        try:
            cap.tick_once()
        except Exception as e:                   # noqa: BLE001
            tick_failures.append("%d: %r" % (i, e))

        # Keep the outbound buffer from growing (the editor would be draining
        # it); leaving it would masquerade as a leak.
        if len(usb_cdc.data.out) > 8192:
            usb_cdc.data.out = bytearray()

        if i == WARMUP:
            gc.collect()
            baseline = _sizes(cap)
            peak = dict(baseline)
        elif baseline is not None:
            cur = _sizes(cap)
            for k, v in cur.items():
                if v > peak[k]:
                    peak[k] = v

    gc.collect()
    final = _sizes(cap)

    print("  baseline (after warmup): %s" % baseline)
    print("  peak during run:         %s" % peak)
    print("  final:                   %s" % final)

    # --- assertions ---
    check("no tick_once ever raised", not tick_failures,
          "%d failures, first: %s" % (len(tick_failures), tick_failures[0] if tick_failures else ""))
    check("no MIDI injection ever raised", not injection_failures,
          "%d failures, first: %s" % (len(injection_failures), injection_failures[0] if injection_failures else ""))

    # Bounded structures: keyed by a tiny fixed domain, must never grow with
    # the number of events processed.
    for k in ("bank_msb", "bank_lsb", "scroll", "dirty", "binding_index",
              "mode_index", "block_state", "published", "cdc_in"):
        check("bounded: %s does not grow with events (peak %d)" % (k, peak[k]),
              peak[k] <= baseline[k] + 32, "baseline %d peak %d" % (baseline[k], peak[k]))

    # Live object count: allow churn but not unbounded growth. Over hundreds of
    # thousands of events a true per-event leak would blow far past this.
    grew = final["objects"] - baseline["objects"]
    allowed = int(baseline["objects"] * 0.10) + 4000
    check("live object count stays bounded over the run (grew %d, allow %d)" % (grew, allowed),
          grew <= allowed, "baseline %d final %d" % (baseline["objects"], final["objects"]))


def main():
    iters = 60000
    if "--long" in sys.argv:
        iters = 600000
    if "--iters" in sys.argv:
        iters = int(sys.argv[sys.argv.index("--iters") + 1])
    print("Soak / endurance test: %d iterations (~%d simulated minutes)\n"
          % (iters, iters * 20 // 60000))
    run_soak(iters)
    print("\n%d passed, %d failed" % (PASS, FAIL))
    if FAILURES:
        print("\nFailures:")
        for f in FAILURES:
            print("  - " + f)
    sys.exit(1 if FAIL else 0)


if __name__ == "__main__":
    main()
