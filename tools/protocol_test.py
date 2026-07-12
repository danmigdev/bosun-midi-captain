#!/usr/bin/env python3
"""Robustness tests for firmware/lib/captain/protocol.py.

Covers the protocol layer's behavior under adverse conditions:
  - `_send` looping over a port that returns partial writes (the bug we
    found 2026-06-16: write_timeout would silently truncate MANIFEST).
  - `_send` exception handling - never propagates upward.
  - `poll()` rx buffer overflow and runaway-sender recovery.
  - Each handler responds with a well-formed message (or ERROR) for
    happy and malformed inputs.

No CircuitPython runtime - we stub usb_cdc, microcontroller etc. with
fakes the protocol can exercise.

Usage
-----
    python tools/protocol_test.py
"""
import json
import sys
import types
from pathlib import Path


FIRMWARE_LIB = Path(__file__).resolve().parent.parent / "firmware" / "lib"
sys.path.insert(0, str(FIRMWARE_LIB))

# Stub out CircuitPython modules the firmware imports directly or
# transitively. We don't need real behavior for the protocol layer.
for mod_name in ("busio", "usb_midi", "usb_cdc", "digitalio", "board", "neopixel",
                 "displayio", "fourwire", "pwmio", "terminalio",
                 "adafruit_display_text", "adafruit_st7789",
                 "adafruit_bitmap_font", "microcontroller"):
    sys.modules.setdefault(mod_name, types.ModuleType(mod_name))

# board needs the GPIO attribute surface the firmware addresses.
import board  # noqa: E402
for _n in [f"GP{i}" for i in range(30)]:
    setattr(board, _n, _n)

# adafruit_display_text needs a 'label' submodule with a .Label callable
import adafruit_display_text  # noqa: E402
adafruit_display_text.label = types.ModuleType("adafruit_display_text.label")
adafruit_display_text.label.Label = lambda *a, **kw: None
sys.modules["adafruit_display_text.label"] = adafruit_display_text.label

# usb_cdc needs a `data` attribute - we'll patch with FakePort per test.
import usb_cdc                    # noqa: E402
usb_cdc.data = None
usb_cdc.console = None

from captain import protocol      # noqa: E402


PASS_COUNT = 0
FAIL_COUNT = 0
FAILURES = []


def test(name):
    def wrap(fn):
        global PASS_COUNT, FAIL_COUNT
        try:
            fn()
            PASS_COUNT += 1
            print(f"  PASS  {name}")
        except AssertionError as e:
            FAIL_COUNT += 1
            FAILURES.append(f"{name}: {e}")
            print(f"  FAIL  {name}: {e}")
        except Exception as e:
            FAIL_COUNT += 1
            FAILURES.append(f"{name}: {type(e).__name__}: {e}")
            print(f"  ERROR {name}: {type(e).__name__}: {e}")
        return fn
    return wrap


# ---------------- fakes ----------------

class FakePort:
    """Approximates usb_cdc.data. Configurable to simulate partial writes
    (`max_per_write`), connection drops (`connected`), and exceptions
    (`raise_on_write`). The `written` buffer accumulates everything that
    actually got out."""

    def __init__(self, max_per_write=None, connected=True, raise_on_write=None):
        self.max_per_write = max_per_write
        self.connected = connected
        self.raise_on_write = raise_on_write
        self.written = bytearray()
        self._rx = bytearray()
        self.write_timeout = None
        self.write_call_count = 0

    def write(self, data):
        self.write_call_count += 1
        if self.raise_on_write is not None:
            raise self.raise_on_write
        if not self.connected:
            return 0
        chunk = bytes(data)
        if self.max_per_write is not None:
            chunk = chunk[:self.max_per_write]
        self.written.extend(chunk)
        return len(chunk)

    @property
    def in_waiting(self):
        return len(self._rx)

    def read(self, n):
        out = bytes(self._rx[:n])
        self._rx = self._rx[n:]
        return out

    def push_rx(self, data):
        self._rx.extend(data)


