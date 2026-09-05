#!/usr/bin/env python3
"""Offline tests for the Kemper plugin's inbound (bidirectional) handling.

The Kemper plugin (firmware/lib/plugins/kemper.py) is pure Python - no
CircuitPython imports - so we drive it with a fake `app`/`midi` and feed it
the SYSEX frames the Player broadcasts, asserting how it mirrors state:
effect-block on/off -> switch latched, tuner mode -> display context, rig
name, sensing/keep-alive, and the beacon it emits.

This pins down the firmware's INTERPRETATION of the protocol. It cannot
observe what a real Player actually transmits (that needs a hardware trace).

Usage:
    python tools/kemper_plugin_test.py
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "firmware" / "lib"))
import plugins.kemper as kemper            # noqa: E402


PASS = 0
FAIL = 0
FAILURES = []

def check(name, ok, detail=""):
    global PASS, FAIL
    if ok:
        PASS += 1
        print("  ok   " + name)
    else:
        FAIL += 1
        FAILURES.append(name + ((" - " + detail) if detail else ""))
        print("  FAIL " + name + " " + detail)


class FakeMidi:
    def __init__(self):
        self.sysex = []
    def send_sysex(self, data): self.sysex.append(tuple(data))
    def send_cc(self, *a): pass
    def send_pc(self, *a): pass


class FakeApp:
    def __init__(self, bindings, kemper_cfg):
        self.device = {"kemper": kemper_cfg}
        self._bindings = bindings           # list of (sw_name, binding)
        self.midi = FakeMidi()
        self.latched = {}                   # sw_name -> bool
        self.latched_updates = []
        self.context = {"bank": 1, "slot": 1}
        self.display_context = self.context
        self._pending_context_updates = {}
        self.context_updates = []
        self._bank_lsb = {}
        # Patch-navigation state, mirrors app.py for the rig-follow path.
        self.current_bank = 1
        self.current_slot = 1
        self.current_patch = {"bank": 1, "slot": 1}
        self.last_patch_switch_ms = 0
        self.switch_patch_calls = []
        self.patch_switch_events = []
    def current_bindings(self): return list(self._bindings)
    def set_switch_latched(self, sw, on):
        self.latched_updates.append((sw, bool(on)))
        self.latched[sw] = on
        return True
    def update_context(self, updates):
        self.context_updates.append(dict(updates))
        self.context.update(updates)
    def get_last_bank_lsb(self, port, ch): return self._bank_lsb.get((port, ch), 0)
    def _now_ms(self): return getattr(self, "now_ms", 0)

    # Faithful mirror of app.py:switch_patch echo suppression.
    def switch_patch(self, bank, slot, source="editor", fire_on_enter=True,
                     force_reload=False):
        self.switch_patch_calls.append(
            (bank, slot, source, fire_on_enter, force_reload))
        if (source == "midi_in"
                and self.current_patch is not None
                and (bank, slot) == (self.current_bank, self.current_slot)
                and (self._now_ms() - self.last_patch_switch_ms) < 1200
                and not force_reload):
            # Real app.py invokes the plugin hook in this branch.  Calling it
            # here is essential: otherwise the PC-echo test bypasses the race
            # it claims to cover.
            kemper.on_patch_loaded(self)
            return True
        t0 = self._now_ms()
        self.current_bank = bank
        self.current_slot = slot
        self.context["bank"] = bank
        self.context["slot"] = slot
        self.last_patch_switch_ms = t0
        self.current_patch = {"bank": bank, "slot": slot}
        self.patch_switch_events.append((bank, slot, source))
        return True


def reset_bidir():
    kemper._WAH.update({"generation": -1, "query_generation": -1,
                        "retire_ms": 0, "attempts": 0, "known": False,
                        "pending": False, "next_ms": 0,
                        "types": 0, "slots": 0, "states": 0, "on": 0,
                        "fixed": -1, "target": 8, "cursor": 0,
                        "queried_slots": 0, "slots_retire_ms": 0})
    kemper._BIDIR_STATE["published"] = {}
    kemper._BIDIR_STATE["confirmed"] = False
    kemper._BIDIR_STATE["init_sent"] = False
    kemper._BIDIR_STATE["last_beacon_ms"] = 0
    kemper._BIDIR_STATE["last_sensed_ms"] = 0
    kemper._BIDIR_STATE["settle_until_ms"] = 0
    kemper._BIDIR_STATE["generation"] = 0
    kemper._BIDIR_STATE["target_rig"] = None
    kemper._BIDIR_STATE["reconcile_generation"] = 0
    kemper._BIDIR_STATE["reconcile_pending"] = ()
    kemper._BIDIR_STATE["reconcile_fallback_ms"] = 0
    kemper._BIDIR_STATE["reconcile_attempt"] = 0
    kemper._BIDIR_STATE["reconcile_queried"] = ()
    kemper._BIDIR_STATE["query_retire_ms"] = 0
    kemper._BIDIR_STATE["orphan_blocks"] = ()
    kemper._BIDIR_STATE["orphan_until_ms"] = 0
    kemper._BIDIR_STATE["pending_name"] = ""
    kemper._BIDIR_STATE["pending_name_ms"] = 0
    kemper._BIDIR_STATE["pending_name_generation"] = 0
    kemper._BIDIR_STATE["tuner_active"] = False
    kemper._BIDIR_STATE["query_guard_expire_ms"] = 0
    kemper._BIDIR_STATE["awaiting_local_pc"] = None
    kemper._BIDIR_STATE["orphan_local_pcs"] = ()
    kemper._QUERY_GUARDS.clear()
    kemper._BLOCK_STATE.clear()
    kemper._BLOCK_GENERATION.clear()
    kemper._RIG_INFO["name"] = ""
    kemper._RIG_INFO["rig"] = None


def effect_binding(slot):
    return {"switch": "x", "mode": "latched",
            "actions": {"toggle_on": {"messages": [
                {"type": "kemper_effect_toggle", "slot": slot, "value": "on"}]}}}


# SYSEX single-parameter response (without F0/F7), as the plugin expects:
# [00 20 33, product, device, 0x01, instance, page, addr, val_msb, val_lsb]
def param_response(page, addr, value):
    return [0x00, 0x20, 0x33, 0x02, 0x7F, 0x01, 0x00, page, addr,
            (value >> 7) & 0x7F, value & 0x7F]

def sensing():
    return [0x00, 0x20, 0x33, 0x00, 0x00, 0x7E, 0x00, 0x7F]

def string_response(page, addr, text):
    return [0x00, 0x20, 0x33, 0x02, 0x7F, 0x03, 0x00, page, addr] + [ord(c) for c in text] + [0x00]


CFG = {"enabled": True, "midi_channel": 1, "auto_follow_effects": True,
       "auto_follow_rig": True, "bidirectional": True}


def feed_sysex(app, data):
    kemper.on_midi_in(0, 0, 0xF0, data, app)


# ---------------- effect blocks ----------------

def feed_block(app, block, on):
    page, addr = kemper._BLOCK_ONOFF[block]
    feed_sysex(app, param_response(page, addr, 1 if on else 0))


def test_effect_blocks_mirror_to_bound_switches():
    reset_bidir()
    # ACOUSTIC-like: switch 4 -> slot C, switch 1 -> slot A.
    app = FakeApp([("4", effect_binding("C")), ("1", effect_binding("A"))], dict(CFG))
    feed_block(app, "C", True)
    check("block C on -> switch 4 latched on", app.latched.get("4") is True,
          "latched=%r" % app.latched)
    feed_block(app, "A", True)
    check("block A on -> switch 1 latched on (multi-block mirror works)",
          app.latched.get("1") is True, "latched=%r" % app.latched)
    feed_block(app, "C", False)
    check("block C off -> switch 4 latched off", app.latched.get("4") is False)


def test_every_block_page_maps():
    reset_bidir()
    bindings = [(name, effect_binding(name)) for name in kemper._BLOCK_ONOFF]
    app = FakeApp(bindings, dict(CFG))
    ok = True
    for block in kemper._BLOCK_ONOFF:
        app.latched = {}
        feed_block(app, block, True)
        if app.latched.get(block) is not True:
            ok = False
            break
    check("all 8 effect blocks (A-D,X,Mod,Delay,Reverb) mirror their switch", ok,
          "stuck at block; latched=%r" % app.latched)


def test_block_reply_latches_only_exact_bindings_including_multi_action():
    reset_bidir()
    multi = {"actions": {"toggle_on": {"messages": [
        {"type": "kemper_effect_toggle", "slot": "X", "value": "on"},
        {"type": "kemper_effect_toggle", "slot": "Mod", "value": "on"},
    ]}}}
    app = FakeApp([
        ("x", effect_binding("X")),
        ("mod", effect_binding("Mod")),
        ("both", multi),
    ], dict(CFG))

    kemper.on_midi_in(0, 1, 0xB0, [kemper._EFFECT_CC["X"], 127], app)
    check("CC X latches X + multi bindings, never the distinct Mod binding",
          app.latched_updates == [("x", True), ("both", True)],
          "latched=%r" % app.latched_updates)

    app.latched_updates = []
    feed_block(app, "Mod", True)
    check("SysEx Mod latches Mod + multi bindings, never the distinct X binding",
          app.latched_updates == [("mod", True), ("both", True)],
          "latched=%r" % app.latched_updates)

    app.latched_updates = []
    kemper._BIDIR_STATE["reconcile_pending"] = ("X", "Mod")
    kemper._BIDIR_STATE["reconcile_generation"] = 0
    kemper._BIDIR_STATE["reconcile_attempt"] = 1
    feed_block(app, "X", False)
    check("reconcile X reply remains exactly isolated too",
          app.latched_updates == [("x", False), ("both", False)]
          and kemper._BIDIR_STATE["reconcile_pending"] == ("Mod",),
          "latched=%r pending=%r" % (
              app.latched_updates,
              kemper._BIDIR_STATE["reconcile_pending"]))


def test_reconcile_republishes_unchanged_block_as_fresh():
    """A queried value equal to the previous rig still belongs to the new rig."""
    reset_bidir()
    app = FakeApp([("3", effect_binding("X"))], dict(CFG))
    app.now_ms = 1000
    feed_block(app, "X", True)
    app.context_updates = []
    # Patch loading resets every physical switch even though the persistent
    # Kemper cache still says X=on.
    app.latched["3"] = False
    kemper._BIDIR_STATE["reconcile_pending"] = ("X",)
    kemper._BIDIR_STATE["reconcile_generation"] = 0
    kemper._BIDIR_STATE["reconcile_attempt"] = 1

    feed_block(app, "X", True)

    check("targeted reconcile republishes unchanged X=on",
          app.context_updates == [{"kemper_block_X": "on"}],
          "updates=%r" % app.context_updates)
    check("targeted reconcile restores the Captain's physical latched state",
          app.latched.get("3") is True, "latched=%r" % app.latched)


def test_failed_publication_keeps_reply_pending_for_retry():
    reset_bidir()

    class FailingApp(FakeApp):
        fail_once = True
        def update_context(self, updates):
            if self.fail_once and "kemper_block_X" in updates:
                self.fail_once = False
                raise MemoryError("fragmented display context")
            super().update_context(updates)

    app = FailingApp([("3", effect_binding("X"))], dict(CFG))
    app.now_ms = 1000
    kemper.on_patch_switch_started(app, "editor", True)
    app.now_ms += kemper._SETTLE_MS
    kemper.tick(app, app.now_ms)
    try:
        feed_block(app, "X", True)
    except MemoryError:
        pass  # PluginRegistry supplies this isolation on-device.
    check("failed context publication leaves X pending and uncommitted",
          kemper._BIDIR_STATE["reconcile_pending"] == ("X",)
          and "kemper_block_X" not in kemper._BIDIR_STATE["published"],
          "state=%r" % kemper._BIDIR_STATE)

    feed_block(app, "X", True)
    check("repeated X reply completes after transient allocation failure",
          kemper._BIDIR_STATE["reconcile_pending"] == ()
          and app.context.get("kemper_block_X") == "on"
          and app.latched.get("3") is True,
          "state=%r context=%r latched=%r" % (
              kemper._BIDIR_STATE, app.context, app.latched))


def test_patch_start_invalidates_old_public_blocks_and_arms_reconcile():
    """A GET_CONTEXT after patch_switched must not expose old-rig effects."""
    reset_bidir()
    app = FakeApp([("3", effect_binding("X"))], dict(CFG))
    app.now_ms = 1000
    app.context.update({
        "bank": 1,
        "slot": 2,
        "kemper_block_X": "on",
        "kemper_block_Reverb": "on",
    })
    app._pending_context_updates.update({
        "slot": 2,
        "kemper_block_X": "on",
        "kemper_block_Reverb": "on",
    })
    kemper._BLOCK_STATE.update({"X": True, "Reverb": True})
    kemper._BIDIR_STATE["published"].update({
        "kemper_block_X": "on", "kemper_block_Reverb": "on",
        "kemper_connected": "on",
    })

    kemper.on_patch_switch_started(app, "editor", True)

    check("patch start removes stale public Kemper block fields",
          not any(k.startswith("kemper_block_") for k in app.context),
          "context=%r" % app.context)
    check("patch start removes stale unsent block deltas only",
          app._pending_context_updates == {"slot": 2},
          "pending=%r" % app._pending_context_updates)
    check("patch start retains private cache for reconciliation",
          kemper._BLOCK_STATE == {"X": True, "Reverb": True},
          "cache=%r" % kemper._BLOCK_STATE)
    check("patch start invalidates block de-duplication generation only",
          kemper._BIDIR_STATE["published"] == {"kemper_connected": "on"},
          "published=%r" % kemper._BIDIR_STATE["published"])
    check("local patch start arms a fresh settle generation",
          kemper._BIDIR_STATE["settle_until_ms"] == 1000 + kemper._SETTLE_MS,
          "state=%r" % kemper._BIDIR_STATE)

    app.now_ms += kemper._SETTLE_MS
    kemper.tick(app, app.now_ms)
    queried = [m for m in app.midi.sysex
               if len(m) > 8 and m[5] == kemper._FN_SINGLE_PARAM_REQUEST]
    check("fresh generation queries the new patch's bound block",
          len(queried) == 1
          and queried[0][7:9] == kemper._BLOCK_ONOFF["X"],
          "sysex=%r" % (queried,))


def test_rig_name_burst_cannot_overwrite_new_patch_during_settle():
    """The Player can repeat the OLD name before announcing the loaded rig."""
    reset_bidir()
    app = FakeApp([], dict(CFG))
    app.now_ms = 1000
    app.current_slot = 2
    app.context.update({
        "patch_name": "ACOUSTIC",
        "kemper_rig_name": "CLEAN",
    })
    kemper._RIG_INFO["name"] = "CLEAN"
    kemper._RIG_INFO["rig"] = 1
    kemper._BIDIR_STATE["published"].update({
        "patch_name": "CLEAN",
        "kemper_rig_name": "CLEAN",
    })

    kemper.on_patch_switch_started(app, "editor", True)
    # app.py writes the selected patch's local name immediately after the hook.
    app.context["patch_name"] = "ACOUSTIC"
    app.context_updates = []

    app.now_ms = 1050
    feed_sysex(app, string_response(kemper._PAGE_STRINGS,
                                   kemper._ADDR_RIG_NAME, "CLEAN"))
    app.now_ms = 1166
    feed_sysex(app, string_response(kemper._PAGE_STRINGS,
                                   kemper._ADDR_RIG_NAME, "ACOUSTIC"))

    name_updates = [u for u in app.context_updates
                    if "patch_name" in u or "kemper_rig_name" in u]
    check("old/new rig-name burst is quarantined for the whole settle window",
          name_updates == [] and app.context.get("patch_name") == "ACOUSTIC",
          "updates=%r context=%r" % (name_updates, app.context))
    info = kemper.get_rig_info(app, request=True)
    check("quarantined old name is never tagged fresh for the new rig",
          info["name"] == "CLEAN" and info["rig"] == 1
          and info["fresh"] is False and app.midi.sysex == [],
          "info=%r sysex=%r" % (info, app.midi.sysex))

    app.now_ms = 1000 + kemper._SETTLE_MS
    kemper.tick(app, app.now_ms)
    check("only the last stable rig name is committed after settle",
          app.context.get("patch_name") == "ACOUSTIC"
          and app.context.get("kemper_rig_name") == "ACOUSTIC",
          "updates=%r context=%r" % (app.context_updates, app.context))


def test_late_previous_generation_reply_is_not_current_confirmation():
    """A response has no request ID: an old X reply must not certify new X."""
    reset_bidir()
    app = FakeApp([("3", effect_binding("X"))], dict(CFG))
    app.now_ms = 1000
    kemper._BLOCK_STATE["X"] = False
    kemper.on_patch_switch_started(app, "editor", True)

    # Generation 1 issues an X query; leave its reply in flight.
    app.now_ms = 1000 + kemper._SETTLE_MS
    kemper.tick(app, app.now_ms)
    check("first generation has an in-flight X query",
          kemper._BIDIR_STATE.get("reconcile_pending") == ("X",),
          "state=%r" % kemper._BIDIR_STATE)

    # Switch again immediately. At generation 2 settle expiry, the old
    # implementation issued another indistinguishable X query. The delayed
    # OFF reply below was then consumed as if it confirmed generation 2.
    app.now_ms += 10
    app.current_slot = 2
    kemper.on_patch_switch_started(app, "editor", True)
    app.context_updates = []
    app.latched_updates = []
    app.now_ms += kemper._SETTLE_MS
    kemper.tick(app, app.now_ms)
    feed_block(app, "X", False)  # delayed generation-1 reply

    block_updates = [u for u in app.context_updates if "kemper_block_X" in u]
    check("late prior-generation X reply is quarantined, never published OFF",
          block_updates == [] and ("3", False) not in app.latched_updates,
          "updates=%r latched=%r state=%r" % (
              app.context_updates, app.latched_updates, kemper._BIDIR_STATE))

    # Once the old request's bounded retirement window has elapsed, query the
    # current generation and accept only that later answer.
    retire = kemper._BIDIR_STATE.get("orphan_until_ms", app.now_ms)
    app.now_ms = max(app.now_ms + 1, retire)
    kemper.tick(app, app.now_ms)
    feed_block(app, "X", True)
    block_updates = [u for u in app.context_updates if "kemper_block_X" in u]
    check("fresh generation X reply is the first state exposed to Stage",
          block_updates == [{"kemper_block_X": "on"}]
          and app.latched.get("3") is True,
          "updates=%r latched=%r state=%r" % (
              block_updates, app.latched, kemper._BIDIR_STATE))


def test_rapid_switch_orphan_stress_converges_without_false_edges():
    reset_bidir()
    app = FakeApp([("3", effect_binding("X"))], dict(CFG))
    app.now_ms = 1000
    ok = True
    detail = ""
    for cycle in range(100):
        # Generation A gets as far as issuing X, then generation B supersedes
        # it. Deliver A's opposite reply only after B's settle has elapsed.
        app.current_slot = 1 if cycle & 1 else 2
        kemper.on_patch_switch_started(app, "editor", True)
        app.now_ms += kemper._SETTLE_MS
        kemper.tick(app, app.now_ms)

        app.now_ms += 10
        app.current_slot = 2 if app.current_slot == 1 else 1
        expected = bool(cycle & 1)
        kemper.on_patch_switch_started(app, "editor", True)
        app.context_updates = []
        app.latched_updates = []
        app.now_ms += kemper._SETTLE_MS
        kemper.tick(app, app.now_ms)
        feed_block(app, "X", not expected)
        if (app.context_updates or app.latched_updates):
            ok = False
            detail = "cycle=%d leaked=%r/%r" % (
                cycle, app.context_updates, app.latched_updates)
            break

        app.now_ms = kemper._BIDIR_STATE["orphan_until_ms"]
        kemper.tick(app, app.now_ms)
        feed_block(app, "X", expected)
        states = [u["kemper_block_X"] for u in app.context_updates
                  if "kemper_block_X" in u]
        wanted = "on" if expected else "off"
        if states != [wanted] or app.latched.get("3") is not expected:
            ok = False
            detail = "cycle=%d states=%r latch=%r" % (
                cycle, states, app.latched)
            break

        # Retire B too, then start the next pair from a clean wire epoch.
        app.now_ms = kemper._BIDIR_STATE["query_retire_ms"]
        kemper.tick(app, app.now_ms)
        app.now_ms += 1
        app.midi.sysex = []

    check("100 rapid A/B generations never expose an orphan reply",
          ok, detail)


def test_missing_replies_retry_bound_blocks_then_recover_from_cache():
    """Dropped query replies recover in-bounds without querying unbound slots."""
    reset_bidir()
    app = FakeApp([("3", effect_binding("X"))], dict(CFG))
    app.now_ms = 1000
    kemper._BLOCK_STATE.update({"X": False, "Reverb": False})
    kemper.on_patch_switch_started(app, "editor", True)
    # A trustworthy current-generation delta arrives in the load burst, but
    # its UI publication stays gated. If both later query replies are lost,
    # the fallback may use this stamped observation (never the old cache).
    app.now_ms += 100
    feed_block(app, "X", True)

    app.now_ms = 1000 + kemper._SETTLE_MS
    kemper.tick(app, app.now_ms)  # attempt 1, deliberately unanswered
    app.now_ms += kemper._RECONCILE_REPLY_MS
    kemper.tick(app, app.now_ms)  # retry, deliberately unanswered
    app.now_ms += kemper._RECONCILE_REPLY_MS
    kemper.tick(app, app.now_ms)  # bounded cache recovery

    queries = [m[7:9] for m in app.midi.sysex
               if len(m) > 8 and m[5] == kemper._FN_SINGLE_PARAM_REQUEST
               and tuple(m[7:9]) in kemper._BLOCK_ONOFF.values()]
    check("missing reply retries only the bound X block",
          queries == [kemper._BLOCK_ONOFF["X"], kemper._BLOCK_ONOFF["X"]],
          "queries=%r" % (queries,))
    check("bounded retry fallback restores X on Captain and Stage",
          app.latched.get("3") is True
          and app.context.get("kemper_block_X") == "on",
          "latched=%r context=%r state=%r" % (
              app.latched, app.context, kemper._BIDIR_STATE))
    check("unbound cached Reverb is never exposed by rig recovery",
          "kemper_block_Reverb" not in app.context,
          "context=%r" % app.context)


def test_reconcile_queries_every_block_in_multi_action_binding():
    reset_bidir()
    binding = {"actions": {"toggle_on": {"messages": [
        {"type": "kemper_effect_toggle", "slot": "X", "value": "on"},
        {"type": "kemper_effect_toggle", "slot": "Mod", "value": "on"},
    ]}}}
    app = FakeApp([("3", binding)], dict(CFG))
    app.now_ms = 1000
    kemper.on_patch_switch_started(app, "editor", True)
    app.now_ms += kemper._SETTLE_MS
    kemper.tick(app, app.now_ms)

    queries = [tuple(m[7:9]) for m in app.midi.sysex
               if len(m) > 8 and m[5] == kemper._FN_SINGLE_PARAM_REQUEST]
    check("multi-action binding reconciles both X and Mod",
          queries == [kemper._BLOCK_ONOFF["X"],
                      kemper._BLOCK_ONOFF["Mod"]],
          "queries=%r" % (queries,))


def test_no_bound_blocks_never_republish_old_cache_after_settle():
    reset_bidir()
    app = FakeApp([], dict(CFG))
    app.now_ms = 1000
    kemper._BLOCK_STATE["X"] = True
    kemper._BLOCK_GENERATION["X"] = 0
    kemper._BIDIR_STATE["published"]["kemper_block_X"] = "on"
    kemper.on_patch_switch_started(app, "editor", True)
    app.now_ms += kemper._SETTLE_MS
    kemper.tick(app, app.now_ms)

    check("patch with no effect bindings never exposes old cached X",
          "kemper_block_X" not in app.context
          and not any("kemper_block_X" in u for u in app.context_updates),
          "context=%r updates=%r" % (app.context, app.context_updates))


def test_delayed_same_rig_pc_cannot_shorten_latest_settle():
    reset_bidir()
    app = FakeApp([("3", effect_binding("X"))], dict(CFG))
    app.now_ms = 1000
    app.last_patch_switch_ms = 1000
    kemper.on_patch_switch_started(app, "editor", True)
    kemper.update_context(
        {"type": "kemper_rig", "bank": 1, "rig": 1}, app.display_context)

    # A second selection of the same target starts a later generation. The
    # next matching PC may still be the first selection's delayed echo.
    app.now_ms = 1100
    app.last_patch_switch_ms = 1100
    kemper.on_patch_switch_started(app, "editor", True)
    kemper.update_context(
        {"type": "kemper_rig", "bank": 1, "rig": 1}, app.display_context)
    latest_deadline = 1100 + kemper._SETTLE_MS
    app.now_ms = 1150
    kemper.on_midi_in(0, 1, 0xC0, [0], app)

    check("delayed same-rig PC preserves the newest settle deadline",
          kemper._BIDIR_STATE["settle_until_ms"] == latest_deadline,
          "state=%r" % kemper._BIDIR_STATE)

    # There were two real local A commands, so both of their acknowledgements
    # are confirmations. Losing the retired token here used to make the second
    # PC look like a physical reselect and emit another patch event.
    generation = kemper._BIDIR_STATE["generation"]
    app.now_ms = 1200
    kemper.on_midi_in(0, 1, 0xC0, [0], app)
    check("two rapid same-rig commands consume two PCs without a reload",
          kemper._BIDIR_STATE["generation"] == generation
          and app.patch_switch_events == []
          and kemper._BIDIR_STATE["orphan_local_pcs"] == (),
          "state=%r events=%r" % (
              kemper._BIDIR_STATE, app.patch_switch_events))


def test_pc_after_nominal_settle_preserves_current_query_round():
    reset_bidir()
    app = FakeApp([("3", effect_binding("X"))], dict(CFG))
    app.now_ms = 1000
    app.last_patch_switch_ms = 1000
    kemper.on_patch_switch_started(app, "editor", True)
    kemper.update_context(
        {"type": "kemper_rig", "bank": 1, "rig": 1}, app.display_context)
    first_generation = kemper._BIDIR_STATE["generation"]
    app.now_ms += kemper._SETTLE_MS
    kemper.tick(app, app.now_ms)  # current-generation X query is now in flight

    app.now_ms += 50
    kemper.on_midi_in(0, 1, 0xC0, [0], app)
    check("late matching PC preserves the in-flight current generation",
          kemper._BIDIR_STATE["generation"] == first_generation
          and kemper._BIDIR_STATE["reconcile_pending"] == ("X",)
          and kemper._BIDIR_STATE["orphan_blocks"] == ()
          and kemper._BIDIR_STATE["settle_until_ms"] == 0,
          "state=%r" % kemper._BIDIR_STATE)
    app.now_ms += 3
    feed_block(app, "X", True)
    check("reply immediately following the semantic PC is accepted",
          app.context.get("kemper_block_X") == "on"
          and app.latched.get("3") is True,
          "context=%r latched=%r state=%r" % (
              app.context, app.latched, kemper._BIDIR_STATE))


def test_pc_echo_between_query_and_replies_keeps_current_round_live():
    """Reproduce the real CLEAN wire ordering captured on the RPi hub.

    The local PC is sent after the generation boundary.  About 371 ms later
    the Captain queries Reverb and X; the Player's matching PC confirmation
    follows at 460 ms, then the two correct query replies at 463/467 ms.  A PC
    confirmation is not a new generation and must not turn those already-sent
    current-generation queries into orphan queries.
    """
    reset_bidir()
    app = FakeApp([
        ("UP", effect_binding("Reverb")),
        ("3", effect_binding("X")),
    ], dict(CFG))
    app.current_slot = 2
    app.context["slot"] = 2
    app.now_ms = 1000
    kemper.on_patch_switch_started(app, "editor", True)
    kemper.update_context(
        {"type": "kemper_rig", "bank": 1, "rig": 2}, app.display_context)

    app.now_ms = 1466                 # PC+337 ms: rig-burst Reverb off
    feed_block(app, "Reverb", False)
    # The fixed settle deadline was armed before the comparatively slow patch
    # load/on-enter path; this models the observed query at PC+371 ms.
    app.now_ms = 1000 + kemper._SETTLE_MS
    kemper.tick(app, app.now_ms)
    check("real CLEAN ordering has Reverb/X queries pending before PC echo",
          kemper._BIDIR_STATE["reconcile_pending"] == ("Reverb", "X"),
          "state=%r" % kemper._BIDIR_STATE)

    app.now_ms = 1589                 # PC+460 ms in the captured wire trace
    kemper.on_midi_in(0, 1, 0xC0, [1], app)
    app.now_ms = 1592                 # PC+463 ms: Reverb query reply
    feed_block(app, "Reverb", False)
    app.now_ms = 1596                 # PC+467 ms: X query reply
    feed_block(app, "X", True)

    check("semantic PC keeps its current-generation replies consumable",
          kemper._BIDIR_STATE["reconcile_pending"] == ()
          and kemper._BIDIR_STATE["orphan_blocks"] == (),
          "state=%r" % kemper._BIDIR_STATE)
    check("captured Reverb-off/X-on replies converge Captain and Stage",
          app.context.get("kemper_block_Reverb") == "off"
          and app.context.get("kemper_block_X") == "on"
          and app.latched.get("UP") is False
          and app.latched.get("3") is True,
          "context=%r latched=%r updates=%r" % (
              app.context, app.latched, app.context_updates))
    app.now_ms = 2700                 # old code waited to here and re-queried
    kemper.tick(app, app.now_ms)
    queries = [m for m in app.midi.sysex
               if len(m) > 8 and m[5] == kemper._FN_SINGLE_PARAM_REQUEST
               and tuple(m[7:9]) in kemper._BLOCK_ONOFF.values()]
    check("accepted PC-adjacent replies need no 1.2s recovery round",
          len(queries) == 2, "queries=%r state=%r" % (
              queries, kemper._BIDIR_STATE))


def test_pc_confirmation_keeps_prior_generation_query_fenced():
    """The fast current-round path must not release an older generation."""
    reset_bidir()
    app = FakeApp([("3", effect_binding("X"))], dict(CFG))
    app.now_ms = 1000
    kemper.on_patch_switch_started(app, "editor", True)
    kemper.update_context(
        {"type": "kemper_rig", "bank": 1, "rig": 1}, app.display_context)
    app.now_ms = 1000 + kemper._SETTLE_MS
    kemper.tick(app, app.now_ms)             # generation-1 X query in flight

    app.now_ms += 10
    app.current_slot = 2
    app.context["slot"] = 2
    kemper.on_patch_switch_started(app, "editor", True)
    kemper.update_context(
        {"type": "kemper_rig", "bank": 1, "rig": 2}, app.display_context)
    orphan_deadline = kemper._BIDIR_STATE["orphan_until_ms"]

    app.now_ms += 90
    kemper.on_midi_in(0, 1, 0xC0, [1], app)  # generation-2 PC confirmation
    app.now_ms += 1
    feed_block(app, "X", False)              # delayed generation-1 reply
    check("semantic PC does not release a prior-generation orphan reply",
          kemper._BIDIR_STATE["orphan_blocks"] == ("X",)
          and not any("kemper_block_X" in u for u in app.context_updates)
          and app.latched_updates == [],
          "state=%r updates=%r latched=%r" % (
              kemper._BIDIR_STATE, app.context_updates, app.latched_updates))

    app.now_ms = orphan_deadline
    kemper.tick(app, app.now_ms)
    app.now_ms += 1
    feed_block(app, "X", True)
    check("fresh query after the orphan deadline still converges",
          app.context.get("kemper_block_X") == "on"
          and app.latched.get("3") is True,
          "context=%r latched=%r" % (app.context, app.latched))


def test_2342ms_pc_echo_is_consumed_once_without_patch_reload():
    """Reproduce the real B1/R1 -> C=on -> delayed same-PC regression."""
    reset_bidir()
    app = FakeApp([("harm", effect_binding("C"))], dict(CFG))
    app.now_ms = 1000
    kemper.on_patch_switch_started(app, "editor", True)
    kemper.update_context(
        {"type": "kemper_rig", "bank": 1, "rig": 1}, app.display_context)
    generation = kemper._BIDIR_STATE["generation"]

    app.now_ms = 1100
    feed_block(app, "C", True)
    app.now_ms = 1500
    kemper.tick(app, app.now_ms)
    app.now_ms = 1510
    feed_block(app, "C", True)
    check("HARM is on before the delayed PC confirmation",
          app.context.get("kemper_block_C") == "on"
          and app.latched.get("harm") is True,
          "context=%r latched=%r" % (app.context, app.latched))

    app.context_updates = []
    app.latched_updates = []
    app.now_ms = 3342  # 2.342 s after the local patch event
    kemper.on_midi_in(0, 1, 0xC0, [0], app)

    check("2342ms matching PC is confirmation, not a second patch switch",
          kemper._BIDIR_STATE["generation"] == generation
          and kemper._BIDIR_STATE["awaiting_local_pc"] is None
          and app.switch_patch_calls == []
          and app.patch_switch_events == [],
          "state=%r switches=%r events=%r" % (
              kemper._BIDIR_STATE,
              app.switch_patch_calls, app.patch_switch_events))
    check("delayed confirmation preserves the already-published HARM state",
          app.context.get("kemper_block_C") == "on"
          and app.latched.get("harm") is True
          and "kemper_block_C" in kemper._BIDIR_STATE["published"],
          "context=%r latched=%r" % (app.context, app.latched))

    # The token is single-use. A subsequent same-PC is a real physical
    # reselect and must still be allowed to reload the current patch.
    app.now_ms = 3400
    kemper.on_midi_in(0, 1, 0xC0, [0], app)
    check("a subsequent physical same-rig reselect remains legitimate",
          kemper._BIDIR_STATE["generation"] == generation + 1
          and app.patch_switch_events == [(1, 1, "midi_in")]
          and app.switch_patch_calls[-1][-1] is True,
          "state=%r switches=%r events=%r" % (
              kemper._BIDIR_STATE, app.switch_patch_calls,
              app.patch_switch_events))


def test_different_external_pc_is_not_swallowed_by_local_confirmation():
    reset_bidir()
    app = FakeApp([], dict(CFG))
    app.now_ms = 1000
    kemper.on_patch_switch_started(app, "editor", True)
    kemper.update_context(
        {"type": "kemper_rig", "bank": 1, "rig": 1}, app.display_context)
    app.now_ms = 1100
    kemper.on_midi_in(0, 1, 0xC0, [2], app)
    check("different external PC still follows bank 1 rig 3",
          app.current_bank == 1 and app.current_slot == 3
          and app.patch_switch_events == [(1, 3, "midi_in")],
          "current=%r/%r events=%r" % (
              app.current_bank, app.current_slot, app.patch_switch_events))


def test_external_pc_quarantines_superseded_local_echo():
    reset_bidir()
    app = FakeApp([], dict(CFG))
    app.now_ms = 1000
    kemper.on_patch_switch_started(app, "editor", True)
    kemper.update_context(
        {"type": "kemper_rig", "bank": 1, "rig": 1}, app.display_context)

    app.now_ms = 1100
    kemper.on_midi_in(0, 1, 0xC0, [2], app)  # genuine external B1/R3
    generation = kemper._BIDIR_STATE["generation"]
    events = list(app.patch_switch_events)
    app.now_ms = 3342
    kemper.on_midi_in(0, 1, 0xC0, [0], app)  # late local B1/R1 echo

    check("local A -> external B -> late A echo stays on B",
          (app.current_bank, app.current_slot) == (1, 3)
          and kemper._BIDIR_STATE["generation"] == generation
          and app.patch_switch_events == events,
          "current=%r/%r state=%r events=%r" % (
              app.current_bank, app.current_slot,
              kemper._BIDIR_STATE, app.patch_switch_events))


def test_expired_orphan_pc_is_followed_as_external():
    reset_bidir()
    app = FakeApp([], dict(CFG))
    app.now_ms = 1000
    kemper.on_patch_switch_started(app, "editor", True)
    kemper.update_context(
        {"type": "kemper_rig", "bank": 1, "rig": 1}, app.display_context)
    app.now_ms = 1100
    kemper.on_midi_in(0, 1, 0xC0, [2], app)

    app.now_ms = kemper._BIDIR_STATE["orphan_local_pcs"][0][1] + 1
    kemper.on_midi_in(0, 1, 0xC0, [0], app)
    check("expired orphan target becomes a genuine external selection",
          (app.current_bank, app.current_slot) == (1, 1)
          and app.patch_switch_events == [
              (1, 3, "midi_in"), (1, 1, "midi_in")],
          "current=%r/%r state=%r events=%r" % (
              app.current_bank, app.current_slot,
              kemper._BIDIR_STATE, app.patch_switch_events))


def test_expired_active_pc_is_a_real_same_rig_reselect():
    reset_bidir()
    app = FakeApp([], dict(CFG))
    app.now_ms = 1000
    kemper.on_patch_switch_started(app, "editor", True)
    kemper.update_context(
        {"type": "kemper_rig", "bank": 1, "rig": 1}, app.display_context)
    generation = kemper._BIDIR_STATE["generation"]

    app.now_ms = kemper._BIDIR_STATE["awaiting_local_pc"][2] + 1
    kemper.on_midi_in(0, 1, 0xC0, [0], app)
    check("expired active token cannot swallow a later physical reselect",
          kemper._BIDIR_STATE["generation"] == generation + 1
          and app.patch_switch_events == [(1, 1, "midi_in")]
          and app.switch_patch_calls[-1][-1] is True,
          "state=%r switches=%r events=%r" % (
              kemper._BIDIR_STATE, app.switch_patch_calls,
              app.patch_switch_events))


def test_missing_patch_navigation_does_not_arm_local_echo_token():
    reset_bidir()
    app = FakeApp([], dict(CFG))
    app.now_ms = 1000
    kemper.on_navigate(app, 1, 2)
    check("on_navigate leaves missing-patch PC authoritative",
          kemper._BIDIR_STATE["awaiting_local_pc"] is None)
    app.now_ms = 1100
    kemper.on_midi_in(0, 1, 0xC0, [1], app)
    check("missing-patch navigation PC follows the external path",
          (app.current_bank, app.current_slot) == (1, 2)
          and app.patch_switch_events == [(1, 2, "midi_in")],
          "current=%r/%r events=%r" % (
              app.current_bank, app.current_slot, app.patch_switch_events))


def test_patch_start_boundary_gates_boot_and_midi_in_arming():
    reset_bidir()
    app = FakeApp([("3", effect_binding("X"))], dict(CFG))
    app.now_ms = 1000
    kemper.on_patch_switch_started(app, "boot", False)
    check("authoritative boot does not arm an outbound reconcile",
          kemper._BIDIR_STATE["settle_until_ms"] == 0)

    kemper._arm(app)
    armed = kemper._BIDIR_STATE["settle_until_ms"]
    app.now_ms = 1100
    kemper.on_patch_switch_started(app, "midi_in", False)
    check("MIDI-in patch load preserves its pre-armed deadline",
          kemper._BIDIR_STATE["settle_until_ms"] == armed,
          "state=%r" % kemper._BIDIR_STATE)


def test_live_cc_fences_stale_query_reply_without_on_off_on():
    """A query started before a live toggle must not undo that newer toggle."""
    reset_bidir()
    app = FakeApp([("3", effect_binding("X"))], dict(CFG))
    app.now_ms = 1500
    kemper._BIDIR_STATE["settle_until_ms"] = 1500
    kemper.tick(app, 1500)                 # sends the targeted X query
    check("X query is pending before the toggle race",
          kemper._BIDIR_STATE["reconcile_pending"] == ("X",))
    app.context_updates = []
    app.latched_updates = []

    # Exact real-world failure ordering: live CC says ON, the already-issued
    # query answers with its older OFF snapshot, then the live SysEx says ON.
    app.now_ms = 1510
    kemper.on_midi_in(0, 1, 0xB0, [kemper._EFFECT_CC["X"], 127], app)
    app.now_ms = 1520
    feed_block(app, "X", False)
    app.now_ms = 1530
    feed_block(app, "X", True)

    block_updates = [u["kemper_block_X"] for u in app.context_updates
                     if "kemper_block_X" in u]
    check("stale query reply cannot produce Stage ON-OFF-ON",
          block_updates == ["on"], "updates=%r" % block_updates)
    check("stale query reply never writes the Captain LED off",
          ("3", False) not in app.latched_updates,
          "latched=%r" % app.latched_updates)
    check("live value remains authoritative in the block cache",
          kemper._BLOCK_STATE.get("X") is True,
          "cache=%r" % kemper._BLOCK_STATE)

    app.now_ms = 1540
    feed_block(app, "X", False)
    check("guard consumes one stale reply but accepts the next real SysEx",
          app.context.get("kemper_block_X") == "off"
          and kemper._BLOCK_STATE.get("X") is False,
          "context=%r cache=%r guards=%r" % (
              app.context, kemper._BLOCK_STATE, kemper._QUERY_GUARDS))

    # The fence is bounded: a genuinely later state must still be accepted.
    app.now_ms = 1510 + kemper._QUERY_GUARD_MS
    kemper.tick(app, app.now_ms)
    feed_block(app, "X", False)
    check("a state received after the stale-response fence is accepted",
          app.context.get("kemper_block_X") == "off",
          "context=%r" % app.context)


def test_live_cc_fences_every_inflight_retry_reply():
    """Two outstanding query rounds must not undo one newer live CC."""
    reset_bidir()
    app = FakeApp([("3", effect_binding("X"))], dict(CFG))
    app.now_ms = 1500
    kemper._BIDIR_STATE["settle_until_ms"] = app.now_ms
    kemper.tick(app, app.now_ms)  # query round 1
    app.now_ms += kemper._RECONCILE_REPLY_MS
    kemper.tick(app, app.now_ms)  # query round 2
    check("two X query replies can be simultaneously in flight",
          kemper._BIDIR_STATE["reconcile_attempt"] == 2
          and kemper._BIDIR_STATE["reconcile_pending"] == ("X",),
          "state=%r" % kemper._BIDIR_STATE)
    app.context_updates = []
    app.latched_updates = []

    app.now_ms += 10
    kemper.on_midi_in(0, 1, 0xB0,
                      [kemper._EFFECT_CC["X"], 127], app)
    app.now_ms += 10
    feed_block(app, "X", False)  # late reply to query 1
    app.now_ms += 10
    feed_block(app, "X", False)  # late reply to query 2

    block_updates = [u["kemper_block_X"] for u in app.context_updates
                     if "kemper_block_X" in u]
    check("all outstanding stale replies remain fenced after live X=on",
          block_updates == ["on"]
          and app.context.get("kemper_block_X") == "on"
          and ("3", False) not in app.latched_updates,
          "updates=%r context=%r latched=%r" % (
              block_updates, app.context, app.latched_updates))


def test_reconcile_fallback_never_promotes_previous_rig_cache():
    """A timeout must stay unknown rather than claim stale state is fresh."""
    reset_bidir()
    app = FakeApp([("3", effect_binding("X"))], dict(CFG))
    app.now_ms = 1000
    kemper._BLOCK_STATE["X"] = True
    kemper._BLOCK_GENERATION["X"] = 0
    kemper._BIDIR_STATE["generation"] = 1
    kemper._BIDIR_STATE["reconcile_generation"] = 1
    kemper._BIDIR_STATE["published"]["kemper_block_X"] = "on"
    kemper._BIDIR_STATE["reconcile_pending"] = ("X",)
    kemper._BIDIR_STATE["reconcile_fallback_ms"] = 1000
    kemper._BIDIR_STATE["reconcile_attempt"] = kemper._RECONCILE_ATTEMPTS
    app.latched["3"] = False

    kemper.tick(app, 1000)

    check("fallback does not restore stale previous-rig X on Captain",
          app.latched.get("3") is False,
          "latched=%r" % app.latched)
    check("fallback does not publish stale previous-rig X to Stage",
          not any("kemper_block_X" in u for u in app.context_updates),
          "updates=%r" % app.context_updates)


def test_idle_patch_reload_republishes_unchanged_bound_state():
    """A non-rig reload may repaint a cache when no reconcile is active."""
    reset_bidir()
    app = FakeApp([("3", effect_binding("X"))], dict(CFG))
    app.now_ms = 1000
    kemper._BLOCK_STATE["X"] = True
    kemper._BLOCK_GENERATION["X"] = 0
    kemper._BIDIR_STATE["published"]["kemper_block_X"] = "on"
    app.latched["3"] = False

    kemper.on_patch_loaded(app)

    check("idle patch reload restores the physical X LED",
          app.latched.get("3") is True, "latched=%r" % app.latched)
    check("idle patch reload republishes unchanged X=on to Stage",
          {"kemper_block_X": "on"} in app.context_updates,
          "updates=%r" % app.context_updates)


def test_real_pc_echo_defers_partial_cache_until_targeted_reply():
    """Drive the PC handler, not on_patch_loaded() directly."""
    reset_bidir()
    app = FakeApp([("3", effect_binding("X"))], dict(CFG))
    app.now_ms = 1000
    app.last_patch_switch_ms = 1000
    kemper._BLOCK_STATE["X"] = False       # deliberately stale/partial
    kemper._BIDIR_STATE["published"]["kemper_block_X"] = "on"
    kemper.on_patch_switch_started(app, "editor", True)
    kemper.update_context(
        {"type": "kemper_rig", "bank": 1, "rig": 1}, app.display_context)

    app.now_ms = 1100
    kemper.on_midi_in(0, 1, 0xC0, [0], app)  # matching local PC echo

    check("real PC echo consumes the semantic confirmation token",
          kemper._BIDIR_STATE["awaiting_local_pc"] is None,
          "state=%r" % kemper._BIDIR_STATE)
    check("PC echo does not force-publish the partial cache",
          app.context_updates == [] and app.latched_updates == [],
          "context=%r latched=%r" % (
              app.context_updates, app.latched_updates))
    check("early PC echo cannot shorten the active settle window",
          kemper._BIDIR_STATE["settle_until_ms"]
          == 1000 + kemper._SETTLE_MS,
          "settle=%r" % kemper._BIDIR_STATE["settle_until_ms"])

    app.now_ms = 1000 + kemper._SETTLE_MS
    kemper.tick(app, app.now_ms)
    check("PC echo reconciliation queries the current bound block",
          kemper._BIDIR_STATE["reconcile_pending"] == ("X",),
          "pending=%r" % (kemper._BIDIR_STATE["reconcile_pending"],))
    app.now_ms += 10
    feed_block(app, "X", True)
    check("fresh PC-echo query response is the first published snapshot",
          [u for u in app.context_updates if "kemper_block_X" in u]
          == [{"kemper_block_X": "on"}],
          "updates=%r" % app.context_updates)


def test_rig_name_reply_does_not_cancel_effect_fallback():
    """Repeated/delayed name replies must not restart rig reconciliation."""
    reset_bidir()
    app = FakeApp([("3", effect_binding("X"))], dict(CFG))
    app.now_ms = 1000
    kemper._BLOCK_STATE["X"] = True
    kemper._BLOCK_GENERATION["X"] = 0
    kemper._BIDIR_STATE["reconcile_pending"] = ("X",)
    kemper._BIDIR_STATE["reconcile_fallback_ms"] = 1400
    kemper._BIDIR_STATE["reconcile_generation"] = 0
    kemper._BIDIR_STATE["reconcile_attempt"] = kemper._RECONCILE_ATTEMPTS
    kemper._RIG_INFO["name"] = "CLEAN"
    kemper._RIG_INFO["rig"] = 1

    feed_sysex(app, string_response(kemper._PAGE_STRINGS,
                                   kemper._ADDR_RIG_NAME, "CLEAN"))

    check("rig-name reply preserves the armed effect fallback",
          kemper._BIDIR_STATE["reconcile_pending"] == ("X",)
          and kemper._BIDIR_STATE["reconcile_fallback_ms"] == 1400,
          "pending=%r fallback=%r" % (
              kemper._BIDIR_STATE["reconcile_pending"],
              kemper._BIDIR_STATE["reconcile_fallback_ms"]))
    kemper.tick(app, 1400)
    check("preserved fallback still republishes X=on",
          {"kemper_block_X": "on"} in app.context_updates,
          "updates=%r" % app.context_updates)


def test_duplicate_rig_name_after_reconcile_does_not_rearm():
    reset_bidir()
    app = FakeApp([("3", effect_binding("X"))], dict(CFG))
    app.now_ms = 1000
    feed_sysex(app, string_response(kemper._PAGE_STRINGS,
                                   kemper._ADDR_RIG_NAME, "CLEAN"))
    app.now_ms = 1000 + kemper._SETTLE_MS
    kemper.tick(app, app.now_ms)
    app.now_ms += 10
    feed_block(app, "X", True)
    # Even a fully answered reconcile keeps its deadline as an ordering fence;
    # finish that generation before delivering the duplicate name.
    app.now_ms = 1000 + kemper._SETTLE_MS + kemper._RECONCILE_REPLY_MS
    kemper.tick(app, app.now_ms)
    check("first rig-name reconciliation fully completed",
          kemper._BIDIR_STATE["settle_until_ms"] == 0
          and kemper._BIDIR_STATE["reconcile_pending"] == ()
          and kemper._BIDIR_STATE["reconcile_fallback_ms"] == 0)

    app.now_ms += 100
    feed_sysex(app, string_response(kemper._PAGE_STRINGS,
                                   kemper._ADDR_RIG_NAME, "CLEAN"))
    check("duplicate rig name does not open another settle/query generation",
          kemper._BIDIR_STATE["settle_until_ms"] == 0
          and kemper._BIDIR_STATE["reconcile_pending"] == ()
          and kemper._BIDIR_STATE["reconcile_fallback_ms"] == 0,
          "state=%r" % kemper._BIDIR_STATE)


def test_keepalive_does_not_publish_mid_settle_cache():
    reset_bidir()
    app = FakeApp([("3", effect_binding("X"))], dict(CFG))
    app.now_ms = 5000
    kemper._BLOCK_STATE["X"] = False
    kemper._BIDIR_STATE["published"]["kemper_block_X"] = "off"
    kemper._BIDIR_STATE["init_sent"] = True
    kemper._BIDIR_STATE["confirmed"] = True
    kemper._BIDIR_STATE["last_sensed_ms"] = 4900
    kemper._BIDIR_STATE["last_beacon_ms"] = 0
    kemper._BIDIR_STATE["settle_until_ms"] = 5500

    feed_block(app, "X", True)             # cache changes, UI stays quiet
    check("block delta itself remains suppressed during settle",
          app.context_updates == [] and app.latched_updates == [])
    kemper.tick(app, 5000)                  # keepalive is due now
    check("keepalive cannot bypass settle and publish the partial cache",
          app.context_updates == [] and app.latched_updates == [],
          "context=%r latched=%r" % (
              app.context_updates, app.latched_updates))
    check("keepalive is still transmitted while cache publication is gated",
          any(len(m) > 5 and m[5] == kemper._FN_EXTENDED
              for m in app.midi.sysex),
          "sysex=%r" % app.midi.sysex)


def test_new_rig_name_arms_reconcile_when_pc_echo_is_missing():
    reset_bidir()
    app = FakeApp([("3", effect_binding("X"))], dict(CFG))
    app.current_slot = 2
    app.now_ms = 1000
    kemper._RIG_INFO["rig"] = 1

    feed_sysex(app, string_response(kemper._PAGE_STRINGS,
                                   kemper._ADDR_RIG_NAME, "CLEAN"))

    check("new rig-name arms reconcile when the PC echo is missing",
          kemper._BIDIR_STATE["settle_until_ms"] > app.now_ms,
          "settle=%r" % kemper._BIDIR_STATE["settle_until_ms"])


def test_delay_reverb_use_dedicated_pages():
    # REPRO of the real bug: a real Player reports Delay on/off at page 0x4A
    # addr 0x02 and Reverb at 0x4B/0x02 - NOT their slot pages 0x3C/0x3D
    # addr 0x03. With the old mapping the Delay/Reverb switches never lit.
    reset_bidir()
    app = FakeApp([("up", effect_binding("Delay")), ("D", effect_binding("Reverb"))], dict(CFG))
    feed_sysex(app, param_response(0x4A, 0x02, 1))   # real Delay-on frame
    check("real Delay frame (page 0x4A addr 0x02) latches the Delay switch",
          app.latched.get("up") is True, "latched=%r" % app.latched)
    feed_sysex(app, param_response(0x4B, 0x02, 1))   # real Reverb-on frame
    check("real Reverb frame (page 0x4B addr 0x02) latches the Reverb switch",
          app.latched.get("D") is True, "latched=%r" % app.latched)
    # The old (wrong) Delay page must NOT be treated as Delay on/off.
    app.latched = {}
    feed_sysex(app, param_response(0x3C, 0x03, 1))
    check("slot page 0x3C addr 0x03 is NOT the Delay on/off (no false latch)",
          app.latched == {}, "latched=%r" % app.latched)


def test_effect_cc_numbers_match_kemper_spec():
    # Authoritative values from PySwitch (CC_EFFECT_SLOT_ENABLE), verified on
    # real Kempers. DLY=27 and REV=29 - NOT 26/28. A wrong CC means pressing
    # the bosun switch toggles the wrong (or no) block on the Kemper, so the
    # block never changes and never broadcasts back -> the switch never
    # mirrors. This is the "switch up / BOOST (Delay) does nothing" bug.
    expected = {"A": 17, "B": 18, "C": 19, "D": 20,
                "X": 22, "Mod": 24, "Delay": 27, "Reverb": 29}
    check("effect on/off CC numbers match the Kemper spec (PySwitch)",
          kemper._EFFECT_CC == expected, "got %r" % (kemper._EFFECT_CC,))


def test_inbound_cc_delay_mirrors_switch():
    reset_bidir()
    # ACOUSTIC binds switch 'up' -> Delay (BOOST). When the Kemper echoes the
    # Delay block on its CC, the switch must latch. With a wrong Delay CC the
    # echo isn't recognised and the switch stays dark.
    app = FakeApp([("up", effect_binding("Delay"))], dict(CFG))
    kemper.on_midi_in(0, 1, 0xB0, [27, 127], app)   # CC 27 = Delay ON (Kemper)
    check("inbound CC 27 (Delay on) latches the Delay-bound switch",
          app.latched.get("up") is True, "latched=%r" % app.latched)


def test_unbound_block_does_not_latch_anything():
    reset_bidir()
    # Patch binds only slot C (like ACOUSTIC). A broadcast for block A must
    # not flip any switch - this documents why "only switch 4 reacts" is
    # correct when only switch 4 controls a Kemper block.
    app = FakeApp([("4", effect_binding("C"))], dict(CFG))
    feed_block(app, "A", True)
    check("block A on with no switch bound to A -> no latch", app.latched == {},
          "latched=%r" % app.latched)


# ---------------- tuner ----------------

def test_tuner_mode_idle_value3_is_off():
    # REPRO: a real Player sends tuner-mode value 3 when the tuner is OFF
    # (browse/normal). The old `value != 0` test lit the tuner permanently.
    reset_bidir()
    app = FakeApp([], dict(CFG))
    feed_sysex(app, param_response(kemper._PAGE_SYSTEM, kemper._ADDR_TUNER_MODE, 3))
    check("tuner mode value 3 (idle) -> kemper_tuner 'off'",
          app.context.get("kemper_tuner") == "off", "ctx=%r" % app.context)


def test_tuner_mode_value1_is_on():
    reset_bidir()
    app = FakeApp([], dict(CFG))
    feed_sysex(app, param_response(kemper._PAGE_SYSTEM, kemper._ADDR_TUNER_MODE, 1))
    check("tuner mode value 1 -> kemper_tuner 'on'", app.context.get("kemper_tuner") == "on")


def test_tuner_telemetry_is_silent_while_off_and_live_while_on():
    reset_bidir()
    app = FakeApp([], dict(CFG))
    feed_sysex(app, param_response(kemper._PAGE_SYSTEM,
                                   kemper._ADDR_TUNER_MODE, 3))
    app.context_updates = []
    for value in ((0, 1) * 500):
        feed_sysex(app, param_response(kemper._PAGE_TUNER_DEVIANCE,
                                       kemper._ADDR_TUNER_DEVIANCE, value))
        feed_sysex(app, param_response(kemper._PAGE_TUNER_NOTE,
                                       kemper._ADDR_TUNER_NOTE, value))
    check("tuner-off note/deviance telemetry produces no context redraws",
          app.context_updates == [], "updates=%r" % app.context_updates)

    feed_sysex(app, param_response(kemper._PAGE_SYSTEM,
                                   kemper._ADDR_TUNER_MODE, 1))
    app.context_updates = []
    feed_sysex(app, param_response(kemper._PAGE_TUNER_NOTE,
                                   kemper._ADDR_TUNER_NOTE, 9))
    feed_sysex(app, param_response(kemper._PAGE_TUNER_DEVIANCE,
                                   kemper._ADDR_TUNER_DEVIANCE, 8192))
    check("tuner-on note/deviance telemetry is published",
          app.context.get("kemper_tuner_note") == "A"
          and app.context.get("kemper_tuner_deviance") == 8192
          and len(app.context_updates) == 2,
          "updates=%r" % app.context_updates)


def test_real_tuner_off_7c00_barrage_is_ignored():
    """Pin the exact unsolicited frame observed from a Player with tuner off."""
    reset_bidir()
    app = FakeApp([("x", effect_binding("X"))], dict(CFG))
    feed_sysex(app, param_response(kemper._PAGE_SYSTEM,
                                   kemper._ADDR_TUNER_MODE, 3))
    app.context_updates = []
    app.latched_updates = []
    # Exact payloads captured on ALSA, excluding the enclosing F0/F7 bytes:
    # F0 00 20 33 00 00 01 00 7C 00 00 {00,01} F7.
    frame = [0x00, 0x20, 0x33, 0x00, 0x00, 0x01,
             0x00, 0x7C, 0x00, 0x00, 0x00]
    for value in ((0, 1) * 500):
        frame[10] = value
        feed_sysex(app, frame)
    check("real tuner-off 7C/00 barrage is entirely ignored",
          app.context_updates == []
          and app.latched_updates == []
          and kemper._BLOCK_STATE == {},
          "updates=%r latched=%r blocks=%r" % (
              app.context_updates, app.latched_updates,
              kemper._BLOCK_STATE))


def test_tuner_cc_updates_telemetry_gate():
    reset_bidir()
    app = FakeApp([], dict(CFG))
    kemper.on_midi_in(0, 1, 0xB0, [31, 127], app)
    for _ in range(100):
        kemper.on_midi_in(0, 1, 0xB0, [31, 127], app)
    cc_deduped = (len(app.context_updates) == 1
                  and app.context_updates[0] == {
                      "kemper_tuner": "on", "tuner": "on"})
    app.context_updates = []
    feed_sysex(app, param_response(kemper._PAGE_TUNER_NOTE,
                                   kemper._ADDR_TUNER_NOTE, 9))
    enabled = app.context.get("kemper_tuner_note") == "A"
    kemper.on_midi_in(0, 1, 0xB0, [31, 0], app)
    app.context_updates = []
    feed_sysex(app, param_response(kemper._PAGE_TUNER_DEVIANCE,
                                   kemper._ADDR_TUNER_DEVIANCE, 1))
    check("inbound CC31 opens and closes the tuner telemetry gate",
          cc_deduped and enabled and app.context_updates == [],
          "deduped=%r enabled=%r updates=%r" % (
              cc_deduped, enabled, app.context_updates))


def test_connect_does_not_engage_tuner():
    reset_bidir()
    app = FakeApp([], dict(CFG))
    # Simulate a connect: beacon tick, then the Player's sensing keep-alive
    # and a rig-name string. NONE of these should set kemper_tuner.
    kemper.tick(app, 0)                              # init beacon
    feed_sysex(app, sensing())                       # keep-alive
    feed_sysex(app, string_response(kemper._PAGE_STRINGS, kemper._ADDR_RIG_NAME, "BRITISH PLEXI"))
    check("connect/sensing sets kemper_connected", app.context.get("kemper_connected") == "on")
    check("connect/sensing does NOT turn the tuner on",
          app.context.get("kemper_tuner") != "on", "ctx=%r" % app.context)
    check("rig name string mirrors to patch_name", app.context.get("patch_name") == "BRITISH PLEXI")


# ---------------- rig follow / bank navigation ----------------

def test_bosun_bank_step_not_reverted_by_player_echo():
    # REPRO of the reported bug: holding bank-up steps to the next bank, but it
    # immediately bounces back to the previous one. Cause: a bosun-initiated
    # bank step SENDS a Bank-LSB (CC32) to the Player, but the core only records
    # INCOMING CC32 in _last_bank_lsb. So when the Player echoes the rig as a
    # bare PC, the auto-follow resolves it against the STALE old bank LSB and
    # "follows" back to the previous bank.
    reset_bidir()
    app = FakeApp([], dict(CFG))
    # Both on bank 2 / rig 1. The last INCOMING Bank LSB the core saw was 1 (=
    # bank 2). Channel 1, port 0.
    app.now_ms = 1000
    app.current_bank = 2; app.current_slot = 1
    app.current_patch = {"bank": 2, "slot": 1}
    app._bank_lsb[(0, 1)] = 1

    # User holds bank-up -> local step to bank 3. on_enter dispatches the rig.
    app.switch_patch(3, 1, source="binding")
    kemper.on_patch_switch_started(app, "binding", True)
    msg = {"type": "kemper_rig", "bank": 3, "rig": 1, "channel": 1}
    kemper.dispatch(msg, app.midi)
    kemper.update_context(msg, app.display_context)
    check("local bank-up lands on bank 3", app.current_bank == 3,
          "current_bank=%r" % app.current_bank)

    # ~400 ms later the Player echoes the new rig as a bare PC (no fresh CC32).
    app.now_ms = 1400
    kemper.on_midi_in(0, 1, 0xC0, [10], app)  # flat PC 10 = bank 3 / rig 1
    check("Player PC echo does NOT revert the bank (stays on 3)",
          app.current_bank == 3, "reverted to bank %r" % app.current_bank)


def test_external_rig_change_is_still_followed():
    # Guard: the echo-window fix must NOT block following a genuine EXTERNAL rig
    # change made on the Player (outside the local-switch window). The Player
    # sends CC32 (recorded by the core) then PC; the bosun should follow.
    reset_bidir()
    app = FakeApp([], dict(CFG))
    app.current_bank = 1; app.current_slot = 1
    app.current_patch = {"bank": 1, "slot": 1}
    app.now_ms = 9000                            # far past any local-switch echo window
    # Flat rig list: the Player broadcasts the rig index as a bare PC. PC 11 =
    # the 12th rig = bank 3 rig 2 ((3-1)*5 + (2-1) = 11).
    kemper.on_midi_in(0, 1, 0xC0, [11], app)
    check("external rig change followed: bosun moves to bank 3 rig 2",
          app.current_bank == 3 and app.current_slot == 2,
          "current=%r/%r" % (app.current_bank, app.current_slot))


def test_beacon_emitted_on_tick():
    reset_bidir()
    app = FakeApp([], dict(CFG))
    kemper.tick(app, 0)
    check("tick emits exactly one beacon SYSEX", len(app.midi.sysex) == 1,
          "sysex=%r" % app.midi.sysex)
    if app.midi.sysex:
        b = app.midi.sysex[0]
        check("beacon is Kemper mfr + function 0x7E",
              b[0:3] == kemper._KEMPER_MFR and b[5] == kemper._FN_EXTENDED,
              "beacon=%r" % (b,))


def test_external_rig_change_opens_settle_window_before_repainting():
    # Regression (2026-08-15 report): switching rig ON THE KEMPER flashed
    # the Boost switch on (stale rig-1 cache) before correctly turning off,
    # while the SAME rig change made from the Captain's own footswitch
    # never flashed. Cause: switch_patch (called from the PC handler below)
    # calls on_patch_loaded synchronously, which paints from _BLOCK_STATE
    # immediately unless a settle window is already open. That window is
    # normally opened by the rig-name SysEx (_handle_sysex) - but on a
    # flat-list Player the bare PC arrives BEFORE the rig-name string, so
    # the paint fired from whatever the previous rig's block cache still
    # held. The PC handler must open the window itself, before calling
    # switch_patch, so this premature paint defers to tick() like every
    # other block delta.
    reset_bidir()
    app = FakeApp([], dict(CFG))
    app.now_ms = 5000
    kemper.on_midi_in(0, 1, 0xC0, [1], app)   # external rig change (flat PC=1 -> bank 1 slot 2)
    check("the PC handler opens the settle window before switch_patch can repaint from a stale cache",
          kemper._BIDIR_STATE["settle_until_ms"] > app.now_ms,
          "settle_until_ms=%r now=%r" % (kemper._BIDIR_STATE["settle_until_ms"], app.now_ms))


def test_boot_deferring_to_kemper_does_not_arm_a_false_echo_window():
    # Regression (2026-08-15 report): after the boot-authority fix, the TFT
    # correctly showed the Kemper's real rig name ("Crunch") on power-up -
    # that field comes straight from the rig-name SysEx broadcast, bypassing
    # switch_patch entirely. But the switch/effect layout stayed on the BOOT
    # patch ("rig 1 acoustic") instead of following to Crunch. Cause: boot()
    # calls switch_patch(source="boot", fire_on_enter=False) to load locally
    # without pushing MIDI - but switch_patch armed the echo-suppression
    # window regardless of fire_on_enter, so the Player's very first real PC
    # (announcing it's actually on Crunch) arrived inside that window and
    # got misread as "just the Player echoing our own boot load", which
    # RE-CONFIRMS the stale bank/slot instead of following it.
    reset_bidir()
    app = FakeApp([], dict(CFG))
    app.now_ms = 1000
    # Mirrors boot(): loads bosun's persisted rig (bank 1 / slot 1 =
    # "acoustic") but must fire no outbound MIDI.
    app.switch_patch(1, 1, source="boot", fire_on_enter=False)
    # The Player's first state announcement arrives 300 ms later - well
    # inside the old (bugged) echo window, which is exactly the case that
    # must now be FOLLOWED, not swallowed. PC 2 = flat rig 3 = bank 1 / rig
    # 3 in bosun's 5-rig grouping = "Crunch".
    app.now_ms = 1300
    kemper.on_midi_in(0, 1, 0xC0, [2], app)
    check("the Kemper's real rig (Crunch, bank 1 slot 3) is followed, not swallowed as a boot echo",
          app.current_bank == 1 and app.current_slot == 3,
          "current=%r/%r (stuck on boot patch would be 1/1)" % (app.current_bank, app.current_slot))


def test_configured_kemper_wants_authoritative_boot():
    # Regression (2026-08-15 report): on power-up bosun pushed its own
    # last-remembered rig onto the Player (defaulting to whatever bank/slot
    # 1 held), silently overriding a rig the user had dialed in directly on
    # the device (Kemper on rig 3, powered on the Captain, both ended up on
    # rig 1). A configured Kemper must claim boot authority so app.py skips
    # that push and lets the beacon's own initial state broadcast set
    # current_bank/current_slot instead.
    #
    # First version of this fix gated on cfg.get("bidirectional") - a
    # config key that CONFIG_SCHEMA explicitly does NOT expose (bilateral
    # sync is unconditional for any Kemper profile, not a user option), so
    # this test's own fixture (CFG, which sets "bidirectional": True
    # despite that not being a real field) made the bug look fixed while
    # the real device.json - which never has that key - kept hitting
    # False. Use a config WITHOUT the key, matching what config.py
    # actually stores, to keep that regression from coming back unnoticed.
    cfg = dict(CFG)
    cfg.pop("bidirectional", None)
    app = FakeApp([], cfg)
    check("a configured Kemper (no bidirectional key, matching real device.json) wants authoritative boot",
          kemper.wants_authoritative_boot(app) is True)


def test_no_kemper_device_does_not_want_authoritative_boot():
    app = FakeApp([], dict(CFG))
    app.device = {}
    check("no kemper device configured -> no boot authority claim",
          kemper.wants_authoritative_boot(app) is False)


def test_unconfirmed_link_retries_init_at_bounded_cadence():
    # Regression (2026-08-15): while `confirmed` stays False (e.g. the
    # Kemper's sensing replies never make it back through an Android
    # USB-MIDI bridge), tick() used to send a full init beacon on EVERY
    # call with no throttle - a real main loop can call tick() far more
    # often than once per second, which flooded send_sysex() badly enough
    # that the physical footswitches stopped responding.
    reset_bidir()
    app = FakeApp([], dict(CFG))
    for now_ms in range(0, 5001, 10):          # simulate a tight 5s loop
        kemper.tick(app, now_ms)
    max_expected = (5000 // kemper._INIT_RETRY_MS) + 1
    check("unconfirmed retries are bounded, not sent on every tick",
          len(app.midi.sysex) <= max_expected,
          "sent=%d, max_expected=%d" % (len(app.midi.sysex), max_expected))
    check("at least one retry actually went out",
          len(app.midi.sysex) >= 1, "sysex=%r" % app.midi.sysex)


def test_confirmed_link_uses_normal_keepalive_cadence():
    # Once the Player has confirmed the link, resends must fall back to
    # the slower _BEACON_RESEND_MS keep-alive interval, not the faster
    # unconfirmed-retry cadence - otherwise a confirmed link would keep
    # spamming the Kemper too.
    reset_bidir()
    app = FakeApp([], dict(CFG))
    kemper.tick(app, 0)                        # init beacon
    feed_sysex(app, sensing())                 # confirms the link
    sent_after_confirm = len(app.midi.sysex)
    for now_ms in range(0, kemper._BEACON_RESEND_MS, 10):
        kemper.tick(app, now_ms)
    check("confirmed link sends no keep-alive before _BEACON_RESEND_MS elapses",
          len(app.midi.sysex) == sent_after_confirm,
          "sysex=%r" % app.midi.sysex)


def _wah_requests(app):
    return [m for m in app.midi.sysex
            if len(m) == 9 and m[5:] == (0x41, 0, 5, 21)]


def _wah_ready_app(empty_slots=True):
    reset_bidir()
    app = FakeApp([], dict(CFG))
    app.now_ms = 1000
    kemper._BIDIR_STATE.update({"confirmed": True, "init_sent": True,
                               "last_sensed_ms": 1000})
    kemper._RIG_INFO.update({"rig": 1, "name": "CLEAN"})
    if empty_slots:
        # Fixed-only tests have independently confirmed eight empty slots.
        kemper._WAH.update({"generation": 0, "types": 255})
    return app


def test_fixed_wah_requires_real_reply_and_tracks_broadcasts():
    app = _wah_ready_app()
    # Exact response captured externally on ALSA from the user's Player.
    off = [0, 0x20, 0x33, 0, 0, 1, 0, 5, 0x15, 0, 0]
    feed_sysex(app, off)
    check("Wah state is unknown before a current-generation query",
          not app.context.get("expression_mode"))
    kemper.tick(app, app.now_ms)
    check("fixed Wah query uses page5 address21 decimal",
          _wah_requests(app) == [(0, 0x20, 0x33, 2, 0x7f, 0x41, 0, 5, 21)])
    feed_sysex(app, off)
    check("captured fixed Wah OFF reply indicates VOL",
          app.context.get("expression_mode") == "VOL")
    feed_sysex(app, param_response(5, 21, 1))
    check("fixed Wah ON broadcast indicates WAH",
          app.context.get("expression_mode") == "WAH")
    feed_sysex(app, param_response(5, 21, 7))
    check("invalid fixed Wah state cannot change the indicator",
          app.context.get("expression_mode") == "WAH")
    feed_sysex(app, off)
    check("fixed Wah OFF broadcast restores VOL without touching LEDs or routing",
          app.context.get("expression_mode") == "VOL"
          and not app.latched_updates and len(app.midi.sysex) == 1)
    for now in range(1010, 1500, 10):
        kemper.tick(app, now)
    check("known fixed Wah does not poll more often than500ms",
          len(_wah_requests(app)) == 1)


def test_fixed_wah_generation_fence_does_not_delay_effects():
    app = _wah_ready_app()
    kemper.tick(app, app.now_ms)
    feed_sysex(app, param_response(5, 21, 1))
    app.now_ms = 1100
    app.current_slot = 2
    app.context["slot"] = 2
    app._bindings = [("3", effect_binding("X"))]
    kemper.on_patch_switch_started(app)
    check("rig change invalidates WAH instead of showing false VOL",
          app.context.get("expression_mode") == "")
    app.now_ms += kemper._SETTLE_MS
    kemper.tick(app, app.now_ms)
    feed_block(app, "X", True)
    kemper._RIG_INFO.update({"rig": 2, "name": "NEW CLEAN"})
    kemper.tick(app, app.now_ms)
    feed_sysex(app, param_response(5, 21, 1))
    check("old Wah reply is quarantined while FLANG already converged",
          app.context.get("expression_mode") == ""
          and app.latched.get("3") is True
          and not kemper._transition_active()
          and len(_wah_requests(app)) == 1)
    app.now_ms = 2200
    kemper.tick(app, app.now_ms)
    feed_sysex(app, param_response(5, 21, 0))
    check("fixed OFF alone cannot establish VOL in a new rig",
          app.context.get("expression_mode") == "")
    for page in kemper._WAH_TYPE_PAGES:
        feed_sysex(app, param_response(page, 0, 0))
    check("new-generation Wah reply establishes VOL after old query retires",
          app.context.get("expression_mode") == "VOL"
          and len(_wah_requests(app)) == 2)


def test_fixed_wah_missing_reply_retries_are_bounded_and_nonblocking():
    app = _wah_ready_app()
    for now in range(1000, 9600, 10):
        app.now_ms = now
        kemper.tick(app, now)
    check("unsupported fixed Wah performs three spaced requests before backoff",
          len(_wah_requests(app)) == 3)
    check("missing fixed Wah reply leaves unknown mode without blocking rig state",
          not app.context.get("expression_mode")
          and not kemper._transition_active()
          and not kemper._BIDIR_STATE["reconcile_pending"])


def test_fixed_wah_waits_for_link_and_fresh_identity():
    app = _wah_ready_app()
    kemper._BIDIR_STATE["confirmed"] = False
    kemper.tick(app, 1000)
    kemper._BIDIR_STATE["confirmed"] = True
    kemper._RIG_INFO["rig"] = 2
    kemper.tick(app, 1010)
    check("fixed Wah waits for both confirmed link and current rig identity",
          not _wah_requests(app))
    kemper._RIG_INFO["rig"] = 1
    kemper.tick(app, 1020)
    feed_sysex(app, param_response(5, 21, 1))
    kemper.tick(app, 1000 + kemper._SENSING_TIMEOUT_MS + 1)
    check("link timeout removes measured WAH mode",
          app.context.get("expression_mode") == ""
          and kemper._WAH["query_generation"] == -1)


def test_fixed_wah_failed_publication_remains_retryable():
    app = _wah_ready_app()
    kemper.tick(app, app.now_ms)
    real_update = app.update_context

    def fail_once(updates):
        app.update_context = real_update
        raise MemoryError("display-context allocation")

    app.update_context = fail_once
    failed = False
    try:
        feed_sysex(app, param_response(5, 21, 1))
    except MemoryError:
        failed = True
    check("failed Wah publication does not mark unobserved display state delivered",
          failed and not kemper._WAH["known"]
          and not app.context.get("expression_mode"))
    app.now_ms += kemper._QUERY_RETIRE_MS
    kemper.tick(app, app.now_ms)
    feed_sysex(app, param_response(5, 21, 1))
    check("Wah publication recovers on the bounded query retry",
          app.context.get("expression_mode") == "WAH"
          and kemper._WAH["known"] and len(_wah_requests(app)) == 2)


def test_fixed_wah_outbound_command_does_not_guess_indicator():
    app = _wah_ready_app()
    kemper.tick(app, app.now_ms)
    feed_sysex(app, param_response(5, 21, 0))
    sent = []
    app.midi.send_cc = lambda *args: sent.append(args)
    command = {"type": "kemper_fixed_toggle", "effect": "Wah", "value": "on"}
    kemper.dispatch(command, app.midi)
    kemper.update_context(command, app.context)
    check("outbound fixed Wah preserves NRPN routing and waits for measured state",
          sent == [(1, 99, 5), (1, 98, 21), (1, 6, 0), (1, 38, 1)]
          and app.context.get("expression_mode") == "VOL")


def test_fixed_wah_expired_link_does_not_send_diagnostic_request():
    app = _wah_ready_app()
    app.now_ms = 1000 + kemper._SENSING_TIMEOUT_MS + 1
    kemper.tick(app, app.now_ms)
    check("expired sensing link never sends a Wah request before invalidating",
          not _wah_requests(app)
          and not kemper._BIDIR_STATE["confirmed"])


def test_fixed_wah_sparse_navigation_does_not_reuse_old_mode():
    app = _wah_ready_app()
    kemper.tick(app, app.now_ms)
    feed_sysex(app, param_response(5, 21, 1))
    # Real Captain returns False BEFORE updating current_bank/current_slot
    # when PatchStore.has() rejects an absent destination, including MIDI-in.
    app.switch_patch = lambda *args, **kwargs: False
    app.now_ms = 3000
    kemper.on_navigate(app, 1, 3)
    check("sparse outbound navigation immediately clears the old WAH mode",
          app.context.get("expression_mode") == "")
    kemper.tick(app, app.now_ms)
    feed_sysex(app, param_response(5, 21, 1))
    check("sparse navigation cannot query or restore old Wah before its PC",
          len(_wah_requests(app)) == 1
          and app.context.get("expression_mode") == "")
    app.now_ms = 3100
    kemper.on_midi_in(0, 1, 0xC0, [2], app)
    feed_sysex(app, string_response(0, 1, "MISSING RIG"))
    app.now_ms = 5000
    kemper.tick(app, app.now_ms)
    feed_sysex(app, param_response(5, 21, 0))
    check("unconfirmed sparse Captain identity stays unknown, never reuses old mode",
          app.current_slot == 1 and app.context.get("kemper_rig") == 3
          and app.context.get("expression_mode") == ""
          and len(_wah_requests(app)) == 1)


def test_fixed_wah_silent_external_change_is_read_by_periodic_query():
    app = _wah_ready_app()
    kemper.tick(app, app.now_ms)
    feed_sysex(app, param_response(5, 21, 0))
    # Hardware reproduced this exact sequence: external NRPN Wah ON causes
    # no 05/15 broadcast. A later explicit query is the only observation.
    initial_updates = len(app.context_updates)
    app.now_ms = 1499
    kemper.tick(app, app.now_ms)
    check("fixed Wah polling waits500ms after its successful observation",
          len(_wah_requests(app)) == 1)
    app.now_ms = 1500
    kemper.tick(app, app.now_ms)
    check("silent external Wah changes cause a new read without a broadcast",
          len(_wah_requests(app)) == 2
          and app.context.get("expression_mode") == "VOL"
          and len(app.context_updates) == initial_updates)
    feed_sysex(app, param_response(5, 21, 1))
    check("periodic reply reveals silent external ON as WAH",
          app.context.get("expression_mode") == "WAH")
    updates = len(app.context_updates)
    for now in (2000, 2500, 3000, 3500):
        app.now_ms = now
        kemper.tick(app, now)
        feed_sysex(app, param_response(5, 21, 1))
    check("unchanged periodic Wah replies produce no CONTEXT redraws",
          len(_wah_requests(app)) == 6
          and len(app.context_updates) == updates)


def test_fixed_wah_lost_polls_clear_stale_mode_and_back_off():
    app = _wah_ready_app()
    kemper.tick(app, app.now_ms)
    feed_sysex(app, param_response(5, 21, 0))
    for now in range(1500, 2700, 10):
        app.now_ms = now
        kemper.tick(app, now)
    check("fixed Wah allows at most one outstanding read",
          len(_wah_requests(app)) == 2)
    app.now_ms = 2700
    kemper.tick(app, app.now_ms)
    check("a timed-out Wah poll clears stale VOL instead of showing it forever",
          app.context.get("expression_mode") == ""
          and not kemper._WAH["known"])
    for now in range(2710, 10100, 10):
        app.now_ms = now
        kemper.tick(app, now)
    check("three lost Wah reads enforce5s backoff without rig blockage",
          len(_wah_requests(app)) == 4 and not kemper._transition_active())
    app.now_ms = 10100
    kemper.tick(app, app.now_ms)
    feed_sysex(app, param_response(5, 21, 1))
    check("Wah read recovers after backoff with the newly observed mode",
          len(_wah_requests(app)) == 5
          and app.context.get("expression_mode") == "WAH")


def test_crunch_slot_wah_overrides_fixed_wah_off():
    app = _wah_ready_app(empty_slots=False)
    app.current_slot = 3
    app.context.update({"slot": 3, "kemper_rig": 3, "patch_name": "CRUNCH"})
    kemper._RIG_INFO.update({"rig": 3, "name": "CRUNCH"})
    kemper.tick(app, app.now_ms)
    # Exact read-only ALSA capture on user's unchanged CRUNCH, 2026-09-06.
    for frame in (
        "00 20 33 00 00 01 00 05 15 00 00",  # fixed Wah OFF
        "00 20 33 00 00 01 00 32 00 00 01",  # A type=Wah Wah
        "00 20 33 00 00 01 00 32 03 00 01",  # A ON
    ):
        feed_sysex(app, bytes.fromhex(frame))
    check("real CRUNCH Wah in slotA overrides fixed Wah OFF",
          app.context.get("expression_mode") == "WAH", repr(app.context))
    feed_sysex(app, param_response(5, 21, 0))
    check("later fixed-OFF poll cannot overwrite an active slot Wah",
          app.context.get("expression_mode") == "WAH")


def test_slot_wah_discovery_and_bypass_follow_requested_replies():
    app = _wah_ready_app(empty_slots=False)
    consumed = 0
    requests = []
    # Answer only requests actually emitted by the plugin. An active ordinary
    # effect in B must not count as Wah; A is the CRUNCH Wah Wah slot.
    for now in range(1000, 1300, 10):
        app.now_ms = now
        kemper.tick(app, now)
        for request in app.midi.sysex[consumed:]:
            if len(request) != 9 or request[5] != 0x41:
                continue
            page, addr = request[-2:]
            requests.append((page, addr))
            value = 1 if (page, addr) in ((50, 0), (50, 3)) else 0
            if (page, addr) == (51, 0):
                value = 17  # ordinary distortion
            feed_sysex(app, param_response(page, addr, value))
        consumed = len(app.midi.sysex)
    check("slot discovery requests all types and the detected Wah state",
          all((page, 0) in requests for page in kemper._WAH_TYPE_PAGES)
          and (50, 3) in requests and (51, 3) not in requests)
    check("CRUNCH discovery never transiently publishes false VOL",
          app.context.get("expression_mode") == "WAH"
          and not any(u.get("expression_mode") == "VOL"
                      for u in app.context_updates))
    feed_block(app, "B", True)
    feed_block(app, "A", False)
    check("bypassing slot Wah restores VOL despite another active effect",
          app.context.get("expression_mode") == "VOL")
    feed_block(app, "A", True)
    check("reenabling the Wah slot restores WAH",
          app.context.get("expression_mode") == "WAH")


def test_wah_slot_queries_do_not_depend_on_dictionary_iteration_order():
    import ast
    import types

    global kemper
    original = kemper
    source_path = Path(kemper.__file__)
    tree = ast.parse(source_path.read_text(), filename=str(source_path))
    # CPython preserves dict insertion order; CircuitPython's runtime hash
    # order differed on the real Captain. Reorder the CC map BEFORE executing
    # the actual module, so a tuple derived from its keys reproduces that bug.
    for node in tree.body:
        if (isinstance(node, ast.Assign)
                and any(isinstance(target, ast.Name) and target.id == "_EFFECT_CC"
                        for target in node.targets)):
            node.value.keys.reverse()
            node.value.values.reverse()
            break
    else:
        raise AssertionError("Kemper effect-CC map not found")
    reordered = types.ModuleType("reordered_kemper")
    exec(compile(tree, str(source_path), "exec"), reordered.__dict__)
    # Physical type and ON/OFF addresses from the Kemper protocol. Delay and
    # Reverb use dedicated ON/OFF pages, unlike the other six effects.
    slots = (
        ("A", 50, (50, 3)), ("B", 51, (51, 3)),
        ("C", 52, (52, 3)), ("D", 53, (53, 3)),
        ("X", 56, (56, 3)), ("Mod", 58, (58, 3)),
        ("Delay", 60, (74, 2)), ("Reverb", 61, (75, 2)),
    )
    kemper = reordered
    try:
        for wah_name, wah_page, wah_state_address in slots:
            app = _wah_ready_app(empty_slots=False)
            replies = {(5, 21): 0}
            for name, type_page, state_address in slots:
                replies[(type_page, 0)] = 1 if name == wah_name else 0
                replies[state_address] = 1 if name == wah_name else 0
            consumed = 0
            requested = []
            for now in range(1000, 1350, 10):
                app.now_ms = now
                kemper.tick(app, now)
                for request in app.midi.sysex[consumed:]:
                    if len(request) != 9 or request[5] != 0x41:
                        continue
                    address = request[-2:]
                    requested.append(address)
                    feed_sysex(app, param_response(*address, replies[address]))
                consumed = len(app.midi.sysex)
            check("reordered CC dictionary queries %s Wah's own ON/OFF address" % wah_name,
                  wah_state_address in requested,
                  "type page=%d requests=%r" % (wah_page, requested))
            check("reordered CC dictionary recognizes active Wah in %s" % wah_name,
                  app.context.get("expression_mode") == "WAH",
                  repr(app.context))
    finally:
        kemper = original


def test_slot_wah_queries_are_quarantined_across_rigs():
    for completed_retry in (False, True):
        app = _wah_ready_app(empty_slots=False)
        kemper.tick(app, app.now_ms)
        feed_sysex(app, param_response(5, 21, 0))
        # Full type snapshot leaves the next request targeting slot A ON/OFF.
        for i, page in enumerate(kemper._WAH_TYPE_PAGES):
            feed_sysex(app, param_response(page, 0, 1 if i == 0 else 0))
        app.now_ms = 1020
        kemper.tick(app, app.now_ms)
        check("Wah discovery has a slot-state request in flight",
              app.midi.sysex[-1][-2:] == (50, 3))
        if completed_retry:
            # Two indistinguishable slot replies may now arrive. A successful
            # retry and a newer fixed-Wah request must not forget the older one.
            app.now_ms += kemper._QUERY_RETIRE_MS
            kemper.tick(app, app.now_ms)
            feed_block(app, "A", True)
            app.now_ms += 20
            kemper.tick(app, app.now_ms)
            check("fixed polling resumes after a recovered slot-state query",
                  app.midi.sysex[-1][-2:] == (5, 21))
        slot_retire = (1020 + kemper._QUERY_RETIRE_MS
                       * (2 if completed_retry else 1))
        app.now_ms += 10
        app.current_slot = 2
        app.context["slot"] = 2
        app._bindings = [("1", effect_binding("A"))]
        previous_block_generation = kemper._BLOCK_GENERATION.get("A")
        kemper.on_patch_switch_started(app)
        app.now_ms += kemper._SETTLE_MS
        sent = len(app.midi.sysex)
        kemper.tick(app, app.now_ms)
        check("new rig waits for the old Wah slot reply to retire",
              len(app.midi.sysex) == sent
              and kemper._BIDIR_STATE["reconcile_pending"] == ("A",))
        feed_block(app, "A", False)
        check("old Wah slot reply cannot turn off a new rig's block or LED",
              not app.latched_updates
              and "kemper_block_A" not in app.context
              and kemper._BLOCK_GENERATION.get("A") == previous_block_generation)
        app.now_ms = slot_retire
        kemper.tick(app, app.now_ms)
        feed_block(app, "A", True)
        check("fresh new-rig reply restores block and LED after quarantine",
              app.latched.get("1") is True
              and app.context.get("kemper_block_A") == "on"
              and not kemper._BIDIR_STATE["reconcile_pending"])


def main():
    print("Kemper plugin inbound handling (offline)\n")
    print("effect blocks")
    test_effect_blocks_mirror_to_bound_switches()
    test_every_block_page_maps()
    test_block_reply_latches_only_exact_bindings_including_multi_action()
    test_reconcile_republishes_unchanged_block_as_fresh()
    test_failed_publication_keeps_reply_pending_for_retry()
    test_patch_start_invalidates_old_public_blocks_and_arms_reconcile()
    test_rig_name_burst_cannot_overwrite_new_patch_during_settle()
    test_late_previous_generation_reply_is_not_current_confirmation()
    test_rapid_switch_orphan_stress_converges_without_false_edges()
    test_missing_replies_retry_bound_blocks_then_recover_from_cache()
    test_reconcile_queries_every_block_in_multi_action_binding()
    test_no_bound_blocks_never_republish_old_cache_after_settle()
    test_delayed_same_rig_pc_cannot_shorten_latest_settle()
    test_pc_after_nominal_settle_preserves_current_query_round()
    test_pc_echo_between_query_and_replies_keeps_current_round_live()
    test_pc_confirmation_keeps_prior_generation_query_fenced()
    test_2342ms_pc_echo_is_consumed_once_without_patch_reload()
    test_different_external_pc_is_not_swallowed_by_local_confirmation()
    test_external_pc_quarantines_superseded_local_echo()
    test_expired_orphan_pc_is_followed_as_external()
    test_expired_active_pc_is_a_real_same_rig_reselect()
    test_missing_patch_navigation_does_not_arm_local_echo_token()
    test_patch_start_boundary_gates_boot_and_midi_in_arming()
    test_live_cc_fences_stale_query_reply_without_on_off_on()
    test_live_cc_fences_every_inflight_retry_reply()
    test_reconcile_fallback_never_promotes_previous_rig_cache()
    test_idle_patch_reload_republishes_unchanged_bound_state()
    test_real_pc_echo_defers_partial_cache_until_targeted_reply()
    test_rig_name_reply_does_not_cancel_effect_fallback()
    test_duplicate_rig_name_after_reconcile_does_not_rearm()
    test_keepalive_does_not_publish_mid_settle_cache()
    test_new_rig_name_arms_reconcile_when_pc_echo_is_missing()
    test_delay_reverb_use_dedicated_pages()
    test_effect_cc_numbers_match_kemper_spec()
    test_inbound_cc_delay_mirrors_switch()
    test_unbound_block_does_not_latch_anything()
    print("tuner")
    test_tuner_mode_idle_value3_is_off()
    test_tuner_mode_value1_is_on()
    test_tuner_telemetry_is_silent_while_off_and_live_while_on()
    test_real_tuner_off_7c00_barrage_is_ignored()
    test_tuner_cc_updates_telemetry_gate()
    test_connect_does_not_engage_tuner()
    print("rig follow / bank navigation")
    test_bosun_bank_step_not_reverted_by_player_echo()
    test_external_rig_change_is_still_followed()
    print("beacon")
    test_beacon_emitted_on_tick()
    test_unconfirmed_link_retries_init_at_bounded_cadence()
    test_confirmed_link_uses_normal_keepalive_cadence()
    print("boot authority")
    test_external_rig_change_opens_settle_window_before_repainting()
    test_boot_deferring_to_kemper_does_not_arm_a_false_echo_window()
    test_configured_kemper_wants_authoritative_boot()
    test_no_kemper_device_does_not_want_authoritative_boot()
    print("fixed Wah indicator")
    test_fixed_wah_requires_real_reply_and_tracks_broadcasts()
    test_fixed_wah_generation_fence_does_not_delay_effects()
    test_fixed_wah_missing_reply_retries_are_bounded_and_nonblocking()
    test_fixed_wah_waits_for_link_and_fresh_identity()
    test_fixed_wah_failed_publication_remains_retryable()
    test_fixed_wah_outbound_command_does_not_guess_indicator()
    test_fixed_wah_expired_link_does_not_send_diagnostic_request()
    test_fixed_wah_sparse_navigation_does_not_reuse_old_mode()
    test_fixed_wah_silent_external_change_is_read_by_periodic_query()
    test_fixed_wah_lost_polls_clear_stale_mode_and_back_off()
    test_crunch_slot_wah_overrides_fixed_wah_off()
    test_slot_wah_discovery_and_bypass_follow_requested_replies()
    test_wah_slot_queries_do_not_depend_on_dictionary_iteration_order()
    test_slot_wah_queries_are_quarantined_across_rigs()
    print("\n%d passed, %d failed" % (PASS, FAIL))
    if FAILURES:
        print("\nFailures:")
        for f in FAILURES:
            print("  - " + f)
    sys.exit(1 if FAIL else 0)


if __name__ == "__main__":
    main()
