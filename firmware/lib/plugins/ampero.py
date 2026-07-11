"""Hotone Ampero II Stage MIDI plugin.

CC mappings verified against the official MIDI Control Information List
(firmware V1.0.2; checked against V1.6.4 manual). See the
reference_ampero_ii_stage_midi memory note for the full chart."""

NAME = "ampero_ii_stage"
VERSION = "1.0"
LABEL = "Hotone Ampero II Stage"


_SLOT_CC = {
    "A1": 48, "A2": 49, "A3": 50, "A4": 51, "A5": 52, "A6": 53,
    "B1": 54, "B2": 55, "B3": 56, "B4": 57, "B5": 58, "B6": 59,
}

_LOOPER_CC = {
    "rec":          (63, 127),
    "overdub":      (63, 127),
    "play":         (64, 127),
    "stop":         (64, 0),
    "undo":         (67, 127),
    "redo":         (67, 127),
    "clear":        (68, 127),
    "half_speed":   (65, 0),
    "normal_speed": (65, 127),
    "reverse":      (66, 0),
    "forward":      (66, 127),
    "pre_post":     (71, 127),
    "chain_ab":     (72, 127),
}

_TUNER_VALUE  = {"on": 127, "off": 0}
_ENGAGE_VALUE = {"engage": 2, "analog_bypass": 0, "dsp_bypass": 1}
_QA_SELECT_CC = (16, 18, 20)
_QA_ADJUST_CC = (17, 19, 21)

_SLOT_VALUES   = list(_SLOT_CC.keys())
_LOOPER_VALUES = list(_LOOPER_CC.keys())


MESSAGE_TYPES = {
    "ampero_patch": {
        "label": "Select Patch (P##-#)",
        "params": {
            "patch":   {"type": "string", "pattern": "^P[0-6][0-9]-[1-5]$", "default": "P01-1", "label": "Patch"},
            "channel": {"type": "int", "min": 1, "max": 16, "default": 16, "label": "Channel"},
        },
        "summary": "Patch {patch}",
    },
    "ampero_scene": {
        "label": "Scene",
        "params": {
            "scene":   {"type": "int", "min": 1, "max": 5, "default": 1, "label": "Scene"},
            "channel": {"type": "int", "min": 1, "max": 16, "default": 16, "label": "Channel"},
        },
        "summary": "Scene {scene}",
    },
    "ampero_slot_toggle": {
        "label": "Effect Slot On/Off",
        "params": {
            "slot":    {"type": "enum", "values": _SLOT_VALUES, "default": "A1", "label": "Slot"},
            "value":   {"type": "enum", "values": ["on", "off"], "default": "on", "label": "Value"},
            "channel": {"type": "int", "min": 1, "max": 16, "default": 16, "label": "Channel"},
        },
        "summary": "Slot {slot} {value}",
    },
    "ampero_looper": {
        "label": "Looper",
        "params": {
            "action":  {"type": "enum", "values": _LOOPER_VALUES, "default": "rec", "label": "Action"},
            "channel": {"type": "int", "min": 1, "max": 16, "default": 16, "label": "Channel"},
        },
        "summary": "Looper {action}",
    },
    "ampero_tap_tempo": {
        "label": "Tap Tempo",
        "params": {
            "channel": {"type": "int", "min": 1, "max": 16, "default": 16, "label": "Channel"},
        },
        "summary": "Tap",
    },
    "ampero_set_tempo": {
        "label": "Set BPM",
        "params": {
            "bpm":     {"type": "int", "min": 40, "max": 300, "default": 120, "label": "BPM"},
            "channel": {"type": "int", "min": 1, "max": 16, "default": 16, "label": "Channel"},
        },
        "summary": "BPM {bpm}",
    },
    "ampero_tuner": {
        "label": "Tuner",
        "params": {
            "state":   {"type": "enum", "values": ["on", "off"], "default": "on", "label": "State"},
            "channel": {"type": "int", "min": 1, "max": 16, "default": 16, "label": "Channel"},
        },
        "summary": "Tuner {state}",
    },
    "ampero_engage": {
        "label": "Engage / Bypass",
        "params": {
            "state":   {"type": "enum", "values": ["engage", "analog_bypass", "dsp_bypass"], "default": "engage", "label": "State"},
            "channel": {"type": "int", "min": 1, "max": 16, "default": 16, "label": "Channel"},
        },
        "summary": "{state}",
    },
    "ampero_qa_param": {
        "label": "Quick Access Param",
        "params": {
            "param":   {"type": "enum", "values": [1, 2, 3], "default": 1, "label": "Param"},
            "action":  {"type": "enum", "values": ["step_up", "step_down", "set"], "default": "step_up", "label": "Action"},
            "value":   {"type": "int", "min": 0, "max": 127, "default": 64, "label": "Value", "if": {"action": "set"}},
            "channel": {"type": "int", "min": 1, "max": 16, "default": 16, "label": "Channel"},
        },
        "summary": "QA{param} {action}",
    },
}


