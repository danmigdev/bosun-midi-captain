#!/usr/bin/env python3
"""Offline tests for the Kemper bilateral protocol and inbound-MIDI plumbing.

Covers:
  - plugins/kemper.py: on_midi_in reacts to effect-block CCs, rig PC+LSB,
    tuner CC; respects channel filter; respects enabled / per-feature flags.
  - captain/plugin.py: PluginRegistry.dispatch_midi_in calls every plugin,
    isolates throwing plugins so one bad on_midi_in doesn't kill the rest.
  - captain/midi.py: MidiParser handles running-status, real-time interleave,
    SYSEX skip, and short / malformed sequences without losing alignment.

No hardware, no CircuitPython runtime.

Usage
-----
    python tools/bilateral_test.py
"""
import sys
import types
from pathlib import Path


# ---------------- path setup so we can import the firmware modules ----------------

FIRMWARE_LIB = Path(__file__).resolve().parent.parent / "firmware" / "lib"
sys.path.insert(0, str(FIRMWARE_LIB))

# The kemper plugin imports nothing CircuitPython-side, but captain.midi pulls
# in busio + usb_midi. Stub them out so MidiParser is importable.
for mod_name in ("busio", "usb_midi", "digitalio", "board", "neopixel",
                 "displayio", "fourwire", "pwmio", "terminalio",
                 "adafruit_display_text", "adafruit_st7789",
                 "adafruit_bitmap_font"):
    sys.modules.setdefault(mod_name, types.ModuleType(mod_name))
# board needs the GPIO attribute surface the firmware addresses
import board  # noqa: E402
for _n in [f"GP{i}" for i in range(30)]:
    setattr(board, _n, _n)

# adafruit_display_text needs a 'label' submodule with a .Label callable
import adafruit_display_text  # noqa: E402
adafruit_display_text.label = types.ModuleType("adafruit_display_text.label")
adafruit_display_text.label.Label = lambda *a, **kw: None
sys.modules["adafruit_display_text.label"] = adafruit_display_text.label

from captain.midi import MidiParser              # noqa: E402
from captain.plugin import PluginRegistry        # noqa: E402
from captain.bindings import BindingRunner       # noqa: E402
from plugins import kemper                       # noqa: E402


# ---------------- test harness ----------------

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


# ---------------- fake Captain ----------------

class _FakeMidiOut:
    """Records SYSEX frames a plugin emits (the real one talks to hardware)."""

    def __init__(self):
        self.sysex = []

    def send_sysex(self, data):
        self.sysex.append(tuple(data))


class FakeApp:
    """Mirrors the subset of Captain that plugins reach for via on_midi_in().
    Records every call so tests can assert side effects."""

    def __init__(self, bindings=None, kemper_cfg=None, ampero_cfg=None,
                 midi_learn=None, last_lsb=None, last_msb=None):
        self.device = {}
        if kemper_cfg is not None:
            self.device["kemper"] = kemper_cfg
        if ampero_cfg is not None:
            self.device["ampero"] = ampero_cfg
        self.midi_learn_table = midi_learn or {"pc_to_patch": []}
        self._bindings = bindings or {}
        self._last_lsb = last_lsb or {}
        self._last_msb = last_msb or {}
        # Fake MIDI out: records every SYSEX the plugin emits (e.g. the block
        # state re-query the rig-name handler fires).
        self.midi = _FakeMidiOut()
        # Current loaded patch, read by on_midi_in's echo branch. The echo
        # window is "now - last_local_switch_ms < 1200"; default the last local
        # switch far in the past so an inbound PC with no recent local switch
        # takes the EXTERNAL (genuine rig-change) branch, not the echo branch.
        self.current_bank = 1
        self.current_slot = 1
        self.last_local_switch_ms = -10_000
        # Recorded calls
        self.latched_calls = []
        self.context_updates = []
        self.switch_patch_calls = []

    def current_bindings(self):
        return list(self._bindings.items())

    def set_switch_latched(self, name, on):
        self.latched_calls.append((name, bool(on)))
        return True

    def update_context(self, updates):
        self.context_updates.append(dict(updates))

    def get_last_bank_lsb(self, port, channel):
        return self._last_lsb.get((port, channel), 0)

    def get_last_bank_msb(self, port, channel):
        return self._last_msb.get((port, channel), 0)

    def switch_patch(self, bank, slot, source="editor", fire_on_enter=True):
        self.switch_patch_calls.append((bank, slot, source))
        self.last_fire_on_enter = fire_on_enter
        return True

    # Controllable clock for the kemper settle-window logic. Tests set
    # `app.now_ms` to move time; default 0 means "not settling".
    def _now_ms(self):
        return getattr(self, "now_ms", 0)


def _patch_with_block(switch_name, block):
    """Build a minimal binding that targets `block` via kemper_effect_toggle."""
    return {
        "switch": switch_name,
        "mode": "latched",
        "actions": {
            "toggle_on":  {"messages": [{"type": "kemper_effect_toggle",
                                         "slot": block, "value": "on", "channel": 1}]},
            "toggle_off": {"messages": [{"type": "kemper_effect_toggle",
                                         "slot": block, "value": "off", "channel": 1}]},
        },
    }


# =================== Kemper bilateral tests ===================

@test("kemper: CC 19 (block C) at 127 -> set_switch_latched('4', True)")
def _():
    app = FakeApp(
        bindings={"4": _patch_with_block("4", "C")},
        kemper_cfg={"enabled": True, "midi_channel": 1,
                    "auto_follow_effects": True, "auto_follow_rig": True},
    )
    kemper.on_midi_in("usb", 1, 0xB0, [19, 127], app)
    assert app.latched_calls == [("4", True)], app.latched_calls


@test("kemper: CC 19 at value 0 -> set_switch_latched('4', False)")
def _():
    app = FakeApp(
        bindings={"4": _patch_with_block("4", "C")},
        kemper_cfg={"enabled": True, "midi_channel": 1,
                    "auto_follow_effects": True, "auto_follow_rig": True},
    )
    kemper.on_midi_in("usb", 1, 0xB0, [19, 0], app)
    assert app.latched_calls == [("4", False)], app.latched_calls


