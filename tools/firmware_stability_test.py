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

# When source is deployed, CircuitPython compiles it on-device. On the
# Captain's RP2040 it must stay below this empirically verified ceiling or
# import fails despite enough total free heap (contiguous allocation limit).
# A precompiled deployment must exclude the sibling source to avoid that cost.
PLUGIN_SOURCE_BUDGET = 40_000


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

fourwire = _mod("fourwire")
class _FourWire:
    def __init__(self, *a, **k): pass
fourwire.FourWire = _FourWire

displayio = _mod("displayio")
class _Group:
    def __init__(self, *a, **k):
        self._c = []
        self.x = k.get("x", 0); self.y = k.get("y", 0)
        self.scale = k.get("scale", 1); self.hidden = False
    def append(self, x): self._c.append(x)
    def pop(self, index=-1): return self._c.pop(index)
    def __getitem__(self, index): return self._c[index]
    def __setitem__(self, index, value): self._c[index] = value
    def __len__(self): return len(self._c)
    def __iter__(self): return iter(self._c)
class _Bitmap:
    def __init__(self, w, h, n):
        self.w = self.width = w; self.h = self.height = h
    def __setitem__(self, k, v): pass
    def __getitem__(self, k): return 0
class _Palette:
    def __init__(self, n): self._c = [0] * n
    def __setitem__(self, k, v): self._c[k] = v
    def __getitem__(self, k): return self._c[k]
    def make_transparent(self, i): pass
class _TileGrid:
    def __init__(self, bitmap, **k):
        self.bitmap = bitmap
        self.x = k.get("x", 0); self.y = k.get("y", 0)
        self.width = k.get("width", 1); self.height = k.get("height", 1)
        self.pixel_shader = k.get("pixel_shader")
        self.hidden = False
        self._tiles = [k.get("default_tile", 0)] * (self.width * self.height)
    def __getitem__(self, index):
        return self._tiles[index if isinstance(index, int) else index[1] * self.width + index[0]]
    def __setitem__(self, index, value):
        self._tiles[index if isinstance(index, int) else index[1] * self.width + index[0]] = value
displayio.Group = _Group
displayio.Bitmap = _Bitmap
displayio.Palette = _Palette
displayio.TileGrid = _TileGrid
displayio.release_displays = lambda: None

class _BuiltinFont:
    bitmap = _Bitmap(6 * 95, 14, 2)
    def get_bounding_box(self): return (6, 14)
    def get_glyph(self, codepoint):
        if not 32 <= codepoint <= 126:
            return None
        return types.SimpleNamespace(bitmap=self.bitmap, tile_index=codepoint - 32,
                                     width=6, height=14, dx=0, dy=0, shift_x=6, shift_y=0)
terminalio.FONT = _BuiltinFont()

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
import captain.midi as captain_midi         # noqa: E402
from captain.midi import MidiParser        # noqa: E402
from plugins import kemper                  # noqa: E402


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


def drain_background(proto, max_steps=10000):
    """GET_MANIFEST/GET_GLOBAL stream via a resumable generator
    (protocol._start_background/pump_background - 2026-08-16) so a barrage
    of handle() calls with no tick_once() in between leaves it (and every
    _send() response queued behind its still-open wire line - see _send())
    unfinished. Drain it the same way _tick_body does, one step per call,
    before reading back what the firmware wrote."""
    steps = 0
    while proto._bg_gen is not None:
        proto.pump_background()
        steps += 1
        assert steps < max_steps, "background generator never finished"


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