def dispatch(msg, midi):
    t = msg["type"]
    ch = msg.get("channel", 16)
    if t == "ampero_patch":
        _ampero_patch(midi, ch, msg["patch"])
    elif t == "ampero_scene":
        midi.send_cc(ch, 25, int(msg["scene"]))
    elif t == "ampero_slot_toggle":
        midi.send_cc(ch, _SLOT_CC[msg["slot"]], 127 if msg["value"] == "on" else 0)
    elif t == "ampero_looper":
        cc, value = _LOOPER_CC[msg["action"]]
        midi.send_cc(ch, cc, value)
    elif t == "ampero_tap_tempo":
        midi.send_cc(ch, 76, 127)
    elif t == "ampero_set_tempo":
        bpm = int(msg["bpm"])
        if bpm <= 127:
            msb, lsb = 0, bpm
        elif bpm <= 255:
            msb, lsb = 1, bpm - 128
        else:
            msb, lsb = 2, bpm - 256
        midi.send_cc(ch, 74, msb)
        midi.send_cc(ch, 75, lsb)
    elif t == "ampero_tuner":
        state = msg.get("state", "on")
        if state in _TUNER_VALUE:
            midi.send_cc(ch, 60, _TUNER_VALUE[state])
    elif t == "ampero_engage":
        midi.send_cc(ch, 78, _ENGAGE_VALUE[msg["state"]])
    elif t == "ampero_qa_param":
        idx = int(msg["param"]) - 1
        action = msg["action"]
        if action == "step_up":
            midi.send_cc(ch, _QA_ADJUST_CC[idx], 127)
        elif action == "step_down":
            midi.send_cc(ch, _QA_ADJUST_CC[idx], 0)
        elif action == "set":
            midi.send_cc(ch, _QA_SELECT_CC[idx], int(msg["value"]))


def tuner_off(app):
    """Leave tuner mode on the Ampero (CC 60 = 0) when the user stomps a
    footswitch while the tuner splash is up. Only acts for an Ampero profile
    (device.ampero present)."""
    cfg = (app.device or {}).get("ampero")
    if cfg is None:
        return
    ch = int((app.device or {}).get("midi_channel") or 16)
    app.midi.send_cc(ch, 60, 0)