@test("kemper: CC for unbound block -> no latched call")
def _():
    app = FakeApp(
        bindings={"4": _patch_with_block("4", "C")},  # only C bound
        kemper_cfg={"enabled": True, "midi_channel": 1,
                    "auto_follow_effects": True, "auto_follow_rig": True},
    )
    kemper.on_midi_in("usb", 1, 0xB0, [17, 127], app)  # block A
    assert app.latched_calls == [], app.latched_calls


@test("kemper: incoming PC 8 (flat rig list) -> rig 9, bank 2, rig-in-bank 4")
def _():
    # The Player uses a FLAT rig list: the incoming Program Change IS the rig
    # index 0..124 (Bank LSB stays 0). PC 8 = the 9th rig = bosun bank 2 rig 4.
    app = FakeApp(
        kemper_cfg={"enabled": True, "midi_channel": 1,
                    "auto_follow_effects": True, "auto_follow_rig": True},
    )
    kemper.on_midi_in("usb", 1, 0xC0, [8], app)
    assert app.context_updates == [{"kemper_rig": 9, "kemper_bank": 2, "kemper_rig_in_bank": 4}], \
        app.context_updates


@test("kemper: incoming PC 4 -> rig 5, bank 1, rig-in-bank 5 (bank boundary)")
def _():
    app = FakeApp(
        kemper_cfg={"enabled": True, "midi_channel": 1,
                    "auto_follow_effects": True, "auto_follow_rig": True},
    )
    kemper.on_midi_in("usb", 1, 0xC0, [4], app)
    assert app.context_updates == [{"kemper_rig": 5, "kemper_bank": 1, "kemper_rig_in_bank": 5}], \
        app.context_updates


@test("kemper: incoming PC 2 -> rig 3, bank 1, rig-in-bank 3")
def _():
    app = FakeApp(
        kemper_cfg={"enabled": True, "midi_channel": 1,
                    "auto_follow_effects": True, "auto_follow_rig": True},
    )
    kemper.on_midi_in("usb", 1, 0xC0, [2], app)
    assert app.context_updates == [{"kemper_rig": 3, "kemper_bank": 1, "kemper_rig_in_bank": 3}]


@test("kemper: incoming PC auto-follows the rig by switching the matching patch")
def _():
    # A rig change on the Kemper (flat PC 8 -> bank 2, rig-in-bank 4) must switch
    # the bosun to patch 2/4 so it shows that rig's own bindings, not stay stuck
    # on the boot patch ("works only for acoustic").
    app = FakeApp(
        kemper_cfg={"enabled": True, "midi_channel": 1,
                    "auto_follow_effects": True, "auto_follow_rig": True},
    )
    kemper.on_midi_in("usb", 1, 0xC0, [8], app)
    assert app.switch_patch_calls == [(2, 4, "midi_in")], app.switch_patch_calls
    # Must NOT re-send the rig (fire_on_enter=False) - the Player is already on
    # it; re-sending ping-pongs into a $41/echo flood that drops block replies.
    assert app.last_fire_on_enter is False, app.last_fire_on_enter


@test("kemper: tuner CC 31 value 127 -> kemper_tuner='on'")
def _():
    app = FakeApp(
        kemper_cfg={"enabled": True, "midi_channel": 1,
                    "auto_follow_effects": True, "auto_follow_rig": True},
    )
    kemper.on_midi_in("usb", 1, 0xB0, [31, 127], app)
    assert app.context_updates == [{"kemper_tuner": "on"}]


@test("kemper: tuner CC 31 value 0 -> 'off'")
def _():
    app = FakeApp(
        kemper_cfg={"enabled": True, "midi_channel": 1,
                    "auto_follow_effects": True, "auto_follow_rig": True},
    )
    kemper.on_midi_in("usb", 1, 0xB0, [31, 0], app)
    assert app.context_updates == [{"kemper_tuner": "off"}]


@test("kemper: wrong channel -> ignored entirely")
def _():
    app = FakeApp(
        bindings={"4": _patch_with_block("4", "C")},
        kemper_cfg={"enabled": True, "midi_channel": 1,
                    "auto_follow_effects": True, "auto_follow_rig": True},
    )
    kemper.on_midi_in("usb", 5, 0xB0, [19, 127], app)
    kemper.on_midi_in("usb", 5, 0xC0, [0], app)
    assert app.latched_calls == []
    assert app.context_updates == []


@test("kemper: missing kemper config block -> nothing happens, no crash")
def _():
    app = FakeApp(
        bindings={"4": _patch_with_block("4", "C")},
    )
    kemper.on_midi_in("usb", 1, 0xB0, [19, 127], app)
    kemper.on_midi_in("usb", 1, 0xC0, [0], app)
    assert app.latched_calls == []
    assert app.context_updates == []


@test("kemper: bilateral sync is always-on for a kemper profile (legacy flags ignored)")
def _():
    # CONFIG_SCHEMA: enabled / auto_follow_rig / auto_follow_effects /
    # bidirectional are no longer user options - a kemper profile is always
    # fully synced (gated only by the presence of the `kemper` section). A
    # device.json still carrying the old disabling flags must sync anyway.
    app = FakeApp(
        bindings={"4": _patch_with_block("4", "C")},
        kemper_cfg={"enabled": False, "auto_follow_effects": False,
                    "auto_follow_rig": False, "bidirectional": False},
        last_lsb={("usb", 1): 0},
    )
    kemper.on_midi_in("usb", 1, 0xB0, [19, 127], app)   # block C still mirrors
    kemper.on_midi_in("usb", 1, 0xC0, [2], app)         # rig still follows
    assert app.latched_calls == [("4", True)], app.latched_calls
    assert app.switch_patch_calls == [(1, 3, "midi_in")], app.switch_patch_calls


