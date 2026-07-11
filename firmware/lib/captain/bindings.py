import digitalio

from .board import FOOTSWITCHES


class SwitchFsm:
    """Per-switch finite-state machine. Reads a debounced pin level and
    converts presses/releases into binding-action triggers according to the
    binding's mode. Caller passes the mode each tick so live edits take effect
    on the next press without re-instantiation.

    Latched mode supports an "auto-momentary on hold" behavior: if the switch
    is held past auto_momentary_ms then released, the latched state reverts
    to its pre-press value (and fires the opposite toggle action)."""

    DEBOUNCE_MS = 5

    def __init__(self, name, pin,
                 long_press_ms=600,
                 double_tap_window_ms=250,
                 auto_momentary_on_hold=True,
                 auto_momentary_ms=500):
        self.name = name
        self.io = digitalio.DigitalInOut(pin)
        self.io.direction = digitalio.Direction.INPUT
        self.io.pull = digitalio.Pull.UP
        self.long_press_ms = long_press_ms
        self.double_tap_window_ms = double_tap_window_ms
        self.auto_momentary_on_hold = auto_momentary_on_hold
        self.auto_momentary_ms = auto_momentary_ms
        # Raw debounce
        self._last_raw = True              # pull-up idle = HIGH = True
        self._last_raw_change_ms = 0
        self._stable = True                # committed debounced level
        # Mode-specific scratch
        self._press_start_ms = 0
        self._fired_long_press = False
        self.latched_on = False
        self._latched_pre_press = False
        self._tap_pending_until_ms = 0

    def reset(self):
        # Preserve "long-press already consumed" if the switch is still
        # held at reset time. Without this, a binding action that itself
        # triggers a patch reload (captain_patch, captain_bank_step,
        # preset_navigation) ends up clearing _fired_long_press while the
        # user is still holding the switch. The very next tick sees
        # `_stable == False` and `_fired_long_press == False`, the
        # threshold check (now_ms - _press_start_ms >= long_press_ms)
        # is trivially true because press_start was just zeroed, and
        # the FSM re-fires the long_press action. The action reloads
        # the patch, which resets again - and the user ends up ping-
        # ponging between banks until they release the switch.
        still_held = not self._stable
        self._press_start_ms = 0
        self._fired_long_press = still_held
        self.latched_on = False
        self._latched_pre_press = False
        self._tap_pending_until_ms = 0

    def poll(self, now_ms, mode):
        """Returns (raw_edge, triggers) where raw_edge is None/'press'/'release'
        and triggers is a list of action keys to look up in binding.actions."""
        raw = self.io.value
        if raw != self._last_raw:
            self._last_raw = raw
            self._last_raw_change_ms = now_ms

        triggers = []
        raw_edge = None
        if raw != self._stable and (now_ms - self._last_raw_change_ms) >= self.DEBOUNCE_MS:
            self._stable = raw
            if not raw:                     # LOW = pressed
                raw_edge = "press"
                triggers = self._on_press(now_ms, mode)
            else:
                raw_edge = "release"
                triggers = self._on_release(now_ms, mode)

        triggers += self._on_tick(now_ms, mode)
        return raw_edge, triggers

    def _on_press(self, now_ms, mode):
        if mode == "tap":
            return ["press"]
        if mode == "latched":
            self._latched_pre_press = self.latched_on
            self._press_start_ms = now_ms
            self.latched_on = not self.latched_on
            return ["toggle_on" if self.latched_on else "toggle_off"]
        if mode == "momentary":
            return ["press"]
        if mode == "long_press_alt":
            self._press_start_ms = now_ms
            self._fired_long_press = False
            return []
        if mode == "double_tap":
            if self._tap_pending_until_ms and now_ms <= self._tap_pending_until_ms:
                self._tap_pending_until_ms = 0
                return ["double_tap"]
            self._tap_pending_until_ms = now_ms + self.double_tap_window_ms
            return []
        return []

    def _on_release(self, now_ms, mode):
        if mode == "momentary":
            return ["release"]
        if mode == "long_press_alt":
            if not self._fired_long_press:
                held = now_ms - self._press_start_ms
                if held < self.long_press_ms:
                    return ["press"]
            return []
        if mode == "latched" and self.auto_momentary_on_hold:
            held = now_ms - self._press_start_ms
            if held >= self.auto_momentary_ms and self.latched_on != self._latched_pre_press:
                self.latched_on = self._latched_pre_press
                return ["toggle_on" if self.latched_on else "toggle_off"]
        return []

    def _on_tick(self, now_ms, mode):
        triggers = []
        if mode == "long_press_alt" and not self._stable and not self._fired_long_press:
            if now_ms - self._press_start_ms >= self.long_press_ms:
                self._fired_long_press = True
                triggers.append("long_press")
        if mode == "double_tap" and self._tap_pending_until_ms:
            if now_ms > self._tap_pending_until_ms:
                self._tap_pending_until_ms = 0
                triggers.append("press")
        return triggers