def _ampero_patch(midi, channel, patch):
    try:
        bank = int(patch[1:3])
        num = int(patch[-1])
    except (ValueError, IndexError):
        return
    flat = (bank - 1) * 5 + (num - 1)
    if flat < 0 or flat > 299:
        return
    midi.send_cc(channel, 0, flat // 128)
    midi.send_pc(channel, flat % 128)


def on_midi_in(port, channel, status, data, app):
    """Tier-β auto-follow: when the Ampero broadcasts the PC it just loaded
    (or the user pressed a footswitch on the device itself), look the
    (channel, bank MSB, PC) triple up in the captain's midi_learn table
    and load the matching captain patch. Off by default."""
    if status != 0xC0 or not data:
        return
    cfg = (app.device or {}).get("ampero") or {}
    if not cfg.get("auto_follow_pc"):
        return
    bank_msb = app.get_last_bank_msb(port, channel)
    pc = data[0]
    for entry in (app.midi_learn_table or {}).get("pc_to_patch", []):
        if (entry.get("channel") == channel
                and entry.get("bank_msb", 0) == bank_msb
                and entry.get("pc") == pc):
            target = entry.get("captain_patch", "")
            parts = target.split("/")
            if len(parts) == 2:
                try:
                    app.switch_patch(int(parts[0]), int(parts[1]), source="midi_in")
                except ValueError:
                    pass
            return


def update_context(msg, ctx):
    """Track Ampero state for the TFT display."""
    t = msg.get("type")
    if t == "ampero_patch":
        ctx["ampero_preset"] = msg.get("patch", "")
        # Ampero defaults to scene 1 on patch change (unless the user has
        # "Recall Scene" set otherwise on the device). Set context.ampero_scene
        # so the TFT shows "SCENE 1" right after loading. If the patch's
        # on_enter also fires an ampero_scene later, it overrides this.
        ctx["ampero_scene"] = 1
    elif t == "ampero_scene":
        ctx["ampero_scene"] = int(msg.get("scene", 1))
    elif t == "ampero_tuner":
        # Drive the shared full-screen tuner. The Ampero sends no pitch data
        # over MIDI, so the screen shows just the tuner frame (note "-",
        # centred needle) - still a clear "tuner engaged" indication.
        ctx["tuner"] = msg.get("state", "on")


TFT_FIELDS = {
    "ampero_preset": {"label": "Ampero preset (P##-#)", "sample": "P01-4"},
    "ampero_scene":  {"label": "Ampero scene (1-5)",    "sample": 3},
}

CONFIG_SCHEMA = {
    "key": "ampero",
    "label": "Ampero II Stage target",
    "fields": {
        "enabled":         {"type": "bool", "default": True, "label": "enabled"},
        "auto_follow_pc":  {"type": "bool", "default": True, "label": "Tier-β: follow Ampero patch via incoming PC"},
        "din_in_channel":  {"type": "int",  "default": 16, "min": 1, "max": 16, "label": "DIN in ch"},
        "din_out_channel": {"type": "int",  "default": 16, "min": 1, "max": 16, "label": "DIN out ch"},
        "usb_out_channel": {"type": "int",  "default": 1,  "min": 1, "max": 16, "label": "USB out ch"},
    },
}


# Setup recipe - drives the "Ampero Setup" page in the editor. The page
# scans every captain patch for an `ampero_patch` message in on_enter and,
# for each one found, walks the user through configuring the matching
# preset on the Ampero so it broadcasts its PC back over MIDI (required
# for the tier-β auto-follow loop).
#
# The structure is declarative so other plugins can ship their own recipe
# without touching the editor - see PluginRecipe.svelte for the
# interpreter. `pc_layout` is what makes this Ampero-specific: the device
# addresses up to 300 patches as bank_msb (0..2) + PC (0..127), 5 per
# bank. Plugins whose preset string doesn't decompose into MIDI bank/PC
# can omit the pc_layout block - the page then just lists the references
# without computing the MIDI table.
RECIPE_SCHEMA = {
    "id": "ampero_setup",
    "label": "Ampero Setup",
    "icon": "♪",
    "target_message_type": "ampero_patch",
    "preset_field": "patch",
    "channel_field": "channel",
    "channel_default": 16,
    "hint": ("For β auto-follow to work (Ampero footswitch → Captain "
             "follows automatically), each Ampero preset must be configured to "
             "broadcast its PC over MIDI when loaded. Use the recipes below - "
             "one per Captain patch."),
    "missing_message": ("This Captain patch's on_enter doesn't contain an "
                        "ampero_patch message. Add one to its on_enter macro "
                        "so the Ampero gets told which preset to load."),
    "instructions": ("On the Ampero, load preset {preset} and open Patch MIDI "
                     "(System Setup → MIDI → Patch MIDI):"),
    "save_note": "Then Hold Save on the Ampero. Repeat for each preset.",
    "pc_layout": {
        "preset_regex": r"^P(\d{1,2})-([1-5])$",
        "groups": [
            {"name": "bank", "min": 1, "max": 60},
            {"name": "slot", "min": 1, "max": 5},
        ],
        "index_formula": "(bank - 1) * 5 + (slot - 1)",
        "pc_max": 128,
        "bank_msb_label": "CC 0 (Bank MSB)",
        "pc_label": "PC",
    },
}

# Default TFT layout for an Ampero II Stage profile. All labels top-left
# aligned, stacked vertically with enough gap to never overlap regardless
# of text length. terminalio.FONT renders at 12*scale pixels tall, so a
# size-2 label occupies ~24px of vertical space; we leave ~30px between
# adjacent y offsets.
DEFAULT_LAYOUT = [
    {"field": "patch_name",
     "halign": "left", "valign": "top", "x": 0, "y": 0,
     "size": 5, "color": "#ffffff", "font": "system", "scroll": True},
    {"field": "bank",
     "halign": "left", "valign": "top", "x": 0, "y": 60,
     "size": 5, "color": "#9aa1ad", "font": "system",
     "prefix": "BANK ", "suffix": ""},
    {"field": "ampero_preset",
     "halign": "left", "valign": "top", "x": 0, "y": 120,
     "size": 5, "color": "#6fd99b", "font": "system"},
    {"field": "ampero_scene",
     "halign": "left", "valign": "top", "x": 0, "y": 180,
     "size": 5, "color": "#e8ce6f", "font": "system",
     "prefix": "SCENE ", "suffix": ""},
]