@test("kemper: all 8 block CCs map to the right blocks")
def _():
    # DLY/REV use CC 27/29 (the "with spillover" variants the firmware sends),
    # NOT 26/28 - see _EFFECT_CC in kemper.py.
    cases = [
        (17, "A"), (18, "B"), (19, "C"), (20, "D"),
        (22, "X"), (24, "Mod"), (27, "Delay"), (29, "Reverb"),
    ]
    for cc, block in cases:
        app = FakeApp(
            bindings={"sw": _patch_with_block("sw", block)},
            kemper_cfg={"enabled": True, "midi_channel": 1,
                        "auto_follow_effects": True, "auto_follow_rig": True},
        )
        kemper.on_midi_in("usb", 1, 0xB0, [cc, 127], app)
        assert app.latched_calls == [("sw", True)], f"cc {cc} -> {app.latched_calls}"


@test("kemper: empty/malformed CC data does not crash")
def _():
    app = FakeApp(
        kemper_cfg={"enabled": True, "midi_channel": 1,
                    "auto_follow_effects": True, "auto_follow_rig": True},
    )
    kemper.on_midi_in("usb", 1, 0xB0, [], app)
    kemper.on_midi_in("usb", 1, 0xB0, [19], app)        # missing value byte
    kemper.on_midi_in("usb", 1, 0xC0, [], app)           # PC with no program byte
    assert app.latched_calls == []
    assert app.context_updates == []


def _kemper_sysex_param_response(page, addr, value):
    """Build the payload of a Kemper SYSEX $01 response (without F0/F7)."""
    return [0x00, 0x20, 0x33, 0x02, 0x7F,
            0x01, 0x00, page, addr,
            (value >> 7) & 0x7F, value & 0x7F]


def _kemper_sysex_string_response(page, addr, ascii_bytes):
    """Build the payload of a Kemper SYSEX $03 string response (without F0/F7).
    ascii_bytes is the raw payload after the addr byte - caller decides whether
    to append a 0x00 terminator or trailing junk."""
    return [0x00, 0x20, 0x33, 0x02, 0x7F,
            0x03, 0x00, page, addr] + list(ascii_bytes)


def _reset_published():
    """Clear the per-field publish-dedup cache so each test starts fresh."""
    kemper._BIDIR_STATE["published"] = {}
    kemper._BIDIR_STATE["settle_until_ms"] = 0


@test("kemper: SYSEX $01 response for block C on -> set_switch_latched")
def _():
    app = FakeApp(
        bindings={"4": _patch_with_block("4", "C")},
        kemper_cfg={"enabled": True, "midi_channel": 1,
                    "auto_follow_effects": True, "auto_follow_rig": True},
    )
    payload = _kemper_sysex_param_response(0x34, 0x03, 1)   # block C, on/off addr, value=1
    kemper.on_midi_in("usb", 0, 0xF0, payload, app)
    assert app.latched_calls == [("4", True)], app.latched_calls


@test("kemper: SYSEX $01 response value=0 -> latched off")
def _():
    app = FakeApp(
        bindings={"4": _patch_with_block("4", "C")},
        kemper_cfg={"enabled": True, "midi_channel": 1,
                    "auto_follow_effects": True, "auto_follow_rig": True},
    )
    payload = _kemper_sysex_param_response(0x34, 0x03, 0)
    kemper.on_midi_in("usb", 0, 0xF0, payload, app)
    assert app.latched_calls == [("4", False)], app.latched_calls


@test("kemper: SYSEX channel filter is bypassed (SYSEX is global)")
def _():
    # Even though the message arrives on "channel 0" (which would normally
    # be filtered out), SYSEX is still processed because it's not a
    # channel-voice message.
    app = FakeApp(
        bindings={"4": _patch_with_block("4", "C")},
        kemper_cfg={"enabled": True, "midi_channel": 1,
                    "auto_follow_effects": True, "auto_follow_rig": True},
    )
    payload = _kemper_sysex_param_response(0x34, 0x03, 1)
    kemper.on_midi_in("usb", 0, 0xF0, payload, app)
    assert app.latched_calls == [("4", True)]


@test("kemper: SYSEX from wrong manufacturer is ignored")
def _():
    app = FakeApp(
        bindings={"4": _patch_with_block("4", "C")},
        kemper_cfg={"enabled": True, "midi_channel": 1,
                    "auto_follow_effects": True, "auto_follow_rig": True},
    )
    payload = [0x42, 0x10, 0x00, 0x01, 0x02]   # not Kemper's 00 20 33
    kemper.on_midi_in("usb", 0, 0xF0, payload, app)
    assert app.latched_calls == []


@test("kemper: SYSEX for non-on/off addr is ignored")
def _():
    app = FakeApp(
        bindings={"4": _patch_with_block("4", "C")},
        kemper_cfg={"enabled": True, "midi_channel": 1,
                    "auto_follow_effects": True, "auto_follow_rig": True},
    )
    # addr 0x00 = effect type, not on/off
    payload = _kemper_sysex_param_response(0x34, 0x00, 1)
    kemper.on_midi_in("usb", 0, 0xF0, payload, app)
    assert app.latched_calls == []


@test("kemper: SYSEX for non-block page (e.g. 0x00) is ignored")
def _():
    app = FakeApp(
        bindings={"4": _patch_with_block("4", "C")},
        kemper_cfg={"enabled": True, "midi_channel": 1,
                    "auto_follow_effects": True, "auto_follow_rig": True},
    )
    payload = _kemper_sysex_param_response(0x00, 0x03, 1)
    kemper.on_midi_in("usb", 0, 0xF0, payload, app)
    assert app.latched_calls == []


@test("kemper: bidirectional sensing message ($7E) marks confirmed + publishes kemper_connected")
def _():
    app = FakeApp(
        bindings={"4": _patch_with_block("4", "C")},
        kemper_cfg={"enabled": True, "midi_channel": 1,
                    "auto_follow_effects": True, "auto_follow_rig": True},
    )
    # Reset module state in case other tests left it set.
    kemper._BIDIR_STATE["confirmed"] = False
    _reset_published()
    # Sensing format: 00 20 33 00 00 7E 00 7F <something>
    payload = [0x00, 0x20, 0x33, 0x00, 0x00, 0x7E, 0x00, 0x7F, 0x00]
    kemper.on_midi_in("usb", 0, 0xF0, payload, app)
    assert app.latched_calls == []
    assert app.context_updates == [{"kemper_connected": "on"}], app.context_updates
    assert kemper._BIDIR_STATE.get("confirmed") is True, kemper._BIDIR_STATE