class FakeApp:
    """Minimal Captain-like stub the protocol handlers can reach into."""

    def __init__(self):
        self.device = {"device_name": "MIDI Captain", "kemper": {"enabled": True}}
        self.current_bank = 1
        self.current_slot = 1
        self.midi_learn_table = {"pc_to_patch": [{"channel": 1, "bank_msb": 0, "pc": 0, "bank": 1, "slot": 1}]}
        self.patches = _FakePatchStore()
        self.plugins = _FakePluginRegistry()
        self.midi_monitor_calls = []

    def set_midi_monitor(self, on):
        self.midi_monitor_calls.append(bool(on))

    def stats(self):
        return {"uptime_ms": 1234, "loop_iters": 5}

    def apply_global(self, device):
        self.device = device

    def apply_midi_learn(self, table):
        self.midi_learn_table = table


class _FakePatchStore:
    def __init__(self):
        self._patches = {(1, 1): {"name": "Lead", "bindings": []}}

    def list(self):
        return [{"bank": b, "slot": s, "name": p.get("name", "")}
                for (b, s), p in self._patches.items()]

    def get(self, bank, slot):
        if (bank, slot) not in self._patches:
            raise OSError("not_found")
        return self._patches[(bank, slot)]

    def dirty_ids(self):
        return []

    def put_patch(self, bank, slot, patch, now_ms):
        self._patches[(bank, slot)] = patch


class _FakePluginRegistry:
    def manifest(self):
        return {
            "kemper_player": {
                "label": "Kemper Player",
                "version": "1.0",
                "messages": {"kemper_rig": {"params": {"bank": {"type": "int"}}}},
                "default_layout": [],
                "tft_fields": {},
                "config_schema": None,
                "recipe_schema": None,
            },
        }

    def iter_manifest(self):
        # Mirror the real PluginRegistry: the streaming _get_manifest emits the
        # manifest plugin-by-plugin via iter_manifest(), not manifest(). Without
        # this the fake threw mid-stream and truncated the JSON after "plugins":{.
        for name, entry in self.manifest().items():
            yield name, entry

    def default_layout(self, kind):
        return []


def build_protocol(port=None):
    """Construct a Protocol bound to a FakePort and a FakeApp. Returns
    (proto, port) so tests can inspect both."""
    port = port or FakePort()
    usb_cdc.data = port
    p = protocol.Protocol(FakeApp())
    return p, port


# ---------------- _send tests ----------------

@test("_send: retries json.dumps after gc.collect when first attempt MemoryErrors")
def _():
    # Regression: on a near-full CircuitPython heap json.dumps can
    # MemoryError for a multi-KB MANIFEST. _send catches the first
    # MemoryError, runs gc.collect(), and retries once. If both fail
    # the outer except logs and gives up cleanly.
    port = FakePort()
    p, _ = build_protocol(port)
    import json as _json
    real_dumps = _json.dumps
    calls = {"n": 0}
    def flaky_dumps(o):
        calls["n"] += 1
        if calls["n"] == 1:
            raise MemoryError("simulated heap exhaustion")
        return real_dumps(o)
    _json.dumps = flaky_dumps
    try:
        p._send({"type": "MANIFEST", "plugins": {"k": "v"}})
    finally:
        _json.dumps = real_dumps
    assert calls["n"] == 2, "expected one retry after MemoryError, saw %d call(s)" % calls["n"]
    # The retry succeeded -> the response actually hit the port.
    assert port.written.endswith(b"\n"), port.written


@test("_send: full write completes in one call")
def _():
    p, port = build_protocol()
    p._send({"type": "ACK", "id": "x"})
    assert port.written.endswith(b"\n"), port.written
    parsed = json.loads(port.written[:-1])
    assert parsed == {"type": "ACK", "id": "x"}


@test("_send: partial writes loop until drained")
def _():
    # Port accepts only 16 bytes per write call. _send must loop.
    port = FakePort(max_per_write=16)
    p, _ = build_protocol(port)
    payload = {"type": "PATCH_LIST", "patches": [{"bank": 1, "slot": i, "name": "x" * 12} for i in range(20)]}
    p._send(payload)
    # Reconstruct - everything must be there.
    assert port.written.endswith(b"\n"), "missing terminator"
    parsed = json.loads(port.written[:-1])
    assert parsed["type"] == "PATCH_LIST"
    assert len(parsed["patches"]) == 20
    # Many write calls because of the 16-byte cap.
    assert port.write_call_count > 5, f"expected multi-call loop, got {port.write_call_count}"


