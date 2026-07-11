#!/usr/bin/env python3
"""Offline tests for the app-level setlist navigation (device-aware setlist).

Exercises the REAL Captain methods setlist_step / _setlist_items /
_update_setlist_pos against a lightweight fake instance (called unbound), no
CircuitPython, no hardware.

Usage
-----
    python tools/setlist_test.py
"""
import sys
import types
from pathlib import Path

FIRMWARE_LIB = Path(__file__).resolve().parent.parent / "firmware" / "lib"
sys.path.insert(0, str(FIRMWARE_LIB))

for mod_name in ("busio", "usb_midi", "usb_cdc", "digitalio", "board", "neopixel",
                 "displayio", "fourwire", "pwmio", "terminalio", "analogio",
                 "adafruit_display_text", "adafruit_st7789",
                 "adafruit_bitmap_font", "microcontroller"):
    sys.modules.setdefault(mod_name, types.ModuleType(mod_name))
import board  # noqa: E402
for _n in [f"GP{i}" for i in range(30)]:
    setattr(board, _n, _n)
import adafruit_display_text  # noqa: E402
adafruit_display_text.label = types.ModuleType("adafruit_display_text.label")
adafruit_display_text.label.Label = lambda *a, **kw: None
sys.modules["adafruit_display_text.label"] = adafruit_display_text.label
sys.modules["adafruit_st7789"].ST7789 = object
sys.modules["neopixel"].NeoPixel = object
sys.modules["neopixel"].GRB = "GRB"
sys.modules["analogio"].AnalogIn = object
sys.modules["digitalio"].DigitalInOut = object
sys.modules["digitalio"].Direction = types.SimpleNamespace(INPUT="in", OUTPUT="out")
sys.modules["digitalio"].Pull = types.SimpleNamespace(UP="up", DOWN="down")
sys.modules["terminalio"].FONT = object()

from captain.app import Captain  # noqa: E402


PASS = 0
FAIL = 0
FAILS = []


def test(name):
    def wrap(fn):
        global PASS, FAIL
        try:
            fn()
            PASS += 1
            print("  PASS ", name)
        except AssertionError as e:
            FAIL += 1
            FAILS.append("%s: %s" % (name, e))
            print("  FAIL ", name, "-", e)
        except Exception as e:  # noqa: BLE001
            FAIL += 1
            FAILS.append("%s: %s: %s" % (name, type(e).__name__, e))
            print("  ERROR", name, "-", type(e).__name__, e)
        return fn
    return wrap


class FakePatches:
    def __init__(self, present):
        self._present = set(present)   # set of (bank, slot)

    def has(self, bank, slot):
        return (bank, slot) in self._present


def make_fake(items, present=None, cur=(1, 1)):
    if present is None:
        # default: every referenced item exists
        present = [(int(i["bank"]), int(i["slot"])) for i in items]
    fake = types.SimpleNamespace()
    fake.device = {"setlist": {"name": "gig", "items": items}}
    fake.patches = FakePatches(present)
    fake.current_bank, fake.current_slot = cur
    fake.display_context = {"setlist_pos": ""}
    fake.switch_calls = []

    def switch_patch(bank, slot, source="editor", fire_on_enter=True):
        fake.switch_calls.append((bank, slot, source))
        fake.current_bank, fake.current_slot = bank, slot
        return True
    fake.switch_patch = switch_patch
    # setlist_step / _update_setlist_pos call self._setlist_items() as a bound
    # method - route it to the real Captain method on this fake.
    fake._setlist_items = lambda: Captain._setlist_items(fake)
    return fake


def items(*pairs):
    return [{"bank": b, "slot": s} for (b, s) in pairs]


# ---- _setlist_items ----

@test("_setlist_items parses dicts and filters to existing patches")
def _():
    f = make_fake(items((1, 1), (3, 2), (5, 5)), present=[(1, 1), (5, 5)])
    assert Captain._setlist_items(f) == [(1, 1), (5, 5)]


@test("_setlist_items accepts [bank,slot] pairs and skips malformed")
def _():
    f = make_fake([[2, 1], {"bank": 2, "slot": 3}, {"oops": 1}, "junk"],
                  present=[(2, 1), (2, 3)])
    assert Captain._setlist_items(f) == [(2, 1), (2, 3)]


@test("_setlist_items empty when no setlist")
def _():
    f = make_fake([])
    f.device = {}
    assert Captain._setlist_items(f) == []


# ---- setlist_step ----

@test("step +1 from a set member loads the next")
def _():
    f = make_fake(items((1, 1), (3, 2), (5, 5)), cur=(1, 1))
    assert Captain.setlist_step(f, 1) is True
    assert f.switch_calls == [(3, 2, "binding")]


@test("step -1 wraps to the last")
def _():
    f = make_fake(items((1, 1), (3, 2), (5, 5)), cur=(1, 1))
    Captain.setlist_step(f, -1)
    assert f.switch_calls == [(5, 5, "binding")]


@test("step +1 wraps from last to first")
def _():
    f = make_fake(items((1, 1), (3, 2), (5, 5)), cur=(5, 5))
    Captain.setlist_step(f, 1)
    assert f.switch_calls == [(1, 1, "binding")]


@test("current patch not in set: +1 enters at start, -1 at end")
def _():
    f = make_fake(items((1, 1), (3, 2), (5, 5)), cur=(9, 9))
    Captain.setlist_step(f, 1)
    assert f.switch_calls[-1] == (1, 1, "binding")
    f.current_bank, f.current_slot = 9, 9
    Captain.setlist_step(f, -1)
    assert f.switch_calls[-1] == (5, 5, "binding")


@test("step skips setlist entries that have no patch")
def _():
    # (3,2) is in the setlist but has no patch on disk -> filtered out, so
    # +1 from (1,1) lands on (5,5).
    f = make_fake(items((1, 1), (3, 2), (5, 5)), present=[(1, 1), (5, 5)], cur=(1, 1))
    Captain.setlist_step(f, 1)
    assert f.switch_calls == [(5, 5, "binding")]


@test("step is a no-op with an empty setlist or delta 0")
def _():
    f = make_fake([])
    assert Captain.setlist_step(f, 1) is False
    f2 = make_fake(items((1, 1), (2, 1)))
    assert Captain.setlist_step(f2, 0) is False
    assert f2.switch_calls == []


# ---- _update_setlist_pos ----

@test("_update_setlist_pos reports position within the setlist")
def _():
    f = make_fake(items((1, 1), (3, 2), (5, 5)))
    Captain._update_setlist_pos(f, 3, 2)
    assert f.display_context["setlist_pos"] == "2/3"
    Captain._update_setlist_pos(f, 5, 5)
    assert f.display_context["setlist_pos"] == "3/3"


@test("_update_setlist_pos is empty when the patch isn't in the setlist")
def _():
    f = make_fake(items((1, 1), (3, 2)))
    Captain._update_setlist_pos(f, 9, 9)
    assert f.display_context["setlist_pos"] == ""


def main():
    print()
    if FAILS:
        print("%d FAILURE(S):" % len(FAILS))
        for m in FAILS:
            print("  -", m)
        return 1
    print("%d passed, 0 failed" % PASS)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