@test("kemper: tick sends init beacon at first call, keep-alive after")
def _():
    class _Midi:
        def __init__(self): self.sent = []
        def send_sysex(self, data): self.sent.append(tuple(data))
    class _App:
        device = {"kemper": {"enabled": True}}
        midi = _Midi()
    kemper._BIDIR_STATE["last_beacon_ms"] = 0
    kemper._BIDIR_STATE["init_sent"] = False

    app = _App()
    kemper.tick(app, 100)
    assert len(app.midi.sent) == 1, app.midi.sent
    payload = app.midi.sent[0]
    # 00 20 33 02 7F 7E 00 40 02 <flags> <lease>
    assert payload[:9] == (0x00, 0x20, 0x33, 0x02, 0x7F, 0x7E, 0x00, 0x40, 0x02), payload
    assert payload[9] == 0x23, f"first beacon should set init flag (0x23), got 0x{payload[9]:02x}"
    assert payload[10] == 0x05, f"lease/2 expected 0x05, got 0x{payload[10]:02x}"

    # Second tick well within the resend window - should NOT send.
    kemper.tick(app, 200)
    assert len(app.midi.sent) == 1

    # After 5 s - should send a keep-alive (flags 0x22, no init bit).
    kemper.tick(app, 100 + 5000)
    assert len(app.midi.sent) == 2
    assert app.midi.sent[1][9] == 0x22, f"keep-alive flag expected 0x22, got 0x{app.midi.sent[1][9]:02x}"


@test("kemper: SYSEX param response with sensing-style 0x00 0x00 product/device still parses")
def _():
    app = FakeApp(
        bindings={"4": _patch_with_block("4", "C")},
        kemper_cfg={"enabled": True, "midi_channel": 1,
                    "auto_follow_effects": True, "auto_follow_rig": True},
    )
    # Same as a normal $01 response but product/device are zeros (as the
    # Player sometimes uses for broadcast frames). Effect block C on.
    payload = [0x00, 0x20, 0x33, 0x00, 0x00,
               0x01, 0x00, 0x34, 0x03, 0x00, 0x01]
    kemper.on_midi_in("usb", 0, 0xF0, payload, app)
    assert app.latched_calls == [("4", True)]


@test("kemper: SYSEX $03 rig name -> publishes kemper_rig_name AND patch_name")
def _():
    _reset_published()
    app = FakeApp(
        kemper_cfg={"enabled": True, "midi_channel": 1,
                    "auto_follow_effects": True, "auto_follow_rig": True},
    )
    # "PLEXI" + null terminator
    payload = _kemper_sysex_string_response(0x00, 0x01, [0x50, 0x4C, 0x45, 0x58, 0x49, 0x00])
    kemper.on_midi_in("usb", 0, 0xF0, payload, app)
    assert app.context_updates == [{"kemper_rig_name": "PLEXI", "patch_name": "PLEXI"}], app.context_updates


@test("kemper: SYSEX $03 rig name without null terminator still parses")
def _():
    _reset_published()
    app = FakeApp(
        kemper_cfg={"enabled": True, "midi_channel": 1,
                    "auto_follow_effects": True, "auto_follow_rig": True},
    )
    payload = _kemper_sysex_string_response(0x00, 0x01, [0x4C, 0x65, 0x61, 0x64])  # "Lead"
    kemper.on_midi_in("usb", 0, 0xF0, payload, app)
    assert app.context_updates == [{"kemper_rig_name": "Lead", "patch_name": "Lead"}], app.context_updates


@test("kemper: SYSEX $03 rig name drops non-ASCII / control bytes")
def _():
    _reset_published()
    app = FakeApp(
        kemper_cfg={"enabled": True, "midi_channel": 1,
                    "auto_follow_effects": True, "auto_follow_rig": True},
    )
    # "Hi" + 0xFF (out of range) + "!" + 0x00
    payload = _kemper_sysex_string_response(0x00, 0x01, [0x48, 0x69, 0xFF, 0x21, 0x00])
    kemper.on_midi_in("usb", 0, 0xF0, payload, app)
    assert app.context_updates == [{"kemper_rig_name": "Hi!", "patch_name": "Hi!"}], app.context_updates


@test("kemper: SYSEX $03 empty / all-zero string is ignored (no field clobbering)")
def _():
    _reset_published()
    app = FakeApp(
        kemper_cfg={"enabled": True, "midi_channel": 1,
                    "auto_follow_effects": True, "auto_follow_rig": True},
    )
    payload = _kemper_sysex_string_response(0x00, 0x01, [0x00])
    kemper.on_midi_in("usb", 0, 0xF0, payload, app)
    assert app.context_updates == [], app.context_updates


@test("kemper: SYSEX $01 BPM page 0x04 addr 0x00 with value=7680 -> kemper_bpm=120")
def _():
    _reset_published()
    app = FakeApp(
        kemper_cfg={"enabled": True, "midi_channel": 1,
                    "auto_follow_effects": True, "auto_follow_rig": True},
    )
    payload = _kemper_sysex_param_response(0x04, 0x00, 7680)   # 120 * 64
    kemper.on_midi_in("usb", 0, 0xF0, payload, app)
    assert app.context_updates == [{"kemper_bpm": 120}], app.context_updates