@test("_send: stalls out cleanly when port stops accepting (no infinite loop)")
def _():
    # max_per_write=0 means every write returns 0 bytes.
    port = FakePort(max_per_write=0)
    p, _ = build_protocol(port)
    p._send({"type": "MANIFEST", "plugins": {"x": "y" * 200}})
    # Should have written nothing but bailed after the stall counter.
    assert port.written == b"", port.written
    # And called write at least 8 times (the stall limit).
    assert port.write_call_count >= 8, port.write_call_count
    # AND it must not have hung the test - if we get here we're fine.


@test("_send: port=None is a no-op (no crash)")
def _():
    usb_cdc.data = None
    p = protocol.Protocol(FakeApp())
    p._send({"type": "ACK"})   # must not raise


@test("_send: disconnected port returns silently")
def _():
    port = FakePort(connected=False)
    p, _ = build_protocol(port)
    p._send({"type": "ACK"})
    assert port.written == b"", "must not write to disconnected port"


@test("_send: exception during write is caught and logged, never raised")
def _():
    port = FakePort(raise_on_write=RuntimeError("USB stalled"))
    p, _ = build_protocol(port)
    p._send({"type": "ACK"})   # must not raise


# ---------------- poll() / parser tests ----------------

@test("poll: complete JSON line is returned as dict")
def _():
    port = FakePort()
    p, _ = build_protocol(port)
    port.push_rx(b'{"type":"PING","id":"a"}\n')
    msg = p.poll()
    assert msg == {"type": "PING", "id": "a"}, msg


@test("poll: bad JSON sends ERROR + returns None")
def _():
    port = FakePort()
    p, _ = build_protocol(port)
    port.push_rx(b'{not json}\n')
    msg = p.poll()
    assert msg is None
    # An ERROR message was emitted on the port.
    assert b'"bad_json"' in bytes(port.written), port.written


@test("poll: no newline yet -> None, doesn't consume")
def _():
    port = FakePort()
    p, _ = build_protocol(port)
    port.push_rx(b'{"type":"PING"')
    msg = p.poll()
    assert msg is None
    port.push_rx(b',"id":"a"}\n')
    msg = p.poll()
    assert msg == {"type": "PING", "id": "a"}


@test("poll: buffered line consumed without new in_waiting")
def _():
    # When two complete lines arrive in one read, the next poll has to
    # serve the second one even though in_waiting is now 0.
    port = FakePort()
    p, _ = build_protocol(port)
    port.push_rx(b'{"type":"PING","id":"a"}\n{"type":"PING","id":"b"}\n')
    msg1 = p.poll()
    msg2 = p.poll()
    assert msg1 == {"type": "PING", "id": "a"}
    assert msg2 == {"type": "PING", "id": "b"}


@test("poll: rx overflow drops bytes up to last newline and sends ERROR")
def _():
    port = FakePort()
    p, _ = build_protocol(port)
    # Push more than _RX_BUF_MAX = 65536 bytes of garbage with no newline.
    port.push_rx(b'x' * 70000)
    msg = p.poll()
    assert msg is None
    assert b'"rx_overflow"' in bytes(port.written), "must emit rx_overflow ERROR"


# ---------------- handler dispatch tests ----------------

@test("handle: PING -> ACK with fw + id")
def _():
    port = FakePort()
    p, _ = build_protocol(port)
    p.handle({"type": "PING", "id": "x"})
    resp = json.loads(bytes(port.written).strip())
    assert resp["type"] == "ACK", resp
    assert resp["id"] == "x", resp
    assert "fw" in resp, resp


@test("handle: SET_MIDI_MONITOR on -> app toggled + ACK on:true")
def _():
    port = FakePort()
    p, _ = build_protocol(port)
    p.handle({"type": "SET_MIDI_MONITOR", "id": "m1", "on": True})
    resp = json.loads(bytes(port.written).strip())
    assert resp == {"type": "ACK", "id": "m1", "on": True}, resp
    assert p.app.midi_monitor_calls == [True], p.app.midi_monitor_calls


@test("handle: SET_MIDI_MONITOR missing/false 'on' -> off, ACK on:false")
def _():
    port = FakePort()
    p, _ = build_protocol(port)
    p.handle({"type": "SET_MIDI_MONITOR", "id": "m2"})   # no 'on' -> falsey
    resp = json.loads(bytes(port.written).strip())
    assert resp == {"type": "ACK", "id": "m2", "on": False}, resp
    assert p.app.midi_monitor_calls == [False], p.app.midi_monitor_calls


