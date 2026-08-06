"""HeadRush Core MIDI plugin.

MIDI implementation verified against the official HeadRush Core User Guide
v5.1.0. The Core receives MIDI CC and PC messages on its 5-pin DIN MIDI
Input or class-compliant USB-A MIDI host port (Omni or a single channel 1-16
configured in Global Settings > MIDI).

Bidirectional capabilities and limitations
------------------------------------------
Unlike the Kemper (which broadcasts full state via SYSEX and echoes every
block toggle), the HeadRush Core is mostly send-only. It has NO SYSEX
implementation, does not echo block states, and does not broadcast tuner or
rig-name changes. The only automatic inbound stream available is:

  Program Change on rig load -- when "Prog Change Send" is enabled in
  Global Settings > MIDI and each rig has a MIDI PROG number assigned
  (Hardware Assign), the Core transmits a PC whenever a rig is loaded
  (regardless of whether the load came from MIDI, a footswitch, or the
  touchscreen).

This is enough for tier-beta auto-follow: Bosun listens for the PC, looks
up the matching captain patch in the midi_learn table, and switches to it.
Block/tuner/expression state can never be mirrored because the Core never
broadcasts it.

If the user also configures per-rig MIDI Out commands (up to 5 messages on
rig load, scene activation, or footswitch press), those will arrive as
generic MIDI events and can be wired up via midi_learn like any other
inbound CC/PC/Note.

Control categories covered
--------------------------
  - Rig load       (PC 1-128)
  - Rig step       (prev / next via CC 16 / 17)
  - Bank step      (prev / next via CC 18 / 19)
  - Scenes 1-10    (CC 21-30)
  - Block 1-14 toggle (CC 75-88)
  - Footswitch 1-5 press / release (CC 49-53, value 127 / 0)
  - Expression pedal (CC 1, value 0-127)
  - Expression pedal A/B toggle (CC 14)
  - Looper         (CC 65-74)
  - Drum machine   (CC 31-47)
  - Tempo          (CC 12 / 13 / 64)
  - Footswitch mode (CC 94-98)
  - Practice tool  (CC 102-112)
  - Misc toggles   (hands-free, looper page, lock screen, mic dry)
  - Tuner          (CC 92, toggle)
"""

NAME = "headrush_core"
VERSION = "1.0"
LABEL = "HeadRush Core"


# -- CC lookup tables -------------------------------------------------------
# Most HeadRush Core CCs are "triggers": any data value 0-127 activates
# them. Only expression pedal (CC 1) and footswitch 1-5 (CC 49-53)
# interpret the value. For simplicity we always send 127 for triggers.

_SCENE_CC = {i: 20 + i for i in range(1, 11)}          # scene 1-10 -> CC 21-30

_BLOCK_CC = {i: 74 + i for i in range(1, 15)}           # block 1-14 -> CC 75-88

_FS_MODE_CC = {                                         # CC 94-98
    "stomp":   94,
    "hybrid":  95,
    "setlist": 96,
    "rig":     97,
    "5rig":    98,
}

_LOOPER_CC = {                                          # CC 65-74
    "half_speed":   65,
    "double_speed": 66,
    "half_loop":    67,
    "double_loop":  68,
    "start_stop":   69,
    "record":       70,
    "insert":       71,
    "peel":         72,
    "mute":         73,
    "reverse":      74,
}

_DRUM_CC = {                                            # CC 31-47
    "open_close":     31,
    "kit_prev":       32,
    "kit_next":       33,
    "style_prev":     34,
    "style_next":     35,
    "var_prev":       36,
    "var_next":       37,
    "volume_down":    38,
    "volume_up":      39,
    "intensity_down": 40,
    "intensity_up":   41,
    "play_stop":      42,
    "fill":           43,
    "next_bridge":    44,
    "outro":          45,
    "mute":           46,
    "accent":         47,
}

_PRACTICE_CC = {                                        # CC 102-112
    "open_close":  102,
    "play_pause":  103,
    "stop":        104,
    "volume_down": 105,
    "volume_up":   106,
    "loop_in":     107,
    "loop_out":    108,
    "speed_down":  109,
    "speed_up":    110,
    "pitch_down":  111,
    "pitch_up":    112,
}

