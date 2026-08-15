#!/usr/bin/env python3
"""Offline tests for captain.midi.MidiEngine outbound TX behaviour.

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

    def __init__(self, stall_ms=0):
        self._deadline_ns = time.monotonic_ns() + int(stall_ms * 1_000_000)
        self.write_calls = 0

    def write(self, data):
        self.write_calls += 1
        if time.monotonic_ns() < self._deadline_ns:
            return 0
        return len(data)


usb_midi.ports = [_FakeUsbIn(), _FakeUsbOut()]
sys.modules["usb_midi"] = usb_midi

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "firmware" / "lib"))
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


def fresh_engine(stall_ms=0):
    engine = MidiEngine()
    engine.usb_out = _FakeUsbOut(stall_ms=stall_ms)
    return engine


# ---------------- tests ----------------

@test("baseline: an immediately-writable port never calls poll_hook")
def _():
    engine = fresh_engine(stall_ms=0)
    calls = []
    engine.poll_hook = lambda: calls.append(1)
    engine.send_cc(1, 10, 127)
    assert calls == [], f"expected no poll_hook calls, got {len(calls)}"


@test("a stalled write spins the retry loop with no poll_hook set -> no error")
def _():
    # Reproduces the gap: before poll_hook existed, nothing observed the
    # world during a stalled write. With poll_hook left unset (None, the
    # default), the loop must still behave exactly as before - just blind.
    # stall_ms is well under _tx_usb's own ~10ms hard budget, so the write
    # always eventually succeeds; the point is only that this never raises.
    engine = fresh_engine(stall_ms=3)
    engine.send_cc(1, 10, 127)  # must not raise, must eventually return
    assert engine.usb_out.write_calls >= 1, "expected at least one write attempt"


@test("a stalled write calls poll_hook on every retry spin")
def _():
    engine = fresh_engine(stall_ms=4)
    calls = []
    engine.poll_hook = lambda: calls.append(time.monotonic_ns())
    engine.send_cc(1, 10, 127)
    assert len(calls) >= 3, f"expected several poll_hook calls during a 4ms stall, got {len(calls)}"


@test("poll_hook raising an exception never breaks MIDI TX")
def _():
    engine = fresh_engine(stall_ms=2)

    def bad_hook():
        raise RuntimeError("boom")

    engine.poll_hook = bad_hook
    engine.send_cc(1, 10, 127)  # must not raise


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
    engine = fresh_engine(stall_ms=25)
    # Windows' time.sleep() granularity is much coarser than the ~0.5ms the
    # retry loop asks for, so real elapsed wall time between hook() calls is
    # unreliable in this offline test (observed as few as ~6 calls across a
    # 10ms budget). Drive the FSM off a synthetic clock keyed to the call
    # count instead - each call is a discrete "moment" >5ms (the FSM's
    # DEBOUNCE_MS) apart from the last, independent of real scheduling
    # jitter, while still exercising the real poll_hook plumbing end to end.
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
