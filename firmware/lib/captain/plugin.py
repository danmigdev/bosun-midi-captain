"""Plugin system for target-device-specific message types.

A plugin is a Python module under /lib/plugins/ that exposes:
- NAME (str): unique identifier
- VERSION (str)
- LABEL (str): human-readable name
- MESSAGE_TYPES (dict): schema describing each message type and its params
- dispatch(msg, midi): expands a plugin message into raw MIDI via the engine

Plugins are discovered at boot, registered against the message types they
declare, and consulted by BindingRunner for any non-core message type."""

import os


class PluginRegistry:
    def __init__(self):
        self._plugins = {}                        # name -> module
        self._handlers = {}                       # msg type -> module

    def register(self, module):
        name = getattr(module, "NAME", None)
        if not name:
            return False
        if not hasattr(module, "MESSAGE_TYPES") or not callable(getattr(module, "dispatch", None)):
            return False
        self._plugins[name] = module
        for msg_type in module.MESSAGE_TYPES:
            self._handlers[msg_type] = module
        return True

    def handles(self, msg_type):
        return msg_type in self._handlers

    def dispatch(self, msg, midi):
        handler = self._handlers.get(msg.get("type"))
        if handler:
            handler.dispatch(msg, midi)

    def update_context(self, msg, context):
        """Let the plugin write into the display context based on the message
        it just dispatched. Plugin can declare an `update_context(msg, ctx)`
        function; absence is fine (no-op)."""
        handler = self._handlers.get(msg.get("type"))
        if handler and hasattr(handler, "update_context"):
            try:
                handler.update_context(msg, context)
            except Exception as e:
                print("plugin update_context failed:", e)

    def dispatch_midi_in(self, port, channel, status, data, app):
        """Broadcast an incoming MIDI event to every plugin that opted in
        with an `on_midi_in(port, channel, status, data, app)` function.
        Plugins use this to mirror device state (effect on/off LEDs, rig
        number, etc.) without the core needing to know anything about
        them. A throwing plugin doesn't block the others."""
        for m in self._plugins.values():
            if hasattr(m, "on_midi_in"):
                try:
                    m.on_midi_in(port, channel, status, data, app)
                except Exception as e:
                    print("plugin on_midi_in failed:", getattr(m, "NAME", "?"), "-", e)

    def on_patch_loaded(self, app):
        """Notify plugins that a new patch just became active (after the core
        reindexed bindings, reset latched switches and rendered LEDs). Plugins
        use this to repaint device-mirrored switch state from their own cache -
        e.g. effect-block on/off LEDs, which the core's reset_all() wiped and
        the target device only re-broadcasts on change. Opt in by declaring
        `on_patch_loaded(app)`. A throwing plugin doesn't block the others."""
        for m in self._plugins.values():
            if hasattr(m, "on_patch_loaded"):
                try:
                    m.on_patch_loaded(app)
                except Exception as e:
                    print("plugin on_patch_loaded failed:", getattr(m, "NAME", "?"), "-", e)

    def on_navigate(self, app, bank, slot):
        """A preset switch selected (bank, slot) but the core has NO patch
        there. Let plugins navigate the target device to that position anyway
        (e.g. a Kemper bank always has 5 rigs even when we don't keep a bosun
        patch for each), so the whole bank stays reachable from the preset row.
        Opt in by declaring `on_navigate(app, bank, slot)`. A throwing plugin
        doesn't block the others."""
        for m in self._plugins.values():
            if hasattr(m, "on_navigate"):
                try:
                    m.on_navigate(app, bank, slot)
                except Exception as e:
                    print("plugin on_navigate failed:", getattr(m, "NAME", "?"), "-", e)

    def on_preview(self, app, bank, slot):
        """The preset-preview cursor moved to (bank, slot) but nothing was
        loaded (no MIDI sent). Let plugins write their own display fields for
        the previewed target - e.g. the Kemper rig number - so the TFT preview
        reflects the device-specific value too, WITHOUT touching the device.
        Opt in by declaring `on_preview(app, bank, slot)`. Must not send MIDI.
        A throwing plugin doesn't block the others."""
        for m in self._plugins.values():
            if hasattr(m, "on_preview"):
                try:
                    m.on_preview(app, bank, slot)
                except Exception as e:
                    print("plugin on_preview failed:", getattr(m, "NAME", "?"), "-", e)

    def tuner_off(self, app):
        """Ask every plugin that has a tuner to leave tuner mode on the target
        device. Called when the user presses a footswitch while the tuner
        splash is up (see app._exit_tuner), so one stomp both dismisses the
        tuner and performs the switch's own action. Opt in by declaring
        `tuner_off(app)`. A throwing plugin doesn't block the others."""
        for m in self._plugins.values():
            if hasattr(m, "tuner_off"):
                try:
                    m.tuner_off(app)
                except Exception as e:
                    print("plugin tuner_off failed:", getattr(m, "NAME", "?"), "-", e)

    def tick(self, app, now_ms):
        """Per-loop tick hook for plugins that need periodic work - for
        example, sending a keep-alive beacon to a target device that
        only broadcasts state changes while subscribed. Plugins opt in
        by declaring a `tick(app, now_ms)` function."""
        for m in self._plugins.values():
            if hasattr(m, "tick"):
                try:
                    m.tick(app, now_ms)
                except Exception as e:
                    print("plugin tick failed:", getattr(m, "NAME", "?"), "-", e)

    def default_layout(self, kind):
        """Return the default tft.layout for a given device kind, by asking
        the plugin whose NAME matches the kind. Returns [] if no plugin
        matches or the plugin defines no default."""
        for m in self._plugins.values():
            if getattr(m, "NAME", None) == kind:
                return list(getattr(m, "DEFAULT_LAYOUT", []) or [])
        return []

    def _manifest_entry(self, name, m):
        return {
            "label":    getattr(m, "LABEL", name),
            "version":  getattr(m, "VERSION", "0"),
            "messages": m.MESSAGE_TYPES,
            "default_layout": getattr(m, "DEFAULT_LAYOUT", []),
            "tft_fields":     getattr(m, "TFT_FIELDS", {}),
            "config_schema":  getattr(m, "CONFIG_SCHEMA", None),
            "recipe_schema":  getattr(m, "RECIPE_SCHEMA", None),
        }

    def manifest(self):
        out = {}
        for name, m in self._plugins.items():
            out[name] = self._manifest_entry(name, m)
        return out

    def iter_manifest(self):
        """Yield (name, entry) one plugin at a time so the caller can stream
        the manifest without holding every plugin's schema in memory at once
        (the full dict can MemoryError on the RP2040 when json.dumps'd)."""
        for name, m in self._plugins.items():
            yield name, self._manifest_entry(name, m)

    def discover(self, base_path="/lib/plugins"):
        try:
            entries = os.listdir(base_path)
        except OSError:
            return
        entries = [e for e in entries
                   if not (e.startswith("_") or e.startswith("."))]
        # Import the LARGEST plugin first. Compiling a .py needs a big CONTIGUOUS
        # heap block, and that block is only available when the heap is freshest
        # (least fragmented) right after boot. kemper.py (~45 KB) fails to compile
        # ("memory allocation failed") if the smaller plugins have already carved
        # up the heap before it - so order by source size, biggest first.
        def _size(entry):
            try:
                return os.stat(base_path + "/" + entry)[6]
            except OSError:
                return 0
        entries.sort(key=_size, reverse=True)
        for entry in entries:
            if entry.endswith(".py"):
                self._import("plugins." + entry[:-3], entry[:-3])
            else:
                # Directory plugin (package with __init__.py).
                self._import("plugins." + entry, entry)

    def _import(self, full_name, leaf_name):
        # Coalesce the heap before each plugin import. Compiling a large plugin
        # (kemper.py is ~45 KB of source) needs a multi-KB contiguous block; on
        # the fragmented RP2040 heap that alloc fails ("memory allocation
        # failed, allocating N bytes") and the plugin silently doesn't load,
        # even with enough total free memory. gc.collect() merges adjacent free
        # blocks so the import has a chance at the contiguous run it needs.
        try:
            import gc
            gc.collect()
        except Exception:
            pass
        try:
            module = __import__(full_name, None, None, [leaf_name])
            registered = self.register(module)
            if registered:
                print("loaded plugin:", getattr(module, "NAME", leaf_name))
        except Exception as e:
            print("plugin load failed:", leaf_name, "-", e)
