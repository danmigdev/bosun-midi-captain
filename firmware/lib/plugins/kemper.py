"""Kemper Player MIDI plugin.

The Player addresses 125 rigs as 25 banks × 5 rigs (Browser Mode). There
is no "Performance" concept on the Player - that's a Profiler / Stage
thing. All references in this file talk about bank + rig.

Major control categories covered:
- Rig select  (bank + rig within bank, via Bank LSB + PC)
- Step rig    (next / prev within current bank - CC 48 / 49 browser mode)
- Stomp slots A/B/C/D + effect modules X/Mod/Delay/Reverb on/off
- Tuner toggle
- Tap tempo + direct BPM
- Wah / Morph / Volume pedal
- Looper

References:
  - Kemper Profiler MIDI Parameter Documentation 14.1
"""

NAME = "kemper_player"
VERSION = "1.0"
LABEL = "Kemper Player"


# Effect slot CCs - verified against the Profiler manual. The Player has
# four "stomp" slots A-D, plus the fixed effect modules X / Mod / Delay /
# Reverb in the standard signal chain.
# On/off CC per block. Values from PySwitch's CC_EFFECT_SLOT_ENABLE,
# verified on real Kempers. Note DLY=27 and REV=29 (NOT 26/28 - an earlier
# transcription error here made the Delay/Reverb switches send the wrong CC,
# so toggling them did nothing on the Player and they never mirrored state).
_EFFECT_CC = {
    "A":     17,
    "B":     18,
    "C":     19,
    "D":     20,
    "X":     22,
    "Mod":   24,
    "Delay": 27,
    "Reverb":29,
}

_LOOPER_CC = {
    "rec_play":   88,
    "stop_erase": 89,
    "trigger":    91,
    "reverse":    93,
    "half_speed": 94,
}

# The "fixed" input block (between the input and stomp A) holds several
# always-present effects, each individually switchable: Compressor, Noise
# Gate, Pure Booster, Wah, Transpose. Unlike the stomp slots A-D / modules
# X/Mod/Delay/Reverb these have NO simple on/off CC - they're only reachable
# via NRPN. The sequence per effect is:
#   CC99 = _FIXED_FX_PAGE (NRPN MSB / page)
#   CC98 = <effect LSB>   (NRPN LSB / address, from _FIXED_FX_LSB)
#   CC6  = 0              (Data Entry MSB)
#   CC38 = 1 | 0          (Data Entry LSB: 1 enables, 0 disables)
# Verified against the Kemper forum's Fixed-FX MIDI reference. Bidirectional
# sync does NOT mirror these back to a switch LED (the Player doesn't broadcast
# them on the pages the bilateral path watches), so a fixed-block toggle is
# fire-and-forget - the LED tracks whatever other block the binding also drives.
_FIXED_FX_PAGE = 5
_FIXED_FX_LSB = {
    "Compressor":   11,
    "Noise Gate":    6,
    "Pure Booster": 16,
    "Wah":          21,
    "Transpose":     1,
}

_EFFECT_VALUES = list(_EFFECT_CC.keys())
_LOOPER_VALUES = list(_LOOPER_CC.keys())
_FIXED_FX_VALUES = list(_FIXED_FX_LSB.keys())


