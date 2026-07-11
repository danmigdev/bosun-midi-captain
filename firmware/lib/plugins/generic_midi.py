"""Generic MIDI plugin.

A device-agnostic profile so any MIDI gear works out of the box without a
dedicated plugin. It only adds the couple of composite message types the
core primitives (cc / pc / note_on / note_off / delay) can't express by
themselves; everything else is already covered by the core message types.

No device feedback: generic gear has no shared broadcast format, so there
is no on_midi_in / tick here (nothing to mirror). Channels in messages are
1-based, converted to raw the same way ampero.py does."""

NAME = "generic_midi"
VERSION = "1.0"
LABEL = "Generic MIDI"


MESSAGE_TYPES = {
    "program_change_bank": {
        "label": "Program Change with Bank",
        "params": {
            "channel": {"type": "int", "min": 1, "max": 16,  "default": 1, "label": "Channel"},
            "msb":     {"type": "int", "min": 0, "max": 127, "default": 0, "label": "Bank MSB (CC0)"},
            "lsb":     {"type": "int", "min": 0, "max": 127, "default": 0, "label": "Bank LSB (CC32)"},
            "program": {"type": "int", "min": 0, "max": 127, "default": 0, "label": "Program"},
        },
        "summary": "PC {program} bank {msb}/{lsb} ch {channel}",
    },
    "cc_toggle": {
        "label": "CC Toggle (latched)",
        "params": {
            "channel":   {"type": "int",  "min": 1, "max": 16,  "default": 1,   "label": "Channel"},
            "cc":        {"type": "int",  "min": 0, "max": 127, "default": 0,   "label": "CC #"},
            "on_value":  {"type": "int",  "min": 0, "max": 127, "default": 127, "label": "On value"},
            "off_value": {"type": "int",  "min": 0, "max": 127, "default": 0,   "label": "Off value"},
            "state":     {"type": "enum", "values": ["on", "off"], "default": "on", "label": "State"},
        },
        "summary": "CC {cc} {state} ch {channel}",
    },
}


def dispatch(msg, midi):
    t = msg["type"]
    ch = msg.get("channel", 1)
    if t == "program_change_bank":
        # Bank MSB (CC0) + Bank LSB (CC32) then the Program Change, in the
        # order most gear expects (bank select first, then PC latches it).
        midi.send_cc(ch, 0, int(msg.get("msb", 0)))
        midi.send_cc(ch, 32, int(msg.get("lsb", 0)))
        midi.send_pc(ch, int(msg.get("program", 0)))
    elif t == "cc_toggle":
        state = msg.get("state", "on")
        value = int(msg.get("on_value", 127)) if state == "on" else int(msg.get("off_value", 0))
        midi.send_cc(ch, int(msg["cc"]), value)


# Default TFT layout for a generic MIDI profile: the patch name (scrolling
# so long names fit), the bank number and the slot number. Stacked
# top-left, ~60px apart so size-5 labels never overlap. See ampero.py's
# DEFAULT_LAYOUT comment for the vertical-spacing rationale.
DEFAULT_LAYOUT = [
    {"field": "patch_name",
     "halign": "left", "valign": "top", "x": 0, "y": 0,
     "size": 5, "color": "#ffffff", "font": "system", "scroll": True},
    {"field": "bank",
     "halign": "left", "valign": "top", "x": 0, "y": 60,
     "size": 5, "color": "#9aa1ad", "font": "system",
     "prefix": "BANK ", "suffix": ""},
    {"field": "slot",
     "halign": "left", "valign": "top", "x": 0, "y": 120,
     "size": 5, "color": "#6fd99b", "font": "system",
     "prefix": "SLOT ", "suffix": ""},
]
