#!/usr/bin/env python3
"""Offline tests for captain.midi.MidiEngine USB I/O behaviour.

Covers the USB write retry loop in _tx_usb(): a full host endpoint buffer
makes it spin for up to ~10 ms via short sleeps. That loop used to run with
nothing else observing the world - a footswitch pressed and released entirely
inside it was invisible to the main loop (switches are read once per tick,
before/after this call, never during it). poll_hook lets the caller (Captain)
re-sample switch pins on every retry spin instead of losing that window.

No hardware, no CircuitPython.

Usage
-----
    python tools/midi_tx_test.py
"""

import sys
import time
import types
from contextlib import contextmanager
from pathlib import Path


# ---------------- mock CircuitPython surface ----------------

board = types.ModuleType("board")
for _i in range(30):
    setattr(board, f"GP{_i}", f"GP{_i}")
sys.modules["board"] = board

busio = types.ModuleType("busio")


class _UART:
    def __init__(self, *a, **k):
        self.in_waiting = 0

    def read(self, n=None):
        return b""

    def write(self, b):
        return len(b)


busio.UART = _UART
sys.modules["busio"] = busio

digitalio = types.ModuleType("digitalio")


class _MockDigitalInOut:
    def __init__(self, pin):
        self.pin = pin
        self.direction = None
        self.pull = None
        self.value = True   # pull-up idle = HIGH = True


digitalio.DigitalInOut = _MockDigitalInOut
digitalio.Direction = types.SimpleNamespace(INPUT="in", OUTPUT="out")
digitalio.Pull = types.SimpleNamespace(UP="up", DOWN="down")
sys.modules["digitalio"] = digitalio

usb_midi = types.ModuleType("usb_midi")


class _FakeUsbIn:
    pass


class _FakeUsbOut:
    """Simulates a USB-MIDI OUT endpoint. `stall_ms` controls how long
    write() keeps returning 0 (host buffer full) measured from construction;
    after that it accepts the full chunk."""

    def __init__(self, stall_ms=0, clock=None):
        self._clock = clock if clock is not None else time
        self._deadline_ns = self._clock.monotonic_ns() + int(stall_ms * 1_000_000)
        self.write_calls = 0

    def write(self, data):
        self.write_calls += 1
        if self._clock.monotonic_ns() < self._deadline_ns:
            return 0
        return len(data)


usb_midi.ports = [_FakeUsbIn(), _FakeUsbOut()]
sys.modules["usb_midi"] = usb_midi

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "firmware" / "lib"))
import captain.midi as midi_module  # noqa: E402
from captain.midi import MidiEngine  # noqa: E402


# ---------------- harness ----------------

PASS_COUNT = 0
FAIL_COUNT = 0
FAILURES: list[str] = []


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


class _ControlledTime:
    """Clock advanced by retry sleeps or a pause after its next sample."""

    def __init__(self):
        self.now_ns = 0
        self.sleep_calls = []
        self.pause_after_next_sample_ns = 0

    def monotonic_ns(self):
        sampled = self.now_ns
        self.now_ns += self.pause_after_next_sample_ns
        self.pause_after_next_sample_ns = 0
        return sampled

    def sleep(self, seconds):
        self.sleep_calls.append(seconds)
        self.now_ns += int(seconds * 1_000_000_000)


@contextmanager
def controlled_tx_time():
    """Replace only captain.midi's time dependency for one test.

    The test still calls ``MidiEngine.send_cc`` and the production ``_tx_usb``
    implementation.  Controlling its clock removes host scheduler jitter while
    making every requested 0.5 ms retry yield advance time exactly 0.5 ms.
    """
    clock = _ControlledTime()
    original = midi_module.time
    midi_module.time = clock
    try:
        yield clock
    finally:
        midi_module.time = original


def fresh_engine(stall_ms=0, clock=None):
    engine = MidiEngine()
    engine.usb_out = _FakeUsbOut(stall_ms=stall_ms, clock=clock)
    return engine


class _ScriptedUsbOut:
    """Records the real retry loop's chunks and accepts specified byte counts."""

    def __init__(self, counts=()):
        self.counts = list(counts)
        self.calls = []
        self.accepted = bytearray()

    def write(self, data):
        self.calls.append(bytes(data))
        count = self.counts.pop(0) if self.counts else len(data)
        self.accepted.extend(data[:len(data) if count is None else count])
        return count


