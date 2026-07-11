#!/usr/bin/env python3
"""Offline tests for firmware/lib/captain/midi.py MidiParser.

Exercises the streaming MIDI parser against the byte-level edge cases that
real hardware throws at it: running status, real-time interleave, SYSEX
framing, aborted SYSEX, and feeds split across arbitrary boundaries.

MidiParser lives in captain/midi.py, which imports busio + usb_midi and pulls
in captain/board.py (which imports `board`). We stub those CircuitPython
modules the same way the other tools/ tests do so the parser imports and runs
under plain CPython, with no hardware.

Usage
-----
    python tools/midi_parser_test.py
"""

import sys
import types
from pathlib import Path


# ---------------- mock CircuitPython surface ----------------

FIRMWARE_LIB = Path(__file__).resolve().parent.parent / "firmware" / "lib"
sys.path.insert(0, str(FIRMWARE_LIB))

# captain.midi imports busio + usb_midi directly and transitively pulls in
# captain.board (which imports `board`). Stub them so the import succeeds.
for mod_name in ("busio", "usb_midi"):
    sys.modules.setdefault(mod_name, types.ModuleType(mod_name))

_board = types.ModuleType("board")
for _name in [f"GP{i}" for i in range(30)]:
    setattr(_board, _name, _name)
sys.modules.setdefault("board", _board)

from captain.midi import MidiParser   # noqa: E402


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


# =================== channel-voice parsing ===================

@test("note on: (0x90) -> (channel, 0x90, [note, vel])")
def _():
    p = MidiParser()
    out = p.feed(bytes([0x90, 0x3C, 0x64]))
    assert out == [(1, 0x90, [0x3C, 0x64])], out


@test("note off: (0x80) -> (channel, 0x80, [note, vel])")
def _():
    p = MidiParser()
    out = p.feed(bytes([0x80, 0x3C, 0x00]))
    assert out == [(1, 0x80, [0x3C, 0x00])], out


@test("control change: (0xB0) two data bytes")
def _():
    p = MidiParser()
    out = p.feed(bytes([0xB0, 0x07, 0x7F]))
    assert out == [(1, 0xB0, [0x07, 0x7F])], out


@test("program change: (0xC0) single data byte")
def _():
    p = MidiParser()
    out = p.feed(bytes([0xC0, 0x2A]))
    assert out == [(1, 0xC0, [0x2A])], out


@test("channel pressure: (0xD0) single data byte")
def _():
    p = MidiParser()
    out = p.feed(bytes([0xD0, 0x40]))
    assert out == [(1, 0xD0, [0x40])], out


@test("channel decoded from low nibble (0xB7 -> channel 8)")
def _():
    p = MidiParser()
    out = p.feed(bytes([0xB7, 0x10, 0x40]))
    assert out == [(8, 0xB0, [0x10, 0x40])], out


@test("channel 16 (status 0x9F -> channel 16)")
def _():
    p = MidiParser()
    out = p.feed(bytes([0x9F, 0x24, 0x7F]))
    assert out == [(16, 0x90, [0x24, 0x7F])], out


@test("two back-to-back messages with explicit status each")
def _():
    p = MidiParser()
    out = p.feed(bytes([0x90, 0x3C, 0x64, 0x80, 0x3C, 0x00]))
    assert out == [
        (1, 0x90, [0x3C, 0x64]),
        (1, 0x80, [0x3C, 0x00]),
    ], out


# =================== running status ===================

@test("running status: repeated CC payloads reuse the last status")
def _():
    p = MidiParser()
    out = p.feed(bytes([0xB0, 0x10, 0x7F, 0x11, 0x40, 0x12, 0x01]))
    assert out == [
        (1, 0xB0, [0x10, 0x7F]),
        (1, 0xB0, [0x11, 0x40]),
        (1, 0xB0, [0x12, 0x01]),
    ], out


@test("running status: repeated note-on payloads (two-byte)")
def _():
    p = MidiParser()
    out = p.feed(bytes([0x90, 0x3C, 0x64, 0x3E, 0x64, 0x40, 0x64]))
    assert out == [
        (1, 0x90, [0x3C, 0x64]),
        (1, 0x90, [0x3E, 0x64]),
        (1, 0x90, [0x40, 0x64]),
    ], out


@test("running status: repeated PC payloads (one-byte)")
def _():
    p = MidiParser()
    out = p.feed(bytes([0xC0, 0x01, 0x02, 0x03]))
    assert out == [
        (1, 0xC0, [0x01]),
        (1, 0xC0, [0x02]),
        (1, 0xC0, [0x03]),
    ], out


