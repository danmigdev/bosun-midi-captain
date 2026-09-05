NAME = "kemper_player"
VERSION = "1.0"
LABEL = "Kemper Player"


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

_FIXED_FX_PAGE = 5
_FIXED_FX_LSB = {
    "Compressor":   11,
    "Noise Gate":    6,
    "Pure Booster": 16,
    "Wah":          21,
    "Transpose":     1,
}

# Slot indexes also address _WAH_TYPE_PAGES and the Wah-state bitmaps.
# CircuitPython dictionary iteration does not preserve this physical order.
_EFFECT_VALUES = ("A", "B", "C", "D", "X", "Mod", "Delay", "Reverb")
_LOOPER_VALUES = tuple(_LOOPER_CC.keys())
_FIXED_FX_VALUES = tuple(_FIXED_FX_LSB.keys())

_CHANNEL_PARAM = {"type": "int", "min": 1, "max": 16, "default": 1, "label": "Channel"}
_ON_OFF_PARAM = {"type": "enum", "values": ("on", "off"), "default": "on", "label": "Value"}


MESSAGE_TYPE_NAMES = (
    'kemper_rig',
    'kemper_step_rig',
    'kemper_effect_toggle',
    'kemper_fixed_toggle',
    'kemper_tuner',
    'kemper_tap_tempo',
    'kemper_set_tempo',
    'kemper_morph',
    'kemper_morph_trigger',
    'kemper_wah',
    'kemper_volume',
    'kemper_looper',
    'kemper_rotary',
    'kemper_query_state',
)