_MISC_CC = {                                            # single-CC toggles
    "hands_free":   90,
    "looper_page":  91,
    "lock_screen":  93,
    "mic_dry":      89,
}

# Enum value lists for MESSAGE_TYPES params
_LOOPER_VALUES    = list(_LOOPER_CC.keys())
_DRUM_VALUES      = list(_DRUM_CC.keys())
_PRACTICE_VALUES  = list(_PRACTICE_CC.keys())
_FS_MODE_VALUES   = list(_FS_MODE_CC.keys())
_MISC_VALUES      = list(_MISC_CC.keys())


MESSAGE_TYPES = {
    "headrush_rig": {
        "label": "Load Rig",
        "params": {
            "rig":     {"type": "int", "min": 1, "max": 128, "default": 1, "label": "Rig (1-128)"},
            "channel": {"type": "int", "min": 1, "max": 16,  "default": 1, "label": "Channel"},
        },
        "summary": "Rig {rig}",
    },
    "headrush_rig_step": {
        "label": "Step Rig",
        "params": {
            "direction": {"type": "enum", "values": ["up", "down"], "default": "up", "label": "Direction"},
            "channel":   {"type": "int",  "min": 1, "max": 16, "default": 1, "label": "Channel"},
        },
        "summary": "Rig {direction}",
    },
    "headrush_bank_step": {
        "label": "Step Bank",
        "params": {
            "direction": {"type": "enum", "values": ["up", "down"], "default": "up", "label": "Direction"},
            "channel":   {"type": "int",  "min": 1, "max": 16, "default": 1, "label": "Channel"},
        },
        "summary": "Bank {direction}",
    },
    "headrush_scene": {
        "label": "Scene",
        "params": {
            "scene":   {"type": "int",  "min": 1, "max": 10,  "default": 1, "label": "Scene (1-10)"},
            "channel": {"type": "int",  "min": 1, "max": 16,  "default": 1, "label": "Channel"},
        },
        "summary": "Scene {scene}",
    },
    "headrush_block": {
        "label": "Block On/Off",
        "params": {
            "block":   {"type": "int",   "min": 1, "max": 14, "default": 1, "label": "Block (1-14)"},
            "state":   {"type": "enum",  "values": ["on", "off"], "default": "on", "label": "State"},
            "channel": {"type": "int",   "min": 1, "max": 16, "default": 1, "label": "Channel"},
        },
        "summary": "Block {block} {state}",
    },
    "headrush_footswitch": {
        "label": "Footswitch Press/Release",
        "params": {
            "fs":      {"type": "int",   "min": 1, "max": 5,   "default": 1, "label": "Footswitch (1-5)"},
            "action":  {"type": "enum",  "values": ["press", "release"], "default": "press", "label": "Action"},
            "channel": {"type": "int",   "min": 1, "max": 16,  "default": 1, "label": "Channel"},
        },
        "summary": "FS {fs} {action}",
    },
    "headrush_expression": {
        "label": "Expression Pedal",
        "params": {
            "value":   {"type": "int",  "min": 0, "max": 127, "default": 64, "label": "Value (0-127)"},
            "channel": {"type": "int",  "min": 1, "max": 16,  "default": 1,  "label": "Channel"},
        },
        "summary": "Exp {value}",
    },
    "headrush_pedal_switch": {
        "label": "Expression Pedal A/B Toggle",
        "params": {
            "channel": {"type": "int", "min": 1, "max": 16, "default": 1, "label": "Channel"},
        },
        "summary": "Exp pedal A/B toggle",
    },
    "headrush_looper": {
        "label": "Looper",
        "params": {
            "action":  {"type": "enum", "values": _LOOPER_VALUES, "default": "start_stop", "label": "Action"},
            "channel": {"type": "int",  "min": 1, "max": 16, "default": 1, "label": "Channel"},
        },
        "summary": "Looper {action}",
    },
    "headrush_drums": {
        "label": "Drum Machine",
        "params": {
            "action":  {"type": "enum", "values": _DRUM_VALUES, "default": "play_stop", "label": "Action"},
            "channel": {"type": "int",  "min": 1, "max": 16, "default": 1, "label": "Channel"},
        },
        "summary": "Drums {action}",
    },
    "headrush_tempo": {
        "label": "Tempo",
        "params": {
            "action":  {"type": "enum", "values": ["tap", "increase", "decrease"], "default": "tap", "label": "Action"},
            "channel": {"type": "int",  "min": 1, "max": 16, "default": 1, "label": "Channel"},
        },
        "summary": "Tempo {action}",
    },
    "headrush_tuner": {
        "label": "Tuner",
        "params": {
            "state":   {"type": "enum", "values": ["on", "off"], "default": "on", "label": "State"},
            "channel": {"type": "int",  "min": 1, "max": 16, "default": 1, "label": "Channel"},
        },
        "summary": "Tuner {state}",
    },
    "headrush_fs_mode": {
        "label": "Footswitch Mode",
        "params": {
            "mode":    {"type": "enum", "values": _FS_MODE_VALUES, "default": "stomp", "label": "Mode"},
            "channel": {"type": "int",  "min": 1, "max": 16, "default": 1, "label": "Channel"},
        },
        "summary": "FS mode {mode}",
    },
    "headrush_practice": {
        "label": "Practice Tool",
        "params": {
            "action":  {"type": "enum", "values": _PRACTICE_VALUES, "default": "play_pause", "label": "Action"},
            "channel": {"type": "int",  "min": 1, "max": 16, "default": 1, "label": "Channel"},
        },
        "summary": "Practice {action}",
    },
    "headrush_misc": {
        "label": "Misc Action",
        "params": {
            "action":  {"type": "enum", "values": _MISC_VALUES, "default": "hands_free", "label": "Action"},
            "channel": {"type": "int",  "min": 1, "max": 16, "default": 1, "label": "Channel"},
        },
        "summary": "{action}",
    },
}