@test("running status broken by a new status byte, then resumes")
def _():
    p = MidiParser()
    # CC running, then a PC status interrupts. The half-formed CC (only 0x10
    # buffered) must be discarded, not merged into the PC.
    out = p.feed(bytes([0xB0, 0x10, 0xC0, 0x07]))
    assert out == [(1, 0xC0, [0x07])], out


@test("new status resets the data buffer between full messages")
def _():
    p = MidiParser()
    out = p.feed(bytes([0xB0, 0x10, 0x7F, 0x90, 0x3C, 0x64]))
    assert out == [
        (1, 0xB0, [0x10, 0x7F]),
        (1, 0x90, [0x3C, 0x64]),
    ], out


# =================== real-time interleave ===================

@test("real-time clock (0xF8) mid-message is skipped, message intact")
def _():
    p = MidiParser()
    out = p.feed(bytes([0xB0, 0x10, 0xF8, 0x7F]))
    assert out == [(1, 0xB0, [0x10, 0x7F])], out


@test("real-time before a status byte is skipped")
def _():
    p = MidiParser()
    out = p.feed(bytes([0xF8, 0xB0, 0x10, 0x7F]))
    assert out == [(1, 0xB0, [0x10, 0x7F])], out


@test("all real-time bytes 0xF8..0xFF are transparent to a message")
def _():
    for rt in range(0xF8, 0x100):
        p = MidiParser()
        out = p.feed(bytes([0xB0, 0x10, rt, 0x7F]))
        assert out == [(1, 0xB0, [0x10, 0x7F])], f"rt=0x{rt:02X} -> {out}"


@test("real-time between running-status payloads doesn't break running status")
def _():
    p = MidiParser()
    out = p.feed(bytes([0xB0, 0x10, 0x7F, 0xFE, 0x11, 0x40]))  # 0xFE = active sensing
    assert out == [
        (1, 0xB0, [0x10, 0x7F]),
        (1, 0xB0, [0x11, 0x40]),
    ], out


@test("multiple real-time bytes interleaved in one message")
def _():
    p = MidiParser()
    out = p.feed(bytes([0xF8, 0x90, 0xF8, 0x3C, 0xFA, 0x64, 0xFC]))
    assert out == [(1, 0x90, [0x3C, 0x64])], out


# =================== SYSEX collection ===================

@test("SYSEX: 0xF0..0xF7 returns payload without framing, as (0, 0xF0, [...])")
def _():
    p = MidiParser()
    out = p.feed(bytes([0xF0, 0x00, 0x20, 0x33, 0xF7]))
    assert out == [(0, 0xF0, [0x00, 0x20, 0x33])], out


@test("SYSEX: empty payload (0xF0 immediately followed by 0xF7)")
def _():
    p = MidiParser()
    out = p.feed(bytes([0xF0, 0xF7]))
    assert out == [(0, 0xF0, [])], out


@test("SYSEX: multi-byte Kemper-style payload preserved intact")
def _():
    p = MidiParser()
    body = [0x00, 0x20, 0x33, 0x02, 0x7F, 0x01, 0x00, 0x34, 0x03, 0x00, 0x01]
    out = p.feed(bytes([0xF0] + body + [0xF7]))
    assert out == [(0, 0xF0, body)], out


@test("SYSEX: split across two feeds reassembles into one payload")
def _():
    p = MidiParser()
    out1 = p.feed(bytes([0xF0, 0x00, 0x20, 0x33]))
    assert out1 == [], out1
    out2 = p.feed(bytes([0x02, 0x7F, 0x01, 0x00, 0x34, 0x03, 0x00, 0x01, 0xF7]))
    assert out2 == [(0, 0xF0, [0x00, 0x20, 0x33, 0x02, 0x7F,
                               0x01, 0x00, 0x34, 0x03, 0x00, 0x01])], out2


@test("SYSEX: split byte-by-byte across many feeds")
def _():
    p = MidiParser()
    frame = [0xF0, 0x00, 0x20, 0x33, 0x7F, 0xF7]
    out = []
    for b in frame:
        out += p.feed(bytes([b]))
    assert out == [(0, 0xF0, [0x00, 0x20, 0x33, 0x7F])], out


@test("SYSEX: real-time interleaved inside SYSEX is filtered out")
def _():
    p = MidiParser()
    out = p.feed(bytes([0xF0, 0x00, 0xF8, 0x20, 0xF8, 0x33, 0xF7]))
    assert out == [(0, 0xF0, [0x00, 0x20, 0x33])], out


@test("SYSEX: two SYSEX messages in a row are both returned")
def _():
    p = MidiParser()
    out = p.feed(bytes([0xF0, 0x01, 0x02, 0xF7, 0xF0, 0x03, 0x04, 0xF7]))
    assert out == [(0, 0xF0, [0x01, 0x02]), (0, 0xF0, [0x03, 0x04])], out


