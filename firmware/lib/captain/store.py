from . import config


class PatchStore:
    """In-memory cache of patches with dirty tracking + autosave debounce.

    Clean       - RAM matches disk
    Dirty       - RAM modified, disk stale, MIDI engine uses the RAM version
    Saving step - performed in tick() when (now - last_modified) >= debounce_ms,
                  or immediately on save_now().
    """

    def __init__(self, autosave_enabled=True, autosave_debounce_ms=2000):
        self._cache = {}                          # (bank, slot) -> patch dict
        self._dirty_ms = {}                       # (bank, slot) -> last_modified_ms
        # At most the active patch plus one recently used clean patch. Dirty
        # and otherwise non-persisted values are deliberately not tracked here
        # and can never be evicted.
        self._clean_lru = []
        self._protected_key = None
        self.autosave_enabled = autosave_enabled
        self.autosave_debounce_ms = autosave_debounce_ms
        self.on_dirty_changed = None              # callback()
        self.on_saved = None                      # callback(list[(bank, slot)])
        self.on_discarded = None                  # callback(list[(bank, slot)])

    # ---------- reads ----------

    def get(self, bank, slot):
        key = (bank, slot)
        if key not in self._cache:
            # Make room before parsing JSON so peak RAM contains at most the
            # protected patch plus the patch currently being loaded.
            self._reserve_clean_slot()
            self._cache[key] = config.load_patch(bank, slot)
            self._clean_lru.append(key)
        elif key in self._clean_lru:
            self._touch_clean(key)
        return self._cache[key]

    def read(self, bank, slot):
        """Return the authoritative patch without retaining a clean disk read."""
        key = (bank, slot)
        if key in self._cache:
            return self._cache[key]
        return config.load_patch(bank, slot)

    def protect(self, bank, slot):
        """Pin the active patch so clean-cache eviction cannot duplicate it."""
        self._protected_key = (bank, slot)

    def has(self, bank, slot):
        key = (bank, slot)
        if key in self._cache:
            return True
        try:
            # Existence checks (notably preset-navigation discovery) must not
            # retain a complete patch merely to answer a boolean question.
            # Opening the canonical path also rejects missing/unreadable files
            # without allocating their JSON object graph.
            with open(config.patch_path(bank, slot)):
                pass
            return True
        except OSError:
            return False

    def list(self):
        """Return list of {bank, slot, name, dirty, linked_to} for all patches
        on disk plus any RAM-only ones not yet written.

        `linked_to` is included so the editor can draw the link graph in
        the patches view without a follow-up GET_PATCH per row."""
        out = []
        seen = set()
        try:
            disk_patches = config.list_patches()
        except OSError:
            disk_patches = ()
        for bank, slot in disk_patches:
            key = (bank, slot)
            seen.add(key)
            name = ""
            linked_to = None
            try:
                # RAM is authoritative for clean cached values and unsaved
                # edits.  Otherwise load only long enough to copy metadata;
                # using get() here would permanently cache every full patch.
                patch = self.read(bank, slot)
                name = patch.get("name", "")
                linked_to = patch.get("linked_to")
            except OSError:
                pass
            # Drop the full transient object before allocating the next one.
            # `name`/`linked_to` retain only the metadata needed in `out`.
            patch = None
            entry = {
                "bank": bank,
                "slot": slot,
                "name": name,
                "dirty": key in self._dirty_ms,
            }
            if linked_to:
                entry["linked_to"] = linked_to
            out.append(entry)
        for (bank, slot), patch in self._cache.items():
            if (bank, slot) not in seen:
                entry = {
                    "bank": bank,
                    "slot": slot,
                    "name": patch.get("name", ""),
                    "dirty": True,
                }
                lt = patch.get("linked_to")
                if lt:
                    entry["linked_to"] = lt
                out.append(entry)
        out.sort(key=lambda e: (e["bank"], e["slot"]))
        return out

    def dirty_ids(self):
        return [{"bank": b, "slot": s} for (b, s) in self._dirty_ms]

    # ---------- writes ----------

    def put_patch(self, bank, slot, patch, now_ms):
        self._cache[(bank, slot)] = patch
        self._mark_dirty((bank, slot), now_ms)

    def put_binding(self, bank, slot, binding, now_ms):
        switch = binding.get("switch")
        if not switch:
            raise ValueError("binding missing switch")
        patch = self.get(bank, slot)
        bindings = patch.setdefault("bindings", [])
        replaced = False
        for i, b in enumerate(bindings):
            if b.get("switch") == switch:
                bindings[i] = binding
                replaced = True
                break
        if not replaced:
            bindings.append(binding)
        self._mark_dirty((bank, slot), now_ms)

    def delete(self, bank, slot):
        key = (bank, slot)
        was_dirty = key in self._dirty_ms
        try:
            import os
            os.remove(config.patch_path(bank, slot))
        except OSError as e:
            # Deletion is intentionally idempotent: a RAM-only patch has no
            # file yet, and deleting an already absent slot is a successful
            # no-op. Every other filesystem failure is real (notably EROFS
            # while USB mass storage owns CIRCUITPY) and must leave RAM as the
            # authoritative copy rather than silently losing unsaved edits.
            err = getattr(e, "errno", None)
            if err is None and getattr(e, "args", None):
                err = e.args[0]
            if err != 2:  # ENOENT (CircuitPython's errno module is optional)
                raise
        # Commit the in-memory half only after unlink succeeded or the file was
        # already absent. From here on all operations are bounded dict/list
        # removals, so a failed unlink can never leave a half-deleted patch.
        self._cache.pop(key, None)
        self._forget_clean(key)
        self._dirty_ms.pop(key, None)
        if self._protected_key == key:
            self._protected_key = None
        if was_dirty and self.on_dirty_changed:
            self.on_dirty_changed()

    # ---------- persistence ----------

    def save_now(self, bank=None, slot=None):
        if bank is None:
            keys = list(self._dirty_ms.keys())
        else:
            keys = [(bank, slot)] if (bank, slot) in self._dirty_ms else []
        saved = []
        for k in keys:
            try:
                config.save_patch(k[0], k[1], self._cache[k])
                self._dirty_ms.pop(k, None)
                self._touch_clean(k)
                saved.append(k)
            except OSError as e:
                print("save error:", k, e)
                if getattr(e, "errno", None) == 30:
                    # Read-only filesystem (USB MSC active). Stop automatic
                    # retries but KEEP the dirty marker and RAM value: neither
                    # was persisted, so clean-cache eviction must not lose it.
                    if self.autosave_enabled:
                        print("autosave disabled (filesystem read-only)")
                        self.autosave_enabled = False
        if saved:
            if self.on_saved:
                self.on_saved(saved)
            if self.on_dirty_changed:
                self.on_dirty_changed()
        return saved

    def discard(self, bank=None, slot=None):
        if bank is None:
            keys = list(self._dirty_ms.keys())
        else:
            keys = [(bank, slot)] if (bank, slot) in self._dirty_ms else []
        discarded = []
        for k in keys:
            try:
                self._reserve_clean_slot()
                patch = config.load_patch(*k)
                self._cache[k] = patch
                self._dirty_ms.pop(k, None)
                self._clean_lru.append(k)
                patch = None
            except OSError:
                self._cache.pop(k, None)
                self._forget_clean(k)
                self._dirty_ms.pop(k, None)
            discarded.append(k)
        if discarded:
            if self.on_discarded:
                self.on_discarded(discarded)
            if self.on_dirty_changed:
                self.on_dirty_changed()
        return discarded

    def tick(self, now_ms):
        """Call from the main loop. Flushes any patch whose last modification
        is older than autosave_debounce_ms."""
        if not self.autosave_enabled or not self._dirty_ms:
            return
        ready = [k for k, last in self._dirty_ms.items()
                 if now_ms - last >= self.autosave_debounce_ms]
        for k in ready:
            self.save_now(k[0], k[1])

    # ---------- internal ----------

    def _mark_dirty(self, key, now_ms):
        was_clean = key not in self._dirty_ms
        self._forget_clean(key)
        self._dirty_ms[key] = now_ms
        if was_clean and self.on_dirty_changed:
            self.on_dirty_changed()

    def _reserve_clean_slot(self):
        while len(self._clean_lru) >= 2:
            for i, old in enumerate(self._clean_lru):
                if old != self._protected_key:
                    self._clean_lru.pop(i)
                    self._cache.pop(old, None)
                    break
            else:
                return

    def _touch_clean(self, key):
        if key in self._clean_lru:
            self._clean_lru.remove(key)
        else:
            self._reserve_clean_slot()
        self._clean_lru.append(key)

    def _forget_clean(self, key):
        if key in self._clean_lru:
            self._clean_lru.remove(key)