def test_run_arms_the_circuitpython_watchdog_after_boot_and_feeds_completed_ticks():
    from unittest.mock import patch
    import captain.app as app_module

    class StopRun(BaseException):
        pass

    reset_mode = object()
    events = []

    class Timer:
        mode = None

        @property
        def timeout(self):
            return self._timeout

        @timeout.setter
        def timeout(self, value):
            if not 0 < value <= 8:
                raise ValueError("RP2040 timeout must be at most eight seconds")
            self._timeout = value
            events.append(("timeout", value))

        def feed(self):
            events.append(("feed", self.mode))

    timer = Timer()
    # Match the real API: the timer is in microcontroller, the enum is not.
    modules = {
        "microcontroller": types.SimpleNamespace(watchdog=timer),
        "watchdog": types.SimpleNamespace(WatchDogMode=types.SimpleNamespace(RESET=reset_mode)),
    }
    app = object.__new__(Captain)
    app._last_tick_ms = app_module._TICK_BUDGET_MS
    app.boot = lambda: events.append(("boot", timer.mode))
    turns = [0]

    def step():
        turns[0] += 1
        if turns[0] > 3:
            raise StopRun()
        events.append(("tick", timer.mode))

    app.tick_once = step
    with patch.dict(sys.modules, modules):
        try:
            app.run()
        except StopRun:
            pass
    check("run arms the real watchdog API only after boot and feeds completed ticks",
          events == [("boot", None), ("timeout", 8)]
          + [(kind, reset_mode) for _ in range(3) for kind in ("tick", "feed")],
          repr(events))

    events.clear()
    turns[0] = 0
    timer.mode = None
    with patch.dict(sys.modules, {"microcontroller": types.SimpleNamespace(watchdog=None)}):
        try:
            app.run()
        except StopRun:
            pass
    check("run remains usable when the board has no hardware watchdog",
          events == [("boot", None)] + [("tick", None)] * 3,
          repr(events))


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
    drain_background(proto)

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


def test_switch_press_survives_midi_stall():
    """A binding's on_enter chain sends CC; while that USB write is stalled
    (host endpoint buffer full), a footswitch on a DIFFERENT switch is
    pressed and released. Before poll_hook existed, the main loop only read
    pins once per tick - a press timed entirely inside that stall (up to the
    ~10ms retry budget in midi._tx_usb) was silently lost, matching the
    real-world "sometimes needs 2-3 tries" report. Proves it's captured
    (real production wiring: midi.poll_hook -> Captain._poll_switches_mid_op
    -> _pending_triggers) and fires on the next tick instead of vanishing."""
    class _StallingUsbOut:
        """write() returns 0 (buffer full) for the first `stall_calls`
        attempts, driving switch "2"'s pin through a full press/release
        cycle entirely within that window - never seen by a normal tick's
        scan, only by poll_hook's mid-stall re-polls."""

        def __init__(self, switch_two, stall_calls=4):
            self.switch_two = switch_two
            self.stall_calls = stall_calls
            self.calls = 0

        def write(self, data):
            self.calls += 1
            # Pressed for the first 2 calls, released from the 3rd on.
            self.switch_two.io.value = self.calls >= 3
            return 0 if self.calls <= self.stall_calls else len(data)

    class _RetryClock:
        """Deterministic replacement for captain.midi's time module.

        Windows may schedule a requested 0.5ms sleep 15-16ms later.  The old
        test then exhausted _tx_usb's real 10ms deadline after just one retry,
        so it never presented the two samples required by the 5ms debounce.
        That tests the host scheduler, not the Captain.  Advance both deadline
        checks and sleeps explicitly while still exercising the real retry
        loop and poll_hook.
        """

        def __init__(self):
            self.ns = 0

        def monotonic_ns(self):
            self.ns += 100_000
            return self.ns

        def sleep(self, seconds):
            self.ns += int(seconds * 1_000_000_000)

    def run_once(stall_calls):
        cap = Captain()
        cap.patches.put_patch(1, 1, {
            "name": "TEST",
            "bindings": [
                {"switch": "1", "mode": "tap",
                 "actions": {"press": {"messages": [{"type": "cc", "channel": 1, "cc": 20, "value": 127}]}}},
                {"switch": "2", "mode": "tap",
                 "actions": {"press": {"messages": [{"type": "cc", "channel": 1, "cc": 30, "value": 127}]}}},
            ],
        }, cap._now_ms())
        cap.switch_patch(1, 1, source="editor")
        switches_by_name = {sw.name: sw for sw in cap.switches.switches}

        # Debounce time is independent from the MIDI retry deadline. Each
        # mid-operation sample advances 6ms, just beyond DEBOUNCE_MS=5.
        switch_clock = [0]
        cap._now_ms = lambda: (
            switch_clock.__setitem__(0, switch_clock[0] + 6) or
            switch_clock[0]
        )
        usb_out = _StallingUsbOut(switches_by_name["2"], stall_calls)
        cap.midi.usb_out = usb_out

        fired = []
        orig_fire = cap._fire

        def spy_fire(name, action_key):
            fired.append((name, action_key))
            orig_fire(name, action_key)

        cap._fire = spy_fire

        # Switch 1 starts the CC write. Switch 2 performs a complete debounced
        # press/release during that write and is visible only to poll_hook.
        old_time = captain_midi.time
        captain_midi.time = _RetryClock()
        try:
            switches_by_name["1"].io.value = False
            cap.tick_once()                 # records switch 1 raw transition
            cap.tick_once()                 # commits it and enters _tx_usb
            switches_by_name["1"].io.value = True
        finally:
            captain_midi.time = old_time

        queued = any(name == "2" for name, _ in cap._pending_triggers)
        cap.midi.usb_out = None
        cap.tick_once()                     # drains _pending_triggers
        fired_next_tick = any(name == "2" for name, _ in fired)
        usb_cdc.data.out = bytearray()       # bound shared fake-CDC memory
        return queued, fired_next_tick, usb_out.calls

    # Exercise different stall lengths while staying within the deterministic
    # 10ms retry budget. This used to fail intermittently in roughly 1% of
    # runs on Windows; 250 scheduler-independent repetitions pin the behavior.
    queue_failures = []
    fire_failures = []
    for iteration in range(250):
        stall_calls = 4 + (iteration % 9)   # 4..12 zero-length writes
        queued, fired, calls = run_once(stall_calls)
        expected_calls = stall_calls + 1    # final successful write
        if not queued or calls != expected_calls:
            queue_failures.append((iteration, stall_calls, calls, queued))
        if not fired:
            fire_failures.append((iteration, stall_calls, calls))

    check("switch 2's mid-stall press queued in 250 deterministic runs",
          not queue_failures, "failures=%r" % queue_failures[:5])
    check("switch 2's queued press fired on the next tick in all runs",
          not fire_failures, "failures=%r" % fire_failures[:5])


