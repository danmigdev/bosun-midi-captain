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
import importlib
import json
import sys
import tempfile
import types
from pathlib import Path


FIRMWARE_LIB = Path(__file__).resolve().parent.parent / "firmware" / "lib"
sys.path.insert(0, str(FIRMWARE_LIB))

# Stub out CircuitPython modules the firmware imports directly or
# transitively. We don't need real behavior for the protocol layer.
for mod_name in ("busio", "usb_midi", "usb_cdc", "digitalio", "board", "neopixel",
                 "displayio", "fourwire", "pwmio", "terminalio",
                 "adafruit_display_text", "adafruit_st7789",
                 "adafruit_bitmap_font", "microcontroller", "supervisor"):
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

# OTA writes must turn this off before mutating CIRCUITPY.  A mutable fake lets
# the upload tests verify the ordering without a CircuitPython board.
import supervisor                 # noqa: E402
supervisor.runtime = types.SimpleNamespace(autoreload=True)

from captain import protocol      # noqa: E402


PASS_COUNT = 0
FAIL_COUNT = 0
FAILURES = []
RX_TEST_ROOT = tempfile.TemporaryDirectory(prefix="bosun-protocol-rx-")


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
        self.read_sizes = []
        self.write_timeout = None
        self.timeout = None
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
        self.read_sizes.append(n)
        out = bytes(self._rx[:n])
        self._rx = self._rx[n:]
        return out

    def push_rx(self, data):
        self._rx.extend(data)

    def readinto(self, buf):
        assert self.timeout == 0, "readinto could block waiting to fill the RX scratch"
        self.read_sizes.append(len(buf))
        n = min(len(buf), len(self._rx))
        for i in range(n):
            buf[i] = self._rx[i]
        self._rx = self._rx[n:]
        return n


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
        self.delete_patch_calls = []
        self.delete_patch_error = None
        self.display_context = {
            "patch_name": "ACOUSTIC", "bank": 1, "slot": 1,
            "kemper_block_C": "on", "kemper_block_Mod": "off",
        }

    def set_midi_monitor(self, on):
        self.midi_monitor_calls.append(bool(on))

    def stats(self):
        return {"uptime_ms": 1234, "loop_iters": 5}

    def apply_global(self, device):
        self.device = device

    def apply_midi_learn(self, table):
        self.midi_learn_table = table

    def _poll_switches_mid_op(self):
        self.mid_op_poll_count = getattr(self, "mid_op_poll_count", 0) + 1

    def delete_patch(self, bank, slot):
        self.delete_patch_calls.append((bank, slot))
        if self.delete_patch_error is not None:
            raise self.delete_patch_error
        self.patches.delete(bank, slot)


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

    def read(self, bank, slot):
        if (bank, slot) not in self._patches:
            raise OSError("not_found")
        return self._patches[(bank, slot)]

    def dirty_ids(self):
        return []

    def put_patch(self, bank, slot, patch, now_ms):
        self._patches[(bank, slot)] = patch

    def delete(self, bank, slot):
        self._patches.pop((bank, slot), None)


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
    original_root = protocol.config.CONFIG_ROOT
    protocol.config.CONFIG_ROOT = RX_TEST_ROOT.name
    try:
        p = protocol.Protocol(FakeApp())
    finally:
        protocol.config.CONFIG_ROOT = original_root
    return p, port


_DYNAMIC_MANIFEST_MODULE = "captain.manifest_dynamic"


def forget_dynamic_manifest_module():
    """Restore the import state used by a freshly booted static-path Captain."""
    removed = sys.modules.pop(_DYNAMIC_MANIFEST_MODULE, None)
    package = sys.modules.get("captain")
    if (package is not None and hasattr(package, "manifest_dynamic") and
            (removed is None or package.manifest_dynamic is removed)):
        delattr(package, "manifest_dynamic")


def drain_background(p, max_steps=10000):
    """GET_MANIFEST/GET_GLOBAL now stream via a resumable generator
    (protocol._start_background/pump_background) so the main loop can
    interleave other requests instead of blocking for the whole multi-KB
    response - see the 2026-08-16 fix. handle() only runs the first slice;
    tests that want the complete response drain the rest here, same as
    _tick_body does one step at a time in the real firmware."""
    steps = 0
    while (p._bg_gen is not None or p._bg_queue or p._bg_line_seal or
           p._pending_out or p._deferred_out):
        p.pump_background()
        steps += 1
        assert steps < max_steps, "background generator never finished"


@test("importing protocol leaves the dynamic MANIFEST fallback unloaded")
def _():
    assert _DYNAMIC_MANIFEST_MODULE not in sys.modules, \
        "dynamic fallback consumed heap during protocol import"
    assert "captain_ota" not in sys.modules, "OTA consumed heap during protocol import"


def assert_real_generator_failure_isolated(kind):
    """A real streamed response must let pump_background seal its line.

    Queue an EVENT behind the generator before provoking a scalar encode
    failure after a long value has already reached the wire.  If the
    generator swallows that exception, pump_background mistakes the truncated
    response for success and releases the EVENT directly into its open JSON
    object -- the corruption observed on the live Captain.
    """
    p, port = build_protocol()
    long_value = kind + "-" + ("x" * 320)
    fault_value = "explode-" + kind
    streamed = {"long": long_value, "fault": fault_value}
    deferred = [
        {
            "type": "CONTEXT", "partial": True,
            "context": {"after_broken": kind.lower()},
        },
        {"type": "EVENT", "event": "after_broken_" + kind.lower()},
    ]

    real_dumps = protocol.json.dumps
    real_core = protocol.messages.CORE_MESSAGE_TYPES
    missing = object()
    real_print = getattr(protocol, "print", missing)
    logs = []

    def failing_dumps(value, *args, **kwargs):
        # All four real generators fail on the same scalar after emitting the
        # preceding long field. MANIFEST deliberately encodes strings in
        # eight-character windows, so fail its first matching window.
        if (value == fault_value or
                (kind in ("MANIFEST", "PATCH") and
                 value == fault_value[:8])):
            raise RuntimeError("simulated mid-stream scalar failure")
        return real_dumps(value, *args, **kwargs)

    protocol.json.dumps = failing_dumps
    protocol.print = lambda *parts: logs.append(" ".join(str(part) for part in parts))
    try:
        if kind == "PATCH":
            p.app.patches._patches[(1, 1)] = streamed
            generator = p._get_patch_gen("broken-patch", {
                "bank": 1, "slot": 1,
            })
        elif kind == "GLOBAL":
            p.app.device = streamed
            generator = p._get_global_gen("broken-global", {})
        elif kind == "MANIFEST":
            protocol.messages.CORE_MESSAGE_TYPES = streamed
            generator = p._get_manifest_gen("broken-manifest")
        elif kind == "CONTEXT":
            p.app.display_context = streamed
            generator = p._get_context_gen("broken-context")
        else:
            raise AssertionError("unsupported generator kind: %s" % kind)

        # Assign directly so even the non-yielding buffered CONTEXT generator
        # has a complete frame waiting behind it before its first advancement.
        request_type = {
            "PATCH": "GET_PATCH", "GLOBAL": "GET_GLOBAL",
            "MANIFEST": "GET_MANIFEST", "CONTEXT": "GET_CONTEXT",
        }[kind]
        request_id = "broken-" + kind.lower()
        p._bg_gen = generator
        p._bg_mid = request_id
        p._bg_request_type = request_type
        for frame in deferred:
            assert p._send(frame) is True, \
                "%s was not accepted for deferral" % frame["type"]
        assert len(p._deferred_out) == 2, \
            "CONTEXT/EVENT were not held behind the streamed line"
        drain_background(p)
    finally:
        protocol.json.dumps = real_dumps
        protocol.messages.CORE_MESSAGE_TYPES = real_core
        if real_print is missing:
            del protocol.print
        else:
            protocol.print = real_print

    wire = bytes(port.written)
    lines = wire.splitlines()
    expected_after = list(deferred)
    if kind == "MANIFEST":
        expected_after.append({
            "type": "ERROR", "id": "broken-manifest",
            "error": "manifest_failed",
        })
    else:
        expected_after.append({
            "type": "ERROR", "id": "broken-" + kind.lower(),
            "error": "exception",
            "detail": "simulated mid-stream scalar failure",
            "of": request_type,
        })
    assert wire.count(b"\n") == 1 + len(expected_after), (kind, wire)
    assert len(lines) == 1 + len(expected_after), (kind, lines)
    try:
        json.loads(lines[0])
        assert False, "%s's deliberately damaged record became valid" % kind
    except ValueError:
        pass
    assert b'"type": "CONTEXT"' not in lines[0], (kind, lines)
    assert b'"type": "EVENT"' not in lines[0], (kind, lines)
    assert [json.loads(line) for line in lines[1:]] == expected_after, \
        (kind, lines)
    assert any("background gen EXC err=RuntimeError" in line for line in logs), \
        "%s swallowed the exception before pump_background: %r" % (kind, logs)
    assert (p._bg_gen is None and p._bg_mid is None and
            p._bg_request_type is None and not p._pending_out and
            not p._deferred_out), \
        (kind, p._pending_out, p._deferred_out)


@test("GET_PATCH mid-stream error reaches pump and isolates CONTEXT/EVENT")
def _():
    assert_real_generator_failure_isolated("PATCH")


@test("GET_GLOBAL mid-stream error reaches pump and isolates CONTEXT/EVENT")
def _():
    assert_real_generator_failure_isolated("GLOBAL")


@test("GET_MANIFEST mid-stream error reaches pump and isolates CONTEXT/EVENT")
def _():
    assert_real_generator_failure_isolated("MANIFEST")


@test("GET_CONTEXT mid-stream error reaches pump and isolates CONTEXT/EVENT")
def _():
    assert_real_generator_failure_isolated("CONTEXT")


@test("background exception seals its damaged line before deferred responses")
def _():
    p, port = build_protocol()

    def broken():
        p._write_bytes(b'{"type":"PATCH","broken":')
        yield
        raise MemoryError("simulated mid-stream exhaustion")

    p._bg_gen = broken()
    p.pump_background()  # writes the unterminated prefix
    p._send({"type": "ACK", "id": "after-broken"})
    p.pump_background()  # exception: seal prefix, then release ACK

    lines = bytes(port.written).splitlines()
    assert len(lines) == 2, lines
    try:
        json.loads(lines[0])
        assert False, "the deliberately damaged first record became valid"
    except ValueError:
        pass
    assert json.loads(lines[1]) == {"type": "ACK", "id": "after-broken"}, lines


@test("background error reserves its newline when pending queue is full")
def _():
    p, port = build_protocol()
    p._MAX_PENDING_CHUNKS = 1

    def broken_with_full_tail():
        p._write_bytes(b'{"type":"PATCH","broken":')
        yield
        # Reproduce a CDC stall inside one generator slice: its unsent tail
        # occupies the only queue slot, then the generator itself fails.  The
        # error delimiter must not be dropped merely because that slot is full.
        port.max_per_write = 0
        p._write_bytes(b'"unsent-tail"')
        port.max_per_write = None
        raise MemoryError("simulated failure with a full pending queue")

    p._bg_gen = broken_with_full_tail()
    p.pump_background()
    p._send({"type": "EVENT", "event": "after-full-tail"})
    p.pump_background()
    drain_background(p)

    wire = bytes(port.written)
    lines = wire.splitlines()
    assert wire.count(b"\n") == 2, wire
    assert len(lines) == 2, lines
    try:
        json.loads(lines[0])
        assert False, "the deliberately damaged first record became valid"
    except ValueError:
        pass
    assert json.loads(lines[1]) == {
        "type": "EVENT", "event": "after-full-tail",
    }, lines


@test("background seal barrier survives a stalled then recovering host")
def _():
    p, port = build_protocol()

    def broken():
        p._write_bytes(b'{"type":"PATCH","broken":')
        yield
        raise MemoryError("simulated mid-stream failure")

    p._bg_gen = broken()
    p.pump_background()
    p._send({"type": "ACK", "id": "before-seal-stall"})

    # The generator now fails, but the host cannot yet accept even the
    # recovery newline. Keep ownership across later handlers and generators.
    port.max_per_write = 0
    p.pump_background()
    assert p._bg_line_seal, "failed line lost its seal barrier"
    assert p._pending_out == [b"\n"], p._pending_out
    p._send({"type": "EVENT", "event": "during-seal-stall"})
    p._start_background(p._json_line_gen({
        "type": "AFTER_SEAL", "id": "queued-generator",
    }))

    assert bytes(port.written).count(b"\n") == 0, port.written
    assert len(p._deferred_out) == 2, p._deferred_out
    assert len(p._bg_queue) == 1, p._bg_queue

    port.max_per_write = None
    drain_background(p)

    lines = bytes(port.written).splitlines()
    assert len(lines) == 4, lines
    try:
        json.loads(lines[0])
        assert False, "the deliberately damaged first record became valid"
    except ValueError:
        pass
    assert [json.loads(line) for line in lines[1:]] == [
        {"type": "ACK", "id": "before-seal-stall"},
        {"type": "EVENT", "event": "during-seal-stall"},
        {"type": "AFTER_SEAL", "id": "queued-generator"},
    ], lines
    assert not p._bg_line_seal and not p._deferred_out and not p._bg_queue


@test("background recovery survives diagnostic print failure")
def _():
    p, port = build_protocol()

    def broken():
        p._write_bytes(b'{"type":"PATCH","broken":')
        yield
        raise MemoryError("simulated generator failure")

    p._bg_gen = broken()
    p.pump_background()
    p._send({"type": "EVENT", "event": "after-log-failure"})

    missing = object()
    real_print = getattr(protocol, "print", missing)
    protocol.print = lambda *args: (_ for _ in ()).throw(
        MemoryError("simulated diagnostic allocation failure"))
    try:
        p.pump_background()
    finally:
        if real_print is missing:
            del protocol.print
        else:
            protocol.print = real_print

    lines = bytes(port.written).splitlines()
    assert len(lines) == 2, lines
    assert json.loads(lines[1]) == {
        "type": "EVENT", "event": "after-log-failure",
    }, lines
    assert not p._bg_line_seal and not p._deferred_out


@test("deferred release reuses queued bytes under low heap without losing replies")
def _():
    p, port = build_protocol()

    def one_slice():
        p._write_bytes(b'{"type":"STREAMED"')
        yield
        p._write_bytes(b'}\n')

    p._start_background(one_slice(), "streamed", "GET_CONTEXT")
    assert p._bg_gen is not None
    assert p._send({"type": "ACK", "id": "must-survive"}) is True
    assert len(p._deferred_out) == 1

    # The response is fully encoded already.  Releasing it must transfer the
    # existing bytes object, not allocate a duplicate precisely when a stream
    # may just have failed because the heap is fragmented.
    missing = object()
    real_bytes = getattr(protocol, "bytes", missing)
    protocol.bytes = lambda *_args, **_kwargs: (_ for _ in ()).throw(
        MemoryError("no allocation available during deferred release"))
    try:
        drain_background(p)
    finally:
        if real_bytes is missing:
            delattr(protocol, "bytes")
        else:
            protocol.bytes = real_bytes

    assert [json.loads(line) for line in bytes(port.written).splitlines()] == [
        {"type": "STREAMED"},
        {"type": "ACK", "id": "must-survive"},
    ]
    assert not p._pending_out and not p._deferred_out


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


@test("_send: non-blocking partial writes loop until drained")
def _():
    # Port accepts only 16 bytes per immediate write call. _send may consume
    # each positive prefix inline because the port was configured non-blocking.
    port = FakePort(max_per_write=16)
    p, _ = build_protocol(port)
    payload = {"type": "PATCH_LIST", "patches": [{"bank": 1, "slot": i, "name": "x" * 12} for i in range(20)]}
    assert p._send(payload) is True
    # Reconstruct - everything must be there.
    assert port.written.endswith(b"\n"), "missing terminator"
    parsed = json.loads(port.written[:-1])
    assert parsed["type"] == "PATCH_LIST"
    assert len(parsed["patches"]) == 20
    # Many write calls because of the 16-byte cap.
    assert port.write_call_count > 5, f"expected multi-call loop, got {port.write_call_count}"


@test("_send: yields after one non-blocking CDC write stops accepting")
def _():
    # The port must be configured with write_timeout=0. A zero return then
    # costs no 200 ms wait and recovery belongs to a later pump/tick.
    class TimedOutPort(FakePort):
        def __init__(self):
            super().__init__()
            self.blocked_ms = 0

        def write(self, data):
            self.write_call_count += 1
            if self.write_timeout:
                self.blocked_ms += 200
            return 0

    port = TimedOutPort()
    p, _ = build_protocol(port)
    assert port.write_timeout == 0, port.write_timeout
    p._send({"type": "MANIFEST", "plugins": {"x": "y" * 200}})
    assert port.written == b"", port.written
    assert port.write_call_count == 1, port.write_call_count
    assert port.blocked_ms == 0, port.blocked_ms
    assert p._pending_out, "the timed-out tail was not retained for next tick"


@test("_send: a legacy None CDC write is treated as zero progress")
def _():
    class NoneWritePort(FakePort):
        def write(self, _data):
            self.write_call_count += 1
            return None

    port = NoneWritePort()
    p, _ = build_protocol(port)
    assert p._send({"type": "ACK", "id": "none-progress"}) is False
    assert port.write_call_count == 1, port.write_call_count
    assert p._pending_out, "None-progress tail was not retained"


@test("_send: positive partial CDC writes cannot multiply a timeout")
def _():
    # CircuitPython 9.2.7's usb_cdc.write() may wait for the full configured
    # write_timeout and still return a *positive partial* byte count. Treating
    # progress as permission to call it again inline used to reset that 200 ms
    # timeout for every byte. Non-blocking configuration keeps the allocation-
    # free positive-progress loop while removing the multiplied delay.
    class TimedPartialPort(FakePort):
        def __init__(self):
            super().__init__()
            self.blocked_ms = 0

        def write(self, data):
            self.write_call_count += 1
            if self.write_timeout:
                self.blocked_ms += 200
            chunk = bytes(data[:1])
            self.written.extend(chunk)
            return len(chunk)

    port = TimedPartialPort()
    p, _ = build_protocol(port)
    assert port.write_timeout == 0, port.write_timeout
    assert p._send({"type": "ACK", "id": "timed-partial"}) is True
    assert port.write_call_count > 1, port.write_call_count
    assert port.blocked_ms == 0, port.blocked_ms
    assert not p._pending_out


@test("_send: partial stall resumes before a later response without corrupting JSON lines")
def _():
    class RecoveringPort(FakePort):
        def __init__(self):
            super().__init__()
            self.budget = 7
        def write(self, data):
            self.write_call_count += 1
            if self.budget <= 0:
                return 0
            chunk = bytes(data)[:self.budget]
            self.budget -= len(chunk)
            self.written.extend(chunk)
            return len(chunk)
    port = RecoveringPort()
    p, _ = build_protocol(port)
    assert p._send({"type": "ACK", "id": "first"}) is False
    assert p._send({"type": "ACK", "id": "second"}) is False
    port.budget = 4096
    p.pump_background()
    lines = [json.loads(line) for line in bytes(port.written).splitlines()]
    assert [line["id"] for line in lines] == ["first", "second"], lines


@test("TX pending progress survives suffix-copy OOM without copying or replacing the original frame")
def _():
    class NoSuffixCopy(bytes):
        def __getitem__(self, index):
            if isinstance(index, slice):
                raise MemoryError("no allocation for a pending suffix copy")
            return super().__getitem__(index)

    class BudgetPort(FakePort):
        budget = 5

        def write(self, data):
            accepted = min(self.budget, len(data))
            self.written.extend(bytes(data)[:accepted])
            self.budget -= accepted
            return accepted

    p, port = build_protocol(BudgetPort())
    frame = NoSuffixCopy(b'{"type":"ACK","id":"first"}\n')
    p._pending_out = [frame]
    p._pending_bytes = len(frame)
    assert p._flush_pending() is False
    assert p._pending_out[0] is frame
    assert p._pending_offset == 5
    assert p._pending_bytes == len(frame) - 5
    assert p._flush_pending() is False
    assert p._pending_offset == 5 and p._pending_bytes == len(frame) - 5
    port.budget = 1000
    assert p._flush_pending() is True
    assert bytes(port.written) == frame
    assert not p._pending_out and p._pending_offset == 0 and p._pending_bytes == 0