@test("kemper: block on/off broadcast updates the persistent _BLOCK_STATE cache")
def _():
    _reset_published(); kemper._BLOCK_STATE.clear()
    app = FakeApp(
        bindings={"4": _patch_with_block("4", "C")},
        kemper_cfg={"enabled": True, "midi_channel": 1,
                    "auto_follow_effects": True, "auto_follow_rig": True},
    )
    # SYSEX $01 block C (page 0x34 addr 0x03) on -> cache["C"] = True
    kemper.on_midi_in("usb", 0, 0xF0, _kemper_sysex_param_response(0x34, 0x03, 1), app)
    assert kemper._BLOCK_STATE.get("C") is True, kemper._BLOCK_STATE
    # CC path also feeds the cache: CC 17 (block A) = 0 -> cache["A"] = False
    kemper.on_midi_in("usb", 1, 0xB0, [17, 0], app)
    assert kemper._BLOCK_STATE.get("A") is False, kemper._BLOCK_STATE


@test("kemper: on_patch_loaded repaints effect LEDs from the cache (rig-change fix)")
def _():
    # The core resets latched switches on every patch load and the Player only
    # re-broadcasts CHANGED blocks on a rig change, so a block ON across rigs
    # would render dark. on_patch_loaded must re-apply the cached state.
    _reset_published(); kemper._BLOCK_STATE.clear()
    kemper._BLOCK_STATE["X"] = True
    kemper._BLOCK_STATE["Reverb"] = False
    app = FakeApp(
        bindings={"3": _patch_with_block("3", "X"),
                  "up": _patch_with_block("up", "Reverb")},
        kemper_cfg={"enabled": True, "midi_channel": 1,
                    "auto_follow_effects": True, "auto_follow_rig": True},
    )
    kemper.on_patch_loaded(app)
    assert ("3", True) in app.latched_calls, app.latched_calls
    assert ("up", False) in app.latched_calls, app.latched_calls


@test("kemper: on_patch_loaded leaves blocks with no cached state untouched")
def _():
    _reset_published(); kemper._BLOCK_STATE.clear()     # nothing known yet
    app = FakeApp(
        bindings={"3": _patch_with_block("3", "X")},
        kemper_cfg={"enabled": True, "midi_channel": 1,
                    "auto_follow_effects": True, "auto_follow_rig": True},
    )
    kemper.on_patch_loaded(app)
    assert app.latched_calls == [], app.latched_calls


@test("kemper: on_patch_loaded paints from cache and emits NO $41 query (PySwitch model)")
def _():
    # The rig-change LED fix is cache-driven, NOT query-driven: on_patch_loaded
    # repaints from the persistent cache and must NOT send any $41 to the Player.
    # Extra $41 traffic overran the direct USB link and dropped block replies
    # past the first couple of rigs - the factory PySwitch sends none.
    _reset_published(); kemper._BLOCK_STATE.clear()
    kemper._BLOCK_STATE["X"] = True
    app = FakeApp(
        bindings={"3": _patch_with_block("3", "X")},
        kemper_cfg={"enabled": True, "midi_channel": 1,
                    "auto_follow_effects": True, "auto_follow_rig": True},
    )
    kemper.on_patch_loaded(app)
    assert ("3", True) in app.latched_calls, app.latched_calls   # painted from cache
    assert app.midi.sysex == [], "on_patch_loaded must not query the Player"


@test("kemper: tick sends only the beacon, never an auto block re-query")
def _():
    _reset_published()
    kemper._BIDIR_STATE.update({"confirmed": True, "init_sent": False, "last_beacon_ms": 0})
    app = FakeApp(kemper_cfg={"enabled": True, "midi_channel": 1,
                              "auto_follow_effects": True, "bidirectional": True})
    kemper.tick(app, 10000)
    assert len(app.midi.sysex) == 1, app.midi.sysex          # just the beacon
    assert app.midi.sysex[0][5] == kemper._FN_EXTENDED, app.midi.sysex


@test("kemper: cache + broadcast deltas converge block LEDs with no query (PySwitch model)")
def _():
    # The persistent cache is seeded/kept current by the Player's broadcasts and
    # applied on patch load - no $41 round-trip needed. This is how the LED fix
    # works on the direct USB link, which can't take the extra query traffic.
    _reset_published(); kemper._BLOCK_STATE.clear()
    app = FakeApp(
        bindings={"3": _patch_with_block("3", "X")},
        kemper_cfg={"enabled": True, "midi_channel": 1,
                    "auto_follow_effects": True, "auto_follow_rig": True},
    )
    # Player broadcasts X on (delta) -> cache updated, no query emitted.
    kemper.on_midi_in("usb", 0, 0xF0, _kemper_sysex_param_response(0x38, 0x03, 1), app)
    assert kemper._BLOCK_STATE.get("X") is True
    kemper.on_patch_loaded(app)                              # new patch -> paint from cache
    assert ("3", True) in app.latched_calls, app.latched_calls
    assert app.midi.sysex == [], "no outbound query at any point"


@test("kemper: rig change opens a settle window - block deltas don't paint LEDs mid-burst")
def _():
    # The flash fix: while a rig-change broadcast is still streaming, deltas
    # update the cache but must NOT paint LEDs (mid-burst paints = the flash).
    # tick() repaints once from the settled cache after the window closes.
    _reset_published(); kemper._BLOCK_STATE.clear()
    app = FakeApp(
        bindings={"3": _patch_with_block("3", "X")},
        kemper_cfg={"enabled": True, "midi_channel": 1,
                    "auto_follow_effects": True, "auto_follow_rig": True},
    )
    app.now_ms = 1000
    kemper.on_midi_in("usb", 0, 0xF0, _kemper_sysex_string_response(0x00, 0x01, [0x43, 0x00]), app)  # "C"
    assert kemper._BIDIR_STATE["settle_until_ms"] > 1000, "settle window opened"
    app.now_ms = 1100
    kemper.on_midi_in("usb", 0, 0xF0, _kemper_sysex_param_response(0x38, 0x03, 1), app)  # X on
    assert kemper._BLOCK_STATE.get("X") is True
    assert app.latched_calls == [], "no live LED paint during settle"
    app.now_ms = 1000 + kemper._SETTLE_MS + 10
    kemper.tick(app, app.now_ms)                              # window closed -> repaint
    assert ("3", True) in app.latched_calls, app.latched_calls
    kemper._BIDIR_STATE["settle_until_ms"] = 0


