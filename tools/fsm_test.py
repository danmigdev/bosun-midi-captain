#!/usr/bin/env python3
"""Offline tests for the firmware switch FSM.

Runs the actual SwitchFsm class from firmware/lib/captain/bindings.py against
synthetic pin levels and timestamps - no hardware, no CircuitPython, no MIDI.

This is what catches debounce / long-press boundary / double-tap window /
auto-momentary regressions when you change the FSM logic.

Usage
-----
    python tools/fsm_test.py
"""

import sys
import types
from pathlib import Path


# ---------------- mock CircuitPython surface ----------------

_digitalio = types.ModuleType("digitalio")

class _MockDigitalInOut:
    def __init__(self, pin):
        self.pin = pin
        self.direction = None
        self.pull = None
        self.value = True   # pull-up idle = HIGH = True

class _Direction:
    INPUT = "input"
    OUTPUT = "output"

class _Pull:
    UP = "up"
    DOWN = "down"

_digitalio.DigitalInOut = _MockDigitalInOut
_digitalio.Direction = _Direction
_digitalio.Pull = _Pull
sys.modules["digitalio"] = _digitalio

_board = types.ModuleType("board")
for _name in [f"GP{i}" for i in range(30)]:
    setattr(_board, _name, _name)
sys.modules["board"] = _board

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "firmware" / "lib"))
from captain.bindings import SwitchFsm   # noqa: E402


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


def fresh(**kwargs):
    """Build a fresh FSM with sensible defaults overridable per-test."""
    defaults = dict(name="1", pin="GP1", long_press_ms=600,
                    double_tap_window_ms=250, auto_momentary_on_hold=True,
                    auto_momentary_ms=500)
    defaults.update(kwargs)
    return SwitchFsm(**defaults)


def press(fsm):
    fsm.io.value = False  # LOW = pressed (pull-up)

def release(fsm):
    fsm.io.value = True   # HIGH = released

def poll(fsm, mode, now_ms):
    return fsm.poll(now_ms, mode)


# ---------------- tests ----------------

@test("tap: press fires once, release silent")
def _():
    fsm = fresh()
    # Idle for debounce; press at t=100 with stable raw=False after 10ms
    press(fsm)
    raw, trig = poll(fsm, "tap", 100)
    raw, trig = poll(fsm, "tap", 106)   # >5ms debounce
    assert raw == "press", f"expected press, got {raw}"
    assert trig == ["press"], f"expected ['press'], got {trig}"
    release(fsm)
    raw, trig = poll(fsm, "tap", 200)
    raw, trig = poll(fsm, "tap", 206)
    assert raw == "release"
    assert trig == [], f"release on tap mode should fire nothing, got {trig}"


@test("latched: alternates toggle_on / toggle_off")
def _():
    fsm = fresh(auto_momentary_on_hold=False)
    # 3 quick presses
    for i, expected in enumerate(["toggle_on", "toggle_off", "toggle_on"]):
        t = 1000 + i * 200
        press(fsm)
        poll(fsm, "latched", t)
        _, trig = poll(fsm, "latched", t + 6)
        assert trig == [expected], f"press #{i}: expected {expected}, got {trig}"
        release(fsm)
        poll(fsm, "latched", t + 50)
        poll(fsm, "latched", t + 56)
    assert fsm.latched_on is True


@test("momentary: press fires press, release fires release")
def _():
    fsm = fresh()
    press(fsm)
    poll(fsm, "momentary", 100)
    _, trig = poll(fsm, "momentary", 106)
    assert trig == ["press"]
    release(fsm)
    poll(fsm, "momentary", 200)
    _, trig = poll(fsm, "momentary", 206)
    assert trig == ["release"]


@test("long_press_alt: quick press fires 'press' on release, long fires 'long_press' while held")
def _():
    fsm = fresh(long_press_ms=600)
    press(fsm)
    poll(fsm, "long_press_alt", 100)
    _, trig = poll(fsm, "long_press_alt", 106)
    assert trig == []
    # release at 300ms - under threshold, fires 'press'
    release(fsm)
    poll(fsm, "long_press_alt", 300)
    _, trig = poll(fsm, "long_press_alt", 306)
    assert trig == ["press"], f"short release should fire 'press', got {trig}"

    fsm2 = fresh(long_press_ms=600)
    press(fsm2)
    poll(fsm2, "long_press_alt", 1000)
    poll(fsm2, "long_press_alt", 1006)
    # tick at 1700ms (700ms after press) - long-press fires
    _, trig = poll(fsm2, "long_press_alt", 1700)
    assert trig == ["long_press"], f"long held should fire 'long_press', got {trig}"
    # release after long_press fired - should NOT also fire 'press'
    release(fsm2)
    poll(fsm2, "long_press_alt", 1800)
    _, trig = poll(fsm2, "long_press_alt", 1806)
    assert trig == [], f"release after long_press should be silent, got {trig}"


@test("double_tap: two taps in window ->'double_tap'; lone tap ->'press' after window")
def _():
    fsm = fresh(double_tap_window_ms=250)
    # First press
    press(fsm)
    poll(fsm, "double_tap", 1000)
    _, trig = poll(fsm, "double_tap", 1006)
    assert trig == []
    release(fsm)
    poll(fsm, "double_tap", 1060)
    poll(fsm, "double_tap", 1066)
    # Second press at 1150ms - well inside window
    press(fsm)
    poll(fsm, "double_tap", 1150)
    _, trig = poll(fsm, "double_tap", 1156)
    assert trig == ["double_tap"], f"expected double_tap, got {trig}"

    # Lone tap ->fires 'press' after window expiry
    fsm2 = fresh(double_tap_window_ms=250)
    press(fsm2)
    poll(fsm2, "double_tap", 2000)
    poll(fsm2, "double_tap", 2006)
    release(fsm2)
    poll(fsm2, "double_tap", 2060)
    poll(fsm2, "double_tap", 2066)
    # Tick past the window
    _, trig = poll(fsm2, "double_tap", 2300)
    assert trig == ["press"], f"lone tap after window should fire 'press', got {trig}"