MESSAGE_TYPES = {
    "kemper_rig": {
        "label": "Select Rig",
        "params": {
            "bank":    {"type": "int", "min": 1, "max": 25, "default": 1, "label": "Bank (1-25)"},
            "rig":     {"type": "int", "min": 1, "max": 5,  "default": 1, "label": "Rig in bank (1-5)"},
            "channel": {"type": "int", "min": 1, "max": 16, "default": 1, "label": "Channel"},
        },
        "summary": "Rig {bank}-{rig}",
    },
    "kemper_step_rig": {
        "label": "Step Rig",
        "params": {
            "direction": {"type": "enum", "values": ["next", "prev"], "default": "next", "label": "Direction"},
            "channel":   {"type": "int",  "min": 1, "max": 16, "default": 1, "label": "Channel"},
        },
        "summary": "Step rig {direction}",
    },
    "kemper_effect_toggle": {
        "label": "Effect Slot On/Off",
        "params": {
            "slot":    {"type": "enum", "values": _EFFECT_VALUES, "default": "A",  "label": "Slot"},
            "value":   {"type": "enum", "values": ["on", "off"],  "default": "on", "label": "Value"},
            "channel": {"type": "int",  "min": 1, "max": 16, "default": 1, "label": "Channel"},
        },
        "summary": "Slot {slot} {value}",
    },
    "kemper_fixed_toggle": {
        "label": "Fixed Block On/Off",
        "params": {
            "effect":  {"type": "enum", "values": _FIXED_FX_VALUES, "default": "Compressor", "label": "Fixed effect"},
            "value":   {"type": "enum", "values": ["on", "off"],    "default": "on",          "label": "Value"},
            "channel": {"type": "int",  "min": 1, "max": 16, "default": 1, "label": "Channel"},
        },
        "summary": "Fixed {effect} {value}",
    },
    "kemper_tuner": {
        "label": "Tuner",
        "params": {
            "state":   {"type": "enum", "values": ["on", "off"], "default": "on", "label": "State"},
            "channel": {"type": "int",  "min": 1, "max": 16, "default": 1, "label": "Channel"},
        },
        "summary": "Tuner {state}",
    },
    "kemper_tap_tempo": {
        "label": "Tap Tempo",
        "params": {
            "channel": {"type": "int", "min": 1, "max": 16, "default": 1, "label": "Channel"},
        },
        "summary": "Tap",
    },
    "kemper_set_tempo": {
        "label": "Set BPM",
        "params": {
            "bpm":     {"type": "int", "min": 40, "max": 250, "default": 120, "label": "BPM"},
            "channel": {"type": "int", "min": 1, "max": 16,  "default": 1,   "label": "Channel"},
        },
        "summary": "BPM {bpm}",
    },
    "kemper_morph": {
        "label": "Morph Pedal",
        "params": {
            "value":   {"type": "int", "min": 0, "max": 127, "default": 64, "label": "Value"},
            "channel": {"type": "int", "min": 1, "max": 16, "default": 1, "label": "Channel"},
        },
        "summary": "Morph {value}",
    },
    "kemper_wah": {
        "label": "Wah Pedal",
        "params": {
            "value":   {"type": "int", "min": 0, "max": 127, "default": 64, "label": "Value"},
            "channel": {"type": "int", "min": 1, "max": 16, "default": 1, "label": "Channel"},
        },
        "summary": "Wah {value}",
    },
    "kemper_volume": {
        "label": "Volume Pedal",
        "params": {
            "value":   {"type": "int", "min": 0, "max": 127, "default": 100, "label": "Value"},
            "channel": {"type": "int", "min": 1, "max": 16, "default": 1, "label": "Channel"},
        },
        "summary": "Volume {value}",
    },
    "kemper_looper": {
        "label": "Looper",
        "params": {
            "action":  {"type": "enum", "values": _LOOPER_VALUES, "default": "rec_play", "label": "Action"},
            "channel": {"type": "int",  "min": 1, "max": 16, "default": 1, "label": "Channel"},
        },
        "summary": "Looper {action}",
    },
    "kemper_rotary": {
        "label": "Rotary Speed",
        "params": {
            "value":   {"type": "enum", "values": ["slow", "fast"], "default": "slow", "label": "Speed"},
            "channel": {"type": "int",  "min": 1, "max": 16, "default": 1, "label": "Channel"},
        },
        "summary": "Rotary {value}",
    },
    "kemper_query_state": {
        "label": "Query block on/off state",
        "params": {
            "channel": {"type": "int", "min": 1, "max": 16, "default": 1, "label": "Channel (ignored - SYSEX is global)"},
        },
        "summary": "Query block states",
    },
}


# Reverse map of the outbound _EFFECT_CC table - used to translate inbound
# CCs from the Player back into block names when the unit echoes a toggle.
_CC_TO_BLOCK = {cc: name for name, cc in _EFFECT_CC.items()}


# (NRPN page, address) carrying each effect block's ON/OFF state in the
# Player's bidirectional broadcast. Verified by capturing a real Kemper
# Player (tools/kemper_probe.py), NOT just the Profiler doc:
#   - Stomps A-D and modules X / Mod report on/off at their slot page,
#     address 0x03  (e.g. stomp C = page 0x34 addr 0x03).
#   - The post effects DLY / REV are DIFFERENT: they report on/off at
#     dedicated pages 0x4A / 0x4B, address 0x02 (the slot pages 0x3C/0x3D
#     only carry their type/name, never the on/off - which is why the
#     Delay/Reverb switches never mirrored before this was corrected).
# Value is 14-bit: 0 = off, non-zero = on.
_BLOCK_ONOFF = {
    "A":      (0x32, 0x03),
    "B":      (0x33, 0x03),
    "C":      (0x34, 0x03),
    "D":      (0x35, 0x03),
    "X":      (0x38, 0x03),
    "Mod":    (0x3A, 0x03),
    "Delay":  (0x4A, 0x02),
    "Reverb": (0x4B, 0x02),
}
_ONOFF_TO_BLOCK = {pa: b for b, pa in _BLOCK_ONOFF.items()}

# Persistent cache of the last-known on/off of every effect block, keyed by
# block name. Seeded by the Player's full broadcast after the beacon handshake
# and kept current by the per-change deltas it sends afterwards. This mirrors
# how PySwitch keeps a persistent per-slot value that it never wipes: the
# Player only re-broadcasts blocks whose value CHANGED on a rig change, so the
# cache is the only place that holds the FULL picture. on_patch_loaded() reads
# it to repaint a freshly loaded patch's effect LEDs (the core reset them to
# off), so a block that stays ON across rigs no longer goes dark.
_BLOCK_STATE = {}

