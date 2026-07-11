"""Line 6 Helix / HX family MIDI plugin.

CC numbers verified against the Line 6 Helix MIDI implementation, which is
the same across the Helix / HX Stomp / HX Effects family. Reference chart:
  https://helixhelp.com/tips-and-guides/universal/midi
(mirrors the official Line 6 "MIDI Continuous Controller Reference" PDF at
line6.com; the Line 6 owner's manuals document the same assignments).

Well-documented, stable global CCs used here:
  - Preset select:  CC0 (Bank MSB) + CC32 (Bank LSB, setlist) + PC (preset)
  - Snapshot select: CC69, value 0-7 = snapshot 1-8
  - Footswitch/stomp toggle: FS1-FS5 = CC49-CC53, FS7-FS11 = CC54-CC58
    (value >= 64 = on, < 64 = off). FS6 is not MIDI-addressable and CC59 is
    the EXP toe switch / CC60 is looper record, so they are NOT footswitches.
  - Tap tempo:  CC64, value 64 emulates one TAP press
  - Tuner:      CC68, value 64 = on, 0 = off (any value toggles the screen)
  - Looper:     CC60-CC67 (record/overdub, stop/play, once, undo,
    forward/reverse, full/half speed, looper block on/off)

Channels in messages are 1-based, converted to raw the same way ampero.py
does. Pure logic - no hardware imports, `midi` is passed in."""

NAME = "line6_helix"
VERSION = "1.0"
LABEL = "Line 6 Helix"


# Footswitch (Stomp mode) toggle CCs. Per the Line 6 spec the MIDI-addressable
# footswitches are FS1-FS5 (CC49-53) and FS7-FS11 (CC54-58). FS6 has no CC and
# CC59/CC60 are the EXP toe switch / looper record, so the mapping is NOT a
# contiguous CC49+switch stride - it is this explicit table with the FS6 gap.
_FS_CC = {
    1: 49, 2: 50, 3: 51, 4: 52, 5: 53,
    7: 54, 8: 55, 9: 56, 10: 57, 11: 58,
}
_FS_VALUES = ["1", "2", "3", "4", "5", "7", "8", "9", "10", "11"]

# Looper action -> (CC, value). Values follow the Helix convention of
# < 64 for the first sense and >= 64 for the second (e.g. CC60 value 64
# = record, value 0 = overdub).
_LOOPER_CC = {
    "record":      (60, 64),
    "overdub":     (60, 0),
    "play":        (61, 64),
    "stop":        (61, 0),
    "play_once":   (62, 64),
    "undo":        (63, 64),
    "reverse":     (65, 64),
    "forward":     (65, 0),
    "half_speed":  (66, 64),
    "full_speed":  (66, 0),
    "looper_on":   (67, 64),
    "looper_off":  (67, 0),
}

_LOOPER_VALUES = list(_LOOPER_CC.keys())

_TUNER_VALUE = {"on": 64, "off": 0}


