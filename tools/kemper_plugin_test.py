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
        self.context = {}
        self._bank_lsb = {}
        # Patch-navigation state, mirrors app.py for the rig-follow path.
        self.current_bank = 1
        self.current_slot = 1
        self.current_patch = {"bank": 1, "slot": 1}
        self.last_patch_switch_ms = 0
        self.last_local_switch_ms = 0
        self.on_patch_loaded_calls = 0
    def current_bindings(self): return list(self._bindings)
    def set_switch_latched(self, sw, on): self.latched[sw] = on; return True
    def update_context(self, updates): self.context.update(updates)
    def get_last_bank_lsb(self, port, ch): return self._bank_lsb.get((port, ch), 0)
    def _now_ms(self): return getattr(self, "now_ms", 0)

    # Faithful mirror of app.py:switch_patch echo-suppression + source tracking.
    def switch_patch(self, bank, slot, source="editor", fire_on_enter=True):
        if (source == "midi_in"
                and self.current_patch is not None
                and (bank, slot) == (self.current_bank, self.current_slot)
                and (self._now_ms() - self.last_patch_switch_ms) < 1200):
            self.on_patch_loaded_calls += 1     # repaint from cache, no revert
            return True
        t0 = self._now_ms()
        self.current_bank = bank
        self.current_slot = slot
        self.last_patch_switch_ms = t0
        # Gated on fire_on_enter too: a load that sent no outbound MIDI
        # (e.g. boot() deferring to an authoritative Kemper) has nothing to
        # echo, so arming the window here would swallow the device's very
        # next real state announcement as a false self-echo (2026-08-15).
        if source != "midi_in" and fire_on_enter:
            self.last_local_switch_ms = t0
        self.current_patch = {"bank": bank, "slot": slot}
        return True


def reset_bidir():
    kemper._BIDIR_STATE["published"] = {}
    kemper._BIDIR_STATE["confirmed"] = False
    kemper._BIDIR_STATE["init_sent"] = False
    kemper._BIDIR_STATE["last_beacon_ms"] = 0
    kemper._BIDIR_STATE["settle_until_ms"] = 0


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

    # User holds bank-up -> local step to bank 3. on_enter dispatches the rig;
    # dispatch sends CC32(2) to the Player (does NOT touch the incoming LSB).
    app.switch_patch(3, 1, source="binding")
    kemper.dispatch({"type": "kemper_rig", "bank": 3, "rig": 1, "channel": 1}, app.midi)
    check("local bank-up lands on bank 3", app.current_bank == 3,
          "current_bank=%r" % app.current_bank)

    # ~400 ms later the Player echoes the new rig as a bare PC (no fresh CC32).
    app.now_ms = 1400
    kemper.on_midi_in(0, 1, 0xC0, [0], app)     # PC 0 = rig 1
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
    app.last_local_switch_ms = 0
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


def main():
    print("Kemper plugin inbound handling (offline)\n")
    print("effect blocks")
    test_effect_blocks_mirror_to_bound_switches()
    test_every_block_page_maps()
    test_delay_reverb_use_dedicated_pages()
    test_effect_cc_numbers_match_kemper_spec()
    test_inbound_cc_delay_mirrors_switch()
    test_unbound_block_does_not_latch_anything()
    print("tuner")
    test_tuner_mode_idle_value3_is_off()
    test_tuner_mode_value1_is_on()
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
    print("\n%d passed, %d failed" % (PASS, FAIL))
    if FAILURES:
        print("\nFailures:")
        for f in FAILURES:
            print("  - " + f)
    sys.exit(1 if FAIL else 0)


if __name__ == "__main__":
    main()