@test("auto-momentary: hold past threshold then release reverts latched")
def _():
    fsm = fresh(auto_momentary_on_hold=True, auto_momentary_ms=500)
    # First press (quick tap) - latches on
    press(fsm)
    poll(fsm, "latched", 1000)
    _, trig = poll(fsm, "latched", 1006)
    assert trig == ["toggle_on"]
    release(fsm)
    poll(fsm, "latched", 1100)
    poll(fsm, "latched", 1106)
    assert fsm.latched_on is True

    # Second press, hold 700ms, release - should revert (toggle_off)
    press(fsm)
    poll(fsm, "latched", 2000)
    _, trig = poll(fsm, "latched", 2006)
    assert trig == ["toggle_off"]      # immediate toggle on press
    assert fsm.latched_on is False
    # Held > 500ms ->release reverts back to True (which means another toggle_on fires)
    release(fsm)
    poll(fsm, "latched", 2706)
    _, trig = poll(fsm, "latched", 2712)
    assert trig == ["toggle_on"], f"long hold should revert, got {trig}"
    assert fsm.latched_on is True


@test("auto-momentary: short tap does NOT revert")
def _():
    fsm = fresh(auto_momentary_on_hold=True, auto_momentary_ms=500)
    press(fsm)
    poll(fsm, "latched", 1000)
    _, trig = poll(fsm, "latched", 1006)
    assert trig == ["toggle_on"]
    release(fsm)
    poll(fsm, "latched", 1200)   # only 200ms held - well under 500ms
    _, trig = poll(fsm, "latched", 1206)
    assert trig == [], f"short tap should not revert, got {trig}"
    assert fsm.latched_on is True


@test("debounce: <5ms bouncing is filtered")
def _():
    fsm = fresh()
    # Pin bounces from HIGH to LOW to HIGH to LOW within 5ms - should NOT trigger
    fsm.io.value = False
    poll(fsm, "tap", 1000)
    fsm.io.value = True
    poll(fsm, "tap", 1002)
    fsm.io.value = False
    poll(fsm, "tap", 1003)
    # Settled LOW for >5ms now
    _, trig = poll(fsm, "tap", 1010)
    assert trig == ["press"], f"expected press after settling, got {trig}"


@test("reset clears latched state and timers")
def _():
    fsm = fresh()
    press(fsm)
    poll(fsm, "latched", 1000)
    poll(fsm, "latched", 1006)
    assert fsm.latched_on is True
    fsm.reset()
    assert fsm.latched_on is False
    assert fsm._press_start_ms == 0


@test("disabled auto-momentary: long hold does NOT revert")
def _():
    fsm = fresh(auto_momentary_on_hold=False, auto_momentary_ms=500)
    press(fsm)
    poll(fsm, "latched", 1000)
    _, trig = poll(fsm, "latched", 1006)
    assert trig == ["toggle_on"]
    release(fsm)
    poll(fsm, "latched", 2000)        # 994ms held - would normally revert
    _, trig = poll(fsm, "latched", 2006)
    assert trig == [], f"with auto-momentary off, no revert; got {trig}"
    assert fsm.latched_on is True


@test("long_press_alt: reset() while switch held does NOT re-fire long_press on next tick")
def _():
    # Reproduces the "bank ping-pong" regression: a long_press_alt binding
    # whose action triggers a patch reload (and therefore reset_all)
    # would re-fire long_press on the very next tick because reset()
    # zeroed _fired_long_press while the user was still holding.
    fsm = fresh(long_press_ms=600)
    press(fsm)
    poll(fsm, "long_press_alt", 1000)
    poll(fsm, "long_press_alt", 1006)
    _, trig = poll(fsm, "long_press_alt", 1700)
    assert trig == ["long_press"], f"expected long_press, got {trig}"
    # Simulate the consequence of the bound action: patch reload calls
    # switches.reset_all(). Switch is still physically held.
    fsm.reset()
    # Multiple subsequent ticks - must stay silent until the user
    # releases and re-presses.
    for t in (1750, 2000, 2500, 3000):
        _, trig = poll(fsm, "long_press_alt", t)
        assert trig == [], f"tick {t} after reset re-fired: {trig}"
    # Release + new press should fire long_press again normally.
    release(fsm)
    poll(fsm, "long_press_alt", 3100)
    poll(fsm, "long_press_alt", 3106)
    press(fsm)
    poll(fsm, "long_press_alt", 4000)
    poll(fsm, "long_press_alt", 4006)
    _, trig = poll(fsm, "long_press_alt", 4700)
    assert trig == ["long_press"], f"after release+press should fire again, got {trig}"


@test("long_press_alt boundary: held exactly to threshold")
def _():
    fsm = fresh(long_press_ms=600)
    press(fsm)
    poll(fsm, "long_press_alt", 1000)
    poll(fsm, "long_press_alt", 1006)
    # press_start is registered at debounce-settle = t=1006; threshold = 1606.
    _, trig = poll(fsm, "long_press_alt", 1605)
    assert trig == [], f"1ms under threshold should not fire, got {trig}"
    _, trig = poll(fsm, "long_press_alt", 1606)
    assert trig == ["long_press"], f"at threshold should fire, got {trig}"


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