# Kemper SYSEX framing (excluding the F0/F7 bytes the parser strips).
# Outbound: we always identify as Player (product 0x02) and address all
# devices (device 0x7F = OMNI). Inbound: we tolerate any product/device
# value because the Kemper's "sensing" broadcast uses 0x00 0x00 in those
# slots regardless of which device sent it (per the docs).
_KEMPER_MFR = (0x00, 0x20, 0x33)
_KEMPER_PRODUCT_PLAYER = 0x02
_KEMPER_DEVICE_OMNI    = 0x7F

_FN_SINGLE_PARAM_REQUEST  = 0x41
_FN_SINGLE_PARAM_RESPONSE = 0x01
_FN_STRING_PARAM_RESPONSE = 0x03   # ASCII string response (rig name, ...)
_FN_EXTENDED              = 0x7E   # used for the bidirectional beacon

# NRPN address pages used by the bidirectional broadcast set (param set 2).
# These are the pages the Player auto-emits once we send the beacon.
_PAGE_STRINGS         = 0x00       # addr 0x01 = rig name (string response)
_PAGE_RIG_PARAMETERS  = 0x04       # addr 0x00 = BPM (single param, /64)
_PAGE_TUNER_DEVIANCE  = 0x7C       # addr 0x0F = 14-bit, 8192 = in tune
_PAGE_TUNER_NOTE      = 0x7D       # addr 0x54 = value % 12 -> note name
_PAGE_SYSTEM          = 0x7F       # addr 0x7E = tuner mode on/off

_ADDR_RIG_NAME       = 0x01
_ADDR_BPM            = 0x00
_ADDR_TUNER_NOTE     = 0x54
_ADDR_TUNER_DEVIANCE = 0x0F
_ADDR_TUNER_MODE     = 0x7E

# 12-tone names indexed by (tuner_note_value % 12). Matches the PySwitch
# reference default (German-style flats).
_NOTE_NAMES = ("C", "Db", "D", "Eb", "E", "F", "Gb", "G", "Ab", "A", "Bb", "B")

# Bidirectional protocol - without an explicit "beacon" subscribe message
# the Kemper Player doesn't broadcast effect-block state changes at all.
# After we send the beacon, the Player auto-broadcasts every change on
# the parameter set we subscribed to (effect on/off, rig name, tuner,
# tempo, …) and pings us with a sensing message (function 0x7E, addr
# page 0x7F) every ~500 ms. We must re-send the beacon every ~5 s as
# keep-alive (the lease we declare is 10 s, time_lease/2 = 0x05).
_BEACON_ADDR_PAGE  = 0x40
_BEACON_PARAM_SET  = 0x02
_BEACON_LEASE_DIV2 = 0x05
_BEACON_RESEND_MS  = 5000
_FLAGS_INIT = 0x23   # init=1 + sysex=1 + tunemode=1
_FLAGS_KEEPALIVE = 0x22   # sysex=1 + tunemode=1 (no init)

# Mutable plugin-level state. Note: a single Kemper attached per pedal
# is the only supported topology - no per-port disambiguation here.
# `published` holds the last value pushed to display_context for each
# bidir-driven field, so we skip update_context (and the TFT redraw it
# triggers) when nothing actually changed. The Player emits a sensing
# frame every ~500 ms and parameter broadcasts in bursts - de-duping
# here keeps the TFT refresh rate sane.
_BIDIR_STATE = {
    "last_beacon_ms": 0,
    "init_sent": False,
    "confirmed": False,
    "published": {},   # field name -> last published value
    # While >0 and in the future, a rig change is "settling": the Player is
    # still streaming the new rig's effect-state broadcast (slow + ragged,
    # ~400 ms, measured). During this window we update the block cache but do
    # NOT touch the LEDs - painting mid-burst shows an intermediate/stale value
    # (the random BOOST flash). tick() does ONE repaint from the settled cache
    # once the window passes. Outside the window (e.g. a plain effect toggle)
    # block deltas paint the LED live for instant feedback.
    "settle_until_ms": 0,
}

# How long after a rig-change broadcast starts (the rig-name frame) to keep
# suppressing live LED paints. Must exceed the burst length (~400 ms here).
_SETTLE_MS = 500

# How long after a LOCAL (bosun-initiated) patch/bank switch to treat an
# incoming rig PC as our own echo rather than an external change. Matches the
# core's switch_patch echo-suppression window so the two agree.
_ECHO_WINDOW_MS = 1200


# Plugin-level config schema - consumed by the editor's Settings page. The
# `key` is where the values live under device.json (so device.kemper.debug).
CONFIG_SCHEMA = {
    "key": "kemper",
    "label": "Kemper Player target",
    # Bilateral sync, the bidirectional beacon and auto-follow (rig + effects)
    # are ALWAYS on for a Kemper profile - they're not user options, so they no
    # longer appear here. The MIDI channel is a general device setting
    # (device.midi_channel), not Kemper-specific. Only `debug` remains, and it's
    # hidden from the GUI (flip it directly in device.json when troubleshooting).
    # Keeping one field ensures the `kemper` section is seeded into device.json,
    # which is what gates the bilateral sync to Kemper profiles.
    "fields": {
        "debug": {"type": "bool", "default": False, "hidden": True,
                  "label": "debug: trace received SYSEX to console"},
    },
}


