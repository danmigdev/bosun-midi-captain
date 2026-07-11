"""Schemas for core message types. Universal MIDI primitives the firmware
always handles regardless of which plugins are loaded."""

CORE_MESSAGE_TYPES = {
    "cc": {
        "label": "Control Change",
        "params": {
            "channel": {"type": "int", "min": 1, "max": 16, "default": 1, "label": "Channel"},
            "cc":      {"type": "int", "min": 0, "max": 127, "default": 0, "label": "CC #"},
            "value":   {"type": "int", "min": 0, "max": 127, "default": 0, "label": "Value"},
        },
        "summary": "CC {cc}={value} ch {channel}",
    },
    "pc": {
        "label": "Program Change",
        "params": {
            "channel": {"type": "int", "min": 1, "max": 16, "default": 1, "label": "Channel"},
            "program": {"type": "int", "min": 0, "max": 127, "default": 0, "label": "Program"},
        },
        "summary": "PC {program} ch {channel}",
    },
    "note_on": {
        "label": "Note On",
        "params": {
            "channel":  {"type": "int", "min": 1, "max": 16, "default": 1, "label": "Channel"},
            "note":     {"type": "int", "min": 0, "max": 127, "default": 60, "label": "Note"},
            "velocity": {"type": "int", "min": 0, "max": 127, "default": 100, "label": "Velocity"},
        },
        "summary": "Note On {note} v{velocity} ch {channel}",
    },
    "note_off": {
        "label": "Note Off",
        "params": {
            "channel":  {"type": "int", "min": 1, "max": 16, "default": 1, "label": "Channel"},
            "note":     {"type": "int", "min": 0, "max": 127, "default": 60, "label": "Note"},
            "velocity": {"type": "int", "min": 0, "max": 127, "default": 64, "label": "Velocity"},
        },
        "summary": "Note Off {note} ch {channel}",
    },
    "delay": {
        "label": "Delay",
        "params": {
            "ms": {"type": "int", "min": 0, "max": 5000, "default": 100, "label": "Milliseconds"},
        },
        "summary": "Wait {ms}ms",
    },
    "captain_patch": {
        "label": "Switch Captain Patch",
        "params": {
            "bank": {"type": "int", "min": 1, "max": 99, "default": 1, "label": "Bank"},
            "slot": {"type": "int", "min": 1, "max": 10, "default": 1, "label": "Slot"},
        },
        "summary": "→ Captain {bank}/{slot}",
    },
    "captain_bank_step": {
        "label": "Step Captain Bank",
        "params": {
            "delta": {"type": "int", "min": -10, "max": 10, "default": 1, "label": "Delta (banks)"},
        },
        "summary": "Bank step {delta}",
    },
    # Preset preview: scroll a cursor across patches WITHOUT loading any of them
    # (no on_enter/on_exit, no device MIDI). Commit jumps for real; cancel (or an
    # inactivity timeout) returns to the current patch. Lets a player browse
    # banks like presets on an FX pedal and pick one, instead of firing MIDI for
    # every bank stepped through.
    "captain_preview_step": {
        "label": "Preview Step",
        "params": {
            "delta": {"type": "int", "min": -10, "max": 10, "default": 1, "label": "Delta"},
            "scope": {"type": "enum", "values": ["patch", "bank"], "default": "patch", "label": "Scope"},
        },
        "summary": "Preview {scope} {delta}",
    },
    "captain_preview_commit": {
        "label": "Preview Commit",
        "params": {},
        "summary": "Preview commit",
    },
    "captain_preview_cancel": {
        "label": "Preview Cancel",
        "params": {},
        "summary": "Preview cancel",
    },
    # Setlist navigation: step through the on-device setlist (an ordered list of
    # patches stored in device.json) and load the next/previous entry
    # immediately. Lets a player walk a gig in song order with one footswitch,
    # regardless of where each patch lives in the bank/slot grid.
    "captain_setlist_step": {
        "label": "Setlist Step",
        "params": {
            "delta": {"type": "int", "min": -10, "max": 10, "default": 1, "label": "Delta"},
        },
        "summary": "Setlist step {delta}",
    },
}