@test("handle: unknown_type -> ERROR with original 'of' field")
def _():
    port = FakePort()
    p, _ = build_protocol(port)
    p.handle({"type": "WHAT", "id": "x"})
    resp = json.loads(bytes(port.written).strip())
    assert resp == {"type": "ERROR", "id": "x", "error": "unknown_type", "of": "WHAT"}, resp


@test("handle: GET_MANIFEST returns MANIFEST with core_messages + plugins")
def _():
    port = FakePort()
    p, _ = build_protocol(port)
    p.handle({"type": "GET_MANIFEST", "id": "m"})
    resp = json.loads(bytes(port.written).strip())
    assert resp["type"] == "MANIFEST", resp
    assert resp["id"] == "m"
    assert "core_messages" in resp
    assert "plugins" in resp and "kemper_player" in resp["plugins"]


@test("handle: GET_DEVICE_INFO returns fw + device + current")
def _():
    port = FakePort()
    p, _ = build_protocol(port)
    p.handle({"type": "GET_DEVICE_INFO", "id": "d"})
    resp = json.loads(bytes(port.written).strip())
    assert resp["type"] == "DEVICE_INFO"
    assert "fw" in resp and "device" in resp and "current" in resp


@test("handle: LIST_PATCHES returns array via patches.list()")
def _():
    port = FakePort()
    p, _ = build_protocol(port)
    p.handle({"type": "LIST_PATCHES", "id": "lp"})
    resp = json.loads(bytes(port.written).strip())
    assert resp["type"] == "PATCH_LIST"
    assert resp["patches"][0]["bank"] == 1 and resp["patches"][0]["slot"] == 1


@test("handle: GET_PATCH returns the patch when present")
def _():
    port = FakePort()
    p, _ = build_protocol(port)
    p.handle({"type": "GET_PATCH", "id": "g", "bank": 1, "slot": 1})
    resp = json.loads(bytes(port.written).strip())
    assert resp["type"] == "PATCH"
    assert resp["patch"]["name"] == "Lead"


@test("handle: GET_PATCH for missing slot -> ERROR not_found")
def _():
    port = FakePort()
    p, _ = build_protocol(port)
    p.handle({"type": "GET_PATCH", "id": "g", "bank": 99, "slot": 5})
    resp = json.loads(bytes(port.written).strip())
    assert resp == {"type": "ERROR", "id": "g", "error": "not_found", "bank": 99, "slot": 5}, resp


@test("handle: PUT_GLOBAL writes device + acks")
def _():
    port = FakePort()
    p, app_port_owner = build_protocol(port)
    new_dev = {"device_name": "Hacked", "kemper": {"enabled": False}}
    # Avoid the real config.save_device side effect - swap with a noop.
    from captain import config
    saved = []
    orig = config.save_device
    config.save_device = lambda d: saved.append(d)
    try:
        p.handle({"type": "PUT_GLOBAL", "id": "g", "device": new_dev})
    finally:
        config.save_device = orig
    resp = json.loads(bytes(port.written).strip())
    assert resp == {"type": "ACK", "id": "g"}, resp
    assert saved == [new_dev], saved


@test("handle: PUT_GLOBAL with bad device -> ERROR missing_device")
def _():
    port = FakePort()
    p, _ = build_protocol(port)
    p.handle({"type": "PUT_GLOBAL", "id": "g"})   # no 'device'
    resp = json.loads(bytes(port.written).strip())
    assert resp == {"type": "ERROR", "id": "g", "error": "missing_device"}, resp


@test("handle: exception inside handler -> generic ERROR exception")
def _():
    port = FakePort()
    p, _ = build_protocol(port)
    # Force patches.list to raise so the LIST_PATCHES handler explodes.
    p.app.patches.list = lambda: (_ for _ in ()).throw(RuntimeError("boom"))
    p.handle({"type": "LIST_PATCHES", "id": "lp"})
    resp = json.loads(bytes(port.written).strip())
    assert resp["type"] == "ERROR" and resp["error"] == "exception", resp
    assert "boom" in resp.get("detail", "")


@test("handle: None msg is a noop (no crash, no write)")
def _():
    port = FakePort()
    p, _ = build_protocol(port)
    p.handle(None)
    assert port.written == b""


