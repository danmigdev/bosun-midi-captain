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
        self.autosave_enabled = autosave_enabled
        self.autosave_debounce_ms = autosave_debounce_ms
        self.on_dirty_changed = None              # callback()
        self.on_saved = None                      # callback(list[(bank, slot)])
        self.on_discarded = None                  # callback(list[(bank, slot)])

    # ---------- reads ----------

    def get(self, bank, slot):
        key = (bank, slot)
        if key not in self._cache:
            self._cache[key] = config.load_patch(bank, slot)
        return self._cache[key]

    def has(self, bank, slot):
        try:
            self.get(bank, slot)
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
        for bank, slot in config.list_patches():
            seen.add((bank, slot))
            name = ""
            linked_to = None
            try:
                patch = self.get(bank, slot)
                name = patch.get("name", "")
                linked_to = patch.get("linked_to")
            except OSError:
                pass
            entry = {
                "bank": bank,
                "slot": slot,
                "name": name,
                "dirty": (bank, slot) in self._dirty_ms,
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
        self._cache.pop(key, None)
        self._dirty_ms.pop(key, None)
        try:
            import os
            os.remove(config.patch_path(bank, slot))
        except OSError:
            pass
        if self.on_dirty_changed:
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
                saved.append(k)
            except OSError as e:
                print("save error:", k, e)
                if getattr(e, "errno", None) == 30:
                    # Read-only filesystem (USB MSC active). Drop the dirty
                    # marker so we don't loop, and stop trying autosave.
                    self._dirty_ms.pop(k, None)
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
                self._cache[k] = config.load_patch(*k)
            except OSError:
                self._cache.pop(k, None)
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
        self._dirty_ms[key] = now_ms
        if was_clean and self.on_dirty_changed:
            self.on_dirty_changed()