@test("kemper: outside any settle window, a block delta paints the LED live (toggle)")
def _():
    _reset_published(); kemper._BLOCK_STATE.clear()
    app = FakeApp(
        bindings={"3": _patch_with_block("3", "X")},
        kemper_cfg={"enabled": True, "midi_channel": 1,
                    "auto_follow_effects": True, "auto_follow_rig": True},
    )
    app.now_ms = 5000        # settle_until is 0 -> not settling
    kemper.on_midi_in("usb", 0, 0xF0, _kemper_sysex_param_response(0x38, 0x03, 1), app)
    assert ("3", True) in app.latched_calls, app.latched_calls


@test("kemper: SYSEX $01 tuner mode page 0x7F addr 0x7E -> kemper_tuner on/off")
def _():
    _reset_published()
    app = FakeApp(
        kemper_cfg={"enabled": True, "midi_channel": 1,
                    "auto_follow_effects": True, "auto_follow_rig": True},
    )
    kemper.on_midi_in("usb", 0, 0xF0, _kemper_sysex_param_response(0x7F, 0x7E, 1), app)
    kemper.on_midi_in("usb", 0, 0xF0, _kemper_sysex_param_response(0x7F, 0x7E, 0), app)
    assert app.context_updates == [{"kemper_tuner": "on"}, {"kemper_tuner": "off"}], app.context_updates


@test("kemper: SYSEX $01 tuner note page 0x7D addr 0x54 -> kemper_tuner_note")
def _():
    _reset_published()
    app = FakeApp(
        kemper_cfg={"enabled": True, "midi_channel": 1,
                    "auto_follow_effects": True, "auto_follow_rig": True},
    )
    # value 60 % 12 = 0 -> 'C'; 69 % 12 = 9 -> 'A'
    kemper.on_midi_in("usb", 0, 0xF0, _kemper_sysex_param_response(0x7D, 0x54, 60), app)
    kemper.on_midi_in("usb", 0, 0xF0, _kemper_sysex_param_response(0x7D, 0x54, 69), app)
    assert app.context_updates == [{"kemper_tuner_note": "C"}, {"kemper_tuner_note": "A"}], app.context_updates


@test("kemper: SYSEX $01 tuner deviance page 0x7C addr 0x0F -> kemper_tuner_deviance")
def _():
    _reset_published()
    app = FakeApp(
        kemper_cfg={"enabled": True, "midi_channel": 1,
                    "auto_follow_effects": True, "auto_follow_rig": True},
    )
    kemper.on_midi_in("usb", 0, 0xF0, _kemper_sysex_param_response(0x7C, 0x0F, 8192), app)
    assert app.context_updates == [{"kemper_tuner_deviance": 8192}], app.context_updates


@test("kemper: sensing $7E publishes kemper_connected='on', subsequent sensings de-duped")
def _():
    _reset_published()
    kemper._BIDIR_STATE["confirmed"] = False
    app = FakeApp(
        kemper_cfg={"enabled": True, "midi_channel": 1,
                    "auto_follow_effects": True, "auto_follow_rig": True},
    )
    payload = [0x00, 0x20, 0x33, 0x00, 0x00, 0x7E, 0x00, 0x7F, 0x00]
    kemper.on_midi_in("usb", 0, 0xF0, payload, app)
    kemper.on_midi_in("usb", 0, 0xF0, payload, app)
    kemper.on_midi_in("usb", 0, 0xF0, payload, app)
    # First call publishes; the next two should be de-duped by the "published" cache.
    assert app.context_updates == [{"kemper_connected": "on"}], app.context_updates


@test("kemper: repeated identical BPM frames are de-duped (no TFT redraw spam)")
def _():
    _reset_published()
    app = FakeApp(
        kemper_cfg={"enabled": True, "midi_channel": 1,
                    "auto_follow_effects": True, "auto_follow_rig": True},
    )
    payload = _kemper_sysex_param_response(0x04, 0x00, 7680)   # 120 BPM
    for _ in range(5):
        kemper.on_midi_in("usb", 0, 0xF0, payload, app)
    assert app.context_updates == [{"kemper_bpm": 120}], app.context_updates


@test("kemper: kemper_query_state dispatch sends 8 SYSEX requests")
def _():
    sent = []
    class FakeMidi:
        def send_sysex(self, data):
            sent.append(bytes(data))
    kemper.dispatch({"type": "kemper_query_state", "channel": 1}, FakeMidi())
    assert len(sent) == 8, f"expected 8 SYSEX queries, got {len(sent)}"
    # Each request is a $41 single-param read targeting a known block's
    # (page, addr). DLY/REV live at 0x4A/0x4B addr 0x02, the rest at addr
    # 0x03 - mirror _BLOCK_ONOFF exactly rather than hardcoding here.
    seen = []
    for body in sent:
        # body layout: 00 20 33 02 7F 41 00 <page> <addr>
        assert list(body[:5]) == [0x00, 0x20, 0x33, 0x02, 0x7F], body
        assert body[5] == 0x41 and body[6] == 0x00, body
        seen.append((body[7], body[8]))
    assert set(seen) == set(kemper._BLOCK_ONOFF.values()), seen


@test("kemper: rig select uses the flat PC mapping with Bank LSB pinned to 0")
def _():
    # The Player addresses rigs as a flat list: Bank LSB stays 0 and PC =
    # (bank-1)*5 + (rig-1). Bank LSB = bank-1 lands on the wrong rig for bank>=2.
    sent = []
    class M:
        def send_cc(self, ch, cc, v): sent.append(("cc", cc, v))
        def send_pc(self, ch, p): sent.append(("pc", p))
        def send_sysex(self, d): pass
    m = M()
    kemper.dispatch({"type": "kemper_rig", "bank": 1, "rig": 1, "channel": 1}, m)
    assert sent == [("cc", 0, 0), ("cc", 32, 0), ("pc", 0)], sent    # rig 1  -> PC 0
    sent.clear()
    kemper.dispatch({"type": "kemper_rig", "bank": 1, "rig": 3, "channel": 1}, m)
    assert sent == [("cc", 0, 0), ("cc", 32, 0), ("pc", 2)], sent    # rig 3  -> PC 2
    sent.clear()
    kemper.dispatch({"type": "kemper_rig", "bank": 2, "rig": 1, "channel": 1}, m)
    assert sent == [("cc", 0, 0), ("cc", 32, 0), ("pc", 5)], sent    # bank 2 rig 1 -> PC 5
    sent.clear()
    kemper.dispatch({"type": "kemper_rig", "bank": 2, "rig": 4, "channel": 1}, m)
    assert sent == [("cc", 0, 0), ("cc", 32, 0), ("pc", 8)], sent    # bank 2 rig 4 -> PC 8