# =================== aborted SYSEX ===================

@test("SYSEX: stray channel status inside aborts SYSEX, byte re-processed as new message")
def _():
    p = MidiParser()
    # 0xF0 opens SYSEX, 0x00 0x20 collected, then 0xB0 (a channel status) arrives.
    # The current SYSEX must be aborted (no event), and 0xB0 starts a fresh CC
    # that completes with the following two data bytes.
    out = p.feed(bytes([0xF0, 0x00, 0x20, 0xB0, 0x10, 0x7F]))
    assert out == [(1, 0xB0, [0x10, 0x7F])], out


@test("SYSEX: stray one-byte status (0xC0) inside aborts SYSEX and starts a PC")
def _():
    p = MidiParser()
    out = p.feed(bytes([0xF0, 0x00, 0x20, 0xC0, 0x05]))
    assert out == [(1, 0xC0, [0x05])], out


@test("SYSEX: a fresh 0xF0 inside an open SYSEX restarts collection cleanly")
def _():
    p = MidiParser()
    # First SYSEX never terminates; a new 0xF0 restarts. Only the second
    # (properly terminated) SYSEX yields an event.
    out = p.feed(bytes([0xF0, 0xAA, 0xBB, 0xF0, 0x01, 0x02, 0xF7]))
    assert out == [(0, 0xF0, [0x01, 0x02])], out


@test("SYSEX: after an aborted SYSEX, trailing data (no status) is dropped")
def _():
    p = MidiParser()
    # 0xF7 with no open SYSEX just closes nothing; following bytes with no
    # status must not produce a phantom message.
    out = p.feed(bytes([0xF0, 0x00, 0xF0, 0x11, 0xF7, 0x40, 0x50]))
    # Inner 0xF0 restarts; [0x11] collected; 0xF7 closes -> one event.
    # 0x40 0x50 are orphan data bytes (running status dropped by SYSEX) -> ignored.
    assert out == [(0, 0xF0, [0x11])], out


# =================== stray data / status-common ===================

@test("stray data bytes with no status seen are dropped silently")
def _():
    p = MidiParser()
    out = p.feed(bytes([0x40, 0x50, 0x60]))
    assert out == [], out


@test("system common (0xF1..0xF6) drops running status")
def _():
    p = MidiParser()
    # CC running, then 0xF1 (MTC quarter frame, a system-common status) must
    # drop running status so the following data bytes are orphaned, not a CC.
    out = p.feed(bytes([0xB0, 0x10, 0x7F, 0xF1, 0x11, 0x40]))
    assert out == [(1, 0xB0, [0x10, 0x7F])], out


@test("empty feed returns empty list")
def _():
    p = MidiParser()
    assert p.feed(bytes([])) == []


# =================== split-across-feed channel voice ===================

@test("incomplete message buffered, completes on next feed")
def _():
    p = MidiParser()
    assert p.feed(bytes([0xB0, 0x10])) == []
    assert p.feed(bytes([0x7F])) == [(1, 0xB0, [0x10, 0x7F])]


@test("status-only feed then data feed")
def _():
    p = MidiParser()
    assert p.feed(bytes([0x90])) == []
    assert p.feed(bytes([0x3C])) == []
    assert p.feed(bytes([0x64])) == [(1, 0x90, [0x3C, 0x64])]


@test("running status preserved across feed boundaries")
def _():
    p = MidiParser()
    assert p.feed(bytes([0xB0, 0x10, 0x7F])) == [(1, 0xB0, [0x10, 0x7F])]
    # Next feed carries only a data pair - running status must still apply.
    assert p.feed(bytes([0x11, 0x40])) == [(1, 0xB0, [0x11, 0x40])]


@test("mixed stream: CC, SYSEX, running status, real-time, PC")
def _():
    p = MidiParser()
    stream = bytes([
        0xB0, 0x10, 0x7F,             # CC
        0xF0, 0x00, 0x20, 0x33, 0xF7, # SYSEX (drops running status)
        0xC0, 0x05,                   # PC (explicit status after SYSEX)
        0x06,                         # running-status PC
        0xF8,                         # real-time skipped
        0x07,                         # running-status PC
    ])
    out = p.feed(stream)
    assert out == [
        (1, 0xB0, [0x10, 0x7F]),
        (0, 0xF0, [0x00, 0x20, 0x33]),
        (1, 0xC0, [0x05]),
        (1, 0xC0, [0x06]),
        (1, 0xC0, [0x07]),
    ], out


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