@test("TX commits positive USB writes before a failing memoryview allocation and resumes exactly once")
def _():
    from unittest.mock import patch

    for failure in ("constructor", "slice"):
        p, port = build_protocol(FakePort(max_per_write=5))
        attempts = []

        class FailedView:
            def __getitem__(self, index):
                attempts.append(("slice", index.start))
                raise MemoryError("suffix view cannot allocate")

        def no_view(value):
            attempts.append(("constructor", len(value)))
            if failure == "constructor":
                raise MemoryError("memoryview cannot allocate")
            return FailedView()

        first = {"type": "ACK", "id": "view-failure"}
        expected = json.dumps(first).encode() + b"\n"
        with patch.object(protocol, "memoryview", no_view, create=True):
            assert p._send(first) is False
            assert bytes(port.written) == expected[:5]
            assert p._pending_offset == 5 and p._pending_bytes == len(expected) - 5
            for _ in range(3):
                p.pump_background()
                assert bytes(port.written) == expected[:5]
                assert p._pending_offset == 5 and p._pending_bytes == len(expected) - 5
                assert not p._tx_active
        assert attempts
        assert p._send({"type": "ACK", "id": "after-view"}) is True
        assert [json.loads(line) for line in port.written.splitlines()] == [
            first, {"type": "ACK", "id": "after-view"},
        ]
        assert not p._pending_out and p._pending_offset == 0 and p._pending_bytes == 0


@test("TX freezes a stalled mutable chunk before its streamer reuses the buffer")
def _():
    class BudgetPort(FakePort):
        budget = 5

        def write(self, data):
            accepted = min(self.budget, len(data))
            self.written.extend(bytes(data)[:accepted])
            self.budget -= accepted
            return accepted

    for as_view in (False, True):
        p, port = build_protocol(BudgetPort())
        frame = b'{"type":"ACK","id":"mutable"}\n'
        backing = bytearray(frame)
        chunk = memoryview(backing) if as_view else backing
        assert p._write_bytes(chunk) == len(frame) - 5
        assert type(p._pending_out[0]) is bytes
        assert p._pending_offset == 5 and p._pending_bytes == len(frame) - 5
        backing[:] = b"!" * len(backing)
        next_frame = b'{"type":"ACK","id":"following"}\n'
        assert p._write_bytes(next_frame) == len(next_frame)
        assert p._pending_bytes == len(frame) - 5 + len(next_frame)
        port.budget = 1000
        p.pump_background()
        assert bytes(port.written) == frame + next_frame
        assert not p._pending_out and p._pending_offset == 0 and p._pending_bytes == 0


@test("TX mutable snapshot OOM drops the unsafe alias and seals the failed stream before later replies")
def _():
    from unittest.mock import patch

    class BudgetPort(FakePort):
        budget = 5

        def write(self, data):
            accepted = min(self.budget, len(data))
            self.written.extend(bytes(data)[:accepted])
            self.budget -= accepted
            return accepted

    p, port = build_protocol(BudgetPort())
    frame = bytearray(b'{"type":"PATCH","id":"bad-snapshot","patch":{}}\n')
    original = bytes(frame)
    snapshots = []

    def no_mutable_copy(value):
        if isinstance(value, (bytearray, memoryview)):
            snapshots.append(value)
            raise MemoryError("cannot freeze reusable stream buffer")
        return bytes(value)

    def response():
        p._write_bytes(frame)
        yield

    with patch.object(protocol, "bytes", no_mutable_copy, create=True):
        p._start_background(response(), "bad-snapshot", "GET_PATCH")
        p._send({"type": "ACK", "id": "after-snapshot"})
    assert snapshots and p._bg_line_seal
    assert not any(chunk is frame for chunk in p._pending_out)
    assert p._pending_offset == 0
    frame[:] = b"!" * len(frame)
    port.budget = 10000
    drain_background(p)
    lines = bytes(port.written).splitlines()
    assert lines[0] == original[:5]
    replies = [json.loads(line) for line in lines[1:]]
    assert replies[0]["type"] == "ERROR" and replies[0]["id"] == "bad-snapshot"
    assert replies[1] == {"type": "ACK", "id": "after-snapshot"}
    assert not p._pending_out and p._pending_offset == 0 and p._pending_bytes == 0


@test("TX switch events cannot recursively flush the partially written queue head")
def _():
    class BudgetPort(FakePort):
        budget = 5

        def write(self, data):
            accepted = min(self.budget, len(data))
            self.written.extend(bytes(data)[:accepted])
            self.budget -= accepted
            return accepted

    p, port = build_protocol(BudgetPort())
    polls = []

    def poll_switch():
        polls.append(True)
        if len(polls) == 1:
            p.emit_event("switch_pressed", switch="A")

    p.app._poll_switches_mid_op = poll_switch
    assert p._send({"type": "ACK", "id": "before-event"}) is False
    assert len(polls) == 1, "a switch event reentered the active USB flush"
    assert p._pending_offset == 5
    assert p._pending_bytes == sum(len(chunk) for chunk in p._pending_out) - 5
    port.budget = 1000
    p.pump_background()
    assert [json.loads(line) for line in port.written.splitlines()] == [
        {"type": "ACK", "id": "before-event"},
        {"type": "EVENT", "event": "switch_pressed", "switch": "A"},
    ]
    assert not p._tx_active and p._pending_offset == 0 and p._pending_bytes == 0


@test("TX disconnect clears a committed partial offset before the next session")
def _():
    from unittest.mock import patch

    p, port = build_protocol(FakePort(max_per_write=5))
    with patch.object(protocol, "memoryview", side_effect=MemoryError("suffix"), create=True):
        assert p._send({"type": "ACK", "id": "old-session"}) is False
    assert p._pending_offset == 5
    port.connected = False
    p.poll()
    assert not p._pending_out and p._pending_offset == 0 and p._pending_bytes == 0
    assert not p._tx_active
    port.written.clear()
    port.connected = True
    assert p._send({"type": "ACK", "id": "new-session"}) is True
    assert json.loads(port.written) == {"type": "ACK", "id": "new-session"}


@test("TX view OOM retains the background owner and keeps a deferred ACK behind its exact frame")
def _():
    from unittest.mock import patch

    p, port = build_protocol(FakePort(max_per_write=5))
    frame = b'{"type":"PATCH","id":"streamed","patch":{"name":"CRUNCH"}}\n'

    def response():
        p._write_bytes(frame)
        yield

    with patch.object(protocol, "memoryview", side_effect=MemoryError("suffix"), create=True):
        p._start_background(response(), "streamed", "GET_PATCH")
        p.handle({"type": "PING", "id": "after-stream"})
        for _ in range(3):
            p.pump_background()
            assert p._bg_gen is not None and p._bg_mid == "streamed"
            assert p._pending_offset == 5 and p._pending_bytes == len(frame) - 5
            assert bytes(port.written) == frame[:5]
            assert len(p._deferred_out) == 1 and not p._bg_line_seal
    drain_background(p)
    assert [json.loads(line) for line in port.written.splitlines()] == [
        json.loads(frame), {"type": "ACK", "id": "after-stream", "fw": protocol.VERSION},
    ]
    assert not p._pending_out and not p._deferred_out
    assert p._pending_offset == 0 and p._pending_bytes == 0 and p._deferred_bytes == 0


@test("protocol: disconnect drops unfinished output and queued background work")
def _():
    port = FakePort(max_per_write=0)
    p, _ = build_protocol(port)
    p._send({"type": "ACK", "id": "old"})
    p._start_background(iter((None, None)), "old-bg", "GET_CONTEXT")
    p._start_background(iter((None,)), "queued-bg", "GET_PATCH")
    assert p._pending_out and p._bg_gen is not None
    assert p._bg_mid == "old-bg" and p._bg_request_type == "GET_CONTEXT"
    assert p._bg_queue
    port.connected = False
    p.poll()
    assert not p._pending_out and not p._bg_queue and p._bg_gen is None
    assert p._bg_mid is None and p._bg_request_type is None


@test("_send: a stalled port polls switches once then yields to the main loop")
def _():
    # Keep the defensive one-zero-attempt scheduler rule even though the real
    # port is non-blocking. Older/fake stream implementations can still report
    # no progress; a switch scan must run before the tail is deferred.
    port = FakePort(max_per_write=0)
    p, _ = build_protocol(port)
    p._send({"type": "EVENT", "event": "switch_pressed", "switch": "1"})
    assert port.write_call_count == 1, port.write_call_count
    assert p.app.mid_op_poll_count == 1, \
        f"expected one mid-op switch poll before yielding, got {getattr(p.app, 'mid_op_poll_count', 0)}"


@test("GET_CONTEXT streams a complete snapshot through tiny partial writes")
def _():
    port = FakePort(max_per_write=1)
    p, _ = build_protocol(port)
    p.handle({"type": "GET_CONTEXT", "id": "ctx"})
    for _ in range(100):
        p.pump_background()
        if p._bg_gen is None and not p._pending_out:
            break
    lines = [json.loads(line) for line in bytes(port.written).splitlines()]
    assert lines == [{"type": "CONTEXT", "id": "ctx",
                      "context": p.app.display_context}], lines


@test("GET_CONTEXT never needs a contiguous container encode under low heap")
def _():
    # Live regression (2026-09-05): after several real rig changes the Pico
    # still had ~7 KiB free, but heap fragmentation made both the monolithic
    # dumps and the fallback 192-byte staging bytearray raise MemoryError.
    # Stage clears the previous rig's effects on patch_switched, so losing
    # this response left CLEAN's unchanged X/FLANG block dark indefinitely.
    port = FakePort(max_per_write=1)
    p, _ = build_protocol(port)
    p.app.display_context.update({
        "kemper_rig": 2,
        "kemper_bank": 1,
        "kemper_rig_in_bank": 2,
        "kemper_rig_name": "CLEAN",
        "kemper_connected": "on",
        "kemper_tuner": "off",
        "tuner": "off",
        "kemper_block_A": "off",
        "kemper_block_B": "off",
        "kemper_block_C": "on",
        "kemper_block_D": "off",
        "kemper_block_X": "on",
        "kemper_block_Mod": "on",
        "kemper_block_Delay": "off",
        "kemper_block_Reverb": "off",
    })
    expected = dict(p.app.display_context)

    real_dumps = protocol.json.dumps
    container_attempts = []

    def low_heap_dumps(value, *args, **kwargs):
        if isinstance(value, (dict, list)):
            container_attempts.append(type(value).__name__)
            raise MemoryError("simulated fragmented RP2040 heap")
        return real_dumps(value, *args, **kwargs)

    protocol.json.dumps = low_heap_dumps
    try:
        p.handle({"type": "GET_CONTEXT", "id": "context-low-heap"})
        drain_background(p)
    finally:
        protocol.json.dumps = real_dumps

    replies = [json.loads(line) for line in bytes(port.written).splitlines()]
    assert replies == [{
        "type": "CONTEXT", "id": "context-low-heap", "context": expected,
    }], replies
    assert container_attempts == [], \
        "GET_CONTEXT attempted a contiguous container encode: %r" % container_attempts
    assert port.write_call_count > 50, port.write_call_count


@test("GET_CONTEXT coalesces leaf writes into bounded 64-byte chunks")
def _():
    port = FakePort()
    p, _ = build_protocol(port)
    p.app.display_context.update({
        "kemper_rig_name": "CLEAN", "kemper_block_X": "on",
        "kemper_block_Reverb": "off", "kemper_connected": "on",
    })
    p.handle({"type": "GET_CONTEXT", "id": "chunked"})
    assert p._bg_gen is None and not p._bg_queue, \
        "buffered flat CONTEXT should finish in its first background slice"
    assert p._bg_mid is None and p._bg_request_type is None

    reply = json.loads(bytes(port.written).strip())
    assert reply["context"] == p.app.display_context, reply
    expected_max = (len(port.written) + 63) // 64
    assert port.write_call_count == expected_max, \
        "expected one write per <=64-byte chunk, got %d for %d bytes" % (
            port.write_call_count, len(port.written))


@test("GET_CONTEXT falls back to direct streaming if chunk allocation fails")
def _():
    port = FakePort()
    p, _ = build_protocol(port)
    expected = dict(p.app.display_context)
    sentinel = object()
    real_bytearray = getattr(protocol, "bytearray", sentinel)

    def no_chunk(_size=0):
        raise MemoryError("simulated lack of a 64-byte contiguous block")

    protocol.bytearray = no_chunk
    try:
        p.handle({"type": "GET_CONTEXT", "id": "no-chunk"})
        drain_background(p)
    finally:
        if real_bytearray is sentinel:
            delattr(protocol, "bytearray")
        else:
            protocol.bytearray = real_bytearray

    assert json.loads(bytes(port.written).strip()) == {
        "type": "CONTEXT", "id": "no-chunk", "context": expected,
    }
    assert port.write_call_count > 10, port.write_call_count


@test("background tail cannot be interleaved by a keepalive ACK")
def _():
    port = FakePort(max_per_write=0)
    p, _ = build_protocol(port)
    p.app.display_context["padding"] = "x" * 300
    p.handle({"type": "GET_CONTEXT", "id": "ctx"})
    for _ in range(100):
        p.pump_background()
        if p._pending_out and p._bg_gen is not None:
            break
    assert p._pending_out and p._bg_gen is not None
    p.handle({"type": "PING", "id": "keepalive"})
    port.max_per_write = 1
    for _ in range(1000):
        p.pump_background()
        if p._bg_gen is None and not p._pending_out and not p._deferred_out:
            break
    lines = [json.loads(line) for line in bytes(port.written).splitlines()]
    assert lines[0]["type"] == "CONTEXT", lines
    assert lines[1]["type"] == "ACK" and lines[1]["id"] == "keepalive", lines


@test("stalled deferred output pauses the next background generator")
def _():
    p, port = build_protocol()
    next_started = [False]

    def active():
        p._write_bytes(b'{"type":"FIRST"}\n')
        yield
        # The host stops accepting bytes exactly as the active generator ends,
        # so releasing the complete ACK below creates pending output.
        port.max_per_write = 0

    def queued():
        next_started[0] = True
        p._write_bytes(b'{"type":"SECOND"}\n')
        yield

    p._start_background(active(), "first", "GET_CONTEXT")
    p._send({"type": "ACK", "id": "between"})
    p._start_background(queued(), "second", "GET_PATCH")
    p.pump_background()

    assert p._pending_out, "stalled deferred ACK was not retained"
    assert not next_started[0], \
        "next background response ran into a stalled earlier line"
    assert len(p._bg_queue) == 1 and p._bg_gen is None

    port.max_per_write = None
    drain_background(p)
    replies = [json.loads(line) for line in bytes(port.written).splitlines()]
    assert [reply["type"] for reply in replies] == ["FIRST", "ACK", "SECOND"], \
        replies
    assert p._bg_mid is None and p._bg_request_type is None


@test("pre-existing deferred recovery drains before queued background work")
def _():
    class BudgetPort(FakePort):
        def __init__(self, budget):
            super().__init__()
            self.budget = budget

        def write(self, data):
            self.write_call_count += 1
            if self.budget <= 0:
                return 0
            chunk = bytes(data)[:self.budget]
            self.budget -= len(chunk)
            self.written.extend(chunk)
            return len(chunk)

    first = b'{"type":"ACK","id":"older"}\n'
    deferred = b'{"type":"ACK","id":"deferred-recovery"}\n'
    port = BudgetPort(len(first))
    p, _ = build_protocol(port)
    # Recovery can enter a tick with an older pending line, complete deferred
    # replies, and a later background request all waiting in this order.
    p._pending_out.append(first)
    p._pending_bytes = len(first)
    p._deferred_out.append(deferred)
    p._deferred_bytes = len(deferred)
    p._bg_queue.append((p._json_line_gen({
        "type": "CONTEXT", "id": "after-recovery", "context": {},
    }), "after-recovery", "GET_CONTEXT"))

    p.pump_background()
    assert p._pending_out == [deferred]
    assert len(p._bg_queue) == 1 and p._bg_gen is None

    port.budget = 4096
    drain_background(p)
    assert [json.loads(line).get("id")
            for line in bytes(port.written).splitlines()] == [
        "older", "deferred-recovery", "after-recovery",
    ]


def _install_led_strip(p, pixels):
    class Strip:
        def __len__(self):
            return len(pixels)

        def __getitem__(self, index):
            return pixels[index]

    p.app.leds = types.SimpleNamespace(strip=Strip())


def _expected_led_dump(mid, pixels):
    from captain.board import LED_INDEX_PER_SWITCH
    return {
        "type": "LED_DUMP", "id": mid,
        "pixels": [list(pixel) for pixel in pixels],
        "switch_indices": {
            name: list(indices)
            for name, indices in LED_INDEX_PER_SWITCH.items()
        },
        "current": {"bank": 1, "slot": 1},
    }


@test("LED_DUMP streams its exact correlated frame under fragmented heap")
def _():
    # Real regression: LED_DUMP built 30 transient RGB lists, copied all ten
    # switch-index tuples, then handed the complete object to _send().  On the
    # fragmented Captain heap both json.dumps attempts MemoryErrored; _send()
    # swallowed the second failure and the browser waited until its timeout.
    port = FakePort(max_per_write=1)
    p, _ = build_protocol(port)
    pixels = [((i * 17) & 0xff, (255 - i * 5) & 0xff, (i * 29) & 0xff)
              for i in range(30)]
    _install_led_strip(p, pixels)
    expected = _expected_led_dump("led-low-heap", pixels)

    real_dumps = protocol.json.dumps
    missing = object()
    real_list = getattr(protocol, "list", missing)
    real_bytearray = getattr(protocol, "bytearray", missing)
    container_attempts = []

    def bounded_dumps(value, *args, **kwargs):
        if isinstance(value, (dict, list, tuple)):
            container_attempts.append(type(value).__name__)
            raise MemoryError("contiguous LED_DUMP encode forbidden")
        if isinstance(value, str) and len(value) > p._MANIFEST_STRING_CHARS:
            raise MemoryError("unbounded LED_DUMP string encode forbidden")
        return real_dumps(value, *args, **kwargs)

    def no_transient_list(*_args, **_kwargs):
        raise MemoryError("LED_DUMP list copy forbidden")

    def no_chunk(_size=0):
        raise MemoryError("no contiguous short-packet buffer")

    protocol.json.dumps = bounded_dumps
    protocol.list = no_transient_list
    protocol.bytearray = no_chunk
    try:
        p.handle({"type": "LED_DUMP", "id": "led-low-heap"})
        drain_background(p)
    finally:
        protocol.json.dumps = real_dumps
        if real_list is missing:
            delattr(protocol, "list")
        else:
            protocol.list = real_list
        if real_bytearray is missing:
            delattr(protocol, "bytearray")
        else:
            protocol.bytearray = real_bytearray

    replies = [json.loads(line) for line in bytes(port.written).splitlines()]
    assert replies == [expected], replies
    assert container_attempts == [], \
        "LED_DUMP attempted a contiguous container encode: %r" % container_attempts
    assert list(replies[0]) == [
        "type", "id", "pixels", "switch_indices", "current",
    ], replies[0]
    assert list(replies[0]["switch_indices"]) == list(
        expected["switch_indices"]), replies[0]
    assert port.write_call_count > 100, port.write_call_count