# Per-rig LED color, derived from the Kemper Player "Bank-Farbcodes"
# chart (Upgrade Level III). The chart shows three LED columns per rig
# (Player has 3 indicator LEDs); we expose the left LED color here as
# the single representative color a bosun preset switch should display.
# Values are best-effort reads of the official chart - verify against
# your unit before trusting and extend the table as needed.
_RIG_FALLBACK = "#666666"
RIG_COLORS = {
    1:  "#3a8eff",   # blue
    2:  "#f5dc34",   # yellow
    3:  "#e54848",   # red
    4:  "#2a2a2a",   # black
    5:  "#3ecb6e",   # green
    # 6-10 in the chart appear to remain the "black" pattern of the
    # Level I/II box; treat them as dim until the table is filled in.
    11: "#3a8eff",
    12: "#f5dc34",
    13: "#e54848",
    14: "#3ecb6e",
    15: "#c08aff",   # purple
}


def rig_color(rig):
    """Return the LED color for a Kemper Player rig (1-125), or a neutral
    fallback if not in the table. Plugins / editor code use this to seed
    a preset's tft_color when the preset targets a specific rig."""
    return RIG_COLORS.get(int(rig), _RIG_FALLBACK)


def dispatch(msg, midi):
    t = msg["type"]
    ch = int(msg.get("channel", 1))

    if t == "kemper_rig":
        # Browser Mode: Bank LSB (CC 32) picks the bank, PC picks the rig
        # within that bank. Bank MSB stays at 0. A short pause between the
        # bank change and the PC lets the Player register the bank - without
        # it some firmware revisions latch the previous bank.
        import time as _time
        bank = int(msg.get("bank", 1))      # 1..25
        rig = int(msg.get("rig", 1))        # 1..5
        if bank < 1 or bank > 25 or rig < 1 or rig > 5:
            return
        # The Player addresses its 125 rigs as a FLAT list: Bank Select stays 0
        # and the Program Change IS the rig number (0..124). bosun's bank/rig is
        # only a 5-rig grouping for the UI, so the wire mapping is
        #   PC = (bank-1)*5 + (rig-1),  Bank LSB = 0.
        # Verified on hardware (tools, rig-name probe): Bank LSB = bank-1 lands
        # on the WRONG rig for bank >= 2 (e.g. bank 2 rig 4 -> "Crunch" instead
        # of the real rig at PC 8). Bank LSB is pinned to 0 unconditionally so a
        # stale non-zero bank on the Player always self-corrects.
        midi.send_cc(ch, 0, 0)              # Bank MSB
        midi.send_cc(ch, 32, 0)             # Bank LSB (flat rig list -> always 0)
        _time.sleep(0.005)                  # 5 ms settle
        midi.send_pc(ch, (bank - 1) * 5 + (rig - 1))   # PC = flat rig index 0..124

    elif t == "kemper_step_rig":
        # Browser Mode value 0: load next / previous rig (single step).
        cc = 48 if msg.get("direction", "next") == "next" else 49
        midi.send_cc(ch, cc, 0)

    elif t == "kemper_effect_toggle":
        cc = _EFFECT_CC.get(msg["slot"])
        if cc is not None:
            midi.send_cc(ch, cc, 127 if msg["value"] == "on" else 0)

    elif t == "kemper_fixed_toggle":
        lsb = _FIXED_FX_LSB.get(msg.get("effect"))
        if lsb is not None:
            # NRPN: select page + address, then Data Entry MSB/LSB. Order
            # matters - the Player latches the parameter from CC99/CC98 and
            # only acts on the CC6/CC38 data write.
            on = msg.get("value", "on") == "on"
            midi.send_cc(ch, 99, _FIXED_FX_PAGE)   # NRPN MSB (page)
            midi.send_cc(ch, 98, lsb)              # NRPN LSB (effect address)
            midi.send_cc(ch, 6, 0)                 # Data Entry MSB
            midi.send_cc(ch, 38, 1 if on else 0)   # Data Entry LSB

    elif t == "kemper_tuner":
        midi.send_cc(ch, 31, 127 if msg.get("state", "on") == "on" else 0)

    elif t == "kemper_tap_tempo":
        midi.send_cc(ch, 30, 127)

    elif t == "kemper_set_tempo":
        # The Player accepts CC 92 MSB + CC 93 LSB for BPM (msb*128 + lsb).
        bpm = max(40, min(250, int(msg.get("bpm", 120))))
        midi.send_cc(ch, 92, bpm // 128)
        midi.send_cc(ch, 93, bpm % 128)

    elif t == "kemper_morph":
        midi.send_cc(ch, 4, int(msg.get("value", 64)))

    elif t == "kemper_wah":
        midi.send_cc(ch, 1, int(msg.get("value", 64)))

    elif t == "kemper_volume":
        midi.send_cc(ch, 7, int(msg.get("value", 100)))

    elif t == "kemper_looper":
        cc = _LOOPER_CC.get(msg["action"])
        if cc is not None:
            midi.send_cc(ch, cc, 127)

    elif t == "kemper_rotary":
        midi.send_cc(ch, 47, 127 if msg.get("value") == "fast" else 0)

    elif t == "kemper_query_state":
        # Explicitly poll the On/Off state of every effect block. The normal
        # flow is the bidirectional beacon (see `tick`), but on a rig change
        # the Player only broadcasts the blocks that CHANGED, so a full
        # re-read is needed to repaint every LED (see _query_block_states).
        _query_block_states(midi)


def _query_block_states(midi):
    """Fire a SYSEX $41 request for the On/Off parameter of every effect
    block. While bidirectional is active the Player answers each with a $01
    single-param response (verified on real hardware), which on_midi_in
    mirrors onto the matching switch LEDs.

    This is the only reliable way to get the FULL block state: on a rig
    change the Player broadcasts on/off only for the blocks whose value
    differs from the previous rig (deltas - see
    tools/kemper_rigchange_probe.py). Blocks that stay ON across rigs are
    never re-sent, so after switch_patch's reset_all() clears the LEDs they
    would stay dark. Re-querying restores them."""
    for page, addr in _BLOCK_ONOFF.values():
        midi.send_sysex(_KEMPER_MFR + (
            _KEMPER_PRODUCT_PLAYER, _KEMPER_DEVICE_OMNI,
            _FN_SINGLE_PARAM_REQUEST, 0x00, page, addr,
        ))


def on_midi_in(port, channel, status, data, app):
    """Bilateral sync: when the Player echoes a rig change, effect block
    toggle, tuner state, or responds to a SYSEX state query, mirror it
    into the bosun. Only acts when the active profile's device.json has
    a `kemper` section (always on for Kemper profiles). The MIDI channel filter
    uses the general device setting; SYSEX is always processed (it's global)."""
    cfg = (app.device or {}).get("kemper")
    if cfg is None:
        return

    if status == 0xF0:
        _handle_sysex(data, app, cfg)
        return

    if channel != int((app.device or {}).get("midi_channel", 1)):
        return

    if status == 0xB0 and len(data) >= 2:
        cc, value = data[0], data[1]
        if cc in _CC_TO_BLOCK:
            block = _CC_TO_BLOCK[cc]
            on = value >= 64
            _BLOCK_STATE[block] = on        # keep the persistent cache current
            # Paint the LED live only when NOT settling a rig change (see
            # _BIDIR_STATE["settle_until_ms"]): mid-burst paints cause the flash.
            if app._now_ms() >= _BIDIR_STATE["settle_until_ms"]:
                for sw_name, binding in app.current_bindings():
                    if _binding_targets_block(binding, block):
                        app.set_switch_latched(sw_name, on)
                        break
        elif cc == 31:
            on = "on" if value >= 64 else "off"
            # Publish both the kemper_* field (legacy layouts) and the generic
            # `tuner` field the shared tuner screen keys off.
            app.update_context({"kemper_tuner": on, "tuner": on})
    elif status == 0xC0 and data:
        # Echo of a bosun-initiated rig/bank change: within the echo window of a
        # local switch, this PC is just the Player confirming what we just sent.
        # Resolving it via the INCOMING Bank LSB is unsafe here - our OUTBOUND
        # bank-select isn't recorded in _last_bank_lsb, so a bank step would
        # resolve against the stale previous bank and bounce us back. Treat it
        # as confirmation of the patch we already loaded (hits switch_patch's
        # echo-suppression branch: repaints effect LEDs, never reverts).
        if (app._now_ms() - getattr(app, "last_local_switch_ms", 0)) < _ECHO_WINDOW_MS:
            app.switch_patch(app.current_bank, app.current_slot,
                             source="midi_in", fire_on_enter=False)
            return
        # The Player addresses rigs as a flat list (Bank LSB 0, PC = rig-1), so
        # the incoming Program Change is the rig index 0..124. Decompose it back
        # into bosun's 5-rig bank/slot grouping. (Mirrors the outbound mapping
        # in dispatch: PC = (bank-1)*5 + (rig-1).)
        pc = data[0]
        rig = pc + 1                       # 1..125
        bank = pc // 5 + 1                  # 1..25
        rig_in_bank = pc % 5 + 1            # 1..5
        app.update_context({
            "kemper_rig":         rig,
            "kemper_bank":        bank,
            "kemper_rig_in_bank": rig_in_bank,
        })
        # Follow the rig with a patch switch so the bosun shows the new rig's
        # own bindings (effect layout, colors, name) - not just its number on
        # the TFT. Maps Kemper (bank, rig-in-bank) directly to bosun (bank,
        # slot), the convention every kemper patch's on_enter encodes. Uses
        # source="midi_in" so the 1.2 s echo-suppression window swallows the
        # Player's PC echo after a bosun-initiated change (no switch loop).
        # switch_patch no-ops cleanly if no patch exists at that slot. Without
        # this the bosun stayed on the boot patch and only that rig's effect
        # LEDs ever updated ("works only for acoustic").
        # fire_on_enter=False: the Player IS already on this rig, so DON'T
        # re-send the rig MIDI - that would make it reload and re-broadcast,
        # ping-ponging into a $41/echo storm that overruns the USB link and
        # drops the block replies (the real "still doesn't work" cause).
        app.switch_patch(bank, rig_in_bank, source="midi_in", fire_on_enter=False)


def _binding_targets_block(binding, block):
    """True if any action under this binding emits a kemper_effect_toggle
    for `block`. Used to decide which bosun switch should mirror an
    incoming Kemper CC."""
    for action in (binding or {}).get("actions", {}).values():
        for msg in action.get("messages", []):
            if msg.get("type") == "kemper_effect_toggle" and msg.get("slot") == block:
                return True
    return False


def _block_of_binding(binding):
    """Return the effect block a binding's kemper_effect_toggle targets, or
    None if the binding doesn't control a block."""
    for action in (binding or {}).get("actions", {}).values():
        for msg in action.get("messages", []):
            if msg.get("type") == "kemper_effect_toggle":
                return msg.get("slot")
    return None


def _apply_cache(app):
    """Repaint the current patch's effect-block LEDs from the persistent cache.
    The core resets every latched switch to off on a patch load, and the Player
    only re-broadcasts blocks whose on/off CHANGED on a rig change, so a block
    that stays ON across rigs would otherwise render dark. `_BLOCK_STATE` holds
    the full last-known state (seeded by the boot broadcast, kept current by
    deltas); we apply it here - the persistent-value model PySwitch uses. Only
    known blocks are touched; unknown ones keep the core's off default."""
    cfg = (app.device or {}).get("kemper")
    if cfg is None:
        return
    for sw_name, binding in app.current_bindings():
        block = _block_of_binding(binding)
        if block is not None and block in _BLOCK_STATE:
            app.set_switch_latched(sw_name, _BLOCK_STATE[block])


def on_patch_loaded(app):
    """Called by the core after a patch load. Paint effect LEDs from the cache -
    UNLESS a rig change is still settling (the Player's effect-state broadcast is
    mid-flight). During settling we skip the paint; tick() repaints once the
    window closes, so we never show an intermediate/stale value (the BOOST
    flash). When not settling (e.g. an editor reload) we paint immediately."""
    if app._now_ms() < _BIDIR_STATE["settle_until_ms"]:
        return
    _apply_cache(app)


def on_navigate(app, bank, slot):
    """A preset switch selected (bank, slot) but the core has no bosun patch
    there. Navigate the Player straight to that rig so every rig in the bank
    stays reachable from the preset row. `slot` is the rig within the bank
    (1-5); the Player addresses it as kemper_rig(bank, rig=slot)."""
    cfg = (app.device or {}).get("kemper")
    if cfg is None:
        return
    ch = int(cfg.get("midi_channel") or (app.device or {}).get("midi_channel") or 1)
    dispatch({"type": "kemper_rig", "bank": bank, "rig": slot, "channel": ch}, app.midi)
    app.update_context({
        "kemper_bank":        bank,
        "kemper_rig_in_bank": slot,
        "kemper_rig":         (bank - 1) * 5 + slot,
    })


# Kemper-specific bidirectional field -> generic tuner field the shared
# tuner screen (display._render_tuner) reads. We publish both so existing
# kemper_* layouts keep working while the generic screen also lights up.
_TUNER_ALIASES = {
    "kemper_tuner":          "tuner",
    "kemper_tuner_note":     "tuner_note",
    "kemper_tuner_deviance": "tuner_deviance",
}


def _add_tuner_aliases(updates):
    """Return `updates` with generic tuner fields mirrored in from any
    kemper_tuner* keys. Copies only when needed so the common (non-tuner)
    path allocates nothing."""
    extra = None
    for k, generic in _TUNER_ALIASES.items():
        if k in updates and generic not in updates:
            if extra is None:
                extra = dict(updates)
            extra[generic] = updates[k]
    return extra if extra is not None else updates


def _publish(app, updates):
    """Push field updates to display_context, de-duped against the last
    values we sent. Skips the update_context call entirely (and the TFT
    redraw it would trigger) when no field actually changed. Used by all
    bidirectional broadcasts - they fire frequently and unconditional
    publish-on-receive would burn the CPU on TFT renders."""
    updates = _add_tuner_aliases(updates)
    pub = _BIDIR_STATE["published"]
    fresh = {k: v for k, v in updates.items() if pub.get(k) != v}
    if not fresh:
        return
    pub.update(fresh)
    app.update_context(fresh)


def _decode_string(payload):
    """Best-effort ASCII decode of a Kemper string-response payload.
    Stops at the first 0x00 terminator and drops any byte outside the
    printable ASCII range so a glitched frame can't smuggle control
    chars onto the TFT."""
    chars = []
    for b in payload:
        if b == 0x00:
            break
        if 0x20 <= b < 0x7F:
            chars.append(chr(b))
    return "".join(chars).strip()


# Diagnostic SYSEX trace. When device.kemper.debug is true we print every
# inbound Kemper SYSEX frame (raw hex) to the console (COM3) so the real
# Player exchange can be captured without hardware probes - the editor uses
# the data CDC, the console is free for `print`. Capped so it can't spam
# forever. Toggle off (or omit) device.kemper.debug for normal use.
_TRACE = {"n": 0}


def _trace_rx(data, cfg):
    if not cfg.get("debug"):
        return
    if _TRACE["n"] >= 200:
        return
    _TRACE["n"] += 1
    try:
        print("KPRX " + " ".join("%02x" % (b & 0xFF) for b in data))
    except Exception:
        pass


def _handle_sysex(data, app, cfg):
    """Parse a Kemper SYSEX payload (without F0/F7) and react to it.

    Handles:
    - $7E (sensing): the keep-alive the Player sends every ~500 ms once
      bidirectional is established. We mark "connection alive" and
      surface it as `kemper_connected` on the display context.
    - $01 (single parameter response): emitted whenever a subscribed
      parameter changes. Effect blocks (pages 0x32-0x3D, addr 0x03)
      mirror switch state; rig parameters page 0x04 carries BPM;
      tuner mode (page 0x7F), tuner note (page 0x7D), tuner deviance
      (page 0x7C) feed the tuner UI fields.
    - $03 (string parameter response): currently only rig name
      (page 0x00, addr 0x01) - overwrites both `kemper_rig_name` and
      `patch_name` on the context (Kemper is the source of truth for
      what's loaded; bosun's preset name is the fallback).

    Manufacturer must be Kemper (00 20 33). Product / device bytes are
    accepted as-is - the Player's sensing frame fills them with zeros
    regardless of who's listening."""
    _trace_rx(data, cfg)
    if len(data) < 6 or data[0] != _KEMPER_MFR[0] or data[1] != _KEMPER_MFR[1] or data[2] != _KEMPER_MFR[2]:
        return
    # data[3] = product, data[4] = device - ignored (per docs the
    # sensing frame doesn't preserve them).
    fn = data[5]

    if fn == _FN_EXTENDED:
        # Sensing message - beacon was accepted, the Player is alive.
        _BIDIR_STATE["confirmed"] = True
        _publish(app, {"kemper_connected": "on"})
        return

    if fn == _FN_STRING_PARAM_RESPONSE and len(data) >= 9:
        # data: [00 20 33, prod, dev, 03, instance, page, addr, <ascii...>]
        page = data[7]
        addr = data[8]
        if page == _PAGE_STRINGS and addr == _ADDR_RIG_NAME:
            name = _decode_string(data[9:])
            if name:
                _publish(app, {"kemper_rig_name": name, "patch_name": name})
            # A rig-name frame marks the START of a rig-change broadcast. Open a
            # settle window: until it closes, block deltas update the cache but
            # don't paint LEDs (suppresses the mid-burst BOOST flash). tick()
            # repaints once from the settled cache when the window ends.
            # No $41 re-read - the Player streams the full block set, and the
            # cache + this settle give the right final picture (PySwitch model).
            _BIDIR_STATE["settle_until_ms"] = app._now_ms() + _SETTLE_MS
        return

    if fn != _FN_SINGLE_PARAM_RESPONSE or len(data) < 11:
        return
    # data: [00 20 33, prod, dev, 01, instance, page, addr, val_msb, val_lsb, ...]
    page = data[7]
    addr = data[8]
    value = (data[9] << 7) | (data[10] & 0x7F)

    if page == _PAGE_RIG_PARAMETERS and addr == _ADDR_BPM:
        # PySwitch convert_bpm: round(value / 64). The Player encodes BPM
        # as 14-bit in 1/64 BPM units (so 7680 -> 120 BPM).
        bpm = int(round(value / 64))
        _publish(app, {"kemper_bpm": bpm})
        return

    if page == _PAGE_SYSTEM and addr == _ADDR_TUNER_MODE:
        # The Player reports value 1 when the tuner is engaged and value 3
        # when it is NOT (browse/normal). The old `value != 0` test treated
        # the idle value 3 as "tuner on", so the tuner screen showed up
        # permanently. Only value 1 means the tuner is actually on.
        _publish(app, {"kemper_tuner": "on" if value == 1 else "off"})
        return

    if page == _PAGE_TUNER_NOTE and addr == _ADDR_TUNER_NOTE:
        _publish(app, {"kemper_tuner_note": _NOTE_NAMES[value % 12]})
        return

    if page == _PAGE_TUNER_DEVIANCE and addr == _ADDR_TUNER_DEVIANCE:
        _publish(app, {"kemper_tuner_deviance": value})
        return

    block = _ONOFF_TO_BLOCK.get((page, addr))
    if block is None:
        return                           # not an effect block on/off
    # 14-bit value: 0 = off, anything > 0 = on. The Player tends to use
    # raw 0 or 1 for switches; we tolerate the whole non-zero range.
    on = value != 0
    _BLOCK_STATE[block] = on              # keep the persistent cache current
    # Paint live only when NOT settling a rig change (see settle_until_ms);
    # mid-burst paints are what cause the flash. tick() repaints after settle.
    if app._now_ms() >= _BIDIR_STATE["settle_until_ms"]:
        for sw_name, binding in app.current_bindings():
            if _binding_targets_block(binding, block):
                app.set_switch_latched(sw_name, on)
                break


def tick(app, now_ms):
    """Periodic beacon - keeps the Kemper bidirectional subscription
    alive. First call fires the init beacon (flags 0x23); subsequent
    calls send the keep-alive (0x22) once every `_BEACON_RESEND_MS`.
    The lease we declare is 10 s (time_lease/2 = 0x05), so re-sending
    at 5 s keeps us comfortably inside it."""
    cfg = (app.device or {}).get("kemper")
    if cfg is None:
        return
    # Rig-change settle window just closed: repaint the effect LEDs once from
    # the now-complete cache (live paints were suppressed during the burst to
    # avoid the flash). Sits before the beacon's 5 s early-return so it runs
    # promptly. Guard so it only fires the one time the window expires.
    su = _BIDIR_STATE["settle_until_ms"]
    if su and now_ms >= su:
        _BIDIR_STATE["settle_until_ms"] = 0
        _apply_cache(app)
    init = not _BIDIR_STATE["init_sent"]
    if not init and now_ms - _BIDIR_STATE["last_beacon_ms"] < _BEACON_RESEND_MS:
        return
    flags = _FLAGS_INIT if init else _FLAGS_KEEPALIVE
    app.midi.send_sysex(_KEMPER_MFR + (
        _KEMPER_PRODUCT_PLAYER, _KEMPER_DEVICE_OMNI,
        _FN_EXTENDED, 0x00,
        _BEACON_ADDR_PAGE, _BEACON_PARAM_SET,
        flags, _BEACON_LEASE_DIV2,
    ))
    _BIDIR_STATE["last_beacon_ms"] = now_ms
    _BIDIR_STATE["init_sent"] = True


def update_context(msg, ctx):
    """Track Kemper state for the TFT display when the bosun itself
    triggers a rig change. Mirrors what the bilateral MIDI-in path
    writes when the Player echoes the change back."""
    t = msg.get("type")
    if t == "kemper_rig":
        bank = int(msg.get("bank", 1))               # 1..25
        rig = int(msg.get("rig", 1))                 # 1..5
        ctx["kemper_bank"] = bank
        ctx["kemper_rig_in_bank"] = rig
        ctx["kemper_rig"] = (bank - 1) * 5 + rig     # 1..125


TFT_FIELDS = {
    "kemper_rig":            {"label": "Current Kemper rig (1-125)",      "sample": 23},
    "kemper_bank":           {"label": "Kemper bank (1-25)",              "sample": 5},
    "kemper_rig_in_bank":    {"label": "Rig within bank (1-5)",           "sample": 3},
    "kemper_rig_name":       {"label": "Rig name (live from Kemper)",     "sample": "BRITISH PLEXI"},
    "kemper_bpm":            {"label": "Tempo (BPM)",                     "sample": 120},
    "kemper_tuner":          {"label": "Tuner state (on/off)",            "sample": "off"},
    "kemper_tuner_note":     {"label": "Tuner note (C/Db/D/.../B)",       "sample": "A"},
    "kemper_tuner_deviance": {"label": "Tuner deviance (0..16383, 8192=in tune)", "sample": 8192},
    "kemper_connected":      {"label": "Bidirectional link state (on/off)", "sample": "on"},
}

# Default TFT layout for a Kemper Player profile. Patch name on top,
# bosun bank/slot underneath, then the current Kemper rig number.
DEFAULT_LAYOUT = [
    {"field": "patch_name",
     "halign": "left", "valign": "top", "x": 0, "y": 0,
     "size": 5, "color": "#ffffff", "font": "system", "scroll": True},
    {"field": "bank",
     "halign": "left", "valign": "top", "x": 0, "y": 60,
     "size": 5, "color": "#9aa1ad", "font": "system",
     "prefix": "BANK ", "suffix": ""},
    {"field": "kemper_rig",
     "halign": "left", "valign": "top", "x": 0, "y": 120,
     "size": 5, "color": "#6fd99b", "font": "system",
     "prefix": "RIG ", "suffix": ""},
]