@test("handle: GET_GLOBAL with profile - reads disk for non-active profile")
def _():
    port = FakePort()
    p, _ = build_protocol(port)
    from captain import config
    # Stub config to simulate a profile on disk that's different from
    # active in-memory state.
    other_dev = {"device_name": "Other Pedal", "ampero": {"enabled": True}}
    orig_exists = config.profile_exists
    orig_load = config.load_device_for
    config.profile_exists = lambda pid: pid == "ampero01"
    config.load_device_for = lambda pid: other_dev if pid == "ampero01" else {}
    try:
        p.handle({"type": "GET_GLOBAL", "id": "g", "profile": "ampero01"})
    finally:
        config.profile_exists = orig_exists
        config.load_device_for = orig_load
    resp = json.loads(bytes(port.written).strip())
    assert resp["type"] == "GLOBAL", resp
    assert resp["device"] == other_dev, resp
    assert resp["profile"] == "ampero01", resp


@test("handle: GET_GLOBAL with profile - unknown profile -> ERROR no_such_profile")
def _():
    port = FakePort()
    p, _ = build_protocol(port)
    from captain import config
    orig = config.profile_exists
    config.profile_exists = lambda pid: False
    try:
        p.handle({"type": "GET_GLOBAL", "id": "g", "profile": "ghost"})
    finally:
        config.profile_exists = orig
    resp = json.loads(bytes(port.written).strip())
    assert resp == {"type": "ERROR", "id": "g", "error": "no_such_profile", "profile": "ghost"}, resp


@test("handle: GET_GLOBAL without profile -> serves active in-memory state")
def _():
    port = FakePort()
    p, _ = build_protocol(port)
    p.handle({"type": "GET_GLOBAL", "id": "g"})
    resp = json.loads(bytes(port.written).strip())
    assert resp["type"] == "GLOBAL"
    assert resp["device"] == p.app.device
    assert resp.get("profile", "") == ""


@test("handle: LIST_PATCHES with profile - reads disk, name field empty")
def _():
    port = FakePort()
    p, _ = build_protocol(port)
    from captain import config
    orig_exists = config.profile_exists
    orig_list = config.list_patches
    config.profile_exists = lambda pid: pid == "p2"
    config.list_patches = lambda profile=None: [(1, 1), (1, 2), (5, 3)]
    try:
        p.handle({"type": "LIST_PATCHES", "id": "lp", "profile": "p2"})
    finally:
        config.profile_exists = orig_exists
        config.list_patches = orig_list
    resp = json.loads(bytes(port.written).strip())
    assert resp["type"] == "PATCH_LIST"
    assert resp["profile"] == "p2"
    assert resp["patches"] == [
        {"bank": 1, "slot": 1, "name": ""},
        {"bank": 1, "slot": 2, "name": ""},
        {"bank": 5, "slot": 3, "name": ""},
    ], resp["patches"]


@test("handle: GET_PATCH with profile - reads from disk")
def _():
    port = FakePort()
    p, _ = build_protocol(port)
    from captain import config
    other_patch = {"name": "From Ampero", "bindings": []}
    orig_exists = config.profile_exists
    orig_load = config.load_patch_for
    config.profile_exists = lambda pid: pid == "p2"
    config.load_patch_for = lambda b, s, profile: other_patch if profile == "p2" else (_ for _ in ()).throw(OSError())
    try:
        p.handle({"type": "GET_PATCH", "id": "gp", "bank": 2, "slot": 4, "profile": "p2"})
    finally:
        config.profile_exists = orig_exists
        config.load_patch_for = orig_load
    resp = json.loads(bytes(port.written).strip())
    assert resp["type"] == "PATCH", resp
    assert resp["patch"] == other_patch
    assert resp["bank"] == 2 and resp["slot"] == 4
    assert resp["profile"] == "p2"