def test_context_retries_failed_delivery():
    cap = Captain()
    cap.display_context = {"patch_name": "Lead"}
    results = [False, True]
    sent = []
    real_send = cap.protocol._send
    cap.protocol._send = lambda obj: sent.append(obj) or results.pop(0)
    try:
        cap._last_context_check_ms = 0
        cap._push_context(1000)
        check("failed CONTEXT is not marked delivered",
              getattr(cap, "_last_context_fp", None) is None)
        cap._push_context(1300)
        check("failed CONTEXT is retried and committed only on success",
              len(sent) == 2 and getattr(cap, "_last_context_fp", None) is not None)
    finally:
        cap.protocol._send = real_send


def test_patch_switch_notifies_plugins_before_public_event():
    cap = Captain()
    cap.patches.put_patch(1, 1, {"name": "TEST", "bindings": []},
                          cap._now_ms())
    order = []
    real_hook = cap.plugins.on_patch_switch_started
    real_event = cap.protocol.emit_event
    cap.plugins.on_patch_switch_started = (
        lambda app, source="editor", fire_on_enter=True:
        order.append(("hook", source, fire_on_enter,
                      app.current_bank, app.current_slot)))
    cap.protocol.emit_event = lambda event, **fields: order.append(("event", event))
    try:
        cap.switch_patch(1, 1, source="editor")
    finally:
        cap.plugins.on_patch_switch_started = real_hook
        cap.protocol.emit_event = real_event
    check("patch generation hook runs before patch_switched EVENT",
          len(order) >= 2
          and order[0] == ("hook", "editor", True, 1, 1)
          and order[-1] == ("event", "patch_switched"),
          "order=%r" % (order,))