MESSAGE_TYPES = {
    "helix_preset": {
        "label": "Select Preset",
        "params": {
            "setlist": {"type": "int", "min": 0, "max": 127, "default": 0, "label": "Setlist (Bank LSB)"},
            "preset":  {"type": "int", "min": 0, "max": 127, "default": 0, "label": "Preset (PC)"},
            "channel": {"type": "int", "min": 1, "max": 16,  "default": 1, "label": "Channel"},
        },
        "summary": "Preset {preset} setlist {setlist}",
    },
    "helix_snapshot": {
        "label": "Snapshot",
        "params": {
            "snapshot": {"type": "int", "min": 1, "max": 8,  "default": 1, "label": "Snapshot (1-8)"},
            "channel":  {"type": "int", "min": 1, "max": 16, "default": 1, "label": "Channel"},
        },
        "summary": "Snapshot {snapshot}",
    },
    "helix_fs": {
        "label": "Footswitch / Stomp Toggle",
        "params": {
            "switch":  {"type": "enum", "values": _FS_VALUES, "default": "1", "label": "Footswitch (FS6 has no CC)"},
            "state":   {"type": "enum", "values": ["on", "off"], "default": "on", "label": "State"},
            "channel": {"type": "int",  "min": 1, "max": 16, "default": 1, "label": "Channel"},
        },
        "summary": "FS{switch} {state}",
    },
    "helix_tap_tempo": {
        "label": "Tap Tempo",
        "params": {
            "channel": {"type": "int", "min": 1, "max": 16, "default": 1, "label": "Channel"},
        },
        "summary": "Tap",
    },
    "helix_tuner": {
        "label": "Tuner",
        "params": {
            "state":   {"type": "enum", "values": ["on", "off"], "default": "on", "label": "State"},
            "channel": {"type": "int", "min": 1, "max": 16, "default": 1, "label": "Channel"},
        },
        "summary": "Tuner {state}",
    },
    "helix_looper": {
        "label": "Looper",
        "params": {
            "action":  {"type": "enum", "values": _LOOPER_VALUES, "default": "record", "label": "Action"},
            "channel": {"type": "int", "min": 1, "max": 16, "default": 1, "label": "Channel"},
        },
        "summary": "Looper {action}",
    },
}


def dispatch(msg, midi):
    t = msg["type"]
    ch = msg.get("channel", 1)
    if t == "helix_preset":
        # Bank MSB is always 0 on Helix (setlist fits in the LSB); send
        # CC0 anyway so a partial bank-select state on the device is reset.
        midi.send_cc(ch, 0, 0)                       # CC0  Bank MSB
        midi.send_cc(ch, 32, int(msg.get("setlist", 0)))  # CC32 Bank LSB (setlist)
        midi.send_pc(ch, int(msg.get("preset", 0)))       # PC   preset
    elif t == "helix_snapshot":
        # CC69: value 0 = snapshot 1 ... value 7 = snapshot 8.
        snap = int(msg.get("snapshot", 1))
        midi.send_cc(ch, 69, max(0, min(7, snap - 1)))
    elif t == "helix_fs":
        # Look the footswitch up in the explicit CC table (FS6 absent);
        # 127 engages, 0 disengages.
        cc = _FS_CC.get(int(msg.get("switch", 1)))
        if cc is not None:
            value = 127 if msg.get("state", "on") == "on" else 0
            midi.send_cc(ch, cc, value)
    elif t == "helix_tap_tempo":
        # CC64 value 64 emulates a single TAP footswitch press.
        midi.send_cc(ch, 64, 64)
    elif t == "helix_tuner":
        state = msg.get("state", "on")
        if state in _TUNER_VALUE:
            # CC68: 64 shows the tuner, 0 hides it.
            midi.send_cc(ch, 68, _TUNER_VALUE[state])
    elif t == "helix_looper":
        cc, value = _LOOPER_CC[msg["action"]]
        midi.send_cc(ch, cc, value)


def update_context(msg, ctx):
    """Drive the shared tuner screen and mirror snapshot/preset state on
    the TFT. The core reads a generic `tuner` context field (on/off)."""
    t = msg.get("type")
    if t == "helix_tuner":
        ctx["tuner"] = msg.get("state", "on")
    elif t == "helix_snapshot":
        ctx["helix_snapshot"] = int(msg.get("snapshot", 1))
    elif t == "helix_preset":
        ctx["helix_preset"] = int(msg.get("preset", 0))


TFT_FIELDS = {
    "helix_preset":   {"label": "Helix preset (PC)",   "sample": 12},
    "helix_snapshot": {"label": "Helix snapshot (1-8)", "sample": 3},
}


# Default TFT layout for a Helix profile: scrolling patch name, bank and
# slot. Stacked top-left ~60px apart so size-5 labels never overlap (see
# ampero.py's DEFAULT_LAYOUT comment for the spacing rationale).
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