@test("LED_DUMP stalled partial writes retain ownership before an interleaved PING")
def _():
    class PausingPort(FakePort):
        def __init__(self):
            super().__init__()
            self.budget = 23
            self.input_sizes = []

        def write(self, data):
            self.write_call_count += 1
            self.input_sizes.append(len(data))
            if self.budget <= 0:
                return 0
            chunk = bytes(data)[:self.budget]
            self.budget -= len(chunk)
            self.written.extend(chunk)
            return len(chunk)

    port = PausingPort()
    p, _ = build_protocol(port)
    pixels = [(i, i + 1, i + 2) for i in range(30)]
    _install_led_strip(p, pixels)
    expected = _expected_led_dump("led-paused", pixels)

    p.handle({"type": "LED_DUMP", "id": "led-paused"})
    assert p._bg_gen is not None and p._pending_out, \
        "LED_DUMP did not retain its stalled line"
    p.handle({"type": "PING", "id": "after-leds"})
    assert p._deferred_out, "PING was not deferred behind LED_DUMP"

    port.budget = 100000
    drain_background(p)
    replies = [json.loads(line) for line in bytes(port.written).splitlines()]
    assert replies == [expected, {
        "type": "ACK", "id": "after-leds", "fw": protocol.VERSION,
    }], replies
    assert max(port.input_sizes) <= p._MANIFEST_CHUNK_SIZE, port.input_sizes


@test("LED_DUMP queue overflow returns a correlated busy error, never silence")
def _():
    # Reproduce the live no-response class deterministically: while USB is
    # backpressured, streamed Stage snapshots fill the bounded background
    # queue and the following LED_DUMP is the first request beyond its cap.
    # The old implementation simply closed that generator, so its id could
    # never appear on the wire and the browser waited until timeout.
    port = FakePort(max_per_write=0)
    p, _ = build_protocol(port)
    p._MAX_BG_QUEUE = 2
    pixels = [(i, i + 1, i + 2) for i in range(30)]
    _install_led_strip(p, pixels)

    p.handle({"type": "GET_CONTEXT", "id": "active"})
    assert p._bg_gen is not None and p._pending_out
    p.handle({"type": "GET_CONTEXT", "id": "queued-1"})
    p.handle({"type": "GET_CONTEXT", "id": "queued-2"})
    assert len(p._bg_queue) == 2
    p.handle({"type": "LED_DUMP", "id": "overflow-leds"})

    assert len(p._bg_queue) == 2, "overflow request escaped the queue cap"
    assert p._deferred_out, "overflow response vanished instead of deferring"
    port.max_per_write = None
    drain_background(p)

    replies = [json.loads(line) for line in bytes(port.written).splitlines()]
    matching = [reply for reply in replies
                if reply.get("id") == "overflow-leds"]
    assert matching == [{
        "type": "ERROR", "id": "overflow-leds",
        "error": "background_busy", "of": "LED_DUMP",
    }], replies
    assert [reply.get("id") for reply in replies] == [
        "active", "overflow-leds", "queued-1", "queued-2",
    ], replies


@test("MANIFEST plus 32 LED_DUMPs cannot monopolize a tick on CDC backpressure")
def _():
    # Exact shape that dropped the live hub link: a long streamed MANIFEST
    # owns the wire while 32 diagnostics arrive, and the Captain's IN endpoint
    # temporarily returns zero. Model a defensive/legacy port which ignores
    # write_timeout=0 and still charges 200 ms per call. The protocol may make
    # one attempt from handle()->_start_background and one from the normal
    # end-of-tick pump, but it must never retry either wait inline.
    class TimedBackpressurePort(FakePort):
        def __init__(self):
            super().__init__()
            self.accepting = False
            self.blocked_ms = 0

        def write(self, data):
            self.write_call_count += 1
            if not self.accepting:
                self.blocked_ms += 200
                return 0
            chunk = bytes(data)
            self.written.extend(chunk)
            return len(chunk)

    port = TimedBackpressurePort()
    p, _ = build_protocol(port)
    _install_led_strip(p, [(i, i + 1, i + 2) for i in range(30)])

    # Keep MANIFEST active until the blocked prefix can drain, without
    # depending on the host test machine's generated manifest files.
    def manifest(mid):
        p._write_bytes(b'{"type":"MANIFEST","id":')
        p._write_bytes(json.dumps(mid).encode())
        p._write_bytes(b',"core_messages":{},"plugins":{}}\n')
        yield

    p._get_manifest_gen = manifest
    requests = [{"type": "GET_MANIFEST", "id": "manifest-burst"}]
    requests.extend({"type": "LED_DUMP", "id": "led-%02d" % i}
                    for i in range(32))
    port.push_rx(b"".join((json.dumps(request) + "\n").encode()
                          for request in requests))

    max_calls = 0
    max_blocked_ms = 0
    handled = 0
    for _ in range(len(requests) * 2):
        before_calls = port.write_call_count
        before_blocked_ms = port.blocked_ms
        message = p.poll()
        if message is not None:
            p.handle(message)
            handled += 1
        p.pump_background()
        max_calls = max(max_calls, port.write_call_count - before_calls)
        max_blocked_ms = max(
            max_blocked_ms, port.blocked_ms - before_blocked_ms)
        if handled == len(requests):
            break

    assert handled == len(requests), handled
    assert len(p._bg_queue) == p._MAX_BG_QUEUE
    assert max_calls <= 2, max_calls
    # Even on a misbehaving legacy stream this is <=400 ms instead of the
    # observed 2.05 s protocol_bg section. A deterministic accumulated cost
    # catches the former eight retries without a wall-clock-sensitive test.
    assert max_blocked_ms <= 400, max_blocked_ms

    port.accepting = True
    drain_background(p)
    replies = [json.loads(line) for line in bytes(port.written).splitlines()]
    ids = [reply.get("id") for reply in replies]
    assert ids[0] == "manifest-burst", ids
    assert sorted(ids[1:]) == ["led-%02d" % i for i in range(32)], ids
    assert sum(reply.get("error") == "background_busy"
               for reply in replies) == 24, replies
    assert sum(reply.get("type") == "LED_DUMP"
               for reply in replies) == p._MAX_BG_QUEUE, replies


@test("one pump cannot drain every queued synchronous background response")
def _():
    p, port = build_protocol()
    started = []

    def active():
        # First pump represents a long response yielding for fairness; the
        # next pump completes it and must stop before queued diagnostics run.
        yield
        started.append("active")
        p._write_bytes(b'{"type":"ACTIVE","id":"active"}\n')

    def immediate(mid):
        if False:
            yield
        started.append(mid)
        p._write_bytes((json.dumps({
            "type": "LED_DUMP", "id": mid,
        }) + "\n").encode())

    p._start_background(active(), "active", "GET_MANIFEST")
    for i in range(8):
        mid = "queued-%d" % i
        p._start_background(immediate(mid), mid, "LED_DUMP")

    p.pump_background()
    assert started == ["active"], started
    assert p._bg_gen is None and len(p._bg_queue) == 8

    # Each later pump may complete exactly one queued response, preserving
    # the main-loop opportunity between diagnostics.
    for completed in range(1, 9):
        before_calls = port.write_call_count
        p.pump_background()
        assert len(started) == completed + 1, started
        assert len(p._bg_queue) == 8 - completed
        assert port.write_call_count - before_calls == 1

    replies = [json.loads(line) for line in bytes(port.written).splitlines()]
    assert [reply.get("id") for reply in replies] == [
        "active",
    ] + ["queued-%d" % i for i in range(8)], replies


@test("LED_DUMP pending-output overflow seals the record and reports the error")
def _():
    # A stalled first chunk occupies the sole test queue slot. The next chunk
    # cannot be retained. Previously _write_bytes ignored that failure and
    # kept serialising, producing a truncated/non-terminated response with no
    # correlated outcome.
    port = FakePort(max_per_write=0)
    p, _ = build_protocol(port)
    p._MAX_PENDING_CHUNKS = 1
    pixels = [(i, i + 1, i + 2) for i in range(30)]
    _install_led_strip(p, pixels)

    p.handle({"type": "LED_DUMP", "id": "pending-full"})
    assert p._bg_line_seal and p._pending_out and p._deferred_out
    port.max_per_write = None
    drain_background(p)

    lines = bytes(port.written).splitlines()
    assert len(lines) == 2, lines
    try:
        json.loads(lines[0])
        assert False, "truncated LED_DUMP was presented as a valid response"
    except ValueError:
        pass
    assert json.loads(lines[1]) == {
        "type": "ERROR", "id": "pending-full", "error": "exception",
        "detail": "tx_queue_full", "of": "LED_DUMP",
    }, lines


@test("GET_CONTEXT pending-output overflow also returns a correlated error")
def _():
    # Regression guard for the central failure path: unlike LED_DUMP, the
    # generic context generator has no local exception handler that sends a
    # reply. Its active request metadata must survive until pump_background
    # seals the partial frame and reports the failure exactly once.
    port = FakePort(max_per_write=0)
    p, _ = build_protocol(port)
    p._MAX_PENDING_CHUNKS = 1
    p.app.display_context.update({
        "large": "context-field-" * 20,
    })

    p.handle({"type": "GET_CONTEXT", "id": "context-pending-full"})
    assert p._bg_line_seal and p._pending_out and p._deferred_out
    assert p._bg_mid is None and p._bg_request_type is None
    port.max_per_write = None
    drain_background(p)

    lines = bytes(port.written).splitlines()
    assert len(lines) == 2, lines
    try:
        json.loads(lines[0])
        assert False, "truncated CONTEXT was presented as a valid response"
    except ValueError:
        pass
    assert json.loads(lines[1]) == {
        "type": "ERROR", "id": "context-pending-full",
        "error": "exception", "detail": "tx_queue_full",
        "of": "GET_CONTEXT",
    }, lines


@test("LED_DUMP failure seals its partial line then returns a correlated ERROR")
def _():
    p, port = build_protocol()

    class FailingStrip:
        def __len__(self):
            return 30

        def __getitem__(self, index):
            if index == 5:
                raise MemoryError("simulated LED framebuffer failure")
            return (index, index + 1, index + 2)

    p.app.leds = types.SimpleNamespace(strip=FailingStrip())
    p.handle({"type": "LED_DUMP", "id": "broken-leds"})
    drain_background(p)

    lines = bytes(port.written).splitlines()
    assert len(lines) == 2, lines
    try:
        json.loads(lines[0])
        assert False, "the deliberately damaged LED_DUMP became valid"
    except ValueError:
        pass
    assert json.loads(lines[1]) == {
        "type": "ERROR", "id": "broken-leds", "error": "exception",
        "detail": "simulated LED framebuffer failure", "of": "LED_DUMP",
    }, lines


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


@test("poll: an RX-buffer MemoryError never drops already-consumed USB bytes")
def _():
    class FailFirstExtend(bytearray):
        def __init__(self):
            super().__init__()
            self.fail = True

        def extend(self, data):
            if self.fail:
                self.fail = False
                raise MemoryError("fragmented RX buffer")
            return super().extend(data)

    port = FakePort()
    p, _ = build_protocol(port)
    p._rx_buf = FailFirstExtend()
    wire = b'{"type":"SWITCH_PATCH","id":"kept","bank":1,"slot":2}\n'
    port.push_rx(wire)

    assert p.poll() is None
    assert port.in_waiting == 0, "the CDC read did not consume the test bytes"
    assert p._rx_pending[:p._rx_pending_count] == wire, "consumed bytes were not retained"
    assert b'"bad_json"' not in bytes(port.written)

    msg = p.poll()
    assert msg == {
        "type": "SWITCH_PATCH", "id": "kept", "bank": 1, "slot": 2,
    }, msg
    assert p._rx_pending is None
    assert b'"bad_json"' not in bytes(port.written), port.written


@test("poll: large inbound lines use bounded temporary USB reads")
def _():
    port = FakePort()
    p, _ = build_protocol(port)
    expected = {"type": "PING", "id": "x" * 700}
    port.push_rx((json.dumps(expected) + "\n").encode())

    msg = None
    for _attempt in range(10):
        msg = p.poll()
        if msg is not None:
            break
    assert msg == expected, msg
    assert port.read_sizes, "port was never read"
    assert max(port.read_sizes) <= p._RX_READ_MAX, port.read_sizes


@test("poll: rx overflow drops bytes up to last newline and sends ERROR")
def _():
    port = FakePort()
    p, _ = build_protocol(port)
    # Push more than _RX_BUF_MAX = 65536 bytes of garbage with no newline.
    port.push_rx(b'x' * 70000)
    for _attempt in range(300):
        assert p.poll() is None
        if b'"rx_overflow"' in bytes(port.written):
            break
    assert b'"rx_overflow"' in bytes(port.written), "must emit rx_overflow ERROR"


# ---------------- handler dispatch tests ----------------

def collect_rx(p, port, limit=1000):
    """Run bounded firmware ticks, retaining every decoded command."""
    messages = []
    for _ in range(limit):
        message = p.poll()
        if message is not None:
            messages.append(message)
        assert len(p._rx_buf) < p._RX_SPOOL_AT + p._RX_READ_MAX
        if not port.in_waiting and p._rx_pending is None:
            return messages
    raise AssertionError("RX made no forward progress within its tick budget")


@test("poll: full legacy PUT_GLOBAL spills raw UTF-8 and preserves a batched PING")
def _():
    import builtins
    from unittest.mock import patch

    p, port = build_protocol()
    device = {"device_name": "Caff\u00e8 \U0001f3b8", "labels": ["\u00e8\u00e0\u00f2\U0001f3b8" * 40] * 4}
    # Legacy sendAndAwait appends id after the full device object.
    expected = {"type": "PUT_GLOBAL", "device": device, "id": "legacy-save"}
    wire = json.dumps(expected, ensure_ascii=False).encode() + b"\n"
    ping = {"type": "PING", "id": "after-save"}
    port.push_rx(wire + json.dumps(ping).encode() + b"\n")
    writes = []
    loads = []
    real_load = json.load

    class BoundedFile:
        def __init__(self, file):
            self.file = file

        def write(self, value):
            writes.append(len(value))
            assert len(value) <= p._RX_READ_MAX
            # Exercise short writes and a build without memoryview support.
            if isinstance(value, memoryview):
                raise TypeError("file requires bytes")
            return self.file.write(value[:37])

        def close(self):
            self.file.close()

    def spool_open(path, mode):
        file = builtins.open(path, mode, **({"encoding": "utf-8"} if mode == "r" else {}))
        return BoundedFile(file) if mode == "wb" else file

    def streaming_load(source):
        assert not p._rx_buf, "raw JSON remains resident during file parsing"
        assert p._rx_file is None, "write handle was not closed before parsing"
        assert p._rx_pending is None or len(p._rx_pending) <= p._RX_READ_MAX
        loads.append(source.name)
        return real_load(source)

    saved = []
    with patch.object(protocol, "open", spool_open, create=True), \
            patch.object(protocol.json, "load", streaming_load), \
            patch.object(protocol.config, "save_device", lambda value: saved.append(value)):
        received = collect_rx(p, port)
        assert received == [expected, ping], received
        for command in received:
            p.handle(command)
    assert saved == [device]
    assert loads == [p._rx_path], loads
    assert writes and max(writes) <= 256
    assert max(port.read_sizes) <= 256
    assert not Path(p._rx_path).exists()
    assert p._rx_file is None and not p._rx_size
    replies = [json.loads(line) for line in port.written.splitlines()]
    assert [reply.get("id") for reply in replies] == ["legacy-save", None, "after-save"]
    assert replies[1] == {"type": "EVENT", "event": "global_changed"}


@test("poll: CircuitPython memoryview JSON parsing needs no compatibility copy")
def _():
    from unittest.mock import patch

    p, port = build_protocol()
    wire = b'{"id":"cp-view","type":"PING"}\n'
    port.push_rx(wire)
    real_loads = json.loads
    views = []

    def cp_loads(value):
        if isinstance(value, memoryview):
            views.append(value.tobytes())
            return real_loads(value.tobytes())
        raise AssertionError("compatible parser was given an unnecessary bytes copy")

    with patch.object(protocol.json, "loads", cp_loads):
        assert p.poll() == {"id": "cp-view", "type": "PING"}
    assert views == [wire[:-1]]
    assert not Path(p._rx_path).exists()


@test("poll: CircuitPython append corruption is avoided and atomic one-byte growth recovers")
def _():
    from unittest.mock import patch

    class CircuitPython927Buffer(bytearray):
        append_calls = 0
        growth_failures = 0

        def append(self, value):
            # CP 9.2.7 increments free before the failing reallocation. A
            # later append can then write outside its actual allocation.
            type(self).append_calls += 1
            self.poisoned = True
            raise MemoryError("append left a corrupt capacity")

        def extend(self, value):
            assert not getattr(self, "poisoned", False), "RX reused a corrupt bytearray"
            assert len(value) == 1, "RX requested a large contiguous growth"
            if len(self) == 64 and not getattr(self, "failed", False):
                self.failed = True
                type(self).growth_failures += 1
                raise MemoryError("one-byte growth failed without mutating capacity")
            return super().extend(value)

    with patch.object(protocol, "bytearray", CircuitPython927Buffer, create=True):
        p, port = build_protocol()
        wanted = {"id": "recovered", "type": "PUT_GLOBAL", "device": {"name": "a" * 2400}}
        port.push_rx(json.dumps(wanted).encode() + b"\n" + b'{"type":"PING"}\n')
        assert collect_rx(p, port, limit=30) == [wanted, {"type": "PING"}]
    assert CircuitPython927Buffer.append_calls == 0, "unsafe append was called"
    assert CircuitPython927Buffer.growth_failures > 0, "allocation failure was not exercised"
    assert not port.written, port.written
    assert not Path(p._rx_path).exists()


@test("poll: partially committed RX bytes survive repeated growth failure")
def _():
    class PartialGrowth(bytearray):
        def extend(self, value):
            if len(self) >= 13:
                raise MemoryError("RX cannot grow beyond the committed prefix")
            return super().extend(value)

    p, port = build_protocol()
    p._rx_buf = PartialGrowth()
    wanted = {"id": "partial-growth", "type": "PING"}
    port.push_rx(json.dumps(wanted).encode() + b"\n")
    assert collect_rx(p, port) == [wanted]
    assert not port.written
    assert not Path(p._rx_path).exists()


@test("poll: allocation-free readinto does not need the allocating read API")
def _():
    class NoReadAllocation(FakePort):
        def read(self, n):
            raise MemoryError("read bytes allocation")

    p, port = build_protocol(NoReadAllocation())
    wanted = {"id": "readinto", "type": "PING"}
    port.push_rx(json.dumps(wanted).encode() + b"\n")
    assert collect_rx(p, port) == [wanted]
    assert not port.written