def test_delete_active_patch_enters_blank_state_without_midi():
    cap = Captain()
    bank, slot = 97, 7
    device_expression = [{
        "jack": 1, "enabled": False,
        "message": {"type": "cc", "channel": 1, "cc": 7},
    }]
    cap.device["expression"] = device_expression
    active = {
        "name": "DELETE ME",
        "expression": [{
            "jack": 1,
            "message": {"type": "cc", "channel": 1, "cc": 11},
        }],
        "bindings": [{
            "switch": "A", "mode": "tap",
            "actions": {"press": {"messages": [
                {"type": "pc", "channel": 1, "program": 42},
            ]}},
        }],
    }
    cap.patches.put_patch(bank, slot, active, 10)
    cap.current_bank, cap.current_slot = bank, slot
    cap.current_patch = active
    cap.patches.protect(bank, slot)
    cap._reindex_patch()
    cap.display_context.update({
        "patch_name": active["name"], "bank": bank, "slot": slot,
        "preview": "NEXT",
    })
    cap._preview = {"bank": bank, "slot": slot, "until_ms": 999999,
                    "saved_context": {}}
    midi_runs = []
    expression_configs = []
    real_run = cap.runner.run
    real_configure = cap.expression.configure
    cap.runner.run = lambda action: midi_runs.append(action)
    cap.expression.configure = lambda cfg: expression_configs.append(cfg)
    try:
        cap.delete_patch(bank, slot)
    finally:
        cap.runner.run = real_run
        cap.expression.configure = real_configure

    check("deleting active patch clears runtime/config ghost",
          cap.current_patch is None
          and cap.display_context.get("patch_name") == ""
          and cap.display_context.get("bank") == bank
          and cap.display_context.get("slot") == slot
          and cap._binding_index == {}
          and cap._mode_index == {}
          and cap._preview is None
          and "preview" not in cap.display_context
          and not cap.patches.has(bank, slot)
          and cap.patches._protected_key is None
          and {"bank": bank, "slot": slot} not in cap.patches.dirty_ids()
          and expression_configs == [device_expression],
          "current=%r context=%r dirty=%r protected=%r" %
          (cap.current_patch, cap.display_context, cap.patches.dirty_ids(),
           cap.patches._protected_key))
    check("deleting active patch never selects or transmits another rig",
          midi_runs == [], "runner calls=%r" % (midi_runs,))


def test_delete_nonactive_patch_does_not_reload_current():
    cap = Captain()
    current_key = (96, 1)
    other_key = (96, 2)
    active = {"name": "KEEP ACTIVE", "bindings": [{
        "switch": "B", "mode": "tap", "actions": {},
    }]}
    cap.patches.put_patch(*current_key, active, 10)
    cap.patches.put_patch(*other_key,
                          {"name": "DELETE OTHER", "bindings": []}, 11)
    cap.current_bank, cap.current_slot = current_key
    cap.current_patch = active
    cap.patches.protect(*current_key)
    cap._reindex_patch()
    before_context = dict(cap.display_context)
    reloads = []
    real_reload = cap.reload_current_patch
    cap.reload_current_patch = lambda: reloads.append(True)
    try:
        cap.delete_patch(*other_key)
    finally:
        cap.reload_current_patch = real_reload

    check("deleting non-active patch preserves current object and indexes",
          cap.current_patch is active
          and cap._binding_index.get("B") is active["bindings"][0]
          and cap.patches.read(*current_key) is active
          and not cap.patches.has(*other_key)
          and cap.patches._protected_key == current_key
          and cap.display_context == before_context
          and reloads == [],
          "current=%r reloads=%r protected=%r" %
          (cap.current_patch, reloads, cap.patches._protected_key))


def test_delete_failure_keeps_active_runtime_and_skips_reload():
    cap = Captain()
    key = (95, 5)
    active = {"name": "SURVIVE EROFS", "bindings": []}
    cap.patches.put_patch(*key, active, 12)
    cap.current_bank, cap.current_slot = key
    cap.current_patch = active
    cap.patches.protect(*key)
    cap.display_context["patch_name"] = active["name"]
    reloads = []
    real_reload = cap.reload_current_patch
    real_remove = os.remove
    cap.reload_current_patch = lambda: reloads.append(True)
    os.remove = lambda _path: (_ for _ in ()).throw(
        OSError(30, "read-only filesystem"))
    raised = None
    try:
        cap.delete_patch(*key)
    except OSError as e:
        raised = e
    finally:
        os.remove = real_remove
        cap.reload_current_patch = real_reload

    check("failed active delete is fail-closed across store and Captain",
          raised is not None
          and cap.current_patch is active
          and cap.patches.read(*key) is active
          and cap.patches._protected_key == key
          and {"bank": key[0], "slot": key[1]} in cap.patches.dirty_ids()
          and cap.display_context.get("patch_name") == active["name"]
          and reloads == [],
          "raised=%r current=%r dirty=%r reloads=%r" %
          (raised, cap.current_patch, cap.patches.dirty_ids(), reloads))