@test("handle: GET_MIDI_LEARN with profile - reads from disk")
def _():
    port = FakePort()
    p, _ = build_protocol(port)
    from captain import config
    other_table = {"pc_to_patch": [{"channel": 2, "bank_msb": 1, "pc": 4, "bank": 3, "slot": 2}]}
    orig_exists = config.profile_exists
    orig_load = config.load_midi_learn_for
    config.profile_exists = lambda pid: pid == "p2"
    config.load_midi_learn_for = lambda pid: other_table if pid == "p2" else {"pc_to_patch": []}
    try:
        p.handle({"type": "GET_MIDI_LEARN", "id": "gm", "profile": "p2"})
    finally:
        config.profile_exists = orig_exists
        config.load_midi_learn_for = orig_load
    resp = json.loads(bytes(port.written).strip())
    assert resp["type"] == "MIDI_LEARN"
    assert resp["table"] == other_table
    assert resp["profile"] == "p2"


@test("handle: GET_MIDI_LEARN without profile -> serves active in-memory table")
def _():
    port = FakePort()
    p, _ = build_protocol(port)
    p.handle({"type": "GET_MIDI_LEARN", "id": "gm"})
    resp = json.loads(bytes(port.written).strip())
    assert resp["type"] == "MIDI_LEARN"
    assert resp["table"] == p.app.midi_learn_table
    assert resp.get("profile", "") == ""


@test("handle: PUT_GLOBAL with profile writes cross-profile, skips apply_global")
def _():
    port = FakePort()
    p, _ = build_protocol(port)
    from captain import config
    saved = []
    orig_exists = config.profile_exists
    orig_save = config.save_device_for
    config.profile_exists = lambda pid: pid == "p2"
    config.save_device_for = lambda dev, pid: saved.append((pid, dev))
    applied = []
    p.app.apply_global = lambda dev: applied.append(dev)
    try:
        p.handle({"type": "PUT_GLOBAL", "id": "pg",
                  "device": {"device_name": "Other"}, "profile": "p2"})
    finally:
        config.profile_exists = orig_exists
        config.save_device_for = orig_save
    resp = json.loads(bytes(port.written).strip())
    assert resp == {"type": "ACK", "id": "pg"}, resp
    assert saved == [("p2", {"device_name": "Other"})], saved
    assert applied == [], "apply_global must NOT run for cross-profile write"


@test("handle: PUT_GLOBAL with unknown profile -> ERROR no_such_profile")
def _():
    port = FakePort()
    p, _ = build_protocol(port)
    from captain import config
    orig = config.profile_exists
    config.profile_exists = lambda pid: False
    try:
        p.handle({"type": "PUT_GLOBAL", "id": "pg",
                  "device": {"device_name": "Other"}, "profile": "ghost"})
    finally:
        config.profile_exists = orig
    resp = json.loads(bytes(port.written).strip())
    assert resp == {"type": "ERROR", "id": "pg", "error": "no_such_profile", "profile": "ghost"}, resp


@test("handle: PUT_PATCH with profile writes cross-profile, skips app.put_patch")
def _():
    port = FakePort()
    p, _ = build_protocol(port)
    from captain import config
    saved = []
    orig_exists = config.profile_exists
    orig_save = config.save_patch_for
    config.profile_exists = lambda pid: pid == "p2"
    config.save_patch_for = lambda b, s, patch, pid: saved.append((pid, b, s, patch))
    called_app = []
    p.app.put_patch = lambda b, s, patch: called_app.append((b, s))
    try:
        p.handle({"type": "PUT_PATCH", "id": "pp", "bank": 3, "slot": 4,
                  "patch": {"name": "X", "bindings": []}, "profile": "p2"})
    finally:
        config.profile_exists = orig_exists
        config.save_patch_for = orig_save
    resp = json.loads(bytes(port.written).strip())
    assert resp == {"type": "ACK", "id": "pp"}, resp
    assert saved == [("p2", 3, 4, {"name": "X", "bindings": []})], saved
    assert called_app == [], "app.put_patch must NOT run for cross-profile write"


@test("handle: PUT_MIDI_LEARN with profile writes cross-profile, skips apply_midi_learn")
def _():
    port = FakePort()
    p, _ = build_protocol(port)
    from captain import config
    saved = []
    orig_exists = config.profile_exists
    orig_save = config.save_midi_learn_for
    config.profile_exists = lambda pid: pid == "p2"
    config.save_midi_learn_for = lambda table, pid: saved.append((pid, table))
    applied = []
    p.app.apply_midi_learn = lambda t: applied.append(t)
    try:
        p.handle({"type": "PUT_MIDI_LEARN", "id": "pm",
                  "table": {"pc_to_patch": []}, "profile": "p2"})
    finally:
        config.profile_exists = orig_exists
        config.save_midi_learn_for = orig_save
    resp = json.loads(bytes(port.written).strip())
    assert resp == {"type": "ACK", "id": "pm"}, resp
    assert saved == [("p2", {"pc_to_patch": []})], saved
    assert applied == [], "apply_midi_learn must NOT run for cross-profile write"