# -- dispatch ----------------------------------------------------------------

def dispatch(msg, midi):
    t = msg["type"]
    ch = msg.get("channel", 1)

    if t == "headrush_rig":
        rig = int(msg["rig"])
        midi.send_pc(ch, rig - 1)                  # PC 0-127 -> rig 1-128

    elif t == "headrush_rig_step":
        direction = msg.get("direction", "up")
        midi.send_cc(ch, 17 if direction == "up" else 16, 127)

    elif t == "headrush_bank_step":
        direction = msg.get("direction", "up")
        midi.send_cc(ch, 19 if direction == "up" else 18, 127)

    elif t == "headrush_scene":
        scene = int(msg["scene"])
        if 1 <= scene <= 10:
            midi.send_cc(ch, _SCENE_CC[scene], 127)

    elif t == "headrush_block":
        block = int(msg["block"])
        value = 127 if msg.get("state", "on") == "on" else 0
        if 1 <= block <= 14:
            midi.send_cc(ch, _BLOCK_CC[block], value)

    elif t == "headrush_footswitch":
        fs = int(msg["fs"])
        action = msg.get("action", "press")
        value = 127 if action == "press" else 0
        if 1 <= fs <= 5:
            midi.send_cc(ch, 48 + fs, value)        # CC 49-53

    elif t == "headrush_expression":
        midi.send_cc(ch, 1, int(msg["value"]))

    elif t == "headrush_pedal_switch":
        # CC 14 toggles between expression pedal A/B assignments
        midi.send_cc(ch, 14, 127)

    elif t == "headrush_looper":
        cc = _LOOPER_CC[msg["action"]]
        midi.send_cc(ch, cc, 127)

    elif t == "headrush_drums":
        cc = _DRUM_CC[msg["action"]]
        midi.send_cc(ch, cc, 127)

    elif t == "headrush_tempo":
        action = msg.get("action", "tap")
        if action == "tap":
            midi.send_cc(ch, 64, 127)               # tap tempo
        elif action == "increase":
            midi.send_cc(ch, 13, 127)               # global tempo +
        elif action == "decrease":
            midi.send_cc(ch, 12, 127)               # global tempo -

    elif t == "headrush_tuner":
        # CC 92 toggles the tuner -- any data value works
        midi.send_cc(ch, 92, 127)

    elif t == "headrush_fs_mode":
        cc = _FS_MODE_CC[msg["mode"]]
        midi.send_cc(ch, cc, 127)

    elif t == "headrush_practice":
        cc = _PRACTICE_CC[msg["action"]]
        midi.send_cc(ch, cc, 127)

    elif t == "headrush_misc":
        cc = _MISC_CC[msg["action"]]
        midi.send_cc(ch, cc, 127)


