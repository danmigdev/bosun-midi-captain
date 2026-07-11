#!/usr/bin/env python3
"""Offline tests for the Bosun firmware plugins.

Covers the four device profiles under firmware/lib/plugins/:
  - generic_midi.py  (program_change_bank, cc_toggle)
  - line6_helix.py   (preset, snapshot, footswitch table with the FS6 gap,
                      tap tempo, tuner, looper, update_context)
  - ampero.py        (cross-plugin self-consistency)
  - kemper.py        (cross-plugin self-consistency)

Two flavours of test:
  1) Targeted dispatch checks that pin down exact CC numbers / ordering for
     generic_midi and line6_helix.
  2) A CROSS-PLUGIN self-consistency sweep over every plugin's MESSAGE_TYPES:
     summary placeholders resolve to declared params, enum defaults are valid
     members, and each message type is dispatchable with its own declared
     defaults without raising.

The plugins take a `midi` object as a parameter and import nothing
CircuitPython-side at module level, so they import cleanly under CPython. We
add firmware/lib to sys.path the same way bilateral_test.py does.

No hardware, no CircuitPython runtime.

Usage
-----
    python tools/plugins_test.py
"""

import re
import sys
from pathlib import Path


# ---------------- path setup so we can import the firmware plugins ----------------

FIRMWARE_LIB = Path(__file__).resolve().parent.parent / "firmware" / "lib"
sys.path.insert(0, str(FIRMWARE_LIB))

import plugins.generic_midi as generic_midi   # noqa: E402
import plugins.line6_helix as line6_helix     # noqa: E402
import plugins.ampero as ampero               # noqa: E402
import plugins.kemper as kemper               # noqa: E402


# ---------------- test harness (same reporting style as bilateral_test) ----------------

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


# ---------------- fake MIDI engine that records every outbound call ----------------

class FakeMidi:
    def __init__(self):
        self.sent = []

    def send_cc(self, ch, cc, v):
        self.sent.append(("cc", ch, cc, v))

    def send_pc(self, ch, p):
        self.sent.append(("pc", ch, p))

    def send_note_on(self, ch, n, v):
        self.sent.append(("on", ch, n, v))

    def send_note_off(self, ch, n, v):
        self.sent.append(("off", ch, n, v))

    def send_sysex(self, data):
        self.sent.append(("sysex", tuple(data)))


# =================== 1) generic_midi.py dispatch ===================

@test("generic: program_change_bank -> CC0(msb), CC32(lsb), PC(program) in order")
def _():
    m = FakeMidi()
    generic_midi.dispatch(
        {"type": "program_change_bank", "channel": 3,
         "msb": 5, "lsb": 9, "program": 42}, m)
    assert m.sent == [
        ("cc", 3, 0, 5),      # Bank MSB (CC0) first
        ("cc", 3, 32, 9),     # Bank LSB (CC32) second
        ("pc", 3, 42),        # Program Change latches it
    ], m.sent


@test("generic: cc_toggle state on -> CC = on_value")
def _():
    m = FakeMidi()
    generic_midi.dispatch(
        {"type": "cc_toggle", "channel": 1, "cc": 34,
         "on_value": 100, "off_value": 7, "state": "on"}, m)
    assert m.sent == [("cc", 1, 34, 100)], m.sent


@test("generic: cc_toggle state off -> CC = off_value")
def _():
    m = FakeMidi()
    generic_midi.dispatch(
        {"type": "cc_toggle", "channel": 1, "cc": 34,
         "on_value": 100, "off_value": 7, "state": "off"}, m)
    assert m.sent == [("cc", 1, 34, 7)], m.sent


# =================== 2) line6_helix.py dispatch ===================

@test("helix: helix_preset -> CC0=0, CC32=setlist, PC=preset")
def _():
    m = FakeMidi()
    line6_helix.dispatch(
        {"type": "helix_preset", "channel": 1, "setlist": 4, "preset": 17}, m)
    assert m.sent == [
        ("cc", 1, 0, 0),      # Bank MSB always 0 on Helix
        ("cc", 1, 32, 4),     # Bank LSB = setlist
        ("pc", 1, 17),        # PC = preset
    ], m.sent


@test("helix: helix_snapshot 1 -> CC69=0, snapshot 8 -> CC69=7")
def _():
    m = FakeMidi()
    line6_helix.dispatch({"type": "helix_snapshot", "channel": 1, "snapshot": 1}, m)
    assert m.sent == [("cc", 1, 69, 0)], m.sent
    m = FakeMidi()
    line6_helix.dispatch({"type": "helix_snapshot", "channel": 1, "snapshot": 8}, m)
    assert m.sent == [("cc", 1, 69, 7)], m.sent


@test("helix: helix_fs uses the explicit CC table with the FS6 gap")
def _():
    # FS1..FS5 -> CC49..CC53, FS7..FS11 -> CC54..CC58. The switch param is an
    # enum of strings ("1".."11" excluding "6").
    cases = [
        ("1", 49), ("5", 53),     # FS1 boundary, FS5 top of the first run
        ("7", 54), ("11", 58),    # FS7 first after the gap, FS11 top of table
    ]
    for switch, cc in cases:
        m = FakeMidi()
        line6_helix.dispatch(
            {"type": "helix_fs", "channel": 1, "switch": switch, "state": "on"}, m)
        assert m.sent == [("cc", 1, cc, 127)], f"FS{switch} on -> {m.sent}"
        m = FakeMidi()
        line6_helix.dispatch(
            {"type": "helix_fs", "channel": 1, "switch": switch, "state": "off"}, m)
        assert m.sent == [("cc", 1, cc, 0)], f"FS{switch} off -> {m.sent}"


