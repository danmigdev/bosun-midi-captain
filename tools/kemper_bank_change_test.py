#!/usr/bin/env python3
"""Regression test for Kemper rig/bank addressing (no hardware, no CircuitPython).

Guards the fix for: "bank up" changed the pedal display but the Kemper either
stayed put or jumped to the wrong rig, and bank-2 effects didn't work.

Root cause: the Player addresses its 125 rigs as a FLAT list - Bank Select
stays 0 and the Program Change IS the rig index (0..124). bosun was sending
Bank LSB = bank-1, which is fine for bank 1 (LSB 0 either way) but for bank 2+
lands on the wrong rig (verified on hardware: bank 2 rig 4 -> "Crunch" instead
of the real rig at PC 8).

Fix: map (bank, rig) -> PC = (bank-1)*5 + (rig-1) with Bank LSB pinned to 0,
both outbound (dispatch) and inbound (on_midi_in). These tests pin that down.

Usage
-----
    python tools/kemper_bank_change_test.py
"""

import sys
import types
from pathlib import Path


# ---------------- import the firmware kemper plugin ----------------

for _m in ("busio", "usb_midi", "digitalio", "board", "neopixel", "displayio",
           "fourwire", "pwmio", "terminalio", "adafruit_display_text",
           "adafruit_st7789", "adafruit_bitmap_font"):
    sys.modules.setdefault(_m, types.ModuleType(_m))
import board  # noqa: E402
for _n in [f"GP{i}" for i in range(30)]:
    setattr(board, _n, _n)

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "firmware" / "lib"))
from plugins import kemper  # noqa: E402


# ---------------- harness ----------------

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


class RecordMidi:
    """Records the channel-voice messages dispatch() emits, in order."""

    def __init__(self):
        self.sent = []

    def send_cc(self, ch, cc, v):
        self.sent.append(("cc", cc, v))

    def send_pc(self, ch, p):
        self.sent.append(("pc", p))

    def send_sysex(self, d):
        pass


class FakePlayer:
    """Models the Kemper Player: rigs are a FLAT list, so the Program Change it
    receives is the rig index. A real Bank Select would offset by 128, so a
    non-zero Bank LSB lands on a wrong/out-of-range rig - exactly the original
    bug. With the correct addressing (LSB 0) the landed rig is just the PC."""

    def __init__(self):
        self.flat = 0           # current rig index 0..124
        self._lsb = 0

    def send_cc(self, ch, cc, value):
        if cc == 32:
            self._lsb = value

    def send_pc(self, ch, program):
        self.flat = self._lsb * 128 + program

    def send_sysex(self, d):
        pass

    @property
    def bank(self):
        return self.flat // 5 + 1

    @property
    def rig(self):
        return self.flat % 5 + 1


def rig(bank, r, channel=1):
    return {"type": "kemper_rig", "bank": bank, "rig": r, "channel": channel}


# ---------------- tests ----------------

@test("dispatch uses flat PC = (bank-1)*5+(rig-1) with Bank LSB pinned to 0")
def _():
    m = RecordMidi()
    kemper.dispatch(rig(1, 1), m)
    assert m.sent == [("cc", 0, 0), ("cc", 32, 0), ("pc", 0)], m.sent    # rig 1
    m.sent.clear()
    kemper.dispatch(rig(2, 4), m)
    assert m.sent == [("cc", 0, 0), ("cc", 32, 0), ("pc", 8)], m.sent    # bank 2 rig 4 -> PC 8
    m.sent.clear()
    kemper.dispatch(rig(25, 5), m)
    assert m.sent == [("cc", 0, 0), ("cc", 32, 0), ("pc", 124)], m.sent  # last rig -> PC 124


@test("Bank LSB is always 0, never bank-1 (the wrong-rig bug)")
def _():
    m = RecordMidi()
    for b in range(1, 26):
        m.sent.clear()
        kemper.dispatch(rig(b, 1), m)
        lsb = [t[2] for t in m.sent if t[0] == "cc" and t[1] == 32]
        assert lsb == [0], "bank %d sent Bank LSB %r, must be 0" % (b, lsb)


@test("every (bank, rig) lands on the matching rig on a flat-list Player")
def _():
    for (bank, r, expect_flat) in [(1, 1, 0), (1, 4, 3), (2, 1, 5), (2, 4, 8),
                                   (3, 4, 13), (25, 5, 124)]:
        p = FakePlayer()
        kemper.dispatch(rig(bank, r), p)
        assert p.flat == expect_flat, \
            "bank %d rig %d -> flat %d, expected %d" % (bank, r, p.flat, expect_flat)
        assert (p.bank, p.rig) == (bank, r), (bank, r, p.bank, p.rig)


@test("REPRO: bank 2 rig 4 reaches the right rig (was landing on the wrong one)")
def _():
    p = FakePlayer()
    kemper.dispatch(rig(1, 4), p)            # on bank 1 rig 4
    kemper.dispatch(rig(2, 4), p)            # long-press bank up
    # Before the fix dispatch sent Bank LSB=1 -> flat 128+3 = wrong rig. Now it
    # sends LSB 0 + PC 8 -> the real bank-2 rig-4.
    assert p.flat == 8 and (p.bank, p.rig) == (2, 4), (p.flat, p.bank, p.rig)


@test("out-of-range bank/rig is dropped")
def _():
    m = RecordMidi()
    kemper.dispatch(rig(0, 1), m)
    kemper.dispatch(rig(26, 1), m)
    kemper.dispatch(rig(1, 0), m)
    kemper.dispatch(rig(1, 6), m)
    assert m.sent == [], m.sent


@test("no _KEMPER_BANK tracker state survives on the module")
def _():
    assert not hasattr(kemper, "_KEMPER_BANK"), \
        "the optimistic bank tracker should be gone"


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