def test_patch_switch_nav_avoids_inventory_before_midi():
    """Preset LEDs must not scan every patch before the target PC is sent.

    Model the real failure with an expensive metadata inventory.  Both the
    current patch and nav target deliberately exist only in PatchStore RAM so
    the test also proves has() preserves unsaved-patch navigation semantics.
    Duplicate nav targets must be probed only once per hot-path operation.
    """
    cap = Captain()
    cap.device["preset_navigation"] = {
        "switches": {"1": 2, "2": 2, "3": 3},
        "bank_colors": {"1": "#204060"},
    }
    cap.patches.put_patch(1, 1, {
        "name": "CURRENT",
        # Per-patch bindings still win over the preset-nav overlay.
        "bindings": [{
            "switch": "1", "mode": "tap",
            "actions": {"press": {"messages": [
                {"type": "cc", "channel": 1, "cc": 7, "value": 99},
            ]}},
        }],
        "on_enter": {"messages": [
            {"type": "pc", "channel": 1, "program": 17},
        ]},
    }, 0)
    cap.patches.put_patch(1, 2, {
        "name": "RAM ONLY", "bindings": [],
    }, 0)

    clock = [1000]
    timeline = []
    has_calls = []
    real_list = cap.patches.list
    real_has = cap.patches.has
    real_load_patch = config.load_patch
    real_send_pc = cap.midi.send_pc

    def slow_inventory():
        timeline.append(("inventory", clock[0]))
        clock[0] += 300
        return real_list()

    def slow_load_patch(bank, slot):
        timeline.append(("load_patch", clock[0], bank, slot))
        clock[0] += 300
        return real_load_patch(bank, slot)

    def traced_has(bank, slot):
        has_calls.append((bank, slot))
        return real_has(bank, slot)

    cap._now_ms = lambda: clock[0]
    cap.patches.list = slow_inventory
    cap.patches.has = traced_has
    config.load_patch = slow_load_patch
    cap.midi.send_pc = lambda channel, program: timeline.append(
        ("midi_pc", clock[0], channel, program))
    try:
        switched = cap.switch_patch(1, 1, source="editor")
    finally:
        cap.patches.list = real_list
        cap.patches.has = real_has
        config.load_patch = real_load_patch
        cap.midi.send_pc = real_send_pc

    nav_binding = cap._binding_index.get("2") or {}
    switch_one = cap._binding_index.get("1") or {}
    check("patch switch sends on_enter PC without full inventory or JSON loads",
          switched and timeline == [("midi_pc", 1000, 1, 17)]
          and cap.last_patch_switch_duration_ms == 0,
          "timeline=%r duration=%r" %
          (timeline, cap.last_patch_switch_duration_ms))
    check("preset nav probes each distinct target once per reindex/paint",
          has_calls.count((1, 1)) == 1
          and has_calls.count((1, 2)) == 2
          and has_calls.count((1, 3)) == 2
          and len(has_calls) == 5,
          "has_calls=%r" % (has_calls,))
    check("RAM-only preset target remains bound and painted",
          nav_binding.get("actions", {}).get("press", {}).get("messages") == [{
              "type": "captain_patch", "bank": 1, "slot": 2,
          }]
          and cap.leds.strip[3] != (0, 0, 0),
          "binding=%r led=%r" % (nav_binding, cap.leds.strip[3]))
    check("per-patch binding still overrides preset navigation",
          switch_one.get("actions", {}).get("press", {}).get("messages", [{}])[0].get("type") == "cc",
          "binding=%r" % (switch_one,))


def test_display_memory_failure_retries_without_another_rig_change():
    cap = Captain()
    now = [1000]
    cap._now_ms = lambda: now[0]
    cap._splash_until_ms = 0
    attempts = []

    def render(context, layout):
        attempts.append(now[0])
        if len(attempts) == 1:
            raise MemoryError("temporary layout allocation")

    cap.display.render = render
    cap.midi.poll = lambda: []
    cap._refresh_due_ms = now[0]
    cap._refresh_not_before_ms = 0
    cap._tick_body()
    retry = cap._refresh_due_ms
    check("failed TFT frame schedules a bounded retry",
          attempts == [1000] and retry == 1250
          and cap._refresh_not_before_ms == retry)
    now[0] = retry - 1
    cap._tick_body()
    check("TFT memory retry does not spin while waiting",
          attempts == [1000])
    now[0] = retry
    cap._tick_body()
    check("TFT retries on an idle rig and clears pending after success",
          attempts == [1000, 1250] and cap._refresh_due_ms == 0)