class SwitchArray:
    def __init__(self,
                 long_press_ms=600,
                 double_tap_window_ms=250,
                 auto_momentary_on_hold=True,
                 auto_momentary_ms=500):
        self.switches = [
            SwitchFsm(name, pin,
                      long_press_ms=long_press_ms,
                      double_tap_window_ms=double_tap_window_ms,
                      auto_momentary_on_hold=auto_momentary_on_hold,
                      auto_momentary_ms=auto_momentary_ms)
            for name, pin in FOOTSWITCHES.items()
        ]

    def reset_all(self):
        for sw in self.switches:
            sw.reset()


class BindingRunner:
    """Executes the message list attached to a binding action.

    Knows only the universal MIDI primitives (cc/pc/note_on/note_off) and the
    utility 'delay'. Everything else is delegated to the plugin registry."""

    def __init__(self, midi, plugins=None, app=None):
        self.midi = midi
        self.plugins = plugins
        self.app = app

    def run(self, action):
        if action is None:
            return
        for msg in action.get("messages", []):
            self._dispatch(msg)

    def run_message(self, msg):
        """Dispatch a single message outside a binding action. Used by the
        expression-pedal input, which substitutes the live 0..127 into a
        message template and sends it every time the pedal moves."""
        if msg is not None:
            self._dispatch(msg)

    def _dispatch(self, msg):
        t = msg.get("type")
        ch = msg.get("channel", 1)
        if t == "cc":
            self.midi.send_cc(ch, msg["cc"], msg["value"])
        elif t == "pc":
            self.midi.send_pc(ch, msg["program"])
        elif t == "note_on":
            self.midi.send_note_on(ch, msg["note"], msg.get("velocity", 100))
        elif t == "note_off":
            self.midi.send_note_off(ch, msg["note"], msg.get("velocity", 64))
        elif t == "delay":
            import time
            time.sleep(int(msg["ms"]) / 1000.0)
        elif t == "captain_patch":
            if self.app is not None:
                try:
                    bank = int(msg.get("bank", 1))
                    slot = int(msg.get("slot", 1))
                    if not self.app.switch_patch(bank, slot, source="binding"):
                        # No bosun patch at this slot: still navigate the target
                        # device to that rig. Every bank has 5 rigs even when we
                        # don't keep a bosun patch for each, so the whole preset
                        # row stays usable. The device-specific translation
                        # (e.g. kemper_rig) lives in the plugin.
                        if self.plugins is not None:
                            self.plugins.on_navigate(self.app, bank, slot)
                except Exception as e:
                    print("captain_patch failed:", e)
        elif t == "captain_bank_step":
            if self.app is not None:
                try:
                    self.app.bank_step(int(msg.get("delta", 1)))
                except Exception as e:
                    print("captain_bank_step failed:", e)
        elif self.plugins is not None and self.plugins.handles(t):
            self.plugins.dispatch(msg, self.midi)
            # Let the plugin update the TFT display context - but skip the
            # immediate refresh. switch_patch refreshes once at the end of
            # on_enter, and binding actions outside on_enter are followed
            # by their own refresh (or don't change context at all). The
            # old per-message refresh blocked the main loop with a TFT
            # render after every kemper_rig / ampero_scene / etc., adding
            # up to hundreds of ms when on_enter chained several plugin
            # messages.
            if self.app is not None and hasattr(self.app, "display_context"):
                self.plugins.update_context(msg, self.app.display_context)
        else:
            print("unknown message type:", t)