@test("kemper: rig-name broadcast emits no outbound MIDI (cache-only model)")
def _():
    _reset_published()
    app = FakeApp(
        kemper_cfg={"enabled": True, "midi_channel": 1,
                    "auto_follow_effects": True, "auto_follow_rig": True},
    )
    payload = _kemper_sysex_string_response(0x00, 0x01, [0x43, 0x4C, 0x45, 0x41, 0x4E, 0x00])
    kemper.on_midi_in("usb", 0, 0xF0, payload, app)
    assert app.midi.sysex == [], "rig-name must not trigger any $41 query"
    assert app.context_updates == [{"kemper_rig_name": "CLEAN", "patch_name": "CLEAN"}], app.context_updates


# =================== PluginRegistry.dispatch_midi_in ===================

@test("registry: dispatch hits every plugin with on_midi_in")
def _():
    reg = PluginRegistry()
    seen = []

    class P1:
        NAME = "p1"
        VERSION = "1"
        MESSAGE_TYPES = {}
        @staticmethod
        def dispatch(msg, midi): pass
        @staticmethod
        def on_midi_in(port, channel, status, data, app):
            seen.append(("p1", port, channel, status, tuple(data)))

    class P2:
        NAME = "p2"
        VERSION = "1"
        MESSAGE_TYPES = {}
        @staticmethod
        def dispatch(msg, midi): pass
        @staticmethod
        def on_midi_in(port, channel, status, data, app):
            seen.append(("p2", port, channel, status, tuple(data)))

    reg.register(P1)
    reg.register(P2)
    reg.dispatch_midi_in("usb", 1, 0xB0, [19, 127], app=None)
    plugin_names = sorted(s[0] for s in seen)
    assert plugin_names == ["p1", "p2"], seen


@test("registry: throwing plugin doesn't poison the dispatch loop")
def _():
    reg = PluginRegistry()
    seen = []

    class Bad:
        NAME = "bad"
        VERSION = "1"
        MESSAGE_TYPES = {}
        @staticmethod
        def dispatch(msg, midi): pass
        @staticmethod
        def on_midi_in(port, channel, status, data, app):
            raise RuntimeError("kaboom")

    class Good:
        NAME = "good"
        VERSION = "1"
        MESSAGE_TYPES = {}
        @staticmethod
        def dispatch(msg, midi): pass
        @staticmethod
        def on_midi_in(port, channel, status, data, app):
            seen.append("good")

    reg.register(Bad)
    reg.register(Good)
    reg.dispatch_midi_in("usb", 1, 0xB0, [19, 127], app=None)
    assert "good" in seen, "good plugin must still run after bad raised"


@test("registry: plugin without on_midi_in is silently skipped")
def _():
    reg = PluginRegistry()

    class NoHook:
        NAME = "nohook"
        VERSION = "1"
        MESSAGE_TYPES = {}
        @staticmethod
        def dispatch(msg, midi): pass

    reg.register(NoHook)
    # No assertion needed - just must not raise.
    reg.dispatch_midi_in("usb", 1, 0xB0, [19, 127], app=None)


# =================== preset-row navigation on a sparse bank ===================

@test("kemper: on_navigate selects that rig on the Player + updates the TFT context")
def _():
    # A preset switch hit a (bank, slot) with no bosun patch. on_navigate must
    # still drive the Player to that rig so the whole bank stays reachable.
    _reset_published()
    app = FakeApp(kemper_cfg={"enabled": True, "midi_channel": 1,
                              "auto_follow_effects": True, "auto_follow_rig": True})
    rec = []
    class M:
        def send_cc(self, ch, cc, v): rec.append(("cc", cc, v))
        def send_pc(self, ch, p): rec.append(("pc", p))
        def send_sysex(self, d): pass
    app.midi = M()
    kemper.on_navigate(app, 2, 1)                       # bank 2, rig-in-bank 1
    assert rec == [("cc", 0, 0), ("cc", 32, 0), ("pc", 5)], rec   # flat PC = (2-1)*5+0
    assert app.context_updates == [
        {"kemper_bank": 2, "kemper_rig_in_bank": 1, "kemper_rig": 6}], app.context_updates


@test("kemper: on_navigate is a no-op when there's no kemper section")
def _():
    app = FakeApp()                                    # device has no 'kemper'
    rec = []
    class M:
        def send_cc(self, ch, cc, v): rec.append(("cc", cc, v))
        def send_pc(self, ch, p): rec.append(("pc", p))
        def send_sysex(self, d): pass
    app.midi = M()
    kemper.on_navigate(app, 2, 1)
    assert rec == [], rec
    assert app.context_updates == [], app.context_updates


@test("registry: on_navigate calls every plugin's on_navigate hook")
def _():
    reg = PluginRegistry()
    seen = []
    class P:
        NAME = "p"; VERSION = "1"; MESSAGE_TYPES = {}
        @staticmethod
        def dispatch(msg, midi): pass
        @staticmethod
        def on_navigate(app, bank, slot): seen.append((bank, slot))
    reg.register(P)
    reg.on_navigate(None, 3, 2)
    assert seen == [(3, 2)], seen