def test_patch_switch_tft_waits_for_initial_midi_window():
    """A rig switch must leave time to drain the first MIDI reply burst.

    Before this guard, an old `_last_midi_in_ms` made the quiet predicate true
    immediately: the blocking TFT render ran in the same tick as the outbound
    PC, before the Kemper had even had 40 ms in which to answer.
    """
    cap = Captain()
    now = [1000]
    cap._now_ms = lambda: now[0]
    cap.patches.put_patch(1, 1, {
        "name": "CLEAN", "bindings": [],
        "on_enter": {"messages": [
            {"type": "pc", "channel": 1, "program": 0},
        ]},
    }, now[0])
    timeline = []
    cap.midi.send_pc = lambda channel, program: timeline.append(
        ("pc", now[0]))
    cap.display.render = lambda context, layout: timeline.append(
        ("render", now[0]))
    cap.midi.poll = lambda: []

    cap.switch_patch(1, 1, source="editor")
    cap._tick_body()
    check("TFT does not render in the Program Change tick",
          timeline == [("pc", 1000)], "timeline=%r" % (timeline,))

    now[0] = 1039
    cap._tick_body()
    check("TFT stays deferred through the first 39ms reply window",
          timeline == [("pc", 1000)], "timeline=%r" % (timeline,))

    now[0] = 1040
    cap._tick_body()
    check("TFT renders once the initial 40ms window is quiet",
          timeline == [("pc", 1000), ("render", 1040)],
          "timeline=%r" % (timeline,))

    # A reply at +20 ms restarts the genuine MIDI-quiet interval. The initial
    # lower bound alone is not enough: render only 40 ms after that last frame.
    timeline[:] = []
    now[0] = 2000
    cap.update_context({"phase": "reply-window"})
    batches = [[("usb", 1, 0xF8, [])], [], [], []]
    cap.midi.poll = lambda: batches.pop(0)
    for tick_ms in (2020, 2040, 2059):
        now[0] = tick_ms
        cap._tick_body()
    check("incoming MIDI extends the quiet gate beyond the initial window",
          timeline == [], "timeline=%r" % (timeline,))
    now[0] = 2060
    cap._tick_body()
    check("TFT renders 40ms after the last incoming frame",
          timeline == [("render", 2060)], "timeline=%r" % (timeline,))

    # Repeated display updates move the quiet shortcut but must not move the
    # original 250 ms hard cap; a continuous stream cannot starve the screen.
    timeline[:] = []
    cap.midi.poll = lambda: []
    now[0] = 3000
    cap.update_context({"stream": 0})
    for tick_ms in (3030, 3060, 3090, 3120, 3150, 3180, 3210, 3240):
        now[0] = tick_ms
        cap.update_context({"stream": tick_ms})
        cap._tick_body()
    check("continuous updates remain deferred before the original hard cap",
          timeline == [], "timeline=%r due=%r" %
          (timeline, cap._refresh_due_ms))
    now[0] = 3250
    cap.update_context({"stream": 3250})
    cap._tick_body()
    check("250ms hard cap still forces a render under continuous updates",
          timeline == [("render", 3250)], "timeline=%r" % (timeline,))

    # Tuner cadence remains 30 ms. Its deadline intentionally wins over the
    # generic 40 ms quiet opportunity, exactly as it did before this fix.
    timeline[:] = []
    now[0] = 4000
    cap.update_context({"tuner": "on"})
    now[0] = 4029
    cap._tick_body()
    check("tuner redraw stays deferred for its first 29ms",
          timeline == [], "timeline=%r" % (timeline,))
    now[0] = 4030
    cap._tick_body()
    check("tuner keeps its 30ms refresh cadence",
          timeline == [("render", 4030)], "timeline=%r" % (timeline,))


def test_real_patch_switch_hides_stale_kemper_blocks():
    """Cross-component regression for the real ACOUSTIC -> CLEAN flash."""
    cap = Captain()
    cap.plugins.register(kemper)
    cap.device["kemper"] = {}
    cap.patches.put_patch(1, 1, {"name": "CLEAN", "bindings": []},
                          cap._now_ms())
    cap.display_context.update({
        "kemper_block_X": "on", "kemper_block_Reverb": "on",
    })
    cap._pending_context_updates.update({
        "kemper_block_X": "on", "kemper_block_Reverb": "on",
    })
    cap.switch_patch(1, 1, source="editor")
    stale_public = [k for k in cap.display_context
                    if k.startswith("kemper_block_")]
    stale_pending = [k for k in cap._pending_context_updates
                     if k.startswith("kemper_block_")]
    check("real patch switch hides old Kemper blocks from GET_CONTEXT",
          not stale_public and not stale_pending,
          "context=%r pending=%r" %
          (stale_public, stale_pending))