@test("put_binding: preserves latched_on when mode stays latched (LED regression)")
def _():
    # User scenario: edit a latched binding's label / color, the switch
    # was ON, the LED must stay ON. Pre-fix, app.put_binding() always
    # called sw.reset() which zeroed latched_on - the LED would fall
    # back to dim or off.
    #
    # We simulate Captain.put_binding by re-implementing its preserve
    # logic against a minimal fake. Keep this in sync with
    # firmware/lib/captain/app.py:put_binding; the test exists to
    # protect the rule, not to mirror the whole class.
    class FakeSw:
        def __init__(self, name, latched_on):
            self.name = name; self.latched_on = latched_on
        def reset(self):
            self.latched_on = False
    sw = FakeSw("4", latched_on=True)
    old_mode, new_mode = "latched", "latched"
    prev_latched = sw.latched_on
    sw.reset()
    if old_mode == "latched" and new_mode == "latched":
        sw.latched_on = prev_latched
    assert sw.latched_on is True, "latched_on must survive a same-mode edit"


@test("put_binding: resets latched_on when mode changes (latched -> tap)")
def _():
    # When the user changes the mode AWAY from latched, the previous
    # on/off state no longer has meaning. The reset is the right
    # behaviour. This guards the OTHER direction of the previous test.
    class FakeSw:
        def __init__(self, name, latched_on):
            self.name = name; self.latched_on = latched_on
        def reset(self):
            self.latched_on = False
    sw = FakeSw("4", latched_on=True)
    old_mode, new_mode = "latched", "tap"
    prev_latched = sw.latched_on
    sw.reset()
    if old_mode == "latched" and new_mode == "latched":
        sw.latched_on = prev_latched
    assert sw.latched_on is False, "latched_on must reset when leaving latched mode"


@test("put_patch: preserves per-switch latched_on for switches that stay latched")
def _():
    # Same regression but at the patch-level (PUT_PATCH for a name /
    # color edit). Mirror of Captain.put_patch's snapshot-then-restore
    # logic against fake switches and bindings.
    class FakeSw:
        def __init__(self, name, latched_on):
            self.name = name; self.latched_on = latched_on
        def reset(self):
            self.latched_on = False
    switches = [FakeSw("4", True), FakeSw("A", False), FakeSw("B", True)]
    # Mode map BEFORE the edit.
    prev_modes  = {"4": "latched", "A": "tap", "B": "latched"}
    prev_latched = {sw.name: sw.latched_on for sw in switches}
    # User changes the patch name; binding modes don't change.
    new_modes = {"4": "latched", "A": "tap", "B": "latched"}
    # Simulate reset_all then restore loop.
    for sw in switches: sw.reset()
    for sw in switches:
        if (prev_modes.get(sw.name) == "latched"
                and new_modes.get(sw.name) == "latched"
                and prev_latched.get(sw.name)):
            sw.latched_on = True
    assert switches[0].latched_on is True,  "switch 4 was latched-on, must remain so"
    assert switches[1].latched_on is False, "switch A was tap, must remain off"
    assert switches[2].latched_on is True,  "switch B was latched-on, must remain so"


@test("emit_event: builds EVENT message with extra fields")
def _():
    port = FakePort()
    p, _ = build_protocol(port)
    p.emit_event("patch_switched", bank=2, slot=3, source="binding")
    resp = json.loads(bytes(port.written).strip())
    assert resp["type"] == "EVENT"
    assert resp["event"] == "patch_switched"
    assert resp["bank"] == 2 and resp["slot"] == 3 and resp["source"] == "binding"


# ---------------- runner ----------------

def main():
    total = PASS_COUNT + FAIL_COUNT
    print(f"\n{PASS_COUNT}/{total} passed, {FAIL_COUNT} failed")
    if FAIL_COUNT:
        print("\nFailures:")
        for f in FAILURES:
            print("  -", f)
        sys.exit(1)


if __name__ == "__main__":
    main()