# ---------------- tests ----------------

@test("baseline: an immediately-writable port never calls poll_hook")
def _():
    with controlled_tx_time() as clock:
        engine = fresh_engine(stall_ms=0, clock=clock)
        calls = []
        engine.poll_hook = lambda: calls.append(1)
        engine.send_cc(1, 10, 127)
    assert calls == [], f"expected no poll_hook calls, got {len(calls)}"
    assert clock.sleep_calls == [], clock.sleep_calls


@test("a pre-write pause cannot discard a beacon without trying a writable endpoint")
def _():
    payload = bytes((0, 0x20, 0x33, 2, 127, 0x7E, 0, 0x40, 2, 0x22, 5))
    framed = b"\xf0" + payload + b"\xf7"
    for pause_ns in (10_000_000, 12_000_000, 40_000_000):
        with controlled_tx_time() as clock:
            engine = fresh_engine(clock=clock)
            engine.usb_out = _ScriptedUsbOut()
            hooks = []
            engine.poll_hook = lambda: hooks.append(1)
            # Models a pause after the timestamp used to build the deadline,
            # before USB has been given even one opportunity to accept data.
            clock.pause_after_next_sample_ns = pause_ns
            engine.send_sysex(payload)
        assert engine.usb_out.calls == [framed], (pause_ns, engine.usb_out.calls)
        assert engine.usb_out.accepted == framed, engine.usb_out.accepted
        assert getattr(engine, "usb_tx_dropped", 0) == 0
        assert hooks == [] and clock.sleep_calls == []


@test("an endpoint that stays full retains a bounded retry budget after a pre-write pause")
def _():
    for pause_ns, attempts in ((0, 20), (40_000_000, 1)):
        with controlled_tx_time() as clock:
            engine = fresh_engine(stall_ms=100, clock=clock)
            hooks = []
            engine.poll_hook = lambda: hooks.append(1)
            clock.pause_after_next_sample_ns = pause_ns
            engine.send_cc(1, 10, 127)
        assert engine.usb_out.write_calls == attempts, engine.usb_out.write_calls
        assert len(hooks) == attempts and len(clock.sleep_calls) == attempts
        assert clock.now_ns == pause_ns + attempts * 500_000, clock.now_ns
        assert engine.usb_tx_dropped == 3, engine.usb_tx_dropped


@test("short writes retry only the remainder and accept a final None as success")
def _():
    payload = bytes((0, 0x20, 0x33, 2, 127, 0x7E, 0, 0x40, 2, 0x22, 5))
    framed = b"\xf0" + payload + b"\xf7"
    with controlled_tx_time() as clock:
        engine = fresh_engine(clock=clock)
        engine.usb_out = _ScriptedUsbOut((4, 0, 3, None))
        hooks = []
        engine.poll_hook = lambda: hooks.append(1)
        engine.send_sysex(payload)
    assert engine.usb_out.calls == [framed, framed[4:], framed[4:], framed[7:]]
    assert engine.usb_out.accepted == framed, engine.usb_out.accepted
    assert getattr(engine, "usb_tx_dropped", 0) == 0
    assert hooks == [1] and clock.sleep_calls == [0.0005]


@test("a partial first write after a pre-write pause counts only its unsent remainder")
def _():
    with controlled_tx_time() as clock:
        engine = fresh_engine(clock=clock)
        engine.usb_out = _ScriptedUsbOut((1,))
        clock.pause_after_next_sample_ns = 12_000_000
        engine.send_cc(1, 10, 127)
    assert engine.usb_out.calls == [b"\xb0\x0a\x7f"], engine.usb_out.calls
    assert engine.usb_out.accepted == b"\xb0", engine.usb_out.accepted
    assert engine.usb_tx_dropped == 2, engine.usb_tx_dropped
    assert clock.sleep_calls == []


@test("a stalled write spins the retry loop with no poll_hook set -> no error")
def _():
    # Reproduces the gap: before poll_hook existed, nothing observed the
    # world during a stalled write. With poll_hook left unset (None, the
    # default), the loop must still behave exactly as before - just blind.
    # stall_ms is well under _tx_usb's own ~10ms hard budget, so the write
    # always eventually succeeds; the point is only that this never raises.
    with controlled_tx_time() as clock:
        engine = fresh_engine(stall_ms=3, clock=clock)
        engine.send_cc(1, 10, 127)  # must not raise, must eventually return
    assert engine.usb_out.write_calls == 7, engine.usb_out.write_calls
    assert len(clock.sleep_calls) == 6, clock.sleep_calls