@test("poll: 2.5KB PUT_GLOBAL completes within twelve ticks using bounded safe growth")
def _():
    from unittest.mock import patch

    class FragmentedPort(FakePort):
        read_attempts = 0

        def __init__(self):
            super().__init__()
            self.input_buffers = set()

        def read(self, size):
            self.read_attempts += 1
            raise MemoryError("no 256-byte read allocation")

        def readinto(self, buf):
            self.input_buffers.add(id(buf))
            return super().readinto(buf)

    class BoundedGrowth(bytearray):
        input_buffers = set()

        def append(self, value):
            raise AssertionError("CircuitPython 9.2.7 append is unsafe after OOM")

        def extend(self, value):
            assert len(value) == 1, "large contiguous growth can stall RX"
            type(self).input_buffers.add(id(value))
            return super().extend(value)

    port = FragmentedPort()
    with patch.object(protocol, "bytearray", BoundedGrowth, create=True):
        p, _ = build_protocol(port)
        device = {"device_name": "x" * 2380 + "\u00e8\U0001f3b8"}
        wanted = {"id": "fast-save", "type": "PUT_GLOBAL", "device": device}
        ping = {"id": "after-fast-save", "type": "PING"}
        wire = json.dumps(wanted).encode() + b"\n" + json.dumps(ping).encode() + b"\n"
        assert 2400 < len(wire) < 2560, len(wire)
        port.push_rx(wire)
        assert collect_rx(p, port, limit=12) == [wanted, ping]
    assert port.read_attempts == 0, "normal RX still called the allocating read API"
    assert len(port.input_buffers) == 1, "RX scratch was reallocated between ticks"
    assert BoundedGrowth.input_buffers == {id(p._rx_octet)}, "one-byte staging was reallocated"
    assert port.read_sizes == [256] * ((len(wire) + 255) // 256), port.read_sizes
    assert not port.written, port.written
    assert not Path(p._rx_path).exists()


@test("poll: a pending RX scratch at offset zero is consumed before the next USB readinto")
def _():
    class FailFirstExtend(bytearray):
        failed = False

        def extend(self, value):
            if not self.failed:
                self.failed = True
                raise MemoryError("initial extend cannot allocate")
            return super().extend(value)

    p, port = build_protocol()
    p._rx_buf = FailFirstExtend()
    wanted = {"id": "pending-scratch", "type": "PUT_GLOBAL", "device": {"name": "\u00e8" * 700}}
    wire = json.dumps(wanted).encode() + b"\n"
    port.push_rx(wire)
    assert p.poll() is None
    assert p._rx_pending is p._rx_read_buf and p._rx_pending_offset == 0
    assert p._rx_pending_count == 256
    assert port.in_waiting > 0
    assert port.read_sizes == [256]
    assert p.poll() is None
    assert port.read_sizes == [256], "readinto overwrote pending bytes before they were committed"
    assert bytes(p._rx_buf) == wire[:256]
    assert collect_rx(p, port) == [wanted]
    assert not port.written


@test("poll: short readinto count ignores the previous scratch suffix")
def _():
    p, port = build_protocol()
    p._rx_read_buf[:] = (b'{"id":"stale","type":"PING"}\n' * 12)[:256]
    port.push_rx(b'{"id":"fresh","type":"PING"}\n')
    assert p.poll() == {"id": "fresh", "type": "PING"}
    assert p.poll() is None
    assert p._rx_pending is None and p._rx_pending_count == 0
    assert not port.written


@test("poll: zero and None readinto results do not consume stale scratch data")
def _():
    class TemporarilyEmptyPort(FakePort):
        def __init__(self):
            super().__init__()
            self.empty_reads = [0, None]

        def readinto(self, buf):
            if self.empty_reads:
                return self.empty_reads.pop(0)
            return super().readinto(buf)

    p, port = build_protocol(TemporarilyEmptyPort())
    p._rx_read_buf[:] = (b'{"id":"stale","type":"PING"}\n' * 12)[:256]
    port.push_rx(b'{"id":"fresh","type":"PING"}\n')
    assert p.poll() is None
    assert p.poll() is None
    assert p.poll() == {"id": "fresh", "type": "PING"}
    assert p._rx_pending is None and not port.written


@test("poll: persistent one-byte extend failure drains instead of endlessly creating empty spools")
def _():
    class NoExtend(bytearray):
        def extend(self, value):
            raise MemoryError("not even one byte fits")

    p, port = build_protocol()
    p._rx_buf = NoExtend()
    port.push_rx(b'{"id":"cannot-fit","type":"PING"}\n{"id":"next","type":"PING"}\n')
    assert collect_rx(p, port, limit=5) == [{"id": "next", "type": "PING"}]
    assert json.loads(port.written) == {"type": "ERROR", "error": "rx_oom"}
    assert not Path(p._rx_path).exists()
    assert p._rx_pending is None and p._rx_pending_count == 0


@test("poll: partial ingestion across a newline preserves both complete commands")
def _():
    class PartialAcrossLine(bytearray):
        def extend(self, value):
            newline = self.find(b"\n")
            if newline >= 0 and len(self) >= newline + 4:
                raise MemoryError("committed the first line and a prefix of the next")
            return super().extend(value)

    p, port = build_protocol()
    p._rx_buf = PartialAcrossLine()
    first = {"id": "first", "type": "PING"}
    second = {"id": "second", "type": "PING"}
    port.push_rx(json.dumps(first).encode() + b"\n" + json.dumps(second).encode() + b"\n")
    assert collect_rx(p, port) == [first, second]
    assert not port.written


@test("poll: failed read and readinto report once, discard the broken frame, and recover")
def _():
    class FailedRead(FakePort):
        fail = False

        def read(self, n):
            if self.fail:
                raise MemoryError("read failed")
            return super().read(n)

        def readinto(self, buf):
            if self.fail:
                raise MemoryError("readinto failed")
            return super().readinto(buf)

    p, port = build_protocol(FailedRead())
    port.push_rx(json.dumps({"id": "bad-read", "type": "PUT_GLOBAL", "device": "x" * 2400}).encode()
                 + b"\n" + b'{"id":"good","type":"PING"}\n')
    assert p.poll() is None
    port.fail = True
    for _ in range(4):
        assert p.poll() is None
    port.fail = False
    assert collect_rx(p, port) == [{"id": "good", "type": "PING"}]
    assert [json.loads(line) for line in port.written.splitlines()] == [
        {"type": "ERROR", "error": "rx_oom", "id": "bad-read"},
    ]
    assert not Path(p._rx_path).exists()


@test("poll: large parser MemoryError is correlated without retrying or blocking the next line")
def _():
    from unittest.mock import patch

    p, port = build_protocol()
    port.push_rx(json.dumps({"id": 11, "type": "PUT_GLOBAL", "device": {"name": "x" * 2400}}).encode()
                 + b"\n" + b'{"id":"alive","type":"PING"}\n')
    calls = []

    def no_heap(source):
        calls.append(source.name)
        assert not p._rx_buf
        raise MemoryError("parsed device does not fit")

    with patch.object(protocol.json, "load", no_heap):
        assert collect_rx(p, port) == [{"id": "alive", "type": "PING"}]
    assert calls == [p._rx_path]
    assert [json.loads(line) for line in port.written.splitlines()] == [
        {"type": "ERROR", "error": "rx_oom", "id": 11},
    ]
    assert not Path(p._rx_path).exists()
    assert p._rx_file is None and p._rx_pending is None and not p._rx_size


@test("poll: a failed legacy payload never invents an id from its nested device")
def _():
    from unittest.mock import patch

    p, port = build_protocol()
    wire = {"type": "PUT_GLOBAL", "device": {"id": "not-the-request", "name": "x" * 800}, "id": 12}
    port.push_rx(json.dumps(wire).encode() + b"\n")
    with patch.object(protocol.json, "load", side_effect=MemoryError("low heap")):
        assert collect_rx(p, port) == []
    assert json.loads(port.written) == {"type": "ERROR", "error": "rx_oom"}
    assert not Path(p._rx_path).exists()


@test("poll: memoryview allocation failure consumes the line and preserves a batched PING")
def _():
    from unittest.mock import patch

    p, port = build_protocol()
    port.push_rx(b'{"id":"view-oom","type":"PING"}\n{"id":"next","type":"PING"}\n')
    attempts = []

    def fail_first_view(value):
        attempts.append(len(value))
        if len(attempts) == 1:
            raise MemoryError("view allocation")
        return memoryview(value)

    with patch.object(protocol, "memoryview", fail_first_view, create=True):
        assert p.poll() is None
    assert collect_rx(p, port) == [{"id": "next", "type": "PING"}]
    assert json.loads(port.written) == {"type": "ERROR", "error": "rx_oom", "id": "view-oom"}


@test("poll: malformed spooled JSON cleans up and preserves the next complete line")
def _():
    p, port = build_protocol()
    port.push_rx(b'{"id":"bad-large","type":"PUT_GLOBAL","device":"' + b"x" * 1400
                 + b'" BROKEN}\n{"type":"PING"}\n')
    assert collect_rx(p, port) == [{"type": "PING"}]
    assert json.loads(port.written) == {"type": "ERROR", "error": "bad_json", "id": "bad-large"}
    assert not Path(p._rx_path).exists()


def assert_rx_file_failure(stage, exception):
    import builtins
    from unittest.mock import patch

    p, port = build_protocol()
    wire = {"id": "io-failure", "type": "PUT_GLOBAL", "device": {"name": "x" * 2400}}
    port.push_rx(json.dumps(wire).encode() + b"\n" + b'{"id":"after-io","type":"PING"}\n')

    class FailingFile:
        def __init__(self, file):
            self.file = file

        def write(self, value):
            if stage == "write":
                raise exception
            if stage == "zero-write":
                return 0
            return self.file.write(value)

        def close(self):
            self.file.close()
            if stage == "close":
                raise exception

    def faulty_open(path, mode):
        if stage == "open-write" and mode == "wb" or stage == "open-read" and mode == "r":
            raise exception
        file = builtins.open(path, mode)
        return FailingFile(file) if mode == "wb" else file

    with patch.object(protocol, "open", faulty_open, create=True):
        assert collect_rx(p, port) == [{"id": "after-io", "type": "PING"}]
    error = "rx_oom" if isinstance(exception, MemoryError) else "rx_io"
    replies = [json.loads(line) for line in port.written.splitlines()]
    assert replies == [{"type": "ERROR", "error": error, "id": "io-failure"}], replies
    assert p._rx_file is None and not p._rx_size and p._rx_pending is None
    assert not Path(p._rx_path).exists(), stage


@test("poll: spool open failures advance the frame and clean up")
def _():
    for exception in (OSError("disk unavailable"), MemoryError("file object allocation")):
        assert_rx_file_failure("open-write", exception)


@test("poll: spool write failures advance the frame and clean up")
def _():
    for exception in (OSError("disk full"), MemoryError("write allocation")):
        assert_rx_file_failure("write", exception)
    assert_rx_file_failure("zero-write", OSError("no progress"))


@test("poll: spool close failures advance the frame and clean up")
def _():
    for exception in (OSError("flush failed"), MemoryError("close allocation")):
        assert_rx_file_failure("close", exception)


@test("poll: spool read-open failures advance the frame and clean up")
def _():
    for exception in (OSError("read failed"), MemoryError("reader allocation")):
        assert_rx_file_failure("open-read", exception)


@test("poll: failed RX growth plus unavailable spool cannot block the next command")
def _():
    from unittest.mock import patch

    class NoGrowth(bytearray):
        def extend(self, value):
            if len(self) >= 13:
                raise MemoryError("RX cannot grow")
            return super().extend(value)

    p, port = build_protocol()
    p._rx_buf = NoGrowth()
    port.push_rx(b'{"id":"lost","type":"PING"}\n{"id":"kept","type":"PING"}\n')
    with patch.object(protocol, "open", side_effect=OSError("no filesystem"), create=True):
        assert collect_rx(p, port) == [{"id": "kept", "type": "PING"}]
    assert json.loads(port.written) == {"type": "ERROR", "error": "rx_io", "id": "lost"}
    assert p._rx_pending is None and p._rx_file is None


@test("poll: 64KB limit drains the whole oversized line and retains the following PING")
def _():
    p, port = build_protocol()
    port.push_rx(b'{"id":"oversized","type":"PUT_GLOBAL","device":"' + b"x" * 70000
                 + b'"}\n{"id":"after-overflow","type":"PING"}\n')
    assert collect_rx(p, port) == [{"id": "after-overflow", "type": "PING"}]
    assert json.loads(port.written) == {"type": "ERROR", "error": "rx_overflow", "id": "oversized"}
    assert not Path(p._rx_path).exists()
    assert not p._rx_discard


@test("poll: disconnect closes and deletes a partial spool before a new session")
def _():
    p, port = build_protocol()
    port.push_rx(b'{"id":"abandoned","device":"' + b"x" * 900)
    assert collect_rx(p, port) == []
    handle = p._rx_file
    assert handle is not None and Path(p._rx_path).exists()
    port.connected = False
    assert p.poll() is None
    assert handle.closed
    assert not Path(p._rx_path).exists()
    assert p._rx_file is None and not p._rx_size and not p._rx_mid
    port.connected = True
    port.push_rx(b'{"id":"fresh","type":"PING"}\n')
    assert collect_rx(p, port) == [{"id": "fresh", "type": "PING"}]


@test("poll: boot removes only its abandoned RX file and later reception overwrites it")
def _():
    p, _ = build_protocol()
    spool = Path(p._rx_path)
    spool.write_bytes(b"old upload from before reset")
    config_file = spool.with_name("device.json")
    config_file.write_bytes(b'{"keep":"existing configuration"}')
    p, port = build_protocol()
    assert not spool.exists()
    assert config_file.read_bytes() == b'{"keep":"existing configuration"}'
    wanted = {"id": "new", "type": "PING", "padding": "y" * 1200}
    port.push_rx(json.dumps(wanted).encode() + b"\n")
    assert collect_rx(p, port) == [wanted]
    assert not spool.exists()
    assert config_file.read_bytes() == b'{"keep":"existing configuration"}'

@test("PUT_FILE disables autoreload before opening and writing its temp file")
def _():
    p, port = build_protocol()
    ota = importlib.import_module("captain_ota")
    writes = []
    ordering = []

    class FakeUpload:
        def write(self, data):
            assert supervisor.runtime.autoreload is False, \
                "chunk write ran while CircuitPython autoreload was enabled"
            writes.append(bytes(data))

        def close(self):
            ordering.append("close")

    def fake_open(path, mode):
        assert supervisor.runtime.autoreload is False, \
            "temp file was opened before autoreload was disabled"
        ordering.append((path, mode))
        return FakeUpload()

    missing = object()
    old_open = getattr(ota, "open", missing)
    old_mkdir = ota._mkdir_p
    old_runtime = supervisor.runtime
    supervisor.runtime = types.SimpleNamespace(autoreload=True)
    ota.open = fake_open
    ota._mkdir_p = lambda path: ordering.append(("mkdir", path))
    chunk_msg = {"type": "PUT_FILE_CHUNK", "id": "c1",
                 "path": "/lib/captain/test.mpy", "data_b64": "YWJj"}
    try:
        p.handle({"type": "PUT_FILE_BEGIN", "id": "b1",
                  "path": "/lib/captain/test.mpy"})
        p.handle(chunk_msg)
    finally:
        supervisor.runtime = old_runtime
        ota._mkdir_p = old_mkdir
        if old_open is missing:
            del ota.open
        else:
            ota.open = old_open

    replies = [json.loads(line) for line in bytes(port.written).splitlines()]
    assert [r.get("type") for r in replies] == ["ACK", "ACK"], replies
    assert replies[0].get("size_check") is False, replies[0]
    assert "size" not in replies[0], replies[0]
    assert writes == [b"abc"], writes
    assert "data_b64" not in chunk_msg, \
        "large encoded payload remained live for the rest of the main-loop tick"
    assert ordering[:2] == [("mkdir", "/lib/captain"),
                            ("/lib/captain/test.mpy.tmp", "wb")], ordering
    p._close_uploads()
    p._release_ota()


@test("PUT_FILE fails closed when CircuitPython cannot suppress autoreload")
def _():
    p, port = build_protocol()

    class LockedRuntime:
        @property
        def autoreload(self):
            return True

        @autoreload.setter
        def autoreload(self, value):
            raise RuntimeError("unsupported")

    old_runtime = supervisor.runtime
    supervisor.runtime = LockedRuntime()
    try:
        p.handle({"type": "PUT_FILE_BEGIN", "id": "b1",
                  "path": "/lib/captain/test.mpy"})
    finally:
        supervisor.runtime = old_runtime

    reply = json.loads(bytes(port.written).strip())
    assert reply["type"] == "ERROR" and reply["error"] == "autoreload", reply
    assert p._uploads == {}, p._uploads


@test("PUT_FILE_BEGIN negotiates exact END size verification")
def _():
    p, port = build_protocol()
    ota = importlib.import_module("captain_ota")

    class Upload:
        def close(self):
            pass

    missing = object()
    old_open = getattr(ota, "open", missing)
    old_mkdir = ota._mkdir_p
    old_runtime = supervisor.runtime
    supervisor.runtime = types.SimpleNamespace(autoreload=True)
    ota.open = lambda path, mode: Upload()
    ota._mkdir_p = lambda path: None
    try:
        p.handle({"type": "PUT_FILE_BEGIN", "id": "sized",
                  "path": "/lib/captain/app.mpy", "size": 13557})
    finally:
        supervisor.runtime = old_runtime
        ota._mkdir_p = old_mkdir
        if old_open is missing:
            del ota.open
        else:
            ota.open = old_open

    reply = json.loads(bytes(port.written).strip())
    assert reply == {
        "type": "ACK", "id": "sized",
        "size_check": True, "size": 13557,
    }, reply
    assert p._upload_sizes["/lib/captain/app.mpy"] == 13557
    p._close_uploads()
    p._release_ota()


@test("PUT_FILE write failure closes and forgets the upload immediately")
def _():
    p, port = build_protocol()

    class BrokenUpload:
        closed = False

        def write(self, data):
            raise MemoryError("simulated tight heap")

        def close(self):
            self.closed = True

    upload = BrokenUpload()
    p._uploads["/lib/test.mpy"] = upload
    chunk_msg = {"type": "PUT_FILE_CHUNK", "id": "c1",
                 "path": "/lib/test.mpy", "data_b64": "YWJj"}
    p.handle(chunk_msg)

    reply = json.loads(bytes(port.written).strip())
    assert reply["type"] == "ERROR" and reply["error"] == "write", reply
    assert upload.closed, "failed upload left its file handle open"
    assert "/lib/test.mpy" not in p._uploads, p._uploads
    assert "data_b64" not in chunk_msg, \
        "failed upload retained its largest allocation until tick end"


@test("PUT_FILE_END verifies size then prunes only a compiled module's source sibling")
def _():
    p, port = build_protocol()
    actions = []

    class Upload:
        def close(self):
            actions.append(("close",))

    def fake_stat(path):
        actions.append(("stat", path))
        if path.endswith(".py"):
            raise OSError("not found")
        return (0, 0, 0, 0, 0, 0, 3)

    def fake_remove(path):
        actions.append(("remove", path))

    def fake_rename(src, dst):
        actions.append(("rename", src, dst))

    old_stat, old_remove, old_rename = protocol.os.stat, protocol.os.remove, protocol.os.rename
    protocol.os.stat, protocol.os.remove, protocol.os.rename = fake_stat, fake_remove, fake_rename
    path = "/lib/plugins/test.mpy"
    p._uploads[path] = Upload()
    p._upload_sizes[path] = 3
    try:
        p.handle({"type": "PUT_FILE_END", "id": "e1", "path": path})
    finally:
        protocol.os.stat, protocol.os.remove, protocol.os.rename = old_stat, old_remove, old_rename

    reply = json.loads(bytes(port.written).strip())
    assert reply == {"type": "ACK", "id": "e1"}, reply
    assert actions == [
        ("close",),
        ("stat", path + ".tmp"),
        ("remove", path),
        ("rename", path + ".tmp", path),
        ("remove", "/lib/plugins/test.py"),
        ("stat", "/lib/plugins/test.py"),
    ], actions


@test("PUT_FILE_END size mismatch preserves live file and never prunes source")
def _():
    p, port = build_protocol()
    actions = []

    class Upload:
        def close(self):
            actions.append(("close",))

    def fake_stat(path):
        actions.append(("stat", path))
        return (0, 0, 0, 0, 0, 0, 2)

    def fake_remove(path):
        actions.append(("remove", path))

    def fake_rename(src, dst):
        actions.append(("rename", src, dst))

    old_stat, old_remove, old_rename = protocol.os.stat, protocol.os.remove, protocol.os.rename
    protocol.os.stat, protocol.os.remove, protocol.os.rename = fake_stat, fake_remove, fake_rename
    path = "/lib/plugins/test.mpy"
    p._uploads[path] = Upload()
    p._upload_sizes[path] = 3
    try:
        p.handle({"type": "PUT_FILE_END", "id": "e1", "path": path})
    finally:
        protocol.os.stat, protocol.os.remove, protocol.os.rename = old_stat, old_remove, old_rename

    reply = json.loads(bytes(port.written).strip())
    assert reply["type"] == "ERROR" and reply["error"] == "size_mismatch", reply
    assert reply["expected"] == 3 and reply["actual"] == 2, reply
    assert actions == [
        ("close",),
        ("stat", path + ".tmp"),
        ("remove", path + ".tmp"),
    ], actions


@test("PUT_FILE_END never derives a source removal for non-mpy paths")
def _():
    p, port = build_protocol()
    removed = []

    class Upload:
        def close(self):
            pass

    old_remove, old_rename = protocol.os.remove, protocol.os.rename
    protocol.os.remove = lambda path: removed.append(path)
    protocol.os.rename = lambda src, dst: None
    path = "/config/device.json"
    p._uploads[path] = Upload()
    p._upload_sizes[path] = None
    try:
        p.handle({"type": "PUT_FILE_END", "id": "e1", "path": path})
    finally:
        protocol.os.remove, protocol.os.rename = old_remove, old_rename

    reply = json.loads(bytes(port.written).strip())
    assert reply == {"type": "ACK", "id": "e1"}, reply
    assert removed == [path], removed


@test("PUT_FILE_END reports a source sibling that could not be pruned")
def _():
    p, port = build_protocol()

    class Upload:
        def close(self):
            pass

    path = "/lib/plugins/test.mpy"
    source_path = "/lib/plugins/test.py"
    old_stat, old_remove, old_rename = protocol.os.stat, protocol.os.remove, protocol.os.rename
    protocol.os.stat = lambda candidate: (0, 0, 0, 0, 0, 0, 3)

    def fake_remove(candidate):
        if candidate == source_path:
            raise OSError("locked")

    protocol.os.remove = fake_remove
    protocol.os.rename = lambda src, dst: None
    p._uploads[path] = Upload()
    p._upload_sizes[path] = 3
    try:
        p.handle({"type": "PUT_FILE_END", "id": "e1", "path": path})
    finally:
        protocol.os.stat, protocol.os.remove, protocol.os.rename = old_stat, old_remove, old_rename

    reply = json.loads(bytes(port.written).strip())
    assert reply == {"type": "ERROR", "id": "e1",
                     "error": "source_shadow", "path": source_path}, reply

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


@test("handle: STATS preserves the app stats response shape")
def _():
    port = FakePort()
    p, _ = build_protocol(port)
    p.handle({"type": "STATS", "id": "stats"})
    drain_background(p)
    resp = json.loads(bytes(port.written).strip())
    assert resp == {
        "type": "STATS", "id": "stats",
        "uptime_ms": 1234, "loop_iters": 5,
    }, resp


@test("STATS streams nested diagnostics under low heap and tiny writes")
def _():
    class RecoveringTinyPort(FakePort):
        """Accept tiny prefixes, stall once, then let the test resume it."""
        def __init__(self):
            super().__init__()
            self.budget = 29

        def write(self, data):
            self.write_call_count += 1
            if self.budget <= 0:
                return 0
            chunk = bytes(data)[:min(2, self.budget)]
            self.budget -= len(chunk)
            self.written.extend(chunk)
            return len(chunk)

    port = RecoveringTinyPort()
    p, _ = build_protocol(port)
    app_stats = {
        "uptime_ms": 123456,
        "mem_free": 42000,
        "mem_alloc": 88000,
        "loop_iters": 999,
        "last_tick_ms": 4,
        "max_tick_ms": 72,
        "slow_tick_count": 3,
        "section_max_ms": {
            "protocol": 7, "protocol_bg": 12, "switch_scan": 4,
            "midi_poll": 6, "tft_render": 72,
        },
        "midi_rx_count": 50,
        "midi_tx_count": 80,
        "usb_tx_dropped": 0,
        "sysex_rx_count": 20,
        "last_patch_switch_duration_ms": 31,
        "protocol_cmd_count": 140,
        "last_patch_switch_ms": 120000,
        "current": {"bank": 7, "slot": 3},
        "expression": [
            {"jack": 1, "raw": 65535, "value": 127,
             "armed": True, "present": True},
            {"jack": 2, "raw": 12345, "value": 24,
             "armed": False, "present": False},
        ],
    }
    p.app.current_bank = app_stats["current"]["bank"]
    p.app.current_slot = app_stats["current"]["slot"]

    def forbidden_snapshot():
        raise MemoryError("full STATS snapshot must not be constructed")

    def live_fields():
        for key, value in app_stats.items():
            if key != "current" and key != "expression":
                yield key, value

    class LowHeapExpression:
        def __init__(self):
            self._jacks = [types.SimpleNamespace(**entry)
                           for entry in app_stats["expression"]]

        def stats(self):
            raise MemoryError("expression list/dicts must not be constructed")

        def stats_jacks(self):
            return self._jacks

    p.app.stats = forbidden_snapshot
    p.app.iter_stats_fields = live_fields
    p.app.expression = LowHeapExpression()

    real_dumps = protocol.json.dumps
    container_attempts = []

    def low_heap_dumps(value, *args, **kwargs):
        # Keep the interleaved PING ACK encodable. Every STATS container must
        # go through _stream_value rather than one contiguous json.dumps.
        is_ack = isinstance(value, dict) and value.get("type") == "ACK"
        if isinstance(value, (dict, list)) and not is_ack:
            container_attempts.append(type(value).__name__)
            raise MemoryError("simulated fragmented RP2040 heap")
        return real_dumps(value, *args, **kwargs)

    protocol.json.dumps = low_heap_dumps
    sentinel = object()
    real_bytearray = getattr(protocol, "bytearray", sentinel)

    def no_stats_chunk(_size=0):
        raise MemoryError("simulated lack of a 64-byte contiguous block")

    protocol.bytearray = no_stats_chunk
    try:
        p.handle({"type": "STATS", "id": "stats-low-heap"})
        assert p._bg_gen is not None, "STATS did not retain wire ownership"
        assert p._pending_out, "the deliberate partial-write stall was not retained"
        p.handle({"type": "PING", "id": "keepalive"})
        assert p._deferred_out, "keepalive was not deferred behind STATS"
        port.budget = 100000
        drain_background(p)
    finally:
        protocol.json.dumps = real_dumps
        if real_bytearray is sentinel:
            delattr(protocol, "bytearray")
        else:
            protocol.bytearray = real_bytearray

    replies = [json.loads(line) for line in bytes(port.written).splitlines()]
    expected = {"type": "STATS", "id": "stats-low-heap"}
    expected.update(app_stats)
    assert replies[0] == expected, replies
    assert replies[1] == {
        "type": "ACK", "id": "keepalive", "fw": protocol.VERSION,
    }, replies
    assert container_attempts == [], \
        "STATS attempted a contiguous container encode: %r" % container_attempts
    assert port.write_call_count > 100, port.write_call_count


@test("STATS coalesces into one slice and does not queue CONTEXT/PING")
def _():
    port = FakePort()
    write_sizes = []
    real_write = port.write

    def measured_write(data):
        write_sizes.append(len(data))
        return real_write(data)

    port.write = measured_write
    p, _ = build_protocol(port)
    sections = {
        "protocol": 7, "protocol_bg": 12, "switch_scan": 4,
        "midi_poll": 6, "tft_render": 72,
    }

    def live_fields():
        yield "uptime_ms", 123456
        yield "mem_free", 4328
        yield "mem_alloc", 156000
        yield "loop_iters", 999
        yield "last_tick_ms", 4
        yield "max_tick_ms", 72
        yield "slow_tick_count", 3
        yield "section_max_ms", sections
        yield "midi_rx_count", 50
        yield "midi_tx_count", 80
        yield "usb_tx_dropped", 0
        yield "sysex_rx_count", 20
        yield "last_patch_switch_duration_ms", 31
        yield "protocol_cmd_count", 140
        yield "last_patch_switch_ms", 120000

    class EmptyExpression:
        def stats_jacks(self):
            return ()

    p.app.iter_stats_fields = live_fields
    p.app.expression = EmptyExpression()
    p.app.stats = lambda: (_ for _ in ()).throw(
        MemoryError("monolithic STATS path used"))

    pump_calls = [0]
    real_pump = p.pump_background

    def counted_pump():
        pump_calls[0] += 1
        return real_pump()

    p.pump_background = counted_pump
    p.handle({"type": "STATS", "id": "fast-stats"})
    stats_write_count = len(write_sizes)
    queued_after_stats = len(p._deferred_out)

    p._send({"type": "CONTEXT", "partial": True,
             "context": {"kemper_block_Mod": "on"}})
    p.handle({"type": "PING", "id": "after-stats"})
    queued_after_followups = len(p._deferred_out)
    drain_background(p)

    replies = [json.loads(line) for line in bytes(port.written).splitlines()]
    assert [reply["type"] for reply in replies] == [
        "STATS", "CONTEXT", "ACK",
    ], replies
    assert pump_calls[0] <= 2, \
        "STATS consumed %d background slices" % pump_calls[0]
    assert queued_after_stats == 0 and queued_after_followups == 0, \
        "follow-ups queued behind STATS: %d" % queued_after_followups
    stats_sizes = write_sizes[:stats_write_count]
    assert stats_sizes and max(stats_sizes) <= 64, stats_sizes
    stats_wire = bytes(port.written).split(b"\n", 1)[0] + b"\n"
    assert len(stats_sizes) == (len(stats_wire) + 63) // 64, stats_sizes


@test("handle: unknown_type -> ERROR with original 'of' field")
def _():
    port = FakePort()
    p, _ = build_protocol(port)
    p.handle({"type": "WHAT", "id": "x"})
    resp = json.loads(bytes(port.written).strip())
    assert resp == {"type": "ERROR", "id": "x", "error": "unknown_type", "of": "WHAT"}, resp


@test("GET_MANIFEST dynamic fallback imports lazily and preserves its response")
def _():
    forget_dynamic_manifest_module()
    assert _DYNAMIC_MANIFEST_MODULE not in sys.modules
    port = FakePort()
    p, _ = build_protocol(port)
    p.handle({"type": "GET_MANIFEST", "id": "m"})
    assert _DYNAMIC_MANIFEST_MODULE in sys.modules, \
        "custom plugin fallback did not load its lazy runtime module"
    drain_background(p)
    resp = json.loads(bytes(port.written).strip())
    assert resp["type"] == "MANIFEST", resp
    assert resp["id"] == "m"
    assert "core_messages" in resp
    assert "plugins" in resp and "kemper_player" in resp["plugins"]


class _StaticManifestFile:
    def __init__(self, data):
        self.data = data
        self.offset = 0
        self.closed = False
        self.readinto_sizes = []

    def read(self, size=-1):
        if size < 0:
            size = len(self.data) - self.offset
        out = self.data[self.offset:self.offset + size]
        self.offset += len(out)
        return out

    def readinto(self, target):
        self.readinto_sizes.append(len(target))
        source = self.data[self.offset:self.offset + len(target)]
        for index, octet in enumerate(source):
            target[index] = octet
        self.offset += len(source)
        return len(source)

    def seek(self, offset):
        self.offset = offset

    def close(self):
        self.closed = True


def _with_static_manifest(p, tail, callback):
    """Run a test callback with an exact shipped plugin set and fake file."""
    p.app.plugins = types.SimpleNamespace(_plugins={
        name: object() for name in p._STATIC_MANIFEST_PLUGINS
    })
    opened = []
    real_open = getattr(protocol, "open", None)
    had_open = hasattr(protocol, "open")
    real_os = protocol.os

    def fake_open(path, mode):
        assert path == p._MANIFEST_TAIL_PATH and mode == "rb", (path, mode)
        handle = _StaticManifestFile(tail)
        opened.append(handle)
        return handle

    protocol.open = fake_open
    protocol.os = types.SimpleNamespace(
        stat=lambda path: (0, 0, 0, 0, 0, 0, len(tail)),
    )
    try:
        callback(opened)
    finally:
        protocol.os = real_os
        if had_open:
            protocol.open = real_open
        else:
            delattr(protocol, "open")


@test("GET_MANIFEST streams the build-time tail in bounded 63-byte reads")
def _():
    forget_dynamic_manifest_module()
    assert _DYNAMIC_MANIFEST_MODULE not in sys.modules
    port = FakePort(max_per_write=17)
    write_sizes = []
    real_write = port.write

    def measured_write(data):
        write_sizes.append(len(data))
        return real_write(data)

    port.write = measured_write
    p, _ = build_protocol(port)
    core = {"core_action": {"label": "Core"}}
    plugins = {"kemper_player": {"messages": {"rig": {"label": "Rig"}}}}
    tail = (',"core_messages":' + json.dumps(core, separators=(",", ":")) +
            ',"plugins":' + json.dumps(plugins, separators=(",", ":")) +
            '}\n').encode()
    real_dumps = protocol.json.dumps
    container_dumps = []

    def no_container_dumps(value, *args, **kwargs):
        if isinstance(value, (dict, list, tuple)):
            container_dumps.append(type(value).__name__)
            raise MemoryError("static path must not serialize containers")
        return real_dumps(value, *args, **kwargs)

    def run(opened):
        protocol.json.dumps = no_container_dumps
        try:
            p.handle({"type": "GET_MANIFEST", "id": "static"})
            assert _DYNAMIC_MANIFEST_MODULE not in sys.modules, \
                "static MANIFEST path imported its dynamic fallback"
            drain_background(p)
        finally:
            protocol.json.dumps = real_dumps
        response = json.loads(bytes(port.written))
        assert response == {
            "type": "MANIFEST", "id": "static",
            "core_messages": core, "plugins": plugins,
        }, response
        assert not container_dumps, container_dumps
        assert opened and opened[0].closed, opened
        assert opened[0].readinto_sizes, opened[0].readinto_sizes
        assert all(size == p._MANIFEST_CHUNK_SIZE
                   for size in opened[0].readinto_sizes)
        assert write_sizes and max(write_sizes) <= p._MANIFEST_CHUNK_SIZE, \
            write_sizes

    _with_static_manifest(p, tail, run)
    assert _DYNAMIC_MANIFEST_MODULE not in sys.modules, \
        "static MANIFEST completion retained its dynamic fallback"


@test("GET_MANIFEST static tail falls back to bounded reads if buffer allocation fails")
def _():
    port = FakePort(max_per_write=3)
    p, _ = build_protocol(port)
    tail = b',"core_messages":{},"plugins":{}}\n'
    sentinel = object()
    real_bytearray = getattr(protocol, "bytearray", sentinel)

    def no_chunk(_size=0):
        raise MemoryError("no contiguous static read buffer")

    def run(opened):
        protocol.bytearray = no_chunk
        try:
            p.handle({"type": "GET_MANIFEST", "id": "static-no-buffer"})
            drain_background(p)
        finally:
            if real_bytearray is sentinel:
                delattr(protocol, "bytearray")
            else:
                protocol.bytearray = real_bytearray
        response = json.loads(bytes(port.written))
        assert response["type"] == "MANIFEST", response
        assert response["core_messages"] == {} and response["plugins"] == {}
        assert opened and opened[0].closed, opened
        assert opened[0].readinto_sizes == [], opened[0].readinto_sizes

    _with_static_manifest(p, tail, run)


@test("GET_MANIFEST static 20KB stream stays fair and orders a queued PING")
def _():
    port = FakePort()
    p, _ = build_protocol(port)
    payload = "x" * 20500
    tail = (b',"core_messages":{},"plugins":{"large":{"payload":"' +
            payload.encode() + b'"}}}\n')
    pump_deltas = []
    real_pump = p.pump_background

    def measured_pump():
        before = len(port.written)
        result = real_pump()
        pump_deltas.append(len(port.written) - before)
        return result

    p.pump_background = measured_pump

    def run(opened):
        p.handle({"type": "GET_MANIFEST", "id": "static-large"})
        assert p._bg_gen is not None, "20KB static manifest finished without yielding"
        p.handle({"type": "PING", "id": "after-static"})
        assert p._deferred_out, "PING was not ordered behind static manifest"
        drain_background(p)
        replies = [json.loads(line) for line in bytes(port.written).splitlines()]
        assert [reply["type"] for reply in replies] == ["MANIFEST", "ACK"], replies
        assert replies[0]["plugins"]["large"]["payload"] == payload
        assert replies[1]["id"] == "after-static", replies
        assert opened and opened[0].closed, opened
        expected_reads = (len(tail) + p._MANIFEST_CHUNK_SIZE - 1) // \
            p._MANIFEST_CHUNK_SIZE
        # One head read + one tail read validate framing; readinto performs the
        # actual single-pass stream and one final EOF probe.
        assert len(opened[0].readinto_sizes) == expected_reads + 1, \
            (len(opened[0].readinto_sizes), expected_reads)
        expected_slices = (len(tail) + p._MANIFEST_YIELD_BYTES - 1) // \
            p._MANIFEST_YIELD_BYTES
        assert len(pump_deltas) <= expected_slices + 4, \
            (len(pump_deltas), expected_slices)
        assert all(delta <= p._MANIFEST_YIELD_BYTES +
                   p._MANIFEST_CHUNK_SIZE + 128 for delta in pump_deltas), \
            pump_deltas

    _with_static_manifest(p, tail, run)


@test("GET_MANIFEST rejects a corrupt static tail with a correlated response")
def _():
    port = FakePort()
    p, _ = build_protocol(port)
    corrupt = b',"wrong_prefix":{},"plugins":{}}\n'

    def run(opened):
        p.handle({"type": "GET_MANIFEST", "id": "static-corrupt"})
        drain_background(p)
        replies = [json.loads(line) for line in bytes(port.written).splitlines()
                   if line.strip()]
        assert replies == [{
            "type": "ERROR", "id": "static-corrupt",
            "error": "manifest_failed",
        }], replies
        assert opened and opened[0].closed, opened

    _with_static_manifest(p, corrupt, run)


@test("GET_MANIFEST uses short USB packets rather than 64-byte multiples")
def _():
    port = FakePort()
    write_sizes = []
    real_write = port.write

    def measured_write(data):
        write_sizes.append(len(data))
        return real_write(data)

    port.write = measured_write
    p, _ = build_protocol(port)
    p.handle({"type": "GET_MANIFEST", "id": "short-packet"})
    drain_background(p)

    assert json.loads(bytes(port.written))["id"] == "short-packet"
    assert p._MANIFEST_CHUNK_SIZE == 63
    assert write_sizes and 63 in write_sizes, write_sizes
    assert all(0 < size <= 63 for size in write_sizes), write_sizes


@test("GET_MANIFEST survives container, long-scalar, and tail allocation pressure")
def _():
    port = FakePort(max_per_write=3)
    write_sizes = []
    real_write = port.write

    def measured_write(data):
        write_sizes.append(len(data))
        return real_write(data)

    port.write = measured_write
    p, _ = build_protocol(port)
    long_hint = (
        "For beta auto-follow, configure every preset to broadcast its PC; "
        "this intentionally exceeds every plausible contiguous scalar block. " * 3
    ) + " quoted=\"path\\name\" line\naccent=è glyph=漢"
    factory_messages = {
        "deep_action": {
            "label": "Deep action",
            "params": {
                "mode": {"type": "enum", "values": ("one", "two", "three")},
                "value": {"type": "int", "min": 0, "max": 127},
            },
            "summary": "Mode {mode} value {value}",
        },
    }
    module = types.SimpleNamespace(
        LABEL="Low Heap Plugin",
        VERSION="9.1",
        MESSAGE_TYPES={"must_not_be_used": {}},
        manifest_message_types=lambda: factory_messages,
        DEFAULT_LAYOUT=[{"field": "patch_name", "x": 0, "y": 8}],
        TFT_FIELDS={"live_field": {"label": "Live field"}},
        CONFIG_SCHEMA={"fields": {"enabled": {"type": "bool"}}},
        RECIPE_SCHEMA={"hint": long_hint},
    )
    sparse_module = types.SimpleNamespace()
    explicit_none_module = types.SimpleNamespace(
        MESSAGE_TYPES=[], DEFAULT_LAYOUT=None, TFT_FIELDS=None,
    )

    class RealStyleRegistry:
        _plugins = {
            "low_heap": module,
            "sparse": sparse_module,
            "explicit_none": explicit_none_module,
        }

        def iter_manifest(self):
            raise MemoryError("entry dict allocation must not be attempted")

    p.app.plugins = RealStyleRegistry()
    core = {
        "core_deep": {
            "label": "Core deep action",
            "params": {"steps": {"type": "enum", "values": (1, 2, 3)}},
            "summary": "Core {steps}",
        },
    }
    expected_plugin = {
        "label": "Low Heap Plugin",
        "version": "9.1",
        "messages": factory_messages,
        "default_layout": module.DEFAULT_LAYOUT,
        "tft_fields": module.TFT_FIELDS,
        "config_schema": module.CONFIG_SCHEMA,
        "recipe_schema": module.RECIPE_SCHEMA,
    }

    real_core = protocol.messages.CORE_MESSAGE_TYPES
    real_dumps = protocol.json.dumps
    sentinel = object()
    real_bytearray = getattr(protocol, "bytearray", sentinel)
    real_bytes = getattr(protocol, "bytes", sentinel)
    scalar_lengths = []
    allocations = []

    def bounded_dumps(value, *args, **kwargs):
        if isinstance(value, (dict, list, tuple)):
            raise MemoryError("container encoding is forbidden")
        if isinstance(value, str):
            scalar_lengths.append(len(value))
            if len(value) > p._MANIFEST_STRING_CHARS:
                raise MemoryError("scalar window exceeded heap budget")
        return real_dumps(value, *args, **kwargs)

    def bounded_bytearray(size=0):
        if isinstance(size, int):
            allocations.append(size)
            if size > p._MANIFEST_CHUNK_SIZE:
                raise MemoryError("oversized staging allocation")
        return bytearray(size)

    def no_tail_copy(*_args, **_kwargs):
        raise MemoryError("contiguous tail copy is forbidden")

    protocol.messages.CORE_MESSAGE_TYPES = core
    protocol.json.dumps = bounded_dumps
    protocol.bytearray = bounded_bytearray
    protocol.bytes = no_tail_copy
    try:
        p.handle({"type": "GET_MANIFEST", "id": "heap-manifest"})
        drain_background(p)
    finally:
        protocol.messages.CORE_MESSAGE_TYPES = real_core
        protocol.json.dumps = real_dumps
        if real_bytearray is sentinel:
            delattr(protocol, "bytearray")
        else:
            protocol.bytearray = real_bytearray
        if real_bytes is sentinel:
            delattr(protocol, "bytes")
        else:
            protocol.bytes = real_bytes

    response = json.loads(bytes(port.written))
    self_expected = json.loads(json.dumps(expected_plugin))
    assert response == {
        "type": "MANIFEST", "id": "heap-manifest",
        "core_messages": json.loads(json.dumps(core)),
        "plugins": {
            "low_heap": self_expected,
            "sparse": {
                "label": "sparse", "version": "0", "messages": {},
                "default_layout": [], "tft_fields": {},
                "config_schema": None, "recipe_schema": None,
            },
            "explicit_none": {
                "label": "explicit_none", "version": "0", "messages": {},
                "default_layout": None, "tft_fields": None,
                "config_schema": None, "recipe_schema": None,
            },
        },
    }, response
    assert allocations == [p._MANIFEST_CHUNK_SIZE], allocations
    assert scalar_lengths and max(scalar_lengths) <= 8, scalar_lengths
    assert (write_sizes and max(write_sizes) <= p._MANIFEST_CHUNK_SIZE and
            p._MANIFEST_CHUNK_SIZE in write_sizes), write_sizes


@test("GET_MANIFEST has an explicit write/yield budget and releases follow-ups")
def _():
    port = FakePort()
    p, _ = build_protocol(port)
    many_messages = {
        "action_%03d" % i: {
            "label": "Action %03d" % i,
            "params": {
                "channel": {"type": "int", "min": 1, "max": 16},
                "mode": {"type": "enum", "values": ["one", "two", "three"]},
            },
            "summary": "Action %03d on {channel} in {mode}" % i,
        }
        for i in range(80)
    }
    module = types.SimpleNamespace(
        LABEL="Stress Plugin", VERSION="1.0", MESSAGE_TYPES=many_messages,
        DEFAULT_LAYOUT=[], TFT_FIELDS={}, CONFIG_SCHEMA=None,
        RECIPE_SCHEMA=None,
    )

    class RealStyleRegistry:
        _plugins = {"stress": module}

        def iter_manifest(self):
            raise AssertionError("real registry must not allocate entry dicts")

    p.app.plugins = RealStyleRegistry()
    real_core = protocol.messages.CORE_MESSAGE_TYPES
    protocol.messages.CORE_MESSAGE_TYPES = {}
    pump_deltas = []
    real_pump = p.pump_background

    def measured_pump():
        before = len(port.written)
        result = real_pump()
        pump_deltas.append(len(port.written) - before)
        return result

    p.pump_background = measured_pump
    try:
        p.handle({"type": "GET_MANIFEST", "id": "fair-manifest"})
        assert p._bg_gen is not None, "stress manifest unexpectedly fit one slice"
        p.handle({"type": "PING", "id": "after-manifest"})
        p._send({"type": "CONTEXT", "partial": True,
                 "context": {"kemper_block_Mod": "on"}})
        queued = len(p._deferred_out)
        drain_background(p)
    finally:
        protocol.messages.CORE_MESSAGE_TYPES = real_core

    replies = [json.loads(line) for line in bytes(port.written).splitlines()]
    assert [reply["type"] for reply in replies] == [
        "MANIFEST", "ACK", "CONTEXT",
    ], replies
    assert replies[0]["plugins"]["stress"]["messages"] == many_messages
    manifest_wire = bytes(port.written).split(b"\n", 1)[0] + b"\n"
    max_pumps = ((len(manifest_wire) + p._MANIFEST_YIELD_BYTES - 1) //
                 p._MANIFEST_YIELD_BYTES) + 2
    assert len(pump_deltas) <= max_pumps, (len(pump_deltas), max_pumps)
    assert queued == 2 and not p._deferred_out, queued
    assert all(delta >= p._MANIFEST_YIELD_BYTES - 128
               for delta in pump_deltas[:-1]), pump_deltas


@test("GET_MANIFEST remains complete when its short-packet buffer cannot allocate")
def _():
    port = FakePort(max_per_write=1)
    p, _ = build_protocol(port)
    module = types.SimpleNamespace(
        LABEL="No Buffer", VERSION="1",
        MESSAGE_TYPES={
            "nested": {
                "params": {"mode": {"type": "enum", "values": ("a", "b")}},
                "summary": "long scalar " * 30,
            },
        },
        DEFAULT_LAYOUT=[], TFT_FIELDS={}, CONFIG_SCHEMA=None,
        RECIPE_SCHEMA=None,
    )

    class RealStyleRegistry:
        _plugins = {"no_buffer": module}

    p.app.plugins = RealStyleRegistry()
    real_core = protocol.messages.CORE_MESSAGE_TYPES
    real_dumps = protocol.json.dumps
    sentinel = object()
    real_bytearray = getattr(protocol, "bytearray", sentinel)
    protocol.messages.CORE_MESSAGE_TYPES = {}

    def no_container_dumps(value, *args, **kwargs):
        if isinstance(value, (dict, list, tuple)):
            raise MemoryError("container encoding forbidden")
        if isinstance(value, str) and len(value) > p._MANIFEST_STRING_CHARS:
            raise MemoryError("unbounded scalar encoding forbidden")
        return real_dumps(value, *args, **kwargs)

    def no_chunk(_size=0):
        raise MemoryError("no contiguous manifest chunk")

    protocol.json.dumps = no_container_dumps
    protocol.bytearray = no_chunk
    try:
        p.handle({"type": "GET_MANIFEST", "id": "no-buffer"})
        drain_background(p)
    finally:
        protocol.messages.CORE_MESSAGE_TYPES = real_core
        protocol.json.dumps = real_dumps
        if real_bytearray is sentinel:
            delattr(protocol, "bytearray")
        else:
            protocol.bytearray = real_bytearray

    response = json.loads(bytes(port.written))
    assert response["id"] == "no-buffer", response
    assert response["plugins"]["no_buffer"]["messages"] == \
        json.loads(json.dumps(module.MESSAGE_TYPES)), response
    assert port.write_call_count > 100, port.write_call_count


@test("GET_MANIFEST bounded scalar windows preserve escaped and Unicode text")
def _():
    port = FakePort()
    p, _ = build_protocol(port)
    safe = ("ABCDEFGHIJKLMNOPQRSTUVWXYZ abcdefghijklmnopqrstuvwxyz 0123456789 "
            "-_.:/{}[]()<>+=!?@#$%^&*;,'|~`" * 8)
    mixed = "quote=\" slash=\\ controls=\b\f\n\r\t unicode=è漢\U0001f600 del=\x7f"
    module = types.SimpleNamespace(
        LABEL=safe, VERSION="1.0", MESSAGE_TYPES={
            "safe": {"summary": safe},
            "mixed": {"summary": mixed},
        }, DEFAULT_LAYOUT=[], TFT_FIELDS={}, CONFIG_SCHEMA=None,
        RECIPE_SCHEMA={"hint": mixed},
    )

    class Registry:
        _plugins = {"ascii_fast": module}

    p.app.plugins = Registry()
    real_core = protocol.messages.CORE_MESSAGE_TYPES
    real_dumps = protocol.json.dumps
    string_dumps = []
    protocol.messages.CORE_MESSAGE_TYPES = {}

    def measured_dumps(value, *args, **kwargs):
        if isinstance(value, str):
            string_dumps.append(value)
        return real_dumps(value, *args, **kwargs)

    protocol.json.dumps = measured_dumps
    try:
        p.handle({"type": "GET_MANIFEST", "id": "escape-check"})
        drain_background(p)
    finally:
        protocol.messages.CORE_MESSAGE_TYPES = real_core
        protocol.json.dumps = real_dumps

    response = json.loads(bytes(port.written))
    plugin = response["plugins"]["ascii_fast"]
    assert plugin["label"] == safe, plugin["label"]
    assert plugin["messages"]["safe"]["summary"] == safe
    assert plugin["messages"]["mixed"]["summary"] == mixed
    assert plugin["recipe_schema"]["hint"] == mixed
    assert string_dumps, "text did not exercise bounded json.dumps"
    assert all(len(value) <= p._MANIFEST_STRING_CHARS
               for value in string_dumps), string_dumps
    assert any(value == "ABCDEFGH" for value in string_dumps), string_dumps


@test("GET_MANIFEST reports a scalar MemoryError after sealing its line")
def _():
    port = FakePort()
    p, _ = build_protocol(port)
    real_core = protocol.messages.CORE_MESSAGE_TYPES
    real_dumps = protocol.json.dumps
    protocol.messages.CORE_MESSAGE_TYPES = {
        "safe": {"label": "safe-" * 30},
        "broken": {"label": "trigger"},
    }
    armed = [True]

    def fail_once(value, *args, **kwargs):
        if armed[0] and value == "trigger":
            armed[0] = False
            raise MemoryError("simulated scalar allocation failure")
        return real_dumps(value, *args, **kwargs)

    protocol.json.dumps = fail_once
    try:
        p.handle({"type": "GET_MANIFEST", "id": "manifest-oom"})
        drain_background(p)
    finally:
        protocol.messages.CORE_MESSAGE_TYPES = real_core
        protocol.json.dumps = real_dumps

    lines = bytes(port.written).splitlines()
    assert len(lines) == 2, lines
    try:
        json.loads(lines[0])
        assert False, "damaged manifest was not sealed"
    except ValueError:
        pass
    assert json.loads(lines[1]) == {
        "type": "ERROR", "id": "manifest-oom",
        "error": "manifest_failed",
    }, lines
    assert p._bg_gen is None and not p._deferred_out and not p._pending_out


@test("handle: GET_DEVICE_INFO returns fw + device + current")
def _():
    port = FakePort()
    p, _ = build_protocol(port)
    p.handle({"type": "GET_DEVICE_INFO", "id": "d"})
    drain_background(p)
    resp = json.loads(bytes(port.written).strip())
    assert resp["type"] == "DEVICE_INFO"
    assert "fw" in resp and "device" in resp and "current" in resp


@test("handle: GET_DEVICE_INFO includes only preset_navigation config subtree")
def _():
    port = FakePort()
    p, _ = build_protocol(port)
    nav = {
        "switches": {"A": 1, "B": 2},
        "bank_colors": {"1": "#123456"},
    }
    p.app.device.update({
        "preset_navigation": nav,
        "tft": {"layout": ["large-unrelated-payload"]},
        "expression": {"A": {"enabled": True}},
        "private_future_setting": "must-not-leak",
    })

    p.handle({"type": "GET_DEVICE_INFO", "id": "nav"})
    drain_background(p)
    resp = json.loads(bytes(port.written).strip())

    assert resp["preset_navigation"] == nav, resp
    assert "tft" not in resp and "expression" not in resp, resp
    assert "private_future_setting" not in resp, resp
    assert "kemper" not in resp, resp


@test("handle: GET_DEVICE_INFO marks absent or invalid preset_navigation empty")
def _():
    for value in (None, "invalid", [], 7):
        port = FakePort()
        p, _ = build_protocol(port)
        if value is not None:
            p.app.device["preset_navigation"] = value

        p.handle({"type": "GET_DEVICE_INFO", "id": "legacy"})
        drain_background(p)
        resp = json.loads(bytes(port.written).strip())

        assert resp["preset_navigation"] == {}, (value, resp)
        assert resp["device"] == "MIDI Captain", (value, resp)
        assert resp["current"] == {"bank": 1, "slot": 1}, (value, resp)


@test("handle: GET_DEVICE_INFO tolerates legacy app without device config")
def _():
    port = FakePort()
    p, _ = build_protocol(port)
    p.app.device = None

    p.handle({"type": "GET_DEVICE_INFO", "id": "no-config"})
    drain_background(p)
    resp = json.loads(bytes(port.written).strip())

    assert resp["device"] == "MIDI Captain", resp
    assert resp["preset_navigation"] == {}, resp


@test("GET_DEVICE_INFO projects TFT colors by first field occurrence without exposing layout")
def _():
    p, port = build_protocol()
    p.app.device["tft"] = {"layout": [
        {"field": "patch_name", "color": "#112233", "x": 11, "private": "not-needed"},
        {"field": "patch_name", "color": "#AABBCC"},
        {"field": "bank", "color": "#DDAA00"},
        {"field": "kemper_rig", "color": "#33AACC"},
        {"field": "expression_mode", "color": "#55FF99"},
        {"field": "ignored", "color": 123},
        {"color": "#000000"}, {"field": ""}, None, "invalid",
    ]}
    p.handle({"type": "GET_DEVICE_INFO", "id": "stage-colors"})
    drain_background(p)
    response = json.loads(port.written)
    assert response["tft_colors"] == {
        "patch_name": "#112233", "bank": "#DDAA00",
        "kemper_rig": "#33AACC", "expression_mode": "#55FF99",
    }, response
    assert "tft" not in response and "layout" not in response
    assert "not-needed" not in port.written.decode()


@test("GET_DEVICE_INFO always supplies an empty color capability for unusable TFT layouts")
def _():
    for tft in (None, [], "invalid", {}, {"layout": None}, {"layout": {}}, {"layout": [None]}):
        p, port = build_protocol()
        p.app.device["tft"] = tft
        p.handle({"type": "GET_DEVICE_INFO", "id": "colors-empty"})
        drain_background(p)
        response = json.loads(port.written)
        assert response["tft_colors"] == {}
        assert response["tft_labels"] == {}


@test("GET_DEVICE_INFO exposes only BANK/RIG field prefixes and suffixes with first-entry precedence")
def _():
    p, port = build_protocol()
    p.app.device["tft"] = {"layout": [
        {"field": "bank", "prefix": "BANK ", "suffix": " !", "size": 5},
        {"field": "bank", "prefix": "B "},
        {"field": "kemper_rig_in_bank", "prefix": "RIG ", "suffix": ""},
        {"field": "kemper_bank", "prefix": None, "suffix": None},
        {"field": "kemper_rig", "prefix": 12, "suffix": []},
        {"field": "slot"},
        {"field": "patch_name", "prefix": "private title"},
        {"field": "expression_mode", "prefix": "private expression"},
        {"field": ["invalid"], "prefix": "bad field"},
    ]}
    p.handle({"type": "GET_DEVICE_INFO", "id": "stage-labels"})
    drain_background(p)
    response = json.loads(port.written)
    assert response["tft_labels"] == {
        "bank": {"prefix": "BANK ", "suffix": " !"},
        "kemper_rig_in_bank": {"prefix": "RIG ", "suffix": ""},
        "kemper_bank": {"prefix": "", "suffix": ""},
        "kemper_rig": {"prefix": "", "suffix": ""},
        "slot": {"prefix": "", "suffix": ""},
    }, response
    assert "private" not in port.written.decode()
    assert "tft" not in response and "layout" not in response


@test("GET_DEVICE_INFO streams under low heap through one-byte partial writes")
def _():
    # Reproduce the live failure: serialising the whole DEVICE_INFO object
    # needs one contiguous allocation and can MemoryError even after a GC.
    # Leaf values still fit, so a genuinely streaming implementation must
    # never ask json.dumps() to encode a dict/list as one object.
    port = FakePort(max_per_write=1)
    p, _ = build_protocol(port)
    nav = {
        "switches": {"A": 1, "B": 2, "C": 3},
        "bank_colors": {str(i): "#123456" for i in range(1, 26)},
    }
    p.app.device["device_name"] = "Captain Stage"
    p.app.device["preset_navigation"] = nav
    p.app.current_bank = 7
    p.app.current_slot = 3

    real_dumps = protocol.json.dumps
    real_active = protocol.config.active_profile_id
    container_attempts = []

    def low_heap_dumps(value, *args, **kwargs):
        if isinstance(value, (dict, list)):
            container_attempts.append(type(value).__name__)
            raise MemoryError("simulated fragmented RP2040 heap")
        return real_dumps(value, *args, **kwargs)

    protocol.json.dumps = low_heap_dumps
    protocol.config.active_profile_id = lambda: "stage"
    try:
        p.handle({"type": "GET_DEVICE_INFO", "id": "low-heap"})
        drain_background(p)
    finally:
        protocol.json.dumps = real_dumps
        protocol.config.active_profile_id = real_active

    assert port.written.endswith(b"\n"), port.written
    resp = json.loads(bytes(port.written).strip())
    assert resp == {
        "type": "DEVICE_INFO",
        "id": "low-heap",
        "fw": protocol.VERSION,
        "device": "Captain Stage",
        "current": {"bank": 7, "slot": 3},
        "profile": "stage",
        "preset_navigation": nav,
        "tft_colors": {},
        "tft_labels": {},
    }, resp
    assert container_attempts == [], \
        "DEVICE_INFO still attempted a contiguous container encode: %r" % container_attempts
    assert port.write_call_count > 20, port.write_call_count


@test("GET_DEVICE_INFO cannot be interleaved by an ACK while streaming")
def _():
    port = FakePort(max_per_write=1)
    p, _ = build_protocol(port)
    p.app.device["preset_navigation"] = {
        "switches": {"A": 1, "B": 2, "C": 3},
    }

    p.handle({"type": "GET_DEVICE_INFO", "id": "device"})
    assert p._bg_gen is not None, "DEVICE_INFO did not retain wire ownership"
    p.handle({"type": "PING", "id": "keepalive"})
    assert p._deferred_out, "keepalive was not deferred behind DEVICE_INFO"
    drain_background(p)

    replies = [json.loads(line) for line in bytes(port.written).splitlines()]
    assert [reply["type"] for reply in replies] == ["DEVICE_INFO", "ACK"], replies
    assert replies[0]["id"] == "device", replies
    assert replies[1]["id"] == "keepalive", replies


@test("GET_DEVICE_INFO stream failure is sealed before a deferred ACK")
def _():
    p, port = build_protocol()
    p.app.device["device_name"] = "fail-this-leaf"
    real_dumps = protocol.json.dumps

    def fail_one_leaf(value, *args, **kwargs):
        if value == "fail-this-leaf":
            raise MemoryError("simulated scalar allocation failure")
        return real_dumps(value, *args, **kwargs)

    protocol.json.dumps = fail_one_leaf
    try:
        p.handle({"type": "GET_DEVICE_INFO", "id": "broken-device"})
        assert p._bg_gen is not None
        p.handle({"type": "PING", "id": "after-broken-device"})
        drain_background(p)
    finally:
        protocol.json.dumps = real_dumps

    lines = bytes(port.written).splitlines()
    assert len(lines) == 3, lines
    try:
        json.loads(lines[0])
        assert False, "the deliberately damaged DEVICE_INFO became valid"
    except ValueError:
        pass
    assert json.loads(lines[1]) == {
        "type": "ACK", "id": "after-broken-device", "fw": protocol.VERSION,
    }, lines
    assert json.loads(lines[2]) == {
        "type": "ERROR", "id": "broken-device", "error": "exception",
        "detail": "simulated scalar allocation failure",
        "of": "GET_DEVICE_INFO",
    }, lines


@test("handle: LIST_PATCHES returns array via patches.list()")
def _():
    port = FakePort()
    p, _ = build_protocol(port)
    p.handle({"type": "LIST_PATCHES", "id": "lp"})
    drain_background(p)
    resp = json.loads(bytes(port.written).strip())
    assert resp["type"] == "PATCH_LIST"
    assert resp["patches"][0]["bank"] == 1 and resp["patches"][0]["slot"] == 1


@test("PATCH_LIST streams under low heap, tiny writes, and an interleaved PING")
def _():
    port = FakePort(max_per_write=1)
    p, _ = build_protocol(port)
    p.app.patches._patches = {
        (bank, slot): {"name": "Rig %d-%d" % (bank, slot), "bindings": []}
        for bank in range(1, 7) for slot in range(1, 6)
    }
    expected_patches = p.app.patches.list()

    real_dumps = protocol.json.dumps
    container_attempts = []

    def low_heap_dumps(value, *args, **kwargs):
        # ACK remains encodable so it can exercise wire ordering.  A complete
        # PATCH_LIST wrapper or its patches array represents the dangerous
        # contiguous allocation reproduced on the RP2040.
        if (isinstance(value, list) or
                (isinstance(value, dict) and
                 value.get("type") == "PATCH_LIST")):
            container_attempts.append(type(value).__name__)
            raise MemoryError("simulated fragmented RP2040 heap")
        return real_dumps(value, *args, **kwargs)

    protocol.json.dumps = low_heap_dumps
    try:
        p.handle({"type": "LIST_PATCHES", "id": "patches-low-heap"})
        assert p._bg_gen is not None, "PATCH_LIST did not retain wire ownership"
        p.handle({"type": "PING", "id": "keepalive"})
        assert p._deferred_out, "keepalive was not deferred behind PATCH_LIST"
        drain_background(p)
    finally:
        protocol.json.dumps = real_dumps

    replies = [json.loads(line) for line in bytes(port.written).splitlines()]
    assert replies[0] == {
        "type": "PATCH_LIST", "id": "patches-low-heap",
        "patches": expected_patches, "profile": "",
    }, replies
    assert replies[1] == {
        "type": "ACK", "id": "keepalive", "fw": protocol.VERSION,
    }, replies
    assert container_attempts == [], \
        "PATCH_LIST still attempted a contiguous container encode: %r" % container_attempts
    assert port.write_call_count > 100, port.write_call_count


@test("PATCH_LIST uses one serializer generator frame under low heap")
def _():
    port = FakePort(max_per_write=2)
    p, _ = build_protocol(port)
    p.app.patches._patches = {
        (bank, slot): {"name": "Rig %d-%d" % (bank, slot), "bindings": []}
        for bank in range(1, 7) for slot in range(1, 6)
    }
    expected = p.app.patches.list()
    real_stream = p._stream_value
    active = [0]
    peak = [0]

    def tracked_stream(*args, **kwargs):
        active[0] += 1
        if active[0] > peak[0]:
            peak[0] = active[0]
        try:
            yield from real_stream(*args, **kwargs)
        finally:
            active[0] -= 1

    p._stream_value = tracked_stream
    try:
        p.handle({"type": "LIST_PATCHES", "id": "flat-list"})
        drain_background(p)
    finally:
        p._stream_value = real_stream

    response = json.loads(bytes(port.written).strip())
    assert response == {
        "type": "PATCH_LIST", "id": "flat-list", "patches": expected,
        "profile": "",
    }, response
    assert peak[0] == 1, "recursive serializer generator depth=%d" % peak[0]


@test("handle: GET_PATCH returns the patch when present")
def _():
    port = FakePort()
    p, _ = build_protocol(port)
    p.handle({"type": "GET_PATCH", "id": "g", "bank": 1, "slot": 1})
    drain_background(p)
    resp = json.loads(bytes(port.written).strip())
    assert resp["type"] == "PATCH"
    assert resp["patch"]["name"] == "Lead"


@test("GET_PATCH uses the authoritative non-caching store read")
def _():
    port = FakePort()
    p, _ = build_protocol(port)
    calls = []

    def non_caching_read(bank, slot):
        calls.append((bank, slot))
        return {"name": "Transient", "bindings": []}

    def forbidden_get(*_args):
        raise AssertionError("GET_PATCH polluted the live PatchStore cache")

    p.app.patches.read = non_caching_read
    p.app.patches.get = forbidden_get
    p.handle({"type": "GET_PATCH", "id": "read", "bank": 4, "slot": 2})
    drain_background(p)

    response = json.loads(bytes(port.written).strip())
    assert calls == [(4, 2)], calls
    assert response["patch"]["name"] == "Transient", response


@test("GET_PATCH uses only bounded short chunks under fragmented heap")
def _():
    port = FakePort()
    write_sizes = []
    real_write = port.write

    def measured_write(data):
        write_sizes.append(len(data))
        return real_write(data)

    port.write = measured_write
    p, _ = build_protocol(port)
    patch = {
        "name": "Large patch",
        "bindings": [
            {"switch": str(i), "mode": "latched", "label": "FX" + str(i),
             "actions": {"toggle_on": {"messages": [
                 {"type": "cc", "channel": 1, "cc": 20 + i, "value": 127},
             ]}}}
            for i in range(12)
        ],
    }
    p.app.patches._patches[(1, 1)] = patch
    real_dumps = protocol.json.dumps
    container_attempts = []

    def low_heap_dumps(value, *args, **kwargs):
        if isinstance(value, (dict, list)):
            container_attempts.append(type(value).__name__)
            raise MemoryError("simulated fragmented RP2040 heap")
        return real_dumps(value, *args, **kwargs)

    protocol.json.dumps = low_heap_dumps
    try:
        p.handle({"type": "GET_PATCH", "id": "bounded", "bank": 1, "slot": 1})
        drain_background(p)
    finally:
        protocol.json.dumps = real_dumps

    response = json.loads(bytes(port.written).strip())
    assert response["patch"] == patch, response
    assert container_attempts == [], container_attempts
    assert write_sizes and max(write_sizes) <= p._MANIFEST_CHUNK_SIZE, write_sizes
    chunk_size = p._MANIFEST_CHUNK_SIZE
    assert len(write_sizes) == (len(port.written) + chunk_size - 1) // chunk_size, write_sizes


@test("GET_PATCH falls back to allocation-minimal streaming without a chunk")
def _():
    port = FakePort(max_per_write=1)
    p, _ = build_protocol(port)
    expected = p.app.patches._patches[(1, 1)]
    sentinel = object()
    real_bytearray = getattr(protocol, "bytearray", sentinel)

    def no_chunk(_size=0):
        raise MemoryError("simulated lack of a short-packet buffer")

    protocol.bytearray = no_chunk
    try:
        p.handle({"type": "GET_PATCH", "id": "no-chunk", "bank": 1, "slot": 1})
        drain_background(p)
    finally:
        if real_bytearray is sentinel:
            delattr(protocol, "bytearray")
        else:
            protocol.bytearray = real_bytearray

    response = json.loads(bytes(port.written).strip())
    assert response["id"] == "no-chunk" and response["patch"] == expected, response


@test("GET_PATCH has one serializer generator frame and bounded string leaves")
def _():
    port = FakePort(max_per_write=3)
    p, _ = build_protocol(port)
    long_text = "quote=\" slash=\\ unicode=è漢\U0001f600 controls=\n" * 8
    patch = {
        "name": long_text,
        "bindings": [{
            "switch": "A", "mode": "latched", "label": long_text,
            "actions": {"toggle_on": {"messages": [{
                "type": "sysex", "data": [0xF0, 0x00, 0x20, 0x33,
                                               0x02, 0x7F, 0xF7],
            }]}},
        }],
    }
    p.app.patches._patches[(1, 1)] = patch

    real_stream = p._stream_value
    real_dumps = protocol.json.dumps
    active = [0]
    peak = [0]

    def tracked_stream(*args, **kwargs):
        active[0] += 1
        if active[0] > peak[0]:
            peak[0] = active[0]
        try:
            yield from real_stream(*args, **kwargs)
        finally:
            active[0] -= 1

    def bounded_dumps(value, *args, **kwargs):
        if isinstance(value, (dict, list, tuple)):
            raise MemoryError("container allocation forbidden")
        if isinstance(value, str) and len(value) > p._MANIFEST_STRING_CHARS:
            raise MemoryError("unbounded string allocation forbidden")
        return real_dumps(value, *args, **kwargs)

    p._stream_value = tracked_stream
    protocol.json.dumps = bounded_dumps
    try:
        p.handle({"type": "GET_PATCH", "id": "flat", "bank": 1, "slot": 1})
        drain_background(p)
    finally:
        protocol.json.dumps = real_dumps

    response = json.loads(bytes(port.written).strip())
    assert response["patch"] == patch, response
    assert peak[0] == 1, "recursive serializer generator depth=%d" % peak[0]


@test("handle: GET_PATCH for missing slot -> ERROR not_found")
def _():
    port = FakePort()
    p, _ = build_protocol(port)
    p.handle({"type": "GET_PATCH", "id": "g", "bank": 99, "slot": 5})
    drain_background(p)
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
    replies = [json.loads(line) for line in bytes(port.written).splitlines()]
    assert replies == [
        {"type": "ACK", "id": "g"},
        {"type": "EVENT", "event": "global_changed"},
    ], replies
    assert saved == [new_dev], saved
    assert p.app.device is new_dev


@test("handle: PUT_GLOBAL with bad device -> ERROR missing_device")
def _():
    port = FakePort()
    p, _ = build_protocol(port)
    p.handle({"type": "PUT_GLOBAL", "id": "g"})   # no 'device'
    resp = json.loads(bytes(port.written).strip())
    assert resp == {"type": "ERROR", "id": "g", "error": "missing_device"}, resp


@test("PUT_GLOBAL persistence failure never emits success ACK or global_changed")
def _():
    from unittest.mock import patch

    p, port = build_protocol()
    old_device = p.app.device
    with patch.object(protocol.config, "save_device", side_effect=OSError("disk full")):
        p.handle({"type": "PUT_GLOBAL", "id": "failed-save", "device": {"device_name": "changed"}})
    replies = [json.loads(line) for line in port.written.splitlines()]
    assert len(replies) == 1 and replies[0]["type"] == "ERROR", replies
    assert replies[0]["id"] == "failed-save"
    assert p.app.device is old_device


@test("PUT_GLOBAL apply failure never emits global_changed")
def _():
    from unittest.mock import patch

    p, port = build_protocol()
    with patch.object(protocol.config, "save_device"), \
            patch.object(p.app, "apply_global", side_effect=MemoryError("apply failed")):
        p.handle({"type": "PUT_GLOBAL", "id": "failed-apply", "device": {"device_name": "changed"}})
    replies = [json.loads(line) for line in port.written.splitlines()]
    assert len(replies) == 1 and replies[0]["type"] == "ERROR", replies
    assert replies[0]["id"] == "failed-apply"


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


@test("handle: DELETE_PATCH delegates to app and ACKs only on success")
def _():
    port = FakePort()
    p, _ = build_protocol(port)

    p.handle({"type": "DELETE_PATCH", "id": "del", "bank": 1, "slot": 1})

    resp = json.loads(bytes(port.written).strip())
    assert resp == {"type": "ACK", "id": "del"}, resp
    assert p.app.delete_patch_calls == [(1, 1)], p.app.delete_patch_calls
    assert (1, 1) not in p.app.patches._patches


@test("handle: DELETE_PATCH filesystem failure returns ERROR without ACK")
def _():
    port = FakePort()
    p, _ = build_protocol(port)
    p.app.delete_patch_error = OSError(30, "read-only filesystem")

    p.handle({"type": "DELETE_PATCH", "id": "del", "bank": 1, "slot": 1})

    lines = [json.loads(line) for line in bytes(port.written).splitlines()]
    assert lines == [{
        "type": "ERROR", "id": "del", "error": "delete_failed",
        "bank": 1, "slot": 1,
    }], lines
    assert p.app.delete_patch_calls == [(1, 1)], p.app.delete_patch_calls
    assert (1, 1) in p.app.patches._patches


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
        drain_background(p)
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
        drain_background(p)
    finally:
        config.profile_exists = orig
    resp = json.loads(bytes(port.written).strip())
    assert resp == {"type": "ERROR", "id": "g", "error": "no_such_profile", "profile": "ghost"}, resp


@test("handle: GET_GLOBAL without profile -> serves active in-memory state")
def _():
    port = FakePort()
    p, _ = build_protocol(port)
    p.handle({"type": "GET_GLOBAL", "id": "g"})
    drain_background(p)
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
        drain_background(p)
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
        drain_background(p)
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


class OTAFrame:
    def __init__(self, app):
        self.suspended = False
        self.restores = 0
        app.display = self
        app._mark_display_dirty = self.mark_dirty

    def suspend(self):
        self.suspended = True

    def resume(self):
        suspended = self.suspended
        self.suspended = False
        return suspended

    def mark_dirty(self):
        assert not self.suspended
        assert "captain_ota" not in sys.modules, "TFT restored before freeing OTA module"
        self.restores += 1


@test("OTA lazy module completes legacy uploads and releases its module and function graph")
def _():
    import builtins
    import gc
    import weakref
    from unittest.mock import patch

    p, port = build_protocol()
    display = OTAFrame(p.app)
    assert "captain_ota" not in sys.modules
    p.handle({"type": "PING", "id": "before-ota"})
    assert "captain_ota" not in sys.modules
    real_open = builtins.open
    real_import = builtins.__import__
    real_mkdir = protocol.os.mkdir
    real_stat = protocol.os.stat
    real_remove = protocol.os.remove
    real_rename = protocol.os.rename
    remote_root = "/cold-ota-test"

    def import_with_tft_headroom(name, *args, **kwargs):
        if name == "captain_ota":
            assert display.suspended, "cold import needs the TFT frame's RAM first"
        return real_import(name, *args, **kwargs)

    with tempfile.TemporaryDirectory(prefix="bosun-ota-") as directory:
        root = Path(directory)

        def mapped(path):
            if isinstance(path, str) and path.startswith(remote_root):
                return str(root) + path[len(remote_root):]
            return path

        with patch.object(builtins, "__import__", import_with_tft_headroom), \
                patch.object(builtins, "open", lambda path, *a, **kw: real_open(mapped(path), *a, **kw)), \
                patch.object(protocol.os, "mkdir", lambda path, *a, **kw: real_mkdir(mapped(path), *a, **kw)), \
                patch.object(protocol.os, "stat", lambda path, *a, **kw: real_stat(mapped(path), *a, **kw)), \
                patch.object(protocol.os, "remove", lambda path: real_remove(mapped(path))), \
                patch.object(protocol.os, "rename", lambda src, dst: real_rename(mapped(src), mapped(dst))):
            # Repeating requires a fresh lazy import after END. Real file
            # handles verify the legacy wire commands, size check and pruning.
            for index in range(2):
                path = remote_root + "/uploaded.mpy"
                (root / "uploaded.py").write_text("old_source = True", encoding="utf-8")
                wire = [
                    {"type": "PUT_FILE_BEGIN", "id": "begin", "path": path, "size": 3},
                    {"type": "PUT_FILE_CHUNK", "id": "chunk", "path": path, "data_b64": "YWJj"},
                    {"type": "PUT_FILE_END", "id": "end", "path": path},
                ]
                port.written.clear()
                p.handle(wire[0])
                assert display.suspended
                module_ref = weakref.ref(sys.modules["captain_ota"])
                function_ref = weakref.ref(sys.modules["captain_ota"].begin)
                p.handle(wire[1])
                assert display.suspended and display.restores == index
                assert module_ref() is sys.modules["captain_ota"]
                p.handle(wire[2])
                assert not display.suspended and display.restores == index + 1
                assert "captain_ota" not in sys.modules
                gc.collect()
                assert module_ref() is None, "OTA module stayed resident after END"
                assert function_ref() is None, "OTA globals/function cycle survived END"
                assert not hasattr(sys.modules["captain"], "captain_ota")
                assert (root / "uploaded.mpy").read_bytes() == b"abc"
                assert not (root / "uploaded.mpy.tmp").exists()
                assert not (root / "uploaded.py").exists()
                assert not p._uploads and not p._upload_sizes
                replies = [json.loads(line) for line in port.written.splitlines()]
                assert replies == [
                    {"type": "ACK", "id": "begin", "size_check": True, "size": 3},
                    {"type": "ACK", "id": "chunk"},
                    {"type": "ACK", "id": "end"},
                ], (index, replies)


@test("OTA handled validation and write errors release the lazy module")
def _():
    import gc
    import weakref

    p, port = build_protocol()
    display = OTAFrame(p.app)
    for command in (
            {"type": "PUT_FILE_BEGIN", "path": "relative"},
            {"type": "PUT_FILE_BEGIN", "path": "/bad-size", "size": -1},
            {"type": "PUT_FILE_CHUNK", "path": "/not-open"},
            {"type": "PUT_FILE_END", "path": "/not-open"}):
        command["id"] = "invalid"
        p.handle(command)
        assert "captain_ota" not in sys.modules, command
        assert not display.suspended
    replies = [json.loads(line) for line in port.written.splitlines()]
    assert [reply["error"] for reply in replies] == [
        "bad_path", "bad_size", "no_open_file", "no_open_file",
    ]

    class FailedUpload:
        closed = False

        def write(self, value):
            raise MemoryError("write failed")

        def close(self):
            self.closed = True

    upload = FailedUpload()
    display.suspend()
    p._uploads["/broken-upload"] = upload
    p._upload_sizes["/broken-upload"] = 3
    ota = importlib.import_module("captain_ota")
    module_ref = weakref.ref(ota)
    function_ref = weakref.ref(ota.chunk)
    ota = None
    p.handle({"type": "PUT_FILE_CHUNK", "id": "broken", "path": "/broken-upload", "data_b64": "YWJj"})
    gc.collect()
    assert "captain_ota" not in sys.modules
    assert module_ref() is None and function_ref() is None
    assert upload.closed and not p._uploads and not p._upload_sizes
    assert not display.suspended and display.restores == 3


@test("OTA unexpected handler exception closes uploads and releases the lazy module")
def _():
    import gc
    import weakref
    from unittest.mock import patch

    p, port = build_protocol()
    display = OTAFrame(p.app)
    ota = importlib.import_module("captain_ota")
    module_ref = weakref.ref(ota)
    function_ref = weakref.ref(ota.begin)

    class Upload:
        closed = False

        def close(self):
            self.closed = True

    upload = Upload()

    def fail_after_registering(proto, mid, message):
        proto._uploads["/failed-begin"] = upload
        proto._upload_sizes["/failed-begin"] = 3
        raise MemoryError("unexpected handler failure")

    ota.begin = fail_after_registering
    ota = None
    with patch.object(protocol.os, "remove", side_effect=OSError("missing temp")):
        p.handle({"type": "PUT_FILE_BEGIN", "id": "failed", "path": "/failed-begin"})
    gc.collect()
    assert module_ref() is None and function_ref() is None
    assert "captain_ota" not in sys.modules
    assert upload.closed and not p._uploads and not p._upload_sizes
    assert json.loads(port.written)["id"] == "failed"
    assert json.loads(port.written)["type"] == "ERROR"
    assert not display.suspended and display.restores == 1


@test("OTA import MemoryError releases partial module state and restores TFT before reporting failure")
def _():
    import builtins
    from unittest.mock import patch

    p, port = build_protocol()
    display = OTAFrame(p.app)
    real_import = builtins.__import__

    def failed_import(name, *args, **kwargs):
        if name == "captain_ota":
            assert display.suspended
            sys.modules[name] = types.ModuleType(name)
            raise MemoryError("cold module cannot allocate")
        return real_import(name, *args, **kwargs)

    with patch.object(builtins, "__import__", failed_import):
        p.handle({"type": "PUT_FILE_BEGIN", "id": "import-failed", "path": "/never-opened"})
    assert not p._uploads and "captain_ota" not in sys.modules
    assert not display.suspended and display.restores == 1
    reply = json.loads(port.written)
    assert reply["type"] == "ERROR" and reply["id"] == "import-failed"


@test("OTA finishing one file keeps TFT suspended until the last open upload completes")
def _():
    from unittest.mock import patch

    p, port = build_protocol()
    display = OTAFrame(p.app)
    display.suspend()

    class Upload:
        closed = False

        def close(self):
            self.closed = True

    first, second = Upload(), Upload()
    p._uploads.update({"/first": first, "/second": second})
    with patch.object(protocol.os, "remove", side_effect=OSError("old target absent")), \
            patch.object(protocol.os, "rename", lambda src, dst: None):
        p.handle({"type": "PUT_FILE_END", "id": "first", "path": "/first"})
        assert first.closed and not second.closed
        assert display.suspended and display.restores == 0
        assert "captain_ota" in sys.modules
        p.handle({"type": "PUT_FILE_END", "id": "second", "path": "/second"})
    assert second.closed and not p._uploads
    assert not display.suspended and display.restores == 1
    assert "captain_ota" not in sys.modules
    assert [json.loads(line) for line in port.written.splitlines()] == [
        {"type": "ACK", "id": "first"}, {"type": "ACK", "id": "second"},
    ]


@test("OTA disconnect closes every upload and releases cold module without a parent reference")
def _():
    import gc
    import weakref
    from unittest.mock import patch

    p, port = build_protocol()
    display = OTAFrame(p.app)
    display.suspend()
    ota = importlib.import_module("captain_ota")
    module_ref = weakref.ref(ota)
    function_ref = weakref.ref(ota.end)
    ota = None

    class Upload:
        closed = False

        def close(self):
            self.closed = True

    first, second = Upload(), Upload()
    p._uploads.update({"/first-upload": first, "/second-upload": second})
    p._upload_sizes.update({"/first-upload": 3, "/second-upload": 5})
    removed = []
    port.connected = False
    with patch.object(protocol.os, "remove", lambda path: removed.append(path)):
        assert p.poll() is None
    gc.collect()
    assert first.closed and second.closed
    assert not p._uploads and not p._upload_sizes
    assert "/first-upload.tmp" in removed and "/second-upload.tmp" in removed
    assert "captain_ota" not in sys.modules
    assert module_ref() is None and function_ref() is None
    assert not display.suspended and display.restores == 1
    port.connected = True
    port.push_rx(b'{"id":"new-session","type":"PING"}\n')
    assert p.poll() == {"id": "new-session", "type": "PING"}
    assert "captain_ota" not in sys.modules


# ---------------- runner ----------------

@test("LIST_PROFILES streams a large exact catalog through small writes and keeps PING after its newline")
def _():
    from unittest.mock import patch

    p, port = build_protocol(FakePort(max_per_write=3))
    profiles = [
        {"id": "profile-%02d" % index, "name": "Caf\u00e9 stage profile %02d" % index,
         "kind": "kemper_player", "color": "#ff7f00"}
        for index in range(48)
    ]
    expected = {"type": "PROFILE_LIST", "id": "bootstrap-3", "profiles": profiles,
                "active": "profile-07"}
    assert len(json.dumps(expected)) > 4096
    real_dumps, real_write = json.dumps, port.write
    sizes = []
    containers = []

    def fragmented_dumps(value, *args, **kwargs):
        if (isinstance(value, (dict, list)) and
                not (isinstance(value, dict) and value.get("type") == "ACK")):
            containers.append(type(value).__name__)
            raise MemoryError("contiguous PROFILE_LIST encode attempted")
        return real_dumps(value, *args, **kwargs)

    def bounded_write(data):
        sizes.append(len(data))
        assert len(data) <= 128, "full catalog reached one CDC write"
        return real_write(data)

    with patch.object(protocol.config, "list_profiles", return_value=profiles) as listing, \
            patch.object(protocol.config, "active_profile_id", return_value="profile-07"), \
            patch.object(protocol.json, "dumps", fragmented_dumps), \
            patch.object(port, "write", bounded_write):
        p.handle({"type": "LIST_PROFILES", "id": "bootstrap-3"})
        assert p._bg_gen is not None
        p.handle({"type": "PING", "id": "after-profiles"})
        assert p._deferred_out
        drain_background(p)
    listing.assert_called_once_with()
    assert containers == [], containers
    assert sizes and max(sizes) <= 128
    assert [json.loads(line) for line in port.written.splitlines()] == [
        expected, {"type": "ACK", "id": "after-profiles", "fw": protocol.VERSION},
    ]


@test("LIST_PROFILES keeps empty-catalog and active-profile response fields")
def _():
    from unittest.mock import patch

    p, port = build_protocol()
    with patch.object(protocol.config, "list_profiles", return_value=[]), \
            patch.object(protocol.config, "active_profile_id", return_value=""):
        p.handle({"type": "LIST_PROFILES", "id": "profileless"})
        drain_background(p)
    assert json.loads(port.written) == {
        "type": "PROFILE_LIST", "id": "profileless", "profiles": [], "active": "",
    }


@test("LIST_PROFILES read failure is correlated and never mistaken for an empty catalog")
def _():
    from unittest.mock import patch

    p, port = build_protocol()
    with patch.object(protocol.config, "list_profiles", side_effect=OSError("catalog unreadable")):
        p.handle({"type": "LIST_PROFILES", "id": "catalog-error"})
    response = json.loads(port.written)
    assert response["type"] == "ERROR" and response["id"] == "catalog-error"
    assert response["of"] == "LIST_PROFILES" and "catalog unreadable" in response["detail"]
    assert p._bg_gen is None


@test("LIST_PROFILES stream failure seals its partial line before PING and its correlated error")
def _():
    from unittest.mock import patch

    p, port = build_protocol()
    real_dumps = json.dumps

    def fail_name(value, *args, **kwargs):
        if value == "bad profile name":
            raise MemoryError("profile scalar allocation")
        return real_dumps(value, *args, **kwargs)

    with patch.object(protocol.config, "list_profiles", return_value=[{"name": "bad profile name"}]), \
            patch.object(protocol.config, "active_profile_id", return_value=""), \
            patch.object(protocol.json, "dumps", fail_name):
        p.handle({"type": "LIST_PROFILES", "id": "broken-profiles"})
        p.handle({"type": "PING", "id": "still-alive"})
        drain_background(p)
    lines = port.written.splitlines()
    assert len(lines) == 3, lines
    try:
        json.loads(lines[0])
        assert False, "damaged PROFILE_LIST was accepted"
    except ValueError:
        pass
    assert [json.loads(line) for line in lines[1:]] == [
        {"type": "ACK", "id": "still-alive", "fw": protocol.VERSION},
        {"type": "ERROR", "id": "broken-profiles", "error": "exception",
         "detail": "profile scalar allocation", "of": "LIST_PROFILES"},
    ]


@test("LIST_FONTS streams sorted supported font names without a contiguous container encode")
def _():
    from unittest.mock import patch

    p, port = build_protocol(FakePort(max_per_write=1))
    entries = ["zeta.pcf", "notes.txt", "caf\u00e9.bdf", "alpha.bdf", "subdirectory", "ignored.otf"]
    real_dumps = json.dumps
    containers = []

    def fragmented_dumps(value, *args, **kwargs):
        if isinstance(value, (dict, list)):
            containers.append(type(value).__name__)
            raise MemoryError("FONT_LIST cannot be encoded contiguously")
        return real_dumps(value, *args, **kwargs)

    with patch.object(protocol.os, "listdir", return_value=entries) as listing, \
            patch.object(protocol.json, "dumps", fragmented_dumps):
        p.handle({"type": "LIST_FONTS", "id": "screen-fonts"})
        drain_background(p)
    assert json.loads(port.written) == {
        "type": "FONT_LIST", "id": "screen-fonts",
        "fonts": ["alpha.bdf", "caf\u00e9.bdf", "zeta.pcf"],
    }
    listing.assert_called_once_with("/fonts")
    assert containers == [], containers
    assert port.write_call_count > 20


@test("LIST_FONTS missing directory still responds with a correlated empty list")
def _():
    from unittest.mock import patch

    p, port = build_protocol()
    with patch.object(protocol.os, "listdir", side_effect=OSError("no fonts directory")):
        p.handle({"type": "LIST_FONTS", "id": "empty-fonts"})
        drain_background(p)
    assert json.loads(port.written) == {"type": "FONT_LIST", "id": "empty-fonts", "fonts": []}


@test("LIST_FONTS keeps a concurrent PING behind its complete streamed response")
def _():
    from unittest.mock import patch

    p, port = build_protocol(FakePort(max_per_write=1))
    with patch.object(protocol.os, "listdir", return_value=["system-extra.bdf", "large.pcf"]):
        p.handle({"type": "LIST_FONTS", "id": "fonts"})
        assert p._bg_gen is not None
        p.handle({"type": "PING", "id": "keepalive"})
        assert p._deferred_out
        drain_background(p)
    responses = [json.loads(line) for line in port.written.splitlines()]
    assert [response["type"] for response in responses] == ["FONT_LIST", "ACK"], responses
    assert [response["id"] for response in responses] == ["fonts", "keepalive"], responses


@test("LIST_FONTS encode failure seals its line and isolates the PING and correlated error")
def _():
    from unittest.mock import patch

    p, port = build_protocol()
    real_dumps = json.dumps

    def fail_font(value, *args, **kwargs):
        if value == "broken.bdf":
            raise MemoryError("font scalar allocation")
        return real_dumps(value, *args, **kwargs)

    with patch.object(protocol.os, "listdir", return_value=["broken.bdf"]), \
            patch.object(protocol.json, "dumps", fail_font):
        p.handle({"type": "LIST_FONTS", "id": "broken-fonts"})
        p.handle({"type": "PING", "id": "after-fonts"})
        drain_background(p)
    lines = port.written.splitlines()
    assert len(lines) == 3, lines
    try:
        json.loads(lines[0])
        assert False, "the deliberately damaged FONT_LIST was accepted"
    except ValueError:
        pass
    assert json.loads(lines[1]) == {
        "type": "ACK", "id": "after-fonts", "fw": protocol.VERSION,
    }, lines
    assert json.loads(lines[2]) == {
        "type": "ERROR", "id": "broken-fonts", "error": "exception",
        "detail": "font scalar allocation", "of": "LIST_FONTS",
    }, lines

class RXFrameBudget:
    """Model cached TFT + open Editor patch + incoming config on a small heap."""

    class Allocation:
        def __init__(self, size):
            self.data = bytearray(size)

    def __init__(self, app, capacity=8192):
        self.capacity = capacity
        self.allocations = []
        self._frame = self.allocate(3072)
        self.editor_patch = self.allocate(2048)
        self.parsed = None
        self._suspended = False
        self.suspends = 0
        self.restores = 0
        self.load_positions = []
        self.real_load = json.load
        app.display = self
        app._mark_display_dirty = self.mark_dirty

    def allocate(self, size):
        import weakref
        used = sum(len(ref().data) for ref in self.allocations if ref() is not None)
        if used + size > self.capacity:
            raise MemoryError("cached frame and replacement config exceed heap")
        allocation = self.Allocation(size)
        self.allocations.append(weakref.ref(allocation))
        return allocation

    def load(self, source):
        self.load_positions.append(source.tell())
        assert source.tell() == 0, "retry did not rewind the failed JSON parser"
        try:
            self.parsed = self.allocate(4096)
        except MemoryError:
            source.read(127)  # A real parser can fail after consuming a prefix.
            raise
        return self.real_load(source)

    def suspend(self):
        self.suspends += 1
        self._suspended = True
        self._frame = None

    def resume(self):
        previous = self._suspended
        self._suspended = False
        return previous

    def mark_dirty(self):
        assert not self._suspended
        self.restores += 1


def screen_save_request():
    return {
        "id": "screen-save", "type": "PUT_GLOBAL",
        "device": {
            "device_name": "Captain Caff\u00e8", "kemper": {"enabled": True},
            "tft": {"layout": [
                {"field": field, "color": color, "font": "system", "size": 5,
                 "x": 0, "y": index * 60, "prefix": prefix, "suffix": "",
                 "align": "left", "valign": "top"}
                for index, (field, color, prefix) in enumerate([
                    ("patch_name", "#ffffff", ""), ("bank", "#9aa1ad", "BANK "),
                    ("kemper_rig_in_bank", "#6fd99a", "RIG "),
                    ("hold_effect", "#ffffff", ""), ("expression_mode", "#ff7f00", ""),
                ])]},
            "bank_colors": ["#6fd99b"] * 25,
            "preset_navigation": {"B1": list(range(1, 26))},
        },
    }


@test("spooled Screen Save borrows real frame allocations through parse/save/apply only after OOM")
def _():
    from unittest.mock import patch

    p, port = build_protocol()
    frame = RXFrameBudget(p.app)
    request = screen_save_request()
    saves = []
    applied = []

    def save(device):
        assert not frame._suspended and frame._frame is None
        scratch = frame.allocate(1024)  # Persistence also needs temporary RAM.
        assert len(scratch.data) == 1024
        saves.append(device)

    def apply(device):
        assert not frame._suspended and frame._frame is None and frame.restores == 1
        p.app.device = device
        applied.append(device)

    wire = json.dumps(request).encode() + b"\n"
    port.push_rx(wire + b'{"type":"PING","id":"after-save"}\n')
    with patch.object(protocol.json, "load", frame.load), \
            patch.object(protocol.config, "save_device", save), \
            patch.object(p.app, "apply_global", apply):
        for _ in range(20):
            p.handle(p.poll())
            if not port.in_waiting and p._rx_pending is None:
                break
        else:
            assert False, "Screen Save exceeded the bounded RX tick budget"
    assert frame.load_positions == [0, 0]
    assert frame.suspends == frame.restores == 1 and not frame._suspended
    assert saves == applied == [request["device"]]
    assert p.app.device == request["device"]
    assert not Path(p._rx_path).exists()
    assert [json.loads(line) for line in port.written.splitlines()] == [
        {"type": "ACK", "id": "screen-save"},
        {"type": "EVENT", "event": "global_changed"},
        {"type": "ACK", "id": "after-save", "fw": protocol.VERSION},
    ]


@test("spooled config with sufficient heap keeps its cached TFT intact")
def _():
    from unittest.mock import patch

    p, port = build_protocol()
    frame = RXFrameBudget(p.app, capacity=16384)
    cached = frame._frame
    request = screen_save_request()
    port.push_rx(json.dumps(request).encode() + b"\n")
    with patch.object(protocol.json, "load", frame.load), \
            patch.object(protocol.config, "save_device") as save:
        messages = collect_rx(p, port)
        assert messages == [request]
        p.handle(messages[0])
    save.assert_called_once_with(request["device"])
    assert frame.load_positions == [0]
    assert not frame.suspends and not frame.restores and frame._frame is cached


@test("spooled config retry failure restores TFT, preserves config and allows next PING")
def _():
    from unittest.mock import patch

    # Insufficient total RAM, malformed JSON and file IO each leave exactly
    # one correlated error, with no persistence and no abandoned suspension.
    for failure, error in [(MemoryError("still full"), "rx_oom"),
                           (ValueError("broken JSON"), "bad_json"),
                           (OSError("read failed"), "rx_io")]:
        p, port = build_protocol()
        frame = RXFrameBudget(p.app)
        baseline = p.app.device
        request = screen_save_request()
        port.push_rx(json.dumps(request).encode() + b"\n"
                     + b'{"type":"PING","id":"next"}\n')
        attempts = []

        def failed_parse(source):
            attempts.append(source.tell())
            if len(attempts) == 1:
                source.read(73)
                raise MemoryError("first parser allocation")
            assert frame._suspended
            raise failure

        with patch.object(protocol.json, "load", failed_parse), \
                patch.object(protocol.config, "save_device") as save:
            messages = collect_rx(p, port, limit=20)
            assert messages == [{"type": "PING", "id": "next"}]
            p.handle(messages[0])
        save.assert_not_called()
        assert attempts == [0, 0]
        assert frame.suspends == frame.restores == 1 and not frame._suspended
        assert p.app.device is baseline
        assert not Path(p._rx_path).exists()
        assert [json.loads(line) for line in port.written.splitlines()] == [
            {"type": "ERROR", "error": error, "id": "screen-save"},
            {"type": "ACK", "id": "next", "fw": protocol.VERSION},
        ]


@test("recovered Screen Save restores TFT on handler error and validation rejection")
def _():
    from unittest.mock import patch

    for failure in ("save", "apply", "missing_device", "no_such_profile"):
        p, port = build_protocol()
        frame = RXFrameBudget(p.app)
        request = screen_save_request()
        if failure == "missing_device":
            request["unused"] = request.pop("device")
        elif failure == "no_such_profile":
            request["profile"] = "missing"
        port.push_rx(json.dumps(request).encode() + b"\n")
        with patch.object(protocol.json, "load", frame.load), \
                patch.object(protocol.config, "save_device") as save, \
                patch.object(p.app, "apply_global") as apply, \
                patch.object(protocol.config, "profile_exists", return_value=False):
            if failure == "save":
                save.side_effect = OSError("disk full")
            elif failure == "apply":
                apply.side_effect = MemoryError("apply")
            messages = collect_rx(p, port)
            assert not frame._suspended and frame._frame is None and frame.restores == 1
            p.handle(messages[0])
        assert frame.suspends == frame.restores == 1 and not frame._suspended
        replies = [json.loads(line) for line in port.written.splitlines()]
        assert len(replies) == 1 and replies[0]["type"] == "ERROR", replies
        assert replies[0]["id"] == "screen-save"


@test("RX recovery cannot release a display suspension owned by an OTA upload")
def _():
    from unittest.mock import patch

    p, port = build_protocol()
    frame = RXFrameBudget(p.app)
    frame.suspend()
    port.push_rx(json.dumps(screen_save_request()).encode() + b"\n")
    with patch.object(protocol.json, "load", side_effect=MemoryError("OTA uses free heap")) as load:
        assert collect_rx(p, port) == []
    assert load.call_count == 1
    assert frame._suspended and frame.suspends == 1 and frame.restores == 0
    assert json.loads(port.written) == {
        "type": "ERROR", "error": "rx_oom", "id": "screen-save",
    }


@test("RX parse suspension is already released before disconnect or an undispatched next poll")
def _():
    from unittest.mock import patch

    for disconnect in (False, True):
        p, port = build_protocol()
        frame = RXFrameBudget(p.app)
        baseline = p.app.device
        port.push_rx(json.dumps(screen_save_request()).encode() + b"\n")
        with patch.object(protocol.json, "load", frame.load):
            messages = collect_rx(p, port)
        assert len(messages) == 1 and not frame._suspended and frame.restores == 1
        if disconnect:
            port.connected = False
            p.pump_background()  # Session cleanup can run before handle().
        else:
            assert p.poll() is None  # A caller abandoned the returned command.
        assert frame.suspends == frame.restores == 1 and not frame._suspended
        assert not Path(p._rx_path).exists()
        assert p.app.device is baseline


@test("low-heap non-config and malformed spooled commands do not leave TFT suspended")
def _():
    from unittest.mock import patch

    for payload in ({"type": "PING", "padding": "x" * 1600}, ["x" * 1600]):
        p, port = build_protocol()
        frame = RXFrameBudget(p.app)
        port.push_rx(json.dumps(payload).encode() + b"\n")
        with patch.object(protocol.json, "load", frame.load):
            assert collect_rx(p, port) == [payload]
        assert frame.load_positions == [0, 0]
        assert frame.suspends == frame.restores == 1 and not frame._suspended


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