def manifest_message_types():
    """Allocate editor schemas only on demand, never for MIDI registration."""
    return {
        "kemper_rig": {
            "label": "Select Rig",
            "params": {
                "bank":    {"type": "int", "min": 1, "max": 25, "default": 1, "label": "Bank (1-25)"},
                "rig":     {"type": "int", "min": 1, "max": 5,  "default": 1, "label": "Rig in bank (1-5)"},
                "channel": _CHANNEL_PARAM,
            },
            "summary": "Rig {bank}-{rig}",
        },
        "kemper_step_rig": {
            "label": "Step Rig",
            "params": {
                "direction": {"type": "enum", "values": ["next", "prev"], "default": "next", "label": "Direction"},
                "channel":   _CHANNEL_PARAM,
            },
            "summary": "Step rig {direction}",
        },
        "kemper_effect_toggle": {
            "label": "Effect Slot On/Off",
            "params": {
                "slot":    {"type": "enum", "values": _EFFECT_VALUES, "default": "A",  "label": "Slot"},
                "value":   _ON_OFF_PARAM,
                "channel": _CHANNEL_PARAM,
            },
            "summary": "Slot {slot} {value}",
        },
        "kemper_fixed_toggle": {
            "label": "Fixed Block On/Off",
            "params": {
                "effect":  {"type": "enum", "values": _FIXED_FX_VALUES, "default": "Compressor", "label": "Fixed effect"},
                "value":   _ON_OFF_PARAM,
                "channel": _CHANNEL_PARAM,
            },
            "summary": "Fixed {effect} {value}",
        },
        "kemper_tuner": {
            "label": "Tuner",
            "params": {
                "state":   {"type": "enum", "values": ["on", "off"], "default": "on", "label": "State"},
                "channel": _CHANNEL_PARAM,
            },
            "summary": "Tuner {state}",
        },
        "kemper_tap_tempo": {
            "label": "Tap Tempo",
            "params": {
                "channel": _CHANNEL_PARAM,
            },
            "summary": "Tap",
        },
        "kemper_set_tempo": {
            "label": "Set BPM",
            "params": {
                "bpm":     {"type": "int", "min": 40, "max": 250, "default": 120, "label": "BPM"},
                "channel": _CHANNEL_PARAM,
            },
            "summary": "BPM {bpm}",
        },
        "kemper_morph": {
            "label": "Morph Pedal",
            "params": {
                "value":   {"type": "int", "min": 0, "max": 127, "default": 64, "label": "Value"},
                "channel": _CHANNEL_PARAM,
            },
            "summary": "Morph {value}",
        },
        "kemper_morph_trigger": {
            "label": "Morph Trigger",
            "params": {
                "state":   {"type": "enum", "values": ["on", "off"], "default": "on", "label": "State"},
                "channel": _CHANNEL_PARAM,
            },
            "summary": "Morph {state}",
        },
        "kemper_wah": {
            "label": "Wah Pedal",
            "params": {
                "value":   {"type": "int", "min": 0, "max": 127, "default": 64, "label": "Value"},
                "channel": _CHANNEL_PARAM,
            },
            "summary": "Wah {value}",
        },
        "kemper_volume": {
            "label": "Volume Pedal",
            "params": {
                "value":   {"type": "int", "min": 0, "max": 127, "default": 100, "label": "Value"},
                "channel": _CHANNEL_PARAM,
            },
            "summary": "Volume {value}",
        },
        "kemper_looper": {
            "label": "Looper",
            "params": {
                "action":  {"type": "enum", "values": _LOOPER_VALUES, "default": "rec_play", "label": "Action"},
                "channel": _CHANNEL_PARAM,
            },
            "summary": "Looper {action}",
        },
        "kemper_rotary": {
            "label": "Rotary Speed",
            "params": {
                "value":   {"type": "enum", "values": ["slow", "fast"], "default": "slow", "label": "Speed"},
                "channel": _CHANNEL_PARAM,
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


def _block_for_cc(cc):
    for name, value in _EFFECT_CC.items():
        if value == cc:
            return name
    return None


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
def _block_for_param(page, addr):
    target = (page, addr)
    for block, param in _BLOCK_ONOFF.items():
        if param == target:
            return block
    return None

_BLOCK_STATE = {}
_BLOCK_GENERATION = {}

_KEMPER_MFR = (0x00, 0x20, 0x33)
_KEMPER_PRODUCT_PLAYER = 0x02
_KEMPER_DEVICE_OMNI    = 0x7F

_FN_SINGLE_PARAM_REQUEST  = 0x41
_FN_SINGLE_PARAM_RESPONSE = 0x01
_FN_STRING_PARAM_RESPONSE = 0x03
_FN_EXTENDED              = 0x7E

_PAGE_STRINGS         = 0x00
_PAGE_RIG_PARAMETERS  = 0x04
_PAGE_TUNER_DEVIANCE  = 0x7C       # 8192 = in tune
_PAGE_TUNER_NOTE      = 0x7D
_PAGE_SYSTEM          = 0x7F

_ADDR_RIG_NAME       = 0x01
_ADDR_BPM            = 0x00
_ADDR_TUNER_NOTE     = 0x54
_ADDR_TUNER_DEVIANCE = 0x0F
_ADDR_TUNER_MODE     = 0x7E

_NOTE_NAMES = ("C", "Db", "D", "Eb", "E", "F", "Gb", "G", "Ab", "A", "Bb", "B")

_BEACON_ADDR_PAGE  = 0x40
_BEACON_PARAM_SET  = 0x02
_BEACON_LEASE_DIV2 = 0x05
_BEACON_RESEND_MS  = 5000
_INIT_RETRY_MS = 1000
_SENSING_TIMEOUT_MS = 15000
_FLAGS_INIT = 0x23   # init=1 + sysex=1 + tunemode=1
_FLAGS_KEEPALIVE = 0x22   # sysex=1 + tunemode=1 (no init)

_BIDIR_STATE = {
    "last_beacon_ms": 0,
    "init_sent": False,
    "confirmed": False,
    "last_sensed_ms": 0,
    "published": {},
    "settle_until_ms": 0,
    "generation": 0,
    "target_rig": None,
    "reconcile_generation": 0,
    "reconcile_pending": (),
    "reconcile_fallback_ms": 0,
    "reconcile_attempt": 0,
    "reconcile_queried": (),
    "query_retire_ms": 0,
    "orphan_blocks": (),
    "orphan_until_ms": 0,
    "pending_name": "",
    "pending_name_ms": 0,
    "pending_name_generation": 0,
    "tuner_active": False,
    "query_guard_expire_ms": 0,
    "awaiting_local_pc": None,  # (generation, flat rig, expiry ms)
    "orphan_local_pcs": (),
}

_SETTLE_MS = 500
_PC_ECHO_SETTLE_MS = 50
_PC_TOKEN_MS = 10000
_MAX_PC_ORPHANS = 8
_RECONCILE_REPLY_MS = 400
_RECONCILE_ATTEMPTS = 2
_QUERY_RETIRE_MS = 1200
_NAME_STABLE_MS = 150
_QUERY_GUARD_MS = _QUERY_RETIRE_MS
_QUERY_GUARDS = {}       # block -> (latest live bool, guard deadline ms)
_WAH_TYPE_PAGES = (50, 51, 52, 53, 56, 58, 60, 61)
_WAH_TYPES = (1, 2, 3, 4, 6, 7, 8, 9, 10, 12)
# One optional request at a time; slot types are discovered once per rig.
_WAH = {"generation": -1, "query_generation": -1, "retire_ms": 0,
        "attempts": 0, "known": False, "pending": False, "next_ms": 0,
        "types": 0, "slots": 0, "states": 0, "on": 0, "fixed": -1,
        "target": 8, "cursor": 0, "queried_slots": 0, "slots_retire_ms": 0}


def _invalidate_wah(app):
    _WAH["generation"] = _BIDIR_STATE.get("generation", 0)
    _WAH["query_generation"] = -1
    _WAH["attempts"] = 0
    _WAH["known"] = False
    _WAH["pending"] = False
    _WAH["next_ms"] = _WAH["retire_ms"]
    _WAH["types"] = _WAH["slots"] = _WAH["states"] = _WAH["on"] = 0
    _WAH["fixed"] = -1
    _WAH["cursor"] = 0
    # Retain an outstanding request's quarantine across rig generations.
    if "expression_mode" in _BIDIR_STATE["published"]:
        _publish(app, {"expression_mode": ""})


def _query_wah(app, now):
    if (not _BIDIR_STATE["confirmed"] or _transition_active()
            or _RIG_INFO["rig"] != _current_rig_index(app)):
        return
    ctx = getattr(app, "display_context", None)
    if ctx is not None and ctx.get("kemper_rig", _current_rig_index(app)) != _current_rig_index(app):
        return
    generation = _BIDIR_STATE.get("generation", 0)
    if _WAH["generation"] != generation:
        _invalidate_wah(app)
    if _WAH["pending"]:
        if now < _WAH["retire_ms"]:
            return
        # A lost poll breaks freshness of the whole dynamic snapshot.
        _WAH["fixed"] = -1
        _WAH["states"] = 0
        _publish_wah(app)
        _WAH["pending"] = False
        if _WAH["attempts"] >= 3:
            _WAH["attempts"] = 0
            _WAH["next_ms"] = now + 5000
    if now < _WAH["next_ms"]:
        return
    target = _WAH["target"] if _WAH["attempts"] else 8
    if not _WAH["attempts"] and _WAH["fixed"] >= 0:
        for i in range(8):
            bit = 1 << i
            if not _WAH["types"] & bit:
                target = i + 16
                break
            if _WAH["slots"] & bit and not _WAH["states"] & bit:
                target = i
                break
        else:
            for i in range(_WAH["cursor"], 8):
                if _WAH["slots"] & (1 << i):
                    target = i
                    break
    if target == 8:
        page, addr = 5, 21
    elif target >= 16:
        page, addr = _WAH_TYPE_PAGES[target - 16], 0
    else:
        page, addr = _BLOCK_ONOFF[_EFFECT_VALUES[target]]
    _WAH["query_generation"] = generation
    _WAH["target"] = target
    _WAH["attempts"] += 1
    _WAH["pending"] = True
    _WAH["retire_ms"] = now + _QUERY_RETIRE_MS
    if target < 8:
        # These replies also enter the ordinary effect/LED handler. Retain
        # every possibly outstanding slot read across later fixed-Wah reads;
        # a retry can leave an older reply in flight after one has succeeded.
        if now >= _WAH["slots_retire_ms"]:
            _WAH["queried_slots"] = 0
        _WAH["queried_slots"] |= 1 << target
        _WAH["slots_retire_ms"] = _WAH["retire_ms"]
    app.midi.send_sysex(_KEMPER_MFR + (0x02, 0x7F, 0x41, 0, page, addr))


def _publish_wah(app):
    w = _WAH
    active = w["on"] & w["slots"] & w["states"] & w["types"]
    known = w["fixed"] == 0 and w["types"] == 255 and w["states"] & w["slots"] == w["slots"]
    mode = "WAH" if w["fixed"] == 1 or active else "VOL" if known else ""
    _publish(app, {"expression_mode": mode})
    w["known"] = bool(mode)


def _receive_wah(app, value, target=8, live=False):
    if ((target < 16 and value not in (0, 1)) or _transition_active()
            or _WAH["query_generation"] != _BIDIR_STATE.get("generation", 0)):
        return
    if target == 8:
        _WAH["fixed"] = value
    else:
        bit = 1 << (target - 16 if target >= 16 else target)
        if target >= 16:
            _WAH["types"] |= bit
            if value in _WAH_TYPES:
                if not _WAH["slots"] & bit:
                    _WAH["states"] &= ~bit
                _WAH["slots"] |= bit
            else:
                _WAH["slots"] &= ~bit
                _WAH["states"] &= ~bit
        else:
            _WAH["states"] |= bit
            _WAH["on"] = (_WAH["on"] | bit) if value else (_WAH["on"] & ~bit)
    _publish_wah(app)
    if not live and target == _WAH["target"]:
        _WAH["pending"] = False
        _WAH["attempts"] = 0
        if target < 16:
            _WAH["cursor"] = 0 if target == 8 else target + 1
        delay = 500 if target == 8 and _WAH["types"] == 255 else 20
        _WAH["next_ms"] = app._now_ms() + delay

_RIG_INFO = {
    "name": "",
    "rig":  None,   # owning flat rig 1..125
}

CONFIG_SCHEMA = {
    "key": "kemper",
    "label": "Kemper Player target",
    "fields": {
        "debug": {"type": "bool", "default": False, "hidden": True,
                  "label": "debug: trace received SYSEX to console"},
    },
}


_RIG_FALLBACK = "#666666"
RIG_COLORS = {
    1:  "#3a8eff",
    2:  "#f5dc34",
    3:  "#e54848",
    4:  "#2a2a2a",
    5:  "#3ecb6e",
    11: "#3a8eff",
    12: "#f5dc34",
    13: "#e54848",
    14: "#3ecb6e",
    15: "#c08aff",
}


def rig_color(rig):
    return RIG_COLORS.get(int(rig), _RIG_FALLBACK)


def _current_rig_index(app):
    bank = int(getattr(app, "current_bank", 1) or 1)
    slot = int(getattr(app, "current_slot", 1) or 1)
    return (bank - 1) * 5 + slot


def request_rig_info(app):
    cfg = (app.device or {}).get("kemper")
    if cfg is None:
        return False
    # Untagged replies: no query mid-transition.
    if _transition_active() or _BIDIR_STATE.get("pending_name", ""):
        return False
    app.midi.send_sysex(_KEMPER_MFR + (
        _KEMPER_PRODUCT_PLAYER, _KEMPER_DEVICE_OMNI,
        _FN_SINGLE_PARAM_REQUEST, 0x00, _PAGE_STRINGS, _ADDR_RIG_NAME,
    ))
    return True


def get_rig_info(app, request=True):
    cfg = (app.device or {}).get("kemper")
    if cfg is None:
        return None
    if request:
        try:
            request_rig_info(app)
        except Exception:
            pass
    cur = None
    try:
        cur = _current_rig_index(app)
    except Exception:
        cur = None
    return {
        "name":  _RIG_INFO["name"],
        "rig":   _RIG_INFO["rig"],
        "color": rig_color(cur) if cur is not None else None,
        "fresh": (_RIG_INFO["rig"] is not None and _RIG_INFO["rig"] == cur),
    }


def dispatch(msg, midi):
    t = msg["type"]
    ch = int(msg.get("channel", 1))

    if t == "kemper_rig":
        import time as _time
        bank = int(msg.get("bank", 1))
        rig = int(msg.get("rig", 1))
        if bank < 1 or bank > 25 or rig < 1 or rig > 5:
            return
        midi.send_cc(ch, 0, 0)
        midi.send_cc(ch, 32, 0)             # flat rig list
        _time.sleep(0.005)
        midi.send_pc(ch, (bank - 1) * 5 + (rig - 1))

    elif t == "kemper_step_rig":
        cc = 48 if msg.get("direction", "next") == "next" else 49
        midi.send_cc(ch, cc, 0)

    elif t == "kemper_effect_toggle":
        cc = _EFFECT_CC.get(msg["slot"])
        if cc is not None:
            midi.send_cc(ch, cc, 127 if msg["value"] == "on" else 0)

    elif t == "kemper_fixed_toggle":
        lsb = _FIXED_FX_LSB.get(msg.get("effect"))
        if lsb is not None:
            on = msg.get("value", "on") == "on"
            midi.send_cc(ch, 99, _FIXED_FX_PAGE)
            midi.send_cc(ch, 98, lsb)
            midi.send_cc(ch, 6, 0)
            midi.send_cc(ch, 38, 1 if on else 0)

    elif t == "kemper_tuner":
        midi.send_cc(ch, 31, 127 if msg.get("state", "on") == "on" else 0)

    elif t == "kemper_tap_tempo":
        midi.send_cc(ch, 30, 127)

    elif t == "kemper_set_tempo":
        bpm = max(40, min(250, int(msg.get("bpm", 120))))
        midi.send_cc(ch, 92, bpm // 128)
        midi.send_cc(ch, 93, bpm % 128)

    elif t == "kemper_morph":
        midi.send_cc(ch, 4, int(msg.get("value", 64)))

    elif t == "kemper_morph_trigger":
        midi.send_cc(ch, 80, 127 if msg.get("state", "on") == "on" else 0)

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
        _query_block_states(midi)


def _query_block_states(midi):
    for page, addr in _BLOCK_ONOFF.values():
        midi.send_sysex(_KEMPER_MFR + (
            _KEMPER_PRODUCT_PLAYER, _KEMPER_DEVICE_OMNI,
            _FN_SINGLE_PARAM_REQUEST, 0x00, page, addr,
        ))


def _query_blocks(midi, blocks):
    for block in blocks:
        param = _BLOCK_ONOFF.get(block)
        if param is None:
            continue
        page, addr = param
        midi.send_sysex(_KEMPER_MFR + (
            _KEMPER_PRODUCT_PLAYER, _KEMPER_DEVICE_OMNI,
            _FN_SINGLE_PARAM_REQUEST, 0x00, page, addr,
        ))


def _bound_blocks(app):
    blocks = []
    for _sw_name, binding in app.current_bindings():
        for action in (binding or {}).get("actions", {}).values():
            for msg in action.get("messages", []):
                if msg.get("type") != "kemper_effect_toggle":
                    continue
                block = msg.get("slot")
                if block in _BLOCK_ONOFF and block not in blocks:
                    blocks.append(block)
    return tuple(blocks)


def on_midi_in(port, channel, status, data, app):
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
        block = _block_for_cc(cc)
        if block is not None:
            on = value >= 64
            s = _BIDIR_STATE
            now = app._now_ms()
            pending = s.get("reconcile_pending", ())
            was_pending = (block in pending and
                s.get("reconcile_generation") == s.get("generation"))
            query_in_flight = (block in s.get("reconcile_queried", ())
                               and now < s.get("query_retire_ms", 0))
            _BLOCK_STATE[block] = on
            _BLOCK_GENERATION[block] = s.get("generation", 0)
            if now >= s["settle_until_ms"]:
                _publish(app, {"kemper_block_" + block: "on" if on else "off"})
                for sw_name, binding in app.current_bindings():
                    if _binding_targets_block(binding, block):
                        app.set_switch_latched(sw_name, on)
            if was_pending:
                s["reconcile_pending"] = tuple(
                    b for b in pending if b != block)
                if not s["reconcile_pending"]:
                    s["reconcile_fallback_ms"] = 0
                    _commit_pending_rig_name(app, now)
            # One in-flight reply per round, even after reconciliation.
            wah_target = _EFFECT_VALUES.index(block)
            wah_pending = _WAH["pending"] and _WAH["target"] == wah_target
            if query_in_flight or wah_pending:
                deadline = now + _QUERY_GUARD_MS
                budget = (max(1, s.get("reconcile_attempt", 0))
                          if query_in_flight else 0) + int(wah_pending)
                _QUERY_GUARDS[block] = (on, deadline, budget)
                s["query_guard_expire_ms"] = deadline
            _receive_wah(app, on, wah_target, live=True)
        elif cc == 31:
            on = "on" if value >= 64 else "off"
            _BIDIR_STATE["tuner_active"] = on == "on"
            _publish(app, {"kemper_tuner": on})
    elif status == 0xC0 and data:
        pc = data[0]
        rig = pc + 1
        s = _BIDIR_STATE
        now = app._now_ms()
        expected = _active_local_pc(now)
        if (expected is not None
                and expected[0] == s.get("generation")
                and expected[1] == rig
                and rig == _current_rig_index(app)):
            s["awaiting_local_pc"] = None
            _live_local_pc_orphans(now)
            pending = s.get("reconcile_pending", ())
            current_round = (bool(pending)
                and s.get("reconcile_generation") == s.get("generation")
                and s.get("reconcile_attempt", 0) > 0)
            if s["settle_until_ms"]:
                pc_deadline = now + _PC_ECHO_SETTLE_MS
                if pc_deadline > s["settle_until_ms"]:
                    s["settle_until_ms"] = pc_deadline
            elif (not current_round and (pending or
                  (s.get("reconcile_queried", ()) and
                   now < s.get("query_retire_ms", 0)))):
                _defer_current_reconcile(app, now)
            # Keep current queries live across their PC echo; only older
            # generations belong in orphan_blocks (real replies: PC+3/7ms).
            on_patch_loaded(app)
            return

        orphans = _live_local_pc_orphans(now)
        for index, item in enumerate(orphans):
            if item[0] != rig:
                continue
            s["orphan_local_pcs"] = orphans[:index] + orphans[index + 1:]
            return
        # Different PC: external change.
        bank = pc // 5 + 1
        rig_in_bank = pc % 5 + 1
        app.update_context({
            "kemper_rig":         rig,
            "kemper_bank":        bank,
            "kemper_rig_in_bank": rig_in_bank,
        })
        _arm(app, target_rig=rig)
        same = (bank == app.current_bank and rig_in_bank == app.current_slot)
        app.switch_patch(bank, rig_in_bank, source="midi_in",
                         fire_on_enter=False, force_reload=same)


def _tuple_union(left, right):
    result = list(left)
    for item in right:
        if item not in result:
            result.append(item)
    return tuple(result)


def _live_local_pc_orphans(now):
    s = _BIDIR_STATE
    old = s.get("orphan_local_pcs", ())
    live = tuple(item for item in old if now < item[1])
    if live != old:
        s["orphan_local_pcs"] = live
    return live


def _active_local_pc(now):
    expected = _BIDIR_STATE.get("awaiting_local_pc")
    if expected is not None and now >= expected[2]:
        _BIDIR_STATE["awaiting_local_pc"] = None
        return None
    return expected


def _retire_local_pc(now):
    """Quarantine a superseded local target only until its token expires."""
    s = _BIDIR_STATE
    expected = _active_local_pc(now)
    if expected is not None:
        old = _live_local_pc_orphans(now)
        s["orphan_local_pcs"] = (
            old + ((expected[1], expected[2]),))[-_MAX_PC_ORPHANS:]
    s["awaiting_local_pc"] = None


def _orphan_queries_active(now_ms):
    s = _BIDIR_STATE
    blocks = s.get("orphan_blocks", ())
    if not blocks:
        return False
    if now_ms < s.get("orphan_until_ms", 0):
        return True
    s["orphan_blocks"] = ()
    s["orphan_until_ms"] = 0
    return False


def _transition_active():
    s = _BIDIR_STATE
    return bool(s["settle_until_ms"]
                or s.get("reconcile_pending", ())
                or s.get("reconcile_fallback_ms", 0))


def _quarantine_queries(now):
    s = _BIDIR_STATE
    queried = s.get("reconcile_queried", ())
    retire = s.get("query_retire_ms", 0)
    if queried and now < retire:
        if not _orphan_queries_active(now):
            s["orphan_blocks"] = ()
        s["orphan_blocks"] = _tuple_union(s.get("orphan_blocks", ()), queried)
        if retire > s.get("orphan_until_ms", 0):
            s["orphan_until_ms"] = retire
    else:
        _orphan_queries_active(now)
    if now < _WAH["slots_retire_ms"]:
        # Wah slot polling shares the same response addresses as effect
        # reconciliation. Fence it too before changing the rig generation.
        queried = tuple(_EFFECT_VALUES[i] for i in range(8)
                        if _WAH["queried_slots"] & (1 << i))
        s["orphan_blocks"] = _tuple_union(s.get("orphan_blocks", ()), queried)
        s["orphan_until_ms"] = max(s.get("orphan_until_ms", 0),
                                   _WAH["slots_retire_ms"])


def _defer_current_reconcile(app, now):
    s = _BIDIR_STATE
    _quarantine_queries(now)
    s["settle_until_ms"] = now + _PC_ECHO_SETTLE_MS
    s["reconcile_generation"] = s.get("generation", 0)
    s["reconcile_pending"] = ()
    s["reconcile_fallback_ms"] = 0
    s["reconcile_attempt"] = 0
    s["reconcile_queried"] = ()
    s["query_retire_ms"] = 0
    s["query_guard_expire_ms"] = 0
    _QUERY_GUARDS.clear()

    _invalidate_wah(app)


def _arm(app, delay_ms=_SETTLE_MS, target_rig=None):
    """Start a fresh rig generation and quarantine any older query replies."""
    s = _BIDIR_STATE
    now = app._now_ms()

    _quarantine_queries(now)
    _retire_local_pc(now)

    generation = int(s.get("generation", 0)) + 1
    if generation > 0x3FFFFFFF:
        generation = 1
    s["generation"] = generation
    s["target_rig"] = (_current_rig_index(app)
                       if target_rig is None else target_rig)
    s["settle_until_ms"] = now + delay_ms
    s["reconcile_generation"] = generation
    s["reconcile_pending"] = ()
    s["reconcile_fallback_ms"] = 0
    s["reconcile_attempt"] = 0
    s["reconcile_queried"] = ()
    s["query_retire_ms"] = 0
    s["pending_name"] = ""
    s["pending_name_ms"] = 0
    s["pending_name_generation"] = 0
    s["query_guard_expire_ms"] = 0
    _QUERY_GUARDS.clear()
    _invalidate_wah(app)


def _send_reconcile_round(app, now_ms):
    s = _BIDIR_STATE
    pending = s.get("reconcile_pending", ())
    if (not pending
            or s.get("reconcile_generation") != s.get("generation")
            or _orphan_queries_active(now_ms)):
        return False
    s["reconcile_attempt"] = s.get("reconcile_attempt", 0) + 1
    s["reconcile_queried"] = _tuple_union(
        s.get("reconcile_queried", ()), pending)
    s["query_retire_ms"] = now_ms + _QUERY_RETIRE_MS
    s["reconcile_fallback_ms"] = now_ms + _RECONCILE_REPLY_MS
    # Record before sending: partial failures also need quarantine.
    _query_blocks(app.midi, pending)
    return True


def _binding_targets_block(binding, block):
    for action in (binding or {}).get("actions", {}).values():
        for msg in action.get("messages", []):
            if msg.get("type") == "kemper_effect_toggle" and msg.get("slot") == block:
                return True
    return False


def _block_of_binding(binding):
    for action in (binding or {}).get("actions", {}).values():
        for msg in action.get("messages", []):
            if msg.get("type") == "kemper_effect_toggle":
                return msg.get("slot")
    return None


def _apply_cache(app, force_publish=False, only_blocks=None,
                 generation=None):
    cfg = (app.device or {}).get("kemper")
    if cfg is None:
        return
    publish_blocks = only_blocks
    if force_publish and publish_blocks is None:
        publish_blocks = _bound_blocks(app)
    for sw_name, binding in app.current_bindings():
        block = _block_of_binding(binding)
        if (block is not None and block in _BLOCK_STATE
                and (only_blocks is None or block in only_blocks)
                and (generation is None
                     or _BLOCK_GENERATION.get(block) == generation)):
            on = _BLOCK_STATE[block]
            app.set_switch_latched(sw_name, on)
    # Publish all targets; legacy LEDs follow the first.
    for block, on in _BLOCK_STATE.items():
        if ((publish_blocks is None or block in publish_blocks)
                and (generation is None
                     or _BLOCK_GENERATION.get(block) == generation)):
            _publish(app, {"kemper_block_" + block: "on" if on else "off"},
                     force=force_publish)


def _stage_rig_name(name, now_ms):
    s = _BIDIR_STATE
    s["pending_name"] = name
    s["pending_name_ms"] = now_ms
    s["pending_name_generation"] = s.get("generation", 0)


def _accept_rig_name(app, name):
    _publish(app, {"kemper_rig_name": name, "patch_name": name})
    _RIG_INFO["name"] = name
    _RIG_INFO["rig"] = _current_rig_index(app)


def _commit_pending_rig_name(app, now_ms):
    """Publish only a stable name belonging to the completed generation."""
    s = _BIDIR_STATE
    name = s.get("pending_name", "")
    if (not name or _transition_active()
            or s.get("pending_name_generation") != s.get("generation")
            or now_ms - s.get("pending_name_ms", 0) < _NAME_STABLE_MS):
        return False

    target = s.get("target_rig")
    # Load bursts can repeat the old name first.
    if (target is not None and _RIG_INFO["rig"] is not None
            and target != _RIG_INFO["rig"]
            and name == _RIG_INFO["name"]):
        s["pending_name"] = ""
        s["pending_name_ms"] = 0
        s["pending_name_generation"] = 0
        return False

    _accept_rig_name(app, name)
    s["pending_name"] = ""
    s["pending_name_ms"] = 0
    s["pending_name_generation"] = 0
    return True


def wants_authoritative_boot(app):
    cfg = (app.device or {}).get("kemper")
    return cfg is not None


def on_patch_loaded(app):
    s = _BIDIR_STATE
    if (s["settle_until_ms"] or s.get("reconcile_pending", ())
            or s.get("reconcile_fallback_ms", 0)):
        return
    _apply_cache(app, True, generation=s.get("generation", 0))


def on_patch_switch_started(app, source="editor", fire_on_enter=True):
    """Open a generation boundary and hide old public rig state."""
    if (app.device or {}).get("kemper") is None:
        return
    if fire_on_enter and source != "midi_in":
        _arm(app)

    context = getattr(app, "display_context", None)
    if context is None:
        context = getattr(app, "context", None)
    pending = getattr(app, "_pending_context_updates", None)
    published = _BIDIR_STATE["published"]
    for block in _BLOCK_ONOFF:
        key = "kemper_block_" + block
        if context is not None:
            context.pop(key, None)
        if pending is not None:
            pending.pop(key, None)
        published.pop(key, None)
    key = "kemper_rig_name"
    if context is not None:
        context.pop(key, None)
    if pending is not None:
        pending.pop(key, None)
    published.pop(key, None)


def on_preview(app, bank, slot):
    cfg = (app.device or {}).get("kemper")
    if cfg is None:
        return
    app.update_context({
        "kemper_bank":        bank,
        "kemper_rig_in_bank": slot,
        "kemper_rig":         (bank - 1) * 5 + slot,
    })


def tuner_off(app):
    cfg = (app.device or {}).get("kemper")
    if cfg is None:
        return
    ch = int((app.device or {}).get("midi_channel") or 1)
    app.midi.send_cc(ch, 31, 0)


def on_navigate(app, bank, slot):
    cfg = (app.device or {}).get("kemper")
    if cfg is None:
        return
    _WAH["retire_ms"] = max(_WAH["retire_ms"], app._now_ms() + _SETTLE_MS)
    _invalidate_wah(app)
    ch = int(cfg.get("midi_channel") or (app.device or {}).get("midi_channel") or 1)
    dispatch({"type": "kemper_rig", "bank": bank, "rig": slot, "channel": ch}, app.midi)
    app.update_context({
        "kemper_bank":        bank,
        "kemper_rig_in_bank": slot,
        "kemper_rig":         (bank - 1) * 5 + slot,
    })


_TUNER_ALIASES = {
    "kemper_tuner":          "tuner",
    "kemper_tuner_note":     "tuner_note",
    "kemper_tuner_deviance": "tuner_deviance",
}


def _add_tuner_aliases(updates):
    extra = None
    for k, generic in _TUNER_ALIASES.items():
        if k in updates and generic not in updates:
            if extra is None:
                extra = dict(updates)
            extra[generic] = updates[k]
    return extra if extra is not None else updates


def _publish(app, updates, force=False):
    updates = _add_tuner_aliases(updates)
    pub = _BIDIR_STATE["published"]
    fresh = updates if force else {
        k: v for k, v in updates.items() if pub.get(k) != v
    }
    if not fresh:
        return
    app.update_context(fresh)
    pub.update(fresh)


def _decode_string(payload):
    chars = []
    for b in payload:
        if b == 0x00:
            break
        if 0x20 <= b < 0x7F:
            chars.append(chr(b))
            # Bound allocations for hostile rig-name payloads.
            if len(chars) >= 64:
                break
    return "".join(chars).strip()


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
    """Parse a Kemper SYSEX payload (without F0/F7)."""
    _trace_rx(data, cfg)
    if len(data) < 6 or data[0] != _KEMPER_MFR[0] or data[1] != _KEMPER_MFR[1] or data[2] != _KEMPER_MFR[2]:
        return
    fn = data[5]

    if fn == _FN_EXTENDED:
        _BIDIR_STATE["confirmed"] = True
        _BIDIR_STATE["last_sensed_ms"] = app._now_ms()
        _publish(app, {"kemper_connected": "on"})
        return

    if fn == _FN_STRING_PARAM_RESPONSE and len(data) >= 9:
        page = data[7]
        addr = data[8]
        if page == _PAGE_STRINGS and addr == _ADDR_RIG_NAME:
            name = _decode_string(data[9:])
            if name:
                now = app._now_ms()
                s = _BIDIR_STATE
                previous_name = _RIG_INFO["name"]
                current_rig = _current_rig_index(app)
                if (_transition_active() or s.get("pending_name", "")):
                    # Real loads emit OLD then NEW names ~100ms apart.
                    _stage_rig_name(name, now)
                    _commit_pending_rig_name(app, now)
                else:
                    changed = ((previous_name and name != previous_name)
                               or (_RIG_INFO["rig"] is not None
                                   and _RIG_INFO["rig"] != current_rig))
                    if changed:
                        # Missing PC: name establishes the boundary.
                        _arm(app, target_rig=current_rig)
                        _stage_rig_name(name, now)
                    elif not previous_name and _RIG_INFO["rig"] is None:
                        _arm(app, target_rig=current_rig)
                        _accept_rig_name(app, name)
                    else:
                        _accept_rig_name(app, name)
        return

    if fn != _FN_SINGLE_PARAM_RESPONSE or len(data) < 11:
        return
    page = data[7]
    addr = data[8]
    value = (data[9] << 7) | (data[10] & 0x7F)

    if page == _FIXED_FX_PAGE and addr == _FIXED_FX_LSB["Wah"]:
        _receive_wah(app, value)
        return

    if addr == 0 and page in _WAH_TYPE_PAGES:
        _receive_wah(app, value, 16 + _WAH_TYPE_PAGES.index(page))
        return

    if page == _PAGE_RIG_PARAMETERS and addr == _ADDR_BPM:
        bpm = int(round(value / 64))
        _publish(app, {"kemper_bpm": bpm})
        return

    if page == _PAGE_SYSTEM and addr == _ADDR_TUNER_MODE:
        active = value == 1
        _BIDIR_STATE["tuner_active"] = active
        _publish(app, {"kemper_tuner": "on" if active else "off"})
        return

    if page == _PAGE_TUNER_NOTE and addr == _ADDR_TUNER_NOTE:
        if _BIDIR_STATE.get("tuner_active", False):
            _publish(app, {"kemper_tuner_note": _NOTE_NAMES[value % 12]})
        return

    if page == _PAGE_TUNER_DEVIANCE and addr == _ADDR_TUNER_DEVIANCE:
        if _BIDIR_STATE.get("tuner_active", False):
            _publish(app, {"kemper_tuner_deviance": value})
        return

    block = _block_for_param(page, addr)
    if block is None:
        return                           # not an effect block on/off
    on = value != 0
    now = app._now_ms()
    if (_orphan_queries_active(now)
            and block in _BIDIR_STATE.get("orphan_blocks", ())):
        # Unidentifiable old response: do not seed current cache.
        return
    guard = _QUERY_GUARDS.get(block)
    if guard is not None:
        expected, until_ms = guard[0], guard[1]
        if now < until_ms:
            # Fence every old query response behind newer live CCs.
            budget = guard[2] if len(guard) > 2 else 1
            if budget > 1:
                _QUERY_GUARDS[block] = (expected, until_ms, budget - 1)
            else:
                _QUERY_GUARDS.pop(block, None)
                if not _QUERY_GUARDS:
                    _BIDIR_STATE["query_guard_expire_ms"] = 0
            if on != expected:
                _receive_wah(app, expected, _EFFECT_VALUES.index(block))
                return
        else:
            _QUERY_GUARDS.pop(block, None)
            if not _QUERY_GUARDS:
                _BIDIR_STATE["query_guard_expire_ms"] = 0
    _BLOCK_STATE[block] = on
    generation = _BIDIR_STATE.get("generation", 0)
    _BLOCK_GENERATION[block] = generation
    pending = _BIDIR_STATE.get("reconcile_pending", ())
    was_pending = (block in pending
                   and _BIDIR_STATE.get("reconcile_generation") == generation
                   and _BIDIR_STATE.get("reconcile_attempt", 0) > 0)
    if (now >= _BIDIR_STATE["settle_until_ms"]
            and (was_pending or not _transition_active())):
        # Unchanged targeted replies still confirm freshness.
        _publish(app, {"kemper_block_" + block: "on" if on else "off"},
                 force=was_pending)
        for sw_name, binding in app.current_bindings():
            if _binding_targets_block(binding, block):
                app.set_switch_latched(sw_name, on)
        _receive_wah(app, value, _EFFECT_VALUES.index(block))
    if was_pending:
        remaining = tuple(b for b in pending if b != block)
        _BIDIR_STATE["reconcile_pending"] = remaining
        if not remaining:
            _BIDIR_STATE["reconcile_fallback_ms"] = 0
            _commit_pending_rig_name(app, now)


def tick(app, now_ms):
    """Advance reconciliation and maintain the bidirectional lease."""
    cfg = (app.device or {}).get("kemper")
    if cfg is None:
        return
    guard_expire = _BIDIR_STATE.get("query_guard_expire_ms", 0)
    if guard_expire and now_ms >= guard_expire:
        _QUERY_GUARDS.clear()
        _BIDIR_STATE["query_guard_expire_ms"] = 0
    s = _BIDIR_STATE
    _orphan_queries_active(now_ms)
    if (s.get("query_retire_ms", 0)
            and now_ms >= s["query_retire_ms"]):
        s["reconcile_queried"] = ()
        s["query_retire_ms"] = 0
        if not s.get("reconcile_pending", ()):
            s["reconcile_attempt"] = 0

    su = s["settle_until_ms"]
    if su and now_ms >= su:
        s["settle_until_ms"] = 0
        blocks = _bound_blocks(app)
        if blocks:
            s["reconcile_generation"] = s.get("generation", 0)
            s["reconcile_pending"] = blocks
            s["reconcile_fallback_ms"] = 0
            s["reconcile_attempt"] = 0
            s["reconcile_queried"] = ()
            s["query_retire_ms"] = 0
            _send_reconcile_round(app, now_ms)

    pending = s.get("reconcile_pending", ())
    if (pending and not s["settle_until_ms"]
            and s.get("reconcile_generation") == s.get("generation")):
        attempt = s.get("reconcile_attempt", 0)
        fallback = s.get("reconcile_fallback_ms", 0)
        if not attempt:
            _send_reconcile_round(app, now_ms)
        elif fallback and now_ms >= fallback:
            if attempt < _RECONCILE_ATTEMPTS:
                # Retry only pages still missing.
                _send_reconcile_round(app, now_ms)
            else:
                missing = pending
                s["reconcile_pending"] = ()
                s["reconcile_fallback_ms"] = 0
                # Recover only observations stamped in this generation.
                _apply_cache(app, force_publish=True, only_blocks=missing,
                             generation=s.get("generation", 0))

    _commit_pending_rig_name(app, now_ms)
    init = not _BIDIR_STATE["init_sent"]
    unconfirmed_retry = not init and not _BIDIR_STATE["confirmed"]
    if unconfirmed_retry:
        init = True
    elif (not init and _BIDIR_STATE["confirmed"]
          and _BIDIR_STATE["last_sensed_ms"]
          and now_ms - _BIDIR_STATE["last_sensed_ms"] > _SENSING_TIMEOUT_MS):
        _BIDIR_STATE["confirmed"] = False
        _BIDIR_STATE["init_sent"] = False
        _publish(app, {"kemper_connected": "off"})
        _invalidate_wah(app)
        init = True
    _query_wah(app, now_ms)
    if unconfirmed_retry:
        if now_ms - _BIDIR_STATE["last_beacon_ms"] < _INIT_RETRY_MS:
            return
    elif not init and now_ms - _BIDIR_STATE["last_beacon_ms"] < _BEACON_RESEND_MS:
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
    # The 5 s backstop is generation-gated too.
    if not _transition_active():
        try:
            blocks = _bound_blocks(app)
            if blocks:
                _apply_cache(app, only_blocks=blocks,
                             generation=_BIDIR_STATE.get("generation", 0))
        except Exception:
            pass


def update_context(msg, ctx):
    """Mirror an outbound rig target and arm its semantic PC confirmation."""
    t = msg.get("type")
    if t == "kemper_rig":
        bank = int(msg.get("bank", 1))
        rig = int(msg.get("rig", 1))
        if bank < 1 or bank > 25 or rig < 1 or rig > 5:
            return
        flat_rig = (bank - 1) * 5 + rig
        # Called only after successful dispatch; arm only the active patch.
        # Arbitrary rig commands used as effects remain inbound changes.
        if bank == ctx.get("bank") and rig == ctx.get("slot"):
            import time as _time
            now = _time.monotonic_ns() // 1000000
            s = _BIDIR_STATE
            _retire_local_pc(now)
            s["awaiting_local_pc"] = (
                s.get("generation", 0), flat_rig, now + _PC_TOKEN_MS)
        ctx["kemper_bank"] = bank
        ctx["kemper_rig_in_bank"] = rig
        ctx["kemper_rig"] = flat_rig


TFT_FIELDS = {
    "expression_mode":      {"label": "Expression pedal mode (VOL/WAH)", "sample": "VOL"},
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
    {"field": "expression_mode", "halign": "right", "valign": "bottom",
     "x": -6, "y": -6, "size": 2, "color": "#ffffff", "font": "system"},
]