@test("helix: helix_fs for FS6 (not in the table) dispatches NOTHING")
def _():
    # FS6 has no MIDI CC on the Helix, so it must never emit a CC. "6" is not a
    # valid enum member either; the dispatch guards on the CC lookup returning
    # None. Pass the raw int the dispatch keys on to prove the gap holds.
    m = FakeMidi()
    line6_helix.dispatch({"type": "helix_fs", "channel": 1, "switch": 6, "state": "on"}, m)
    assert m.sent == [], m.sent
    m = FakeMidi()
    line6_helix.dispatch({"type": "helix_fs", "channel": 1, "switch": "6", "state": "on"}, m)
    assert m.sent == [], m.sent


@test("helix: helix_tap_tempo -> CC64=64")
def _():
    m = FakeMidi()
    line6_helix.dispatch({"type": "helix_tap_tempo", "channel": 1}, m)
    assert m.sent == [("cc", 1, 64, 64)], m.sent


@test("helix: helix_tuner on -> CC68=64, off -> CC68=0")
def _():
    m = FakeMidi()
    line6_helix.dispatch({"type": "helix_tuner", "channel": 1, "state": "on"}, m)
    assert m.sent == [("cc", 1, 68, 64)], m.sent
    m = FakeMidi()
    line6_helix.dispatch({"type": "helix_tuner", "channel": 1, "state": "off"}, m)
    assert m.sent == [("cc", 1, 68, 0)], m.sent


@test("helix: helix_looper record/overdub -> CC60=64/0, play/stop -> CC61=64/0")
def _():
    cases = [
        ("record", 60, 64), ("overdub", 60, 0),
        ("play", 61, 64), ("stop", 61, 0),
    ]
    for action, cc, value in cases:
        m = FakeMidi()
        line6_helix.dispatch({"type": "helix_looper", "channel": 1, "action": action}, m)
        assert m.sent == [("cc", 1, cc, value)], f"looper {action} -> {m.sent}"


@test("helix: update_context for helix_tuner sets ctx['tuner'] on/off")
def _():
    ctx = {}
    line6_helix.update_context({"type": "helix_tuner", "state": "on"}, ctx)
    assert ctx["tuner"] == "on", ctx
    line6_helix.update_context({"type": "helix_tuner", "state": "off"}, ctx)
    assert ctx["tuner"] == "off", ctx


# =================== 3) CROSS-PLUGIN self-consistency ===================

ALL_PLUGINS = [generic_midi, line6_helix, ampero, kemper]

_PLACEHOLDER_RE = re.compile(r"\{([a-zA-Z_][a-zA-Z0-9_]*)\}")


def _placeholders(summary):
    return set(_PLACEHOLDER_RE.findall(summary or ""))


@test("cross: every summary {placeholder} is a declared param of its message type")
def _():
    bad = []
    for mod in ALL_PLUGINS:
        for mtype, spec in mod.MESSAGE_TYPES.items():
            params = spec.get("params", {})
            for ph in _placeholders(spec.get("summary", "")):
                if ph not in params:
                    bad.append(f"{mod.NAME}.{mtype}: summary uses {{{ph}}} "
                               f"but params has {sorted(params)}")
    assert not bad, "summary/param drift:\n    " + "\n    ".join(bad)


@test("cross: every enum param's default is a member of its values list")
def _():
    bad = []
    for mod in ALL_PLUGINS:
        for mtype, spec in mod.MESSAGE_TYPES.items():
            for pname, pspec in spec.get("params", {}).items():
                if pspec.get("type") != "enum":
                    continue
                values = pspec.get("values", [])
                default = pspec.get("default")
                if default not in values:
                    bad.append(f"{mod.NAME}.{mtype}.{pname}: default {default!r} "
                               f"not in values {values}")
    assert not bad, "enum default not in values:\n    " + "\n    ".join(bad)


@test("cross: every message type dispatches with its own declared defaults (no raise)")
def _():
    # Build a payload from each param's declared "default", plus the type name
    # and channel=1, and dispatch it against a recording FakeMidi. This proves
    # every declared message type is actually reachable with its own defaults.
    #
    # No message type is skipped: all four plugins' dispatch() paths accept the
    # values their own defaults declare. (kemper_rig's dispatch does a real
    # time.sleep(0.005) - harmless under CPython, just a 5 ms pause.)
    errors = []
    for mod in ALL_PLUGINS:
        for mtype, spec in mod.MESSAGE_TYPES.items():
            payload = {"type": mtype, "channel": 1}
            for pname, pspec in spec.get("params", {}).items():
                if "default" in pspec:
                    payload[pname] = pspec["default"]
            m = FakeMidi()
            try:
                mod.dispatch(payload, m)
            except Exception as e:
                errors.append(f"{mod.NAME}.{mtype}: {type(e).__name__}: {e} "
                              f"(payload={payload})")
    assert not errors, "dispatch raised on declared defaults:\n    " + "\n    ".join(errors)


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