def test_delayed_kemper_pc_confirmation_does_not_reload_real_captain():
    """Real trace: local B1/R1, C=on, then matching PC after 2342 ms."""
    state = kemper._BIDIR_STATE
    state.update({
        "published": {}, "settle_until_ms": 0, "generation": 0,
        "target_rig": None, "reconcile_generation": 0,
        "reconcile_pending": (), "reconcile_fallback_ms": 0,
        "reconcile_attempt": 0, "reconcile_queried": (),
        "query_retire_ms": 0, "orphan_blocks": (),
        "orphan_until_ms": 0, "pending_name": "",
        "pending_name_ms": 0, "pending_name_generation": 0,
        "query_guard_expire_ms": 0, "awaiting_local_pc": None,
        "orphan_local_pcs": (),
    })
    kemper._BLOCK_STATE.clear()
    kemper._BLOCK_GENERATION.clear()
    kemper._QUERY_GUARDS.clear()

    cap = Captain()
    cap.plugins.register(kemper)
    cap.device["kemper"] = {}
    cap.device["midi_channel"] = 1
    patch = {
        "name": "ACOUSTIC",
        "bindings": [{
            "switch": "4", "mode": "latched",
            "actions": {"toggle_on": {"messages": [{
                "type": "kemper_effect_toggle", "slot": "C",
                "value": "on", "channel": 1,
            }]}},
        }],
        "on_enter": {"messages": [{
            "type": "kemper_rig", "bank": 1, "rig": 1, "channel": 1,
        }]},
    }
    now = [1000]
    cap._now_ms = lambda: now[0]
    cap.patches.put_patch(1, 1, patch, now[0])
    events = []
    real_event = cap.protocol.emit_event
    cap.protocol.emit_event = lambda event, **fields: events.append(
        (event, fields.get("source")))
    try:
        cap.switch_patch(1, 1, source="editor")
        generation = state["generation"]
        now[0] = 1100
        cap._handle_midi_in("usb", 1, 0xB0, [19, 127])
        now[0] = 1500
        kemper.tick(cap, now[0])
        now[0] = 1510
        cap._handle_midi_in("usb", 1, 0xB0, [19, 127])
        before = dict(cap.display_context)

        now[0] = 3342
        cap._handle_midi_in("usb", 1, 0xC0, [0])
    finally:
        cap.protocol.emit_event = real_event

    check("2342ms Kemper PC echo emits no second patch_switched event",
          events == [("patch_switched", "editor")]
          and state["generation"] == generation,
          "events=%r generation=%r->%r" %
          (events, generation, state["generation"]))
    check("2342ms Kemper PC echo preserves current C/HARM context",
          before.get("kemper_block_C") == "on"
          and cap.display_context.get("kemper_block_C") == "on"
          and state.get("awaiting_local_pc") is None,
          "before=%r after=%r state=%r" %
          (before, cap.display_context, state))


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


