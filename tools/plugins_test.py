#!/usr/bin/env python3
"""Offline tests for the Bosun firmware plugins.

Covers the five device profiles under firmware/lib/plugins/:
  - generic_midi.py   (program_change_bank, cc_toggle)
  - line6_helix.py    (preset, snapshot, footswitch table with the FS6 gap,
                       tap tempo, tuner, looper, update_context)
  - ampero.py         (cross-plugin self-consistency)
  - kemper.py         (cross-plugin self-consistency)
  - headrush_core.py  (rig load/step, bank step, scene, block, footswitch,
                       expression, looper, drums, tempo, tuner, FS mode,
                       practice tool, misc toggles, update_context)

Two flavours of test:
  1) Targeted dispatch checks that pin down exact CC numbers / ordering for
     generic_midi, line6_helix, and headrush_core.
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

import plugins.generic_midi as generic_midi       # noqa: E402
import plugins.line6_helix as line6_helix         # noqa: E402
import plugins.ampero as ampero                   # noqa: E402
import plugins.kemper as kemper                   # noqa: E402
import plugins.headrush_core as headrush_core     # noqa: E402


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


# =================== 3) headrush_core.py dispatch ===================

@test("headrush: headrush_rig -> PC = rig-1 (1-based -> 0-based)")
def _():
    m = FakeMidi()
    headrush_core.dispatch({"type": "headrush_rig", "channel": 1, "rig": 42}, m)
    assert m.sent == [("pc", 1, 41)], m.sent


@test("headrush: headrush_rig_step up -> CC17=127, down -> CC16=127")
def _():
    m = FakeMidi()
    headrush_core.dispatch({"type": "headrush_rig_step", "channel": 1, "direction": "up"}, m)
    assert m.sent == [("cc", 1, 17, 127)], m.sent
    m = FakeMidi()
    headrush_core.dispatch({"type": "headrush_rig_step", "channel": 1, "direction": "down"}, m)
    assert m.sent == [("cc", 1, 16, 127)], m.sent


@test("headrush: headrush_bank_step up -> CC19=127, down -> CC18=127")
def _():
    m = FakeMidi()
    headrush_core.dispatch({"type": "headrush_bank_step", "channel": 1, "direction": "up"}, m)
    assert m.sent == [("cc", 1, 19, 127)], m.sent
    m = FakeMidi()
    headrush_core.dispatch({"type": "headrush_bank_step", "channel": 1, "direction": "down"}, m)
    assert m.sent == [("cc", 1, 18, 127)], m.sent


@test("headrush: headrush_scene 1 -> CC21, scene 10 -> CC30")
def _():
    cases = [(1, 21), (5, 25), (10, 30)]
    for scene, cc in cases:
        m = FakeMidi()
        headrush_core.dispatch({"type": "headrush_scene", "channel": 1, "scene": scene}, m)
        assert m.sent == [("cc", 1, cc, 127)], f"scene {scene} -> {m.sent}"


@test("headrush: headrush_block on/off uses CC75-88 for blocks 1-14")
def _():
    # Block 1 -> CC75, Block 14 -> CC88
    m = FakeMidi()
    headrush_core.dispatch({"type": "headrush_block", "channel": 1, "block": 1, "state": "on"}, m)
    assert m.sent == [("cc", 1, 75, 127)], m.sent
    m = FakeMidi()
    headrush_core.dispatch({"type": "headrush_block", "channel": 1, "block": 14, "state": "off"}, m)
    assert m.sent == [("cc", 1, 88, 0)], m.sent


@test("headrush: headrush_footswitch 1 press -> CC49=127, FS5 release -> CC53=0")
def _():
    m = FakeMidi()
    headrush_core.dispatch({"type": "headrush_footswitch", "channel": 1, "fs": 1, "action": "press"}, m)
    assert m.sent == [("cc", 1, 49, 127)], m.sent
    m = FakeMidi()
    headrush_core.dispatch({"type": "headrush_footswitch", "channel": 1, "fs": 5, "action": "release"}, m)
    assert m.sent == [("cc", 1, 53, 0)], m.sent


@test("headrush: headrush_expression -> CC1 with the given value")
def _():
    m = FakeMidi()
    headrush_core.dispatch({"type": "headrush_expression", "channel": 1, "value": 100}, m)
    assert m.sent == [("cc", 1, 1, 100)], m.sent


@test("headrush: headrush_looper actions map to correct CCs")
def _():
    cases = [
        ("half_speed",   65), ("double_speed", 66),
        ("half_loop",    67), ("double_loop",  68),
        ("start_stop",   69), ("record",       70),
        ("insert",       71), ("peel",         72),
        ("mute",         73), ("reverse",      74),
    ]
    for action, cc in cases:
        m = FakeMidi()
        headrush_core.dispatch({"type": "headrush_looper", "channel": 1, "action": action}, m)
        assert m.sent == [("cc", 1, cc, 127)], f"looper {action} -> {m.sent}"


@test("headrush: headrush_drums actions map to correct CCs")
def _():
    cases = [
        ("open_close", 31), ("play_stop", 42), ("fill", 43),
        ("kit_next", 33), ("volume_up", 39), ("accent", 47),
    ]
    for action, cc in cases:
        m = FakeMidi()
        headrush_core.dispatch({"type": "headrush_drums", "channel": 1, "action": action}, m)
        assert m.sent == [("cc", 1, cc, 127)], f"drums {action} -> {m.sent}"


@test("headrush: headrush_tempo tap -> CC64, increase -> CC13, decrease -> CC12")
def _():
    m = FakeMidi()
    headrush_core.dispatch({"type": "headrush_tempo", "channel": 1, "action": "tap"}, m)
    assert m.sent == [("cc", 1, 64, 127)], m.sent
    m = FakeMidi()
    headrush_core.dispatch({"type": "headrush_tempo", "channel": 1, "action": "increase"}, m)
    assert m.sent == [("cc", 1, 13, 127)], m.sent
    m = FakeMidi()
    headrush_core.dispatch({"type": "headrush_tempo", "channel": 1, "action": "decrease"}, m)
    assert m.sent == [("cc", 1, 12, 127)], m.sent


@test("headrush: headrush_tuner sends CC92 (toggle)")
def _():
    m = FakeMidi()
    headrush_core.dispatch({"type": "headrush_tuner", "channel": 1, "state": "on"}, m)
    assert m.sent == [("cc", 1, 92, 127)], m.sent
    m = FakeMidi()
    headrush_core.dispatch({"type": "headrush_tuner", "channel": 1, "state": "off"}, m)
    assert m.sent == [("cc", 1, 92, 127)], m.sent   # toggle CC, same value either way


@test("headrush: headrush_fs_mode maps stomp/hybrid/setlist/rig/5rig to CC94-98")
def _():
    cases = [("stomp", 94), ("hybrid", 95), ("setlist", 96), ("rig", 97), ("5rig", 98)]
    for mode, cc in cases:
        m = FakeMidi()
        headrush_core.dispatch({"type": "headrush_fs_mode", "channel": 1, "mode": mode}, m)
        assert m.sent == [("cc", 1, cc, 127)], f"mode {mode} -> {m.sent}"


@test("headrush: headrush_practice actions map to correct CCs")
def _():
    cases = [
        ("open_close", 102), ("play_pause", 103), ("stop", 104),
        ("pitch_down", 111), ("pitch_up", 112),
    ]
    for action, cc in cases:
        m = FakeMidi()
        headrush_core.dispatch({"type": "headrush_practice", "channel": 1, "action": action}, m)
        assert m.sent == [("cc", 1, cc, 127)], f"practice {action} -> {m.sent}"


@test("headrush: headrush_misc actions map to correct CCs")
def _():
    cases = [
        ("hands_free", 90), ("looper_page", 91), ("lock_screen", 93), ("mic_dry", 89),
    ]
    for action, cc in cases:
        m = FakeMidi()
        headrush_core.dispatch({"type": "headrush_misc", "channel": 1, "action": action}, m)
        assert m.sent == [("cc", 1, cc, 127)], f"misc {action} -> {m.sent}"


@test("headrush: headrush_pedal_switch sends CC14 (A/B toggle)")
def _():
    m = FakeMidi()
    headrush_core.dispatch({"type": "headrush_pedal_switch", "channel": 1}, m)
    assert m.sent == [("cc", 1, 14, 127)], m.sent


@test("headrush: update_context sets headrush_rig and headrush_scene")
def _():
    ctx = {}
    headrush_core.update_context({"type": "headrush_rig", "rig": 42}, ctx)
    assert ctx["headrush_rig"] == 42, ctx
    headrush_core.update_context({"type": "headrush_scene", "scene": 5}, ctx)
    assert ctx["headrush_scene"] == 5, ctx


@test("headrush: on_midi_in ignores non-PC status")
def _():
    called = []

    class MockApp:
        device = {"headrush_core": {"auto_follow_pc": True}}

    headrush_core.on_midi_in("din", 1, 0xB0, [20, 127], MockApp())
    # CC message should not trigger switch_patch
    # (can't assert on a method that wasn't called, but we verify no crash)


@test("headrush: on_midi_in skips when auto_follow_pc is disabled")
def _():
    class MockApp:
        device = {"headrush_core": {"auto_follow_pc": False}}
        switch_patch = None  # not callable -- would crash if reached

    # Should return early without trying to switch
    headrush_core.on_midi_in("din", 1, 0xC0, [5], MockApp())


@test("headrush: on_midi_in PC lookup matches channel+pc and switches patch")
def _():
    switched = []

    class MockApp:
        device = {"headrush_core": {"enabled": True, "auto_follow_pc": True}}
        midi_learn_table = {
            "pc_to_patch": [
                {"channel": 1, "bank_msb": 0, "pc": 5, "captain_patch": "3/2"},
                {"channel": 2, "bank_msb": 0, "pc": 5, "captain_patch": "4/1"},
            ],
        }

        def switch_patch(self, bank, slot, source=None):
            switched.append((bank, slot, source))

    headrush_core.on_midi_in("din", 1, 0xC0, [5], MockApp())
    assert switched == [(3, 2, "midi_in")], switched


@test("headrush: on_midi_in PC lookup with non-zero bank_msb is skipped")
def _():
    switched = []

    class MockApp:
        device = {"headrush_core": {"enabled": True, "auto_follow_pc": True}}
        midi_learn_table = {
            "pc_to_patch": [
                # This entry has bank_msb=1 -- HeadRush Core never sends
                # bank MSB, so this should NOT match.
                {"channel": 1, "bank_msb": 1, "pc": 5, "captain_patch": "5/1"},
                {"channel": 1, "bank_msb": 0, "pc": 7, "captain_patch": "2/3"},
            ],
        }

        def switch_patch(self, bank, slot, source=None):
            switched.append((bank, slot, source))

    headrush_core.on_midi_in("din", 1, 0xC0, [5], MockApp())
    assert switched == [], f"bank_msb=1 should not match; got {switched}"
    # PC 7 should still match (bank_msb=0)
    headrush_core.on_midi_in("din", 1, 0xC0, [7], MockApp())
    assert switched == [(2, 3, "midi_in")], switched


# =================== 4) CROSS-PLUGIN self-consistency ===================

ALL_PLUGINS = [generic_midi, line6_helix, ampero, kemper, headrush_core]

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