@test("a stalled write calls poll_hook on every retry spin")
def _():
    with controlled_tx_time() as clock:
        engine = fresh_engine(stall_ms=4, clock=clock)
        calls = []
        engine.poll_hook = lambda: calls.append(clock.monotonic_ns())
        engine.send_cc(1, 10, 127)
    assert len(calls) == 8, f"expected 8 poll_hook calls during a 4ms stall, got {len(calls)}"
    assert len(clock.sleep_calls) == 8, clock.sleep_calls


@test("poll_hook raising an exception never breaks MIDI TX")
def _():
    with controlled_tx_time() as clock:
        engine = fresh_engine(stall_ms=2, clock=clock)

        def bad_hook():
            raise RuntimeError("boom")

        engine.poll_hook = bad_hook
        engine.send_cc(1, 10, 127)  # must not raise
    assert len(clock.sleep_calls) == 4, clock.sleep_calls


@test("RX retries with a smaller read when the 256-byte allocation fails")
def _():
    engine = fresh_engine()

    class MemoryBoundUsbIn:
        def __init__(self):
            self.calls = []
            self.payload = b"\xb0\x1e\x7f"

        def read(self, size):
            self.calls.append(size)
            if size > 64:
                raise MemoryError("allocating 257 bytes")
            payload, self.payload = self.payload, b""
            return payload

    usb_in = MemoryBoundUsbIn()
    engine.usb_in = usb_in
    events = engine.poll()

    assert usb_in.calls == [256, 64], usb_in.calls
    assert events == [("usb", 1, 0xB0, [0x1e, 0x7f])], events


@test("RX allocation failure cannot abort the Captain main-loop caller")
def _():
    engine = fresh_engine()

    class ExhaustedUsbIn:
        def read(self, size):
            raise MemoryError("heap exhausted")

    engine.usb_in = ExhaustedUsbIn()
    assert engine.poll() == []


@test("a switch press-and-release timed entirely inside a MIDI stall is captured")
def _():
    # End-to-end proof of the fix: a SwitchFsm polled only via poll_hook (never
    # from a normal tick) still registers the full press/release cycle, as
    # long as poll_hook fires at least twice more than DEBOUNCE_MS apart.
    from captain.bindings import SwitchFsm

    fsm = SwitchFsm(name="1", pin="GP1")
    # _tx_usb's own retry budget is a hard-coded ~10ms regardless of how long
    # the port stays stalled; a generous stall_ms just guarantees the write
    # never opens up on its own before that budget runs out.
    with controlled_tx_time() as clock:
        engine = fresh_engine(stall_ms=25, clock=clock)
        # The FSM clock is deliberately separate from the TX retry clock:
        # every hook represents a pin sample more than DEBOUNCE_MS after the
        # previous one, while _tx_usb itself advances through its real 0.5 ms
        # sleep/retry sequence under the controlled monotonic clock.
        call_count = [0]
        seen_edges = []

        def hook():
            call_count[0] += 1
            now_ms = call_count[0] * 6
            # Held for the first 2 calls (comfortably past the 5ms debounce
            # window on the 2nd), released from the 3rd call on - the whole
            # cycle happens between two normal tick-level polls, which never
            # run here (this test only calls fsm.poll() from inside the hook).
            fsm.io.value = call_count[0] >= 3
            raw_edge, triggers = fsm.poll(now_ms, "tap")
            if raw_edge is not None:
                seen_edges.append(raw_edge)

        engine.poll_hook = hook
        engine.send_cc(1, 10, 127)
    assert call_count[0] == 20, f"expected full 10ms retry budget, got {call_count[0]} hooks"
    assert len(clock.sleep_calls) == 20, clock.sleep_calls
    assert "press" in seen_edges, f"press edge never observed, saw {seen_edges}"
    assert "release" in seen_edges, f"release edge never observed, saw {seen_edges}"


# ---------------- runner ----------------

def main():
    total = PASS_COUNT + FAIL_COUNT
    print(f"\n{PASS_COUNT}/{total} passed, {FAIL_COUNT} failed")
    if FAILURES:
        print("\nFailures:")
        for f in FAILURES:
            print("  -", f)
        sys.exit(1)


if __name__ == "__main__":
    main()
