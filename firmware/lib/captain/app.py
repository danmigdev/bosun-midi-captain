import gc
import time

from . import VERSION, config
from .bindings import BindingRunner, SwitchArray
from .board import LED_INDEX_PER_SWITCH
from .display import Display
from .expression import ExpressionArray
from .leds import Leds, parse_hex
from .midi import MidiEngine
from .plugin import PluginRegistry
from .protocol import Protocol
from .store import PatchStore


# Deferred TFT render timing. A render blocks the loop for tens of ms, during
# which inbound USB-MIDI can overflow the small input buffer and drop block
# deltas (the trailing Delay/Reverb on/off in a rig-change burst are the first
# casualties). So we hold the render until the inbound MIDI burst goes QUIET
# (no message for _REFRESH_QUIET_MS) - this adapts to however long the Player
# takes to stream the burst - with a hard cap (_REFRESH_MAX_DEFER_MS) so a
# continuous stream (e.g. tuner) still refreshes. Block LEDs (NeoPixel) update
# immediately regardless; only the TFT text waits.
_REFRESH_QUIET_MS = 40
_REFRESH_MAX_DEFER_MS = 250
# While the tuner splash is up the render is cheap (the display updates the
# tuner in place - only the indicator moves), so we don't need the big defer
# cap that protects rig-change bursts: refresh it at ~30 fps for a near
# real-time needle. The Kemper streams deviance continuously, so this is the
# gate that actually paces the tuner.
_TUNER_REFRESH_MS = 30

_MIDI_TYPE_NAME = {
    0x80: "note_off",
    0x90: "note_on",
    0xA0: "poly_pressure",
    0xB0: "cc",
    0xC0: "pc",
    0xD0: "channel_pressure",
    0xE0: "pitch_bend",
}