# -- context tracking (for TFT display) -------------------------------------

def update_context(msg, ctx):
    """Track rig and scene references so the TFT can display them."""
    t = msg.get("type")
    if t == "headrush_rig":
        ctx["headrush_rig"] = int(msg.get("rig", 1))
    elif t == "headrush_scene":
        ctx["headrush_scene"] = int(msg.get("scene", 1))


# -- inbound MIDI: auto-follow rig changes ----------------------------------

def on_midi_in(port, channel, status, data, app):
    """Tier-beta auto-follow: when the HeadRush Core sends a PC on rig load
    (requires "Prog Change Send" enabled in Global Settings > MIDI and a
    MIDI PROG number assigned to the rig), look the (channel, PC) pair up
    in the captain's midi_learn table and load the matching captain patch.

    The Core does NOT send bank MSB before PC, so we only match on channel
    and PC number. Bank MSB in the midi_learn table must be 0 for the
    lookup to succeed."""
    if status != 0xC0 or not data:
        return
    cfg = (app.device or {}).get("headrush_core") or {}
    if not cfg.get("auto_follow_pc"):
        return
    pc = data[0]
    for entry in (app.midi_learn_table or {}).get("pc_to_patch", []):
        if (entry.get("channel") == channel
                and entry.get("bank_msb", 0) == 0
                and entry.get("pc") == pc):
            target = entry.get("captain_patch", "")
            parts = target.split("/")
            if len(parts) == 2:
                try:
                    app.switch_patch(int(parts[0]), int(parts[1]), source="midi_in")
                except ValueError:
                    pass
            return


# -- tuner hook --------------------------------------------------------------

def tuner_off(app):
    """Send a second tuner toggle (CC 92) to close the tuner on the Core.
    Because the Core uses a single CC as a toggle (no separate on/off),
    sending the same CC again will close an open tuner. If the tuner is
    already closed this will open it instead -- there is no way to query
    the Core's tuner state over MIDI, so this is best-effort."""
    cfg = (app.device or {}).get("headrush_core")
    if cfg is None:
        return
    ch = int((app.device or {}).get("midi_channel") or 1)
    app.midi.send_cc(ch, 92, 127)


# -- self-description for the editor -----------------------------------------

TFT_FIELDS = {
    "headrush_rig":   {"label": "HeadRush rig (1-128)", "sample": 42},
    "headrush_scene": {"label": "HeadRush scene (1-10)", "sample": 3},
}

DEFAULT_LAYOUT = [
    {"field": "patch_name",
     "halign": "left", "valign": "top", "x": 0, "y": 0,
     "size": 5, "color": "#ffffff", "font": "system", "scroll": True},
    {"field": "bank",
     "halign": "left", "valign": "top", "x": 0, "y": 60,
     "size": 5, "color": "#9aa1ad", "font": "system",
     "prefix": "BANK ", "suffix": ""},
    {"field": "headrush_rig",
     "halign": "left", "valign": "top", "x": 0, "y": 120,
     "size": 5, "color": "#6fd99b", "font": "system",
     "prefix": "RIG ", "suffix": ""},
    {"field": "headrush_scene",
     "halign": "left", "valign": "top", "x": 0, "y": 180,
     "size": 5, "color": "#e8ce6f", "font": "system",
     "prefix": "SCENE ", "suffix": ""},
]

CONFIG_SCHEMA = {
    "key": "headrush_core",
    "label": "HeadRush Core target",
    "fields": {
        "enabled":         {"type": "bool", "default": True, "label": "enabled"},
        "auto_follow_pc":  {"type": "bool", "default": True,
                            "label": "Tier-beta: follow HeadRush rig via incoming PC"},
        "din_in_channel":  {"type": "int",  "default": 1,  "min": 1, "max": 16,
                            "label": "DIN in ch"},
        "din_out_channel": {"type": "int",  "default": 1,  "min": 1, "max": 16,
                            "label": "DIN out ch"},
        "usb_out_channel": {"type": "int",  "default": 1,  "min": 1, "max": 16,
                            "label": "USB out ch"},
    },
}