def test_plugin_source_heap_budget():
    import importlib.util
    from unittest.mock import patch

    plugin = Path(__file__).resolve().parent.parent / "firmware" / "lib" / "plugins" / "kemper.py"
    runtime_lib = plugin.parent.parent
    captain_dir = runtime_lib / "captain"
    # Use the deployment enumerator itself, without requiring pyserial or
    # opening a transport. Its selection decides whether source compilation
    # can occur on the Captain; the checked-out .py size alone does not.
    push_path = Path(__file__).resolve().with_name("push_firmware.py")
    spec = importlib.util.spec_from_file_location("stability_push_firmware", push_path)
    push_module = importlib.util.module_from_spec(spec)
    with patch.dict(sys.modules, {"serial": types.SimpleNamespace(Serial=object)}):
        spec.loader.exec_module(push_module)
    deployed = {dst for _, dst in push_module.collect_files(runtime_lib.parent, None)}
    source_deployed = "/lib/plugins/kemper.py" in deployed
    compiled_deployed = "/lib/plugins/kemper.mpy" in deployed
    size = plugin.stat().st_size
    check("deployed Kemper plugin avoids the Captain source compile heap limit",
          size <= PLUGIN_SOURCE_BUDGET if source_deployed else compiled_deployed,
          "kemper.py=%d bytes, budget=%d, source deployed=%r, bytecode deployed=%r"
          % (size, PLUGIN_SOURCE_BUDGET, source_deployed, compiled_deployed))
    compiled = plugin.with_suffix(".mpy")
    data = compiled.read_bytes() if compiled.exists() else b""
    check("Kemper ships CircuitPython mpy-v6 bytecode for the Captain",
          len(data) > 4 and data[0] == ord("C") and data[1] == 6,
          "missing/incompatible firmware/lib/plugins/kemper.mpy")
    app_compiled = plugin.parent.parent / "captain" / "app.mpy"
    app_data = app_compiled.read_bytes() if app_compiled.exists() else b""
    check("Captain app ships CircuitPython mpy-v6 bytecode",
          len(app_data) > 4 and app_data[0] == ord("C") and app_data[1] == 6,
          "missing/incompatible firmware/lib/captain/app.mpy")

    protocol_path = captain_dir / "protocol.py"
    manifest_dynamic_path = captain_dir / "manifest_dynamic.py"
    protocol_source = protocol_path.read_text()
    manifest_dynamic_source = manifest_dynamic_path.read_text()
    check("RX parser avoids unsupported CircuitPython bytearray slice deletion",
          "del self._rx_buf[" not in protocol_source,
          "protocol.py uses bytearray slice deletion unsupported by CircuitPython 9.2.7")
    runtime_sources = sorted(
        list(captain_dir.glob("*.py")) + list(plugin.parent.glob("*.py"))
        + list(runtime_lib.glob("captain_*.py")))
    compiled_sources = [path for path in runtime_sources
                        if path.name != "__init__.py"]
    check("Captain runtime inventory has 20 precompiled source modules",
          len(runtime_sources) == 22 and len(compiled_sources) == 20
          and runtime_lib / "captain_ota.py" in compiled_sources,
          "runtime sources=%d, precompiled sources=%d; update the pinned mpy inventory"
          % (len(runtime_sources), len(compiled_sources)))
    check("dynamic MANIFEST fallback is isolated in one lazy runtime module",
          (manifest_dynamic_path in compiled_sources and
           "def _get_dynamic_manifest_gen" not in protocol_source and
           protocol_source.count("from . import manifest_dynamic") == 1 and
           "def get_manifest_gen" in manifest_dynamic_source and
           not manifest_dynamic_source.lstrip().startswith(("import ", "from "))),
          "protocol.py must lazy-import captain/manifest_dynamic.py only on fallback")

    check("full firmware deploy prefers mpy and skips its sibling source",
          compiled_deployed and not source_deployed,
          "push_firmware.py would restore kemper.py and override the compiled module")


def main():
    print("Firmware stability (offline, mocked CircuitPython)\n")
    print("A. protocol resilience")
    test_protocol_barrage()
    test_protocol_fuzz()
    print("B. main-loop resilience")
    test_run_arms_the_circuitpython_watchdog_after_boot_and_feeds_completed_ticks()
    test_loop_survives_subcomponent_exceptions()
    test_switch_press_survives_midi_stall()
    test_context_retries_failed_delivery()
    test_patch_switch_notifies_plugins_before_public_event()
    test_delete_active_patch_enters_blank_state_without_midi()
    test_delete_nonactive_patch_does_not_reload_current()
    test_delete_failure_keeps_active_runtime_and_skips_reload()
    test_patch_switch_nav_avoids_inventory_before_midi()
    test_patch_switch_tft_waits_for_initial_midi_window()
    test_display_memory_failure_retries_without_another_rig_change()
    test_real_patch_switch_hides_stale_kemper_blocks()
    test_delayed_kemper_pc_confirmation_does_not_reload_real_captain()
    print("C. MIDI parser robustness")
    test_midi_parser_fuzz()
    print("D. CircuitPython deployment constraints")
    test_plugin_source_heap_budget()
    print("\n%d passed, %d failed" % (PASS, FAIL))
    if FAILURES:
        print("\nFailures:")
        for f in FAILURES:
            print("  - " + f)
    sys.exit(1 if FAIL else 0)


if __name__ == "__main__":
    main()