@test("registry: a throwing on_navigate doesn't poison the others; missing hook is skipped")
def _():
    reg = PluginRegistry()
    seen = []
    class Bad:
        NAME = "bad"; VERSION = "1"; MESSAGE_TYPES = {}
        @staticmethod
        def dispatch(msg, midi): pass
        @staticmethod
        def on_navigate(app, bank, slot): raise RuntimeError("boom")
    class NoHook:
        NAME = "nohook"; VERSION = "1"; MESSAGE_TYPES = {}
        @staticmethod
        def dispatch(msg, midi): pass
    class Good:
        NAME = "good"; VERSION = "1"; MESSAGE_TYPES = {}
        @staticmethod
        def dispatch(msg, midi): pass
        @staticmethod
        def on_navigate(app, bank, slot): seen.append("good")
    reg.register(Bad); reg.register(NoHook); reg.register(Good)
    reg.on_navigate(None, 1, 1)
    assert "good" in seen, seen


@test("bindings: captain_patch to a MISSING patch navigates the device (REPRO of the sparse-bank bug)")
def _():
    # The bug: on a bank where the target slot has no bosun patch, the preset
    # switch fired captain_patch -> switch_patch returned False -> nothing
    # happened ("upper switches don't work after a bank change"). Now the core
    # falls back to on_navigate so the device still moves to that rig.
    nav = []
    class App:
        def switch_patch(self, bank, slot, source="editor"):
            return False                                # no patch at this slot
    class Plugins:
        def handles(self, t): return False
        def on_navigate(self, app, bank, slot): nav.append((bank, slot))
    runner = BindingRunner(midi=None, plugins=Plugins(), app=App())
    runner.run({"messages": [{"type": "captain_patch", "bank": 2, "slot": 1}]})
    assert nav == [(2, 1)], nav


@test("bindings: captain_patch to an EXISTING patch does NOT also navigate")
def _():
    nav = []
    class App:
        def switch_patch(self, bank, slot, source="editor"):
            return True                                 # patch exists -> normal load
    class Plugins:
        def handles(self, t): return False
        def on_navigate(self, app, bank, slot): nav.append((bank, slot))
    runner = BindingRunner(midi=None, plugins=Plugins(), app=App())
    runner.run({"messages": [{"type": "captain_patch", "bank": 1, "slot": 1}]})
    assert nav == [], nav


# =================== MidiParser robustness ===================

@test("parser: complete CC message")
def _():
    p = MidiParser()
    out = p.feed(bytes([0xB0, 0x10, 0x7F]))
    assert out == [(1, 0xB0, [0x10, 0x7F])], out


@test("parser: running status - repeated data bytes after a CC")
def _():
    p = MidiParser()
    # Status once, three CC payloads on the same channel
    out = p.feed(bytes([0xB0, 0x10, 0x7F, 0x11, 0x40, 0x12, 0x01]))
    assert out == [
        (1, 0xB0, [0x10, 0x7F]),
        (1, 0xB0, [0x11, 0x40]),
        (1, 0xB0, [0x12, 0x01]),
    ], out


@test("parser: real-time bytes (0xF8-0xFF) don't break running status")
def _():
    p = MidiParser()
    out = p.feed(bytes([0xB0, 0x10, 0xF8, 0x7F]))  # 0xF8 = clock, must be transparent
    assert out == [(1, 0xB0, [0x10, 0x7F])], out


@test("parser: SYSEX is captured as (0, 0xF0, payload) and drops running status")
def _():
    p = MidiParser()
    # CC, then SYSEX (which both emits its own event and drops running
    # status so the trailing 0x10 0x7F after F7 is NOT a second CC).
    out = p.feed(bytes([0xB0, 0x10, 0x7F, 0xF0, 0x00, 0x20, 0x33, 0xF7, 0x10, 0x7F]))
    assert out == [(1, 0xB0, [0x10, 0x7F]), (0, 0xF0, [0x00, 0x20, 0x33])], out


@test("parser: SYSEX split across feeds reassembles into one payload")
def _():
    p = MidiParser()
    out1 = p.feed(bytes([0xF0, 0x00, 0x20, 0x33]))
    assert out1 == [], out1
    out2 = p.feed(bytes([0x02, 0x7F, 0x01, 0x00, 0x34, 0x03, 0x00, 0x01, 0xF7]))
    assert out2 == [(0, 0xF0, [0x00, 0x20, 0x33, 0x02, 0x7F, 0x01, 0x00, 0x34, 0x03, 0x00, 0x01])], out2


@test("parser: real-time interleaved inside SYSEX is filtered out")
def _():
    p = MidiParser()
    out = p.feed(bytes([0xF0, 0x00, 0xF8, 0x20, 0xF8, 0x33, 0xF7]))
    assert out == [(0, 0xF0, [0x00, 0x20, 0x33])], out


@test("parser: stray data bytes with no status are dropped silently")
def _():
    p = MidiParser()
    out = p.feed(bytes([0x40, 0x50, 0x60]))  # all data, no status seen
    assert out == [], out


@test("parser: PC has only 1 data byte")
def _():
    p = MidiParser()
    out = p.feed(bytes([0xC0, 0x05]))
    assert out == [(1, 0xC0, [0x05])], out


@test("parser: incomplete message at end stays buffered, completes on next feed")
def _():
    p = MidiParser()
    out1 = p.feed(bytes([0xB0, 0x10]))
    assert out1 == [], out1
    out2 = p.feed(bytes([0x7F]))
    assert out2 == [(1, 0xB0, [0x10, 0x7F])], out2


@test("parser: channel decoded from low nibble (status 0xB7 -> channel 8)")
def _():
    p = MidiParser()
    out = p.feed(bytes([0xB7, 0x10, 0x40]))
    assert out == [(8, 0xB0, [0x10, 0x40])], out


@test("parser: status switch between messages clears running-status payload buffer")
def _():
    p = MidiParser()
    # CC partial (1/2 data bytes), then PC arrives. The orphaned 0x10 must not
    # accidentally be interpreted as the PC value.
    out = p.feed(bytes([0xB0, 0x10, 0xC0, 0x07]))
    assert out == [(1, 0xC0, [0x07])], out


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