class Captain:
    def __init__(self):
        self.device = config.load_device()
        self.midi_learn_table = config.load_midi_learn()
        autosave = self.device.get("autosave", {})

        tft = self.device.get("tft", {})
        self.display = Display(
            rotation=tft.get("rotation", 180),
            rowstart=tft.get("rowstart", 80),
            colstart=tft.get("colstart", 0),
        )
        self.leds = Leds()
        self.midi = MidiEngine()
        self.patches = PatchStore(
            autosave_enabled=autosave.get("enabled", True),
            autosave_debounce_ms=autosave.get("debounce_ms", 2000),
        )
        self.switches = SwitchArray(
            long_press_ms=self.device.get("long_press_ms", 600),
            double_tap_window_ms=self.device.get("double_tap_window_ms", 250),
            auto_momentary_on_hold=self.device.get("auto_momentary_on_hold", True),
            auto_momentary_ms=self.device.get("auto_momentary_ms", 500),
        )
        self.plugins = PluginRegistry()
        self.plugins.discover()
        # Kind of the active profile (e.g. "kemper_player"). Plugins without a
        # device.json config section (Helix) gate their global hooks on this.
        self.active_kind = self._compute_active_kind()
        self.runner = BindingRunner(self.midi, self.plugins, app=self)
        # Expression jacks (GP27/GP28). Idle unless a jack is enabled with a
        # message in device.json; a board with nothing plugged in costs nothing.
        self.expression = ExpressionArray(self.device.get("expression"))
        self.protocol = Protocol(self)
        self.midi_learn = False

        # Preset-preview cursor. None = not previewing. Active shape:
        #   {"bank":b, "slot":s, "until_ms":t, "saved_context":{...}}
        # While active the user is scrolling patches on the TFT WITHOUT loading
        # any (no on_enter/on_exit, no device MIDI); commit jumps for real,
        # cancel/timeout returns to the current patch. See preview_* methods.
        self._preview = None

        self.current_bank = 1
        self.current_slot = 1
        self.current_patch = None
        # Timestamp of the last LOCAL (pedal/editor/boot) patch switch, i.e. NOT
        # one triggered by an inbound device echo. Plugins use it to tell their
        # own echo apart from a genuine external change (see kemper auto-follow).
        self.last_local_switch_ms = 0
        self._binding_index = {}
        self._mode_index = {}
        # Generic Bank MSB/LSB tracking so plugins reacting to a downstream
        # PC have the bank context (Ampero uses MSB, Kemper uses LSB, ...).
        self._last_bank_msb = {}                  # (port, channel) -> CC 0  value
        self._last_bank_lsb = {}                  # (port, channel) -> CC 32 value

        # TFT display context: named field values the layout reads from.
        # Captain owns patch_name / bank / slot; everything else is added
        # by plugins via update_context() / on_midi_in(). Layout entries
        # referencing fields that haven't been written yet render as empty.
        self.display_context = {
            "patch_name": "",
            "bank": 1, "slot": 1,
            # Position within the active setlist (e.g. "4/12"), "" when the
            # current patch isn't in the setlist. A TFT layout can show it.
            "setlist_pos": "",
        }
        # Cached layout from device.json (tft.layout). Render falls back to
        # a centered patch name if this is empty.
        self.display_layout = self.device.get("tft", {}).get("layout") or []

        # One-shot: if this profile has no layout yet but its kind matches
        # an installed plugin, seed it from that plugin's DEFAULT_LAYOUT and
        # persist back to device.json so the user has something to start
        # from. The editor's TFT Layout page lets them customize later.
        if not self.display_layout:
            try:
                layout = self.plugins.default_layout(self.active_kind)
                if layout:
                    self.display_layout = layout
                    self.device.setdefault("tft", {})["layout"] = layout
                    config.save_device(self.device)
            except Exception as e:
                print("seed layout failed:", e)

        # Counters for the STATS protocol command - let host-side stress tools
        # observe loop rate, MIDI throughput, command throughput.
        self._boot_ms = self._now_ms()
        # While non-zero and in the future, the boot splash is held on the TFT:
        # _refresh_display() skips real renders until this passes, then the main
        # loop does one render. Non-blocking, so MIDI and the editor connection
        # stay live behind the splash. Set in boot().
        self._splash_until_ms = 0
        # Deferred/coalesced TFT rendering. update_context / switch_patch mark a
        # render "due" a short time out (_REFRESH_DEFER_MS); the main loop does
        # the one slow render only once that elapses. During the wait the loop
        # keeps ticking fast and draining USB-MIDI, so a rig-change broadcast
        # burst (rig name + BPM + block deltas) is fully read BEFORE the
        # blocking render - it no longer overflows the small USB-MIDI input
        # buffer and drops block on/off deltas (which left effect LEDs stale,
        # worst on a direct pedal<->Player USB link). 0 = nothing pending.
        self._refresh_due_ms = 0
        self._last_midi_in_ms = -100000   # for the "MIDI quiet" render gate
        self.loop_iters = 0
        self.midi_rx_count = 0
        self.protocol_cmd_count = 0
        self.last_patch_switch_ms = 0

        self.patches.on_dirty_changed = self._emit_dirty_state
        self.patches.on_saved = self._emit_saved
        self.patches.on_discarded = self._emit_discarded

    # ---------- lifecycle ----------

    def boot(self):
        self.display.show_splash()
        # Hold the splash on the TFT for 2 s. This is non-blocking: the patch
        # still loads and MIDI/connection run normally underneath; only the
        # TFT render is deferred (see _refresh_display + _tick_body) so the
        # brand screen stays visible instead of being overwritten instantly.
        self._splash_until_ms = self._now_ms() + 2000
        self.leds.idle_pattern()
        self.switch_patch(self.current_bank, self.current_slot, source="boot")

    def run(self):
        self.boot()
        print("Captain " + VERSION + " ready")
        while True:
            self.tick_once()
            time.sleep(0.005)

    def tick_once(self):
        """One main-loop iteration, made exception-safe.

        Only protocol.handle() used to be wrapped; everything else in the
        loop (protocol.poll, the MIDI parser, switch FSM, patch autosave)
        ran bare. A single exception there - a malformed MIDI packet, a
        transient store/flash hiccup, an edge case in a sub-component -
        propagated out of run(), code.py exited, and the USB data CDC went
        dead, so the editor saw "not connected". We now catch per-iteration
        and keep the loop (and the connection) alive. Verified by
        tools/firmware_stability_test.py."""
        try:
            self._tick_body()
        except Exception as e:
            try:
                print("loop error:", e)
            except Exception:
                pass

    def _tick_body(self):
        self.loop_iters += 1
        msg = self.protocol.poll()
        if msg is not None:
            self.protocol_cmd_count += 1
            self.protocol.handle(msg)
        now_ms = self._now_ms()
        # Boot splash just expired: render the real screen once (renders were
        # suppressed while the splash was held).
        if self._splash_until_ms and now_ms >= self._splash_until_ms:
            self._splash_until_ms = 0
            self._refresh_display()
        self.patches.tick(now_ms)
        # Tuner exit-on-press: snapshot whether the tuner splash is up BEFORE any
        # switch fires. On the first press edge this tick, dismiss the tuner
        # (device + local) so one stomp both leaves the tuner AND does the
        # switch's own action (pass-through). Gated by device.tuner_exit_on_press.
        tuner_on = self._tuner_is_on()
        tuner_exit_enabled = self.device.get("tuner_exit_on_press", True)
        exited_tuner = False
        for sw in self.switches.switches:
            mode = self._mode_index.get(sw.name, "tap")
            raw_edge, triggers = sw.poll(now_ms, mode)
            if raw_edge is not None:
                self.protocol.emit_event(
                    "switch_pressed" if raw_edge == "press" else "switch_released",
                    switch=sw.name,
                )
            if (raw_edge == "press" and tuner_on and tuner_exit_enabled
                    and not exited_tuner):
                self._exit_tuner()
                exited_tuner = True
            for action_key in triggers:
                self._fire(sw.name, action_key)
        for port, channel, status, data in self.midi.poll():
            self.midi_rx_count += 1
            self._handle_midi_in(port, channel, status, data)
        # Plugins that need a heartbeat (e.g. Kemper bidirectional
        # beacon) hook here. The check is cheap when no plugin
        # opts in.
        self.plugins.tick(self, now_ms)
        # Expression pedals: sample the jacks (throttled) and dispatch a MIDI
        # message for each jack that moved. Cheap no-op when no jack is enabled.
        # These are plain cc / plugin continuous-control messages, so they don't
        # touch the display context and never trigger a TFT render.
        for message, value in self.expression.poll(now_ms):
            m = dict(message)
            m["value"] = value
            self.runner.run_message(m)
        # Preset-preview auto-resolve: the user stopped scrolling past the
        # timeout, so commit (or cancel) the previewed patch. Commit runs a full
        # switch_patch, which marks the display dirty for the deferred render.
        if self._preview is not None and now_ms >= self._preview["until_ms"]:
            self._resolve_preview_timeout()
        # Deferred TFT render: fire the one slow render only after the inbound
        # MIDI burst has gone quiet (no message for _REFRESH_QUIET_MS), or at
        # the hard cap. The fast ticks until then keep draining USB-MIDI, so the
        # whole rig-change burst - including the trailing Delay/Reverb block
        # deltas - is read before the render blocks. No overflow, no dropped
        # deltas. The splash gate in _refresh_display holds this at boot.
        if self._refresh_due_ms and (
                now_ms >= self._refresh_due_ms
                or now_ms - self._last_midi_in_ms >= _REFRESH_QUIET_MS):
            self._refresh_due_ms = 0
            self._refresh_display()
        # Advance any marquee (scrolling) labels. Cheap no-op when no label
        # overflows; it only moves each scrolling label's .x (a small dirty
        # region), so it doesn't block the loop the way a full render does.
        self.display.tick(now_ms)

    def stats(self):
        gc.collect()
        return {
            "uptime_ms":          self._now_ms() - self._boot_ms,
            "mem_free":           gc.mem_free(),
            "mem_alloc":          gc.mem_alloc(),
            "loop_iters":         self.loop_iters,
            "midi_rx_count":      self.midi_rx_count,
            "midi_tx_count":      getattr(self.midi, "tx_count", 0),
            "usb_tx_dropped":     getattr(self.midi, "usb_tx_dropped", 0),
            "sysex_rx_count":     getattr(self.midi, "sysex_rx_count", 0),
            "last_patch_switch_duration_ms": getattr(self, "last_patch_switch_duration_ms", 0),
            "protocol_cmd_count": self.protocol_cmd_count,
            "last_patch_switch_ms": self.last_patch_switch_ms,
            "current": {"bank": self.current_bank, "slot": self.current_slot},
            # Live expression jack readings so the editor can draw a calibration
            # bar and capture min/max from a heel-to-toe sweep.
            "expression": self.expression.stats(),
        }

    # ---------- patch operations (called from Protocol) ----------

    def switch_patch(self, bank, slot, source="editor", fire_on_enter=True):
        if not self.patches.has(bank, slot):
            return False
        # Any real patch load ends an in-progress preset preview: the commit
        # path, or the device changing rig underneath a preview (inbound echo).
        # Clear the cursor and the PREVIEW badge so a stale preview can't linger
        # or auto-commit over this load. No-op in the common (non-preview) case.
        self._preview = None
        self.display_context.pop("preview", None)
        # Echo suppression: a plugin reacting to inbound MIDI may call
        # switch_patch(source="midi_in") for the same patch we just loaded
        # ourselves. Within ~1.2 s of our own load the inbound is just the
        # target device echoing back - skip it. Outside that window we
        # treat it as a deliberate "reload" (re-run on_enter, reset latched
        # switches).
        if (source == "midi_in"
                and self.current_patch is not None
                and (bank, slot) == (self.current_bank, self.current_slot)
                and (self._now_ms() - self.last_patch_switch_ms) < 1200):
            # Echo of a patch WE just loaded (pedal-initiated rig change). The
            # Player sends this PC echo only AFTER it has streamed the new rig's
            # block deltas (verified: PC lands ~400 ms in, after the deltas at
            # ~100-280 ms), so the plugin cache is now CURRENT. Repaint the
            # effect LEDs from it - this fills any block that stayed ON across
            # the change (no delta) without the stale-state flash that applying
            # the cache at load time caused. Don't reload (that'd reset LEDs).
            self.plugins.on_patch_loaded(self)
            self._mark_display_dirty()
            return True
        # Fire the OUTGOING patch's on_exit chain before we load the target
        # (symmetric to the incoming patch's on_enter). Gated like on_enter:
        # only when fire_on_enter is set (an auto-follow with fire_on_enter=False
        # must not emit MIDI the device didn't ask for) and only when we're
        # actually leaving for a different slot (not a same-patch reload).
        if (fire_on_enter and self.current_patch is not None
                and (bank, slot) != (self.current_bank, self.current_slot)):
            on_exit = self.current_patch.get("on_exit")
            if on_exit:
                self.runner.run(on_exit)
        t0 = self._now_ms()
        self.current_bank = bank
        self.current_slot = slot
        self.last_patch_switch_ms = t0
        # A local switch arms the plugins' echo window; an inbound-echo-driven
        # switch (source="midi_in") must NOT, or it would re-arm forever.
        if source != "midi_in":
            self.last_local_switch_ms = t0
        self.current_patch = self.patches.get(bank, slot)
        self._reindex_patch()
        self.switches.reset_all()
        # Repaint device-mirrored switch state (Kemper effect blocks) from the
        # plugin cache ONLY when this load was triggered by the target device
        # itself (source="midi_in" - the Player's PC, which it sends AFTER the
        # new rig's block deltas, so the cache is already current). For a
        # pedal/editor-initiated load the cache is still the PREVIOUS rig's at
        # this instant (we haven't even sent the rig change yet), so applying it
        # would flash stale state; we skip it here and let the Player's PC echo
        # (handled by the echo-suppression branch above) repaint once the deltas
        # have landed. The incoming deltas also update changed switches live.
        if source == "midi_in":
            self.plugins.on_patch_loaded(self)
        self.leds.render_patch(self.current_patch, self.switches.switches)
        self._paint_preset_nav_leds()
        self.display_context["patch_name"] = (self.current_patch or {}).get("name", "")
        self.display_context["bank"] = bank
        self.display_context["slot"] = slot
        self._update_setlist_pos(bank, slot)
        # Order matters for perceived latency: fire on_enter (which dispatches
        # kemper_rig and any other plugin MIDI) BEFORE rendering the TFT, so
        # the target device receives the rig change as fast as possible and
        # the bosun's screen catches up after. The TFT render is the
        # single slowest step in this function - typically dozens of ms
        # for a multi-label layout - so deferring it past the MIDI tx is
        # the biggest win we can get without redoing the display stack.
        # fire_on_enter is False when a plugin auto-follows the target device's
        # OWN state change (e.g. the Kemper switched rig on its front panel):
        # the device is already on that rig, so re-sending on_enter's rig MIDI
        # would make it reload and re-broadcast, which ping-pongs into a flood
        # of re-sends/queries that overruns the direct USB link. Loading the
        # patch (bindings/LEDs/blocks) still happens; only the outbound echo is
        # skipped.
        on_enter = (self.current_patch or {}).get("on_enter")
        if on_enter and fire_on_enter:
            self.runner.run(on_enter)
        # Deferred render (see update_context / _mark_display_dirty): let the
        # main loop drain the rig-change MIDI burst before the one blocking TFT
        # render, instead of rendering here and overflowing the USB-MIDI input
        # buffer. The LEDs (NeoPixel) were already painted above and are fast.
        self._mark_display_dirty()
        self.last_patch_switch_duration_ms = self._now_ms() - t0
        self.protocol.emit_event("patch_switched", bank=bank, slot=slot, source=source)
        return True

    def _refresh_display(self):
        """Render TFT from current context + layout."""
        # Keep the boot splash up for its full duration: skip real renders
        # until the window passes. The main loop does the first render then.
        if self._splash_until_ms and self._now_ms() < self._splash_until_ms:
            return
        try:
            self.display.render(self.display_context, self.display_layout)
        except Exception as e:
            print("display refresh failed:", e)

    def apply_display_layout(self, layout):
        """Called when the user pushes a new tft.layout via PUT_GLOBAL."""
        self.display_layout = layout or []
        self._refresh_display()

    def put_patch(self, bank, slot, patch):
        self.patches.put_patch(bank, slot, patch, self._now_ms())
        if (bank, slot) == (self.current_bank, self.current_slot):
            # Snapshot per-switch latched state BEFORE the reindex so we
            # can restore the visible on/off after the unavoidable
            # reset_all() that follows. Without this, editor-driven
            # patch edits (rename, screen color, ...) would flicker
            # every latched switch off because the FSM was zeroed even
            # though the binding semantics didn't change.
            prev_modes = {sw_name: (b or {}).get("mode", "tap")
                          for sw_name, b in self._binding_index.items()}
            prev_latched = {sw.name: sw.latched_on for sw in self.switches.switches}
            self.current_patch = patch
            self._reindex_patch()
            self.switches.reset_all()
            for sw in self.switches.switches:
                new_b = self._binding_index.get(sw.name)
                new_mode = (new_b or {}).get("mode", "tap")
                if (prev_modes.get(sw.name) == "latched"
                        and new_mode == "latched"
                        and prev_latched.get(sw.name)):
                    sw.latched_on = True
            self.leds.render_patch(self.current_patch, self.switches.switches)
            self._paint_preset_nav_leds()

    def put_binding(self, bank, slot, binding):
        self.patches.put_binding(bank, slot, binding, self._now_ms())
        if (bank, slot) == (self.current_bank, self.current_slot):
            # Capture the current latched state BEFORE reindex/reset.
            # An editor-driven binding update (label rename, color
            # tweak, ...) shouldn't visually flip a latched switch off:
            # the user perception is "I just renamed a label, the LED
            # disappeared". We preserve the previous on/off state
            # whenever the mode stays "latched" before AND after the
            # edit; if the mode changed (latched -> tap, etc.), a
            # reset is the right behavior because the old state has no
            # meaning anymore.
            sw_name = binding.get("switch")
            old_binding = self._binding_index.get(sw_name) or {}
            old_mode = old_binding.get("mode", "tap")
            new_mode = binding.get("mode", "tap")
            self._reindex_patch()
            for sw in self.switches.switches:
                if sw.name == sw_name:
                    prev_latched = sw.latched_on
                    sw.reset()
                    if old_mode == "latched" and new_mode == "latched":
                        sw.latched_on = prev_latched
                    self.leds.set_switch_state(sw_name, binding, sw.latched_on)
                    break

    def bank_step(self, delta):
        """Jump to the same slot in the bank `delta` positions away (with
        wrap-around). Only considers banks that have at least one patch
        on disk. If the target bank doesn't have the current slot, falls
        back to the lowest-numbered slot present in that bank."""
        try:
            d = int(delta)
        except (TypeError, ValueError):
            return False
        if d == 0:
            return False
        pairs = [(p["bank"], p["slot"]) for p in self.patches.list()]
        banks = sorted({b for (b, _s) in pairs})
        if not banks:
            return False
        if self.current_bank in banks:
            i = banks.index(self.current_bank)
            j = (i + d) % len(banks)
        else:
            j = 0 if d > 0 else len(banks) - 1
        new_bank = banks[j]
        if new_bank == self.current_bank:
            return False
        slots = sorted(s for (b, s) in pairs if b == new_bank)
        target_slot = self.current_slot if self.current_slot in slots else slots[0]
        return self.switch_patch(new_bank, target_slot, source="binding")

    def _compute_active_kind(self):
        """Kind of the active profile (falls back to the first profile, then
        empty). Best-effort - a bad profile store must not crash boot."""
        try:
            profiles = config.list_profiles() or []
            for p in profiles:
                if p.get("active"):
                    return p.get("kind", "")
            if profiles:
                return profiles[0].get("kind", "")
        except Exception as e:
            print("active kind lookup failed:", e)
        return ""

    # ---------- preset preview (called from BindingRunner) ----------

    def preview_step(self, delta, scope="patch"):
        """Move the preview cursor by `delta` (scope "patch" or "bank") and show
        the target patch on the TFT WITHOUT loading it - no on_enter/on_exit, no
        device MIDI, no LED/current-patch change. First call snapshots the live
        display context so cancel/timeout can restore it. Returns False when
        there is nothing to preview."""
        from . import navigation
        order = navigation.patch_order(self.patches.list())
        if not order:
            return False
        if self._preview is not None:
            start = (self._preview["bank"], self._preview["slot"])
            saved = self._preview["saved_context"]
        else:
            start = (self.current_bank, self.current_slot)
            saved = dict(self.display_context)
        bank, slot = navigation.step_index(order, start, delta, scope)
        try:
            patch = self.patches.get(bank, slot)
        except OSError:
            patch = None
        timeout = int((self.device.get("preview") or {}).get("timeout_ms", 1500))
        self._preview = {
            "bank": bank, "slot": slot,
            "until_ms": self._now_ms() + timeout,
            "saved_context": saved,
        }
        # Show the previewed patch's core fields plus a PREVIEW marker the
        # display badges. Plugins fill their own fields (e.g. Kemper rig).
        self.display_context["patch_name"] = (patch or {}).get("name", "")
        self.display_context["bank"] = bank
        self.display_context["slot"] = slot
        self.display_context["preview"] = "on"
        self.plugins.on_preview(self, bank, slot)
        self._mark_display_dirty()
        return True

    def preview_commit(self):
        """Load the previewed patch for real (fires on_exit -> on_enter, sends
        device MIDI). No-op when not previewing."""
        if self._preview is None:
            return False
        bank, slot = self._preview["bank"], self._preview["slot"]
        self._preview = None
        return self.switch_patch(bank, slot, source="binding")

    def preview_cancel(self):
        """Discard the preview and restore the pre-preview display. No-op when
        not previewing."""
        if self._preview is None:
            return False
        self.display_context = self._preview["saved_context"]
        self._preview = None
        self._mark_display_dirty()
        return True

    def _resolve_preview_timeout(self):
        """Auto-resolve a preview that the user stopped scrolling. Commit or
        cancel per device.preview.on_timeout (default commit - the FX-pedal
        'stop and it loads' behavior)."""
        on_timeout = (self.device.get("preview") or {}).get("on_timeout", "commit")
        if on_timeout == "cancel":
            self.preview_cancel()
        else:
            self.preview_commit()

    # ---------- setlist navigation (called from BindingRunner) ----------

    def _setlist_items(self):
        """The active setlist as an ordered list of (bank, slot) tuples, kept to
        only the entries that actually have a patch on disk. Read from
        device.setlist.items; each item may be a dict {bank,slot} or a [bank,slot]
        pair. Malformed entries are skipped, never raised."""
        sl = self.device.get("setlist") or {}
        raw = sl.get("items") or []
        out = []
        for it in raw:
            try:
                if isinstance(it, dict):
                    b, s = int(it["bank"]), int(it["slot"])
                else:
                    b, s = int(it[0]), int(it[1])
            except (KeyError, IndexError, TypeError, ValueError):
                continue
            if self.patches.has(b, s):
                out.append((b, s))
        return out

    def setlist_step(self, delta):
        """Load the next/previous patch in the active setlist (wraps around).
        If the current patch isn't in the setlist, the first step enters at the
        start (delta > 0) or the end (delta < 0). No-op when the setlist is
        empty. This loads immediately - it's the live 'next song' control."""
        try:
            d = int(delta)
        except (TypeError, ValueError):
            return False
        if d == 0:
            return False
        items = self._setlist_items()
        if not items:
            return False
        cur = (self.current_bank, self.current_slot)
        idx = -1
        for i, it in enumerate(items):
            if it == cur:
                idx = i
                break
        if idx < 0:
            new = 0 if d > 0 else len(items) - 1
        else:
            new = (idx + d) % len(items)
        b, s = items[new]
        return self.switch_patch(b, s, source="binding")

    def _update_setlist_pos(self, bank, slot):
        """Refresh the setlist_pos display field for the patch being loaded."""
        items = self._setlist_items()
        pos = ""
        if items:
            for i, it in enumerate(items):
                if it == (bank, slot):
                    pos = "%d/%d" % (i + 1, len(items))
                    break
        self.display_context["setlist_pos"] = pos

    # ---------- tuner exit-on-press ----------

    def _tuner_is_on(self):
        ctx = self.display_context
        return ctx.get("tuner") == "on" or ctx.get("kemper_tuner") == "on"

    def _exit_tuner(self):
        """Leave the tuner: tell the target device (plugin hook) and clear the
        local tuner context so the TFT returns to the patch view immediately,
        without waiting for the device to echo tuner-off."""
        self.plugins.tuner_off(self)
        self.update_context({"tuner": "off", "kemper_tuner": "off"})

    def reload_current_patch(self):
        try:
            self.current_patch = self.patches.get(self.current_bank, self.current_slot)
        except OSError:
            self.current_patch = None
        self._reindex_patch()
        self.switches.reset_all()
        self.plugins.on_patch_loaded(self)
        self.leds.render_patch(self.current_patch, self.switches.switches)
        self._paint_preset_nav_leds()

    def apply_global(self, device):
        self.device = device
        auto = device.get("autosave", {})
        self.patches.autosave_enabled = auto.get("enabled", True)
        self.patches.autosave_debounce_ms = auto.get("debounce_ms", 2000)
        long_press_ms = device.get("long_press_ms", 600)
        double_tap_window_ms = device.get("double_tap_window_ms", 250)
        auto_momentary_on_hold = device.get("auto_momentary_on_hold", True)
        auto_momentary_ms = device.get("auto_momentary_ms", 500)
        for sw in self.switches.switches:
            sw.long_press_ms = long_press_ms
            sw.double_tap_window_ms = double_tap_window_ms
            sw.auto_momentary_on_hold = auto_momentary_on_hold
            sw.auto_momentary_ms = auto_momentary_ms
        # Re-apply per-binding overrides on top of refreshed globals
        self._reindex_patch()
        # Rebuild the expression jacks so an enable/disable or a recalibration
        # pushed from the editor takes effect without a reboot.
        self.expression.configure(device.get("expression"))
        # Display layout lives under tft.layout - refresh if it changed.
        self.apply_display_layout(device.get("tft", {}).get("layout") or [])

    def apply_midi_learn(self, table):
        self.midi_learn_table = table

    # ---------- inbound MIDI ----------

    def _handle_midi_in(self, port, channel, status, data):
        # Note when MIDI last arrived so the deferred TFT render can wait for
        # the inbound burst to go quiet before blocking the loop.
        self._last_midi_in_ms = self._now_ms()
        if self.midi_learn:
            self.protocol.emit_event(
                "midi_in_captured",
                port=port,
                channel=channel,
                kind=_MIDI_TYPE_NAME.get(status, "unknown"),
                data=data,
            )

        # Track Bank MSB (CC 0) and Bank LSB (CC 32) so plugins can resolve
        # the next PC into a target. Both are stored unconditionally -
        # plugin code reads them via get_last_bank_msb/lsb().
        if status == 0xB0 and len(data) >= 2:
            if data[0] == 0:
                self._last_bank_msb[(port, channel)] = data[1]
            elif data[0] == 32:
                self._last_bank_lsb[(port, channel)] = data[1]

        # Hand the raw event off to every plugin that opted in. The core
        # itself is target-agnostic; Kemper/Ampero/etc. logic lives in
        # their respective modules under lib/plugins/.
        self.plugins.dispatch_midi_in(port, channel, status, data, self)

    # ---------- plugin helpers ----------
    #
    # These are the only points where a plugin reaches back into the core
    # state. Kept minimal and generic so plugins stay self-contained.

    def get_last_bank_msb(self, port, channel):
        return self._last_bank_msb.get((port, channel), 0)

    def get_last_bank_lsb(self, port, channel):
        return self._last_bank_lsb.get((port, channel), 0)

    def set_switch_latched(self, switch_name, on):
        """Force a latched switch's state without firing its action chain.
        Used by plugins to mirror the target device's state back into the
        bosun LEDs. Idempotent: returns False if no change happened (no
        binding, wrong mode, or state already matches)."""
        binding = self._binding_index.get(switch_name)
        if not binding or binding.get("mode") != "latched":
            return False
        target = bool(on)
        for sw in self.switches.switches:
            if sw.name != switch_name:
                continue
            if sw.latched_on == target:
                return False
            sw.latched_on = target
            self.leds.set_switch_state(switch_name, binding, target)
            return True
        return False

    def update_context(self, updates):
        """Merge a dict of field updates into the display context and mark the
        TFT dirty. Plugins call this to expose their state to the layout.

        The actual TFT render is COALESCED to once per main-loop tick (see
        _tick_body) instead of happening synchronously here. A TFT render takes
        tens of ms and blocks the loop; rendering on every inbound broadcast
        meant that during a rig-change burst (rig name + BPM + block deltas, all
        arriving together) the pedal rendered several times mid-burst, and the
        small USB-MIDI input buffer overflowed - dropping block on/off deltas so
        effect LEDs showed stale/wrong state (worst on a direct pedal<->Player
        USB link). Coalescing keeps the loop draining MIDI and renders once."""
        if not updates:
            return
        self.display_context.update(updates)
        self._mark_display_dirty()

    def _mark_display_dirty(self):
        """Schedule a coalesced TFT render. The actual render waits for the MIDI
        burst to go quiet (see _tick_body), capped at _REFRESH_MAX_DEFER_MS.
        Already-pending renders keep their original cap so a steady update
        stream still refreshes at a bounded rate."""
        if not self._refresh_due_ms:
            ctx = self.display_context
            tuner_on = ctx.get("tuner") == "on" or ctx.get("kemper_tuner") == "on"
            cap = _TUNER_REFRESH_MS if tuner_on else _REFRESH_MAX_DEFER_MS
            self._refresh_due_ms = self._now_ms() + cap

    def find_binding(self, switch_name):
        """Return the current patch's binding for the given switch, or
        None. Plugins use this when they need to inspect a binding to
        decide how to react to inbound MIDI."""
        return self._binding_index.get(switch_name)

    def current_bindings(self):
        """Iterable over (switch_name, binding) for the active patch."""
        return self._binding_index.items()

    def _paint_preset_nav_leds(self):
        """Overlay LED colors for the preset-navigation row. Every switch
        in the row that maps to an existing slot in the current bank lights
        up with the bank's color (from device.preset_navigation.bank_colors,
        falling back to a neutral grey if not configured). The switch
        pointing at the active slot is full bright; the others are dimmed
        by `dim_factor`. Called after leds.render_patch so it overrides the
        LEDs of those switches."""
        nav = self.device.get("preset_navigation") or {}
        sw_map = nav.get("switches") or {}
        if not sw_map:
            return
        dim_factor = max(1, int(nav.get("dim_factor", 4)))
        bank_colors = nav.get("bank_colors") or {}
        bank_color = bank_colors.get(str(self.current_bank)) or "#888888"
        available_slots = {p["slot"] for p in self.patches.list()
                           if p["bank"] == self.current_bank}
        bright_rgb = parse_hex(bank_color)
        dim_rgb = (bright_rgb[0] // dim_factor,
                   bright_rgb[1] // dim_factor,
                   bright_rgb[2] // dim_factor)
        for sw_name, target_slot in sw_map.items():
            target_slot = int(target_slot)
            if target_slot not in available_slots:
                continue
            rgb = bright_rgb if target_slot == self.current_slot else dim_rgb
            for idx in LED_INDEX_PER_SWITCH.get(sw_name, ()):
                self.leds.strip[idx] = rgb
        self.leds.strip.show()

    # ---------- internals ----------

    def _fire(self, switch_name, action_key):
        binding = self._binding_index.get(switch_name)
        if binding is None:
            return
        action = binding.get("actions", {}).get(action_key)
        if action is None:
            return
        self.protocol.emit_event("binding_fired", switch=switch_name, action=action_key)
        self.runner.run(action)
        if binding.get("mode") == "latched":
            for sw in self.switches.switches:
                if sw.name == switch_name:
                    self.leds.set_switch_state(switch_name, binding, sw.latched_on)
                    break

    def _reindex_patch(self):
        self._binding_index = {}
        self._mode_index = {}
        global_am = self.device.get("auto_momentary_on_hold", True)
        global_am_ms = self.device.get("auto_momentary_ms", 500)
        for fsm in self.switches.switches:
            fsm.auto_momentary_on_hold = global_am
            fsm.auto_momentary_ms = global_am_ms
        for b in (self.current_patch or {}).get("bindings", []):
            sw = b.get("switch")
            if not sw:
                continue
            self._binding_index[sw] = b
            self._mode_index[sw] = b.get("mode", "tap")
            for fsm in self.switches.switches:
                if fsm.name == sw:
                    fsm.auto_momentary_on_hold = b.get("auto_momentary", global_am)
                    break
        # Overlay device.preset_navigation: a "preset row" of switches that
        # select a slot inside the current bank. Per-patch wins if the patch
        # declares its own binding for that switch. Only slots that actually
        # hold a patch are wired up - an unbound switch pointing at an empty
        # rig must do nothing (its LED is already off, see
        # _paint_preset_nav_leds), otherwise pressing it would still tell the
        # Kemper to load a rig that has no patch behind it.
        nav_switches = (self.device.get("preset_navigation") or {}).get("switches") or {}
        available_slots = {p["slot"] for p in self.patches.list()
                           if p["bank"] == self.current_bank}
        for sw_name, target_slot in nav_switches.items():
            if sw_name in self._binding_index:
                continue
            if int(target_slot) not in available_slots:
                continue
            self._binding_index[sw_name] = {
                "switch": sw_name,
                "mode": "tap",
                "actions": {"press": {"messages": [
                    {"type": "captain_patch",
                     "bank": self.current_bank,
                     "slot": int(target_slot)},
                ]}},
            }
            self._mode_index[sw_name] = "tap"
        # Overlay device.long_press_actions: switch -> message list. A long
        # press on that switch fires those messages, regardless of the patch
        # - used for global navigation (bank up/down) that should be
        # consistent across every preset. Per-patch wins if it already
        # declares its own long_press action.
        for sw_name, msgs in (self.device.get("long_press_actions") or {}).items():
            if not msgs:
                continue
            existing = self._binding_index.get(sw_name)
            if existing is None:
                self._binding_index[sw_name] = {
                    "switch": sw_name,
                    "mode": "long_press_alt",
                    "actions": {"long_press": {"messages": list(msgs)}},
                }
                self._mode_index[sw_name] = "long_press_alt"
                continue
            actions = existing.get("actions", {})
            if "long_press" in actions:
                continue
            mode = existing.get("mode", "tap")
            # Only tap upgrades cleanly to long_press_alt - every other mode
            # has its own semantics (latched/momentary/double_tap) and we
            # don't want to silently change their behavior.
            if mode != "tap":
                continue
            # Copy before mutating so we don't poison the patch dict that
            # PatchStore holds and may eventually write back to disk.
            patched = dict(existing)
            patched["mode"] = "long_press_alt"
            patched["actions"] = dict(actions)
            patched["actions"]["long_press"] = {"messages": list(msgs)}
            self._binding_index[sw_name] = patched
            self._mode_index[sw_name] = "long_press_alt"

    def _now_ms(self):
        return time.monotonic_ns() // 1_000_000

    def _emit_dirty_state(self):
        self.protocol.emit_event("dirty_state_changed", patches=self.patches.dirty_ids())

    def _emit_saved(self, saved):
        self.protocol.emit_event(
            "saved", patches=[{"bank": b, "slot": s} for (b, s) in saved]
        )

    def _emit_discarded(self, discarded):
        self.protocol.emit_event(
            "discarded", patches=[{"bank": b, "slot": s} for (b, s) in discarded]
        )
