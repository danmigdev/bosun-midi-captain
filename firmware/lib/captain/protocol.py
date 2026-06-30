import binascii
import json
import os

import usb_cdc

from . import VERSION, config, messages


def _mkdir_p(path):
    parts = [p for p in path.strip("/").split("/") if p]
    cur = ""
    for p in parts:
        cur = cur + "/" + p
        try:
            os.mkdir(cur)
        except OSError:
            pass


class Protocol:
    """Line-delimited JSON over the secondary USB CDC data port.
    Editor speaks here; primary CDC console keeps the REPL.
    Construct with the Captain app - handlers reach back into store, switch
    array, MIDI engine via the app reference."""

    def __init__(self, app):
        self.app = app
        self.port = usb_cdc.data
        self._rx_buf = bytearray()
        self._uploads = {}                        # path -> open file
        if self.port is not None:
            # Without a write timeout, usb_cdc.write blocks forever when
            # the host stops reading - the entire firmware main loop
            # freezes. Cap at 200 ms so a wedged host can't lock us out.
            try:
                self.port.write_timeout = 0.2
            except Exception:
                pass

    # ---------- io ----------

    # Hard cap on _rx_buf to prevent a misbehaving host from exhausting
    # RAM by sending a huge no-newline blob. 64 KiB is well above any
    # real protocol message (the largest expected payload is a manifest
    # response, but that's outgoing, not incoming).
    _RX_BUF_MAX = 65536

    def poll(self):
        if self.port is None:
            return None
        # Pull any new bytes into the buffer. Important: even when
        # in_waiting is 0, _rx_buf may already hold a complete line from
        # a previous read (e.g. when the editor sent "\n{json}\n" - the
        # first call processed the empty line, the next one needs to
        # consume the buffered json without waiting for new bytes).
        try:
            avail = self.port.in_waiting
        except Exception:
            avail = 0
        if avail:
            try:
                self._rx_buf.extend(self.port.read(avail))
            except Exception:
                pass
        if len(self._rx_buf) > self._RX_BUF_MAX:
            # Garbage / runaway sender. Drop everything up to the last
            # newline so we resync on the next clean message.
            last_nl = self._rx_buf.rfind(b"\n")
            if last_nl >= 0:
                self._rx_buf = bytearray(self._rx_buf[last_nl + 1:])
            else:
                self._rx_buf = bytearray()
            self._send({"type": "ERROR", "error": "rx_overflow"})
        nl = self._rx_buf.find(b"\n")
        if nl < 0:
            return None
        line = bytes(self._rx_buf[:nl])
        self._rx_buf = bytearray(self._rx_buf[nl + 1:])
        try:
            return json.loads(line)
        except ValueError:
            self._send({"type": "ERROR", "error": "bad_json"})
            return None

    def emit_event(self, event, **fields):
        payload = {"type": "EVENT", "event": event}
        payload.update(fields)
        self._send(payload)

    def _send(self, obj):
        if self.port is None:
            return
        try:
            if not self.port.connected:
                return
            # json.dumps allocates a string roughly the size of the
            # output. On CircuitPython with ~100 KB free heap, the
            # MANIFEST (multi-KB once kemper + ampero are loaded) plus
            # heap fragmentation can hit MemoryError. Retry-on-MemoryError
            # rather than collecting unconditionally - small responses
            # (ACK, EVENT) are the common case and would pay 5-20 ms
            # of gc.collect for nothing. Only the rare big response
            # triggers the recovery path.
            try:
                data = json.dumps(obj).encode() + b"\n"
            except MemoryError:
                try:
                    import gc as _gc
                    _gc.collect()
                except Exception:
                    pass
                data = json.dumps(obj).encode() + b"\n"
            # CircuitPython's usb_cdc.write honors write_timeout (set to
            # 0.2s in __init__ to keep the main loop from wedging on a
            # dead host) and may return a partial byte count when the
            # host's RX buffer can't accept everything within the
            # window. Large responses - notably MANIFEST, which can run
            # into the multi-KB range once a plugin like kemper expands
            # MESSAGE_TYPES + TFT_FIELDS + CONFIG_SCHEMA - would
            # otherwise get silently truncated and the editor would
            # never see the trailing newline. Loop until either all
            # bytes are flushed or we stall (host stopped reading).
            view = memoryview(data)
            total = len(view)
            stalls = 0
            while view and stalls < 8:
                n = self.port.write(view)
                if not n:
                    stalls += 1
                    continue
                view = view[n:]
                stalls = 0
            if view:
                # Stalled out with bytes left to send: the host stopped
                # reading mid-stream. Print to the REPL so the operator
                # running `tio` / Tauri dev sees what got lost - the
                # editor will never see this response.
                t = obj.get("type", "?")
                print("[protocol] _send STALL type=%s sent=%d/%d" % (
                    t, total - len(view), total))
        except Exception as e:
            # Host disconnected mid-write, buffer full, or attribute
            # missing on older CP. Never propagate from _send - a
            # protocol-layer error must not crash the main loop. Print
            # the exception class to the REPL so a stuck send is at
            # least observable.
            print("[protocol] _send EXC type=%s err=%s" % (
                obj.get("type", "?"), type(e).__name__))

    # ---------- dispatch ----------

    def handle(self, msg):
        if msg is None:
            return
        t = msg.get("type")
        mid = msg.get("id")
        # Proactive gc.collect before processing a request. Each request
        # is a user-initiated action (~50-200 ms apart) so the 5-20 ms
        # cost is invisible, and it dramatically reduces MemoryError on
        # the multi-KB responses (MANIFEST, LIST_PROFILES, PATCH_LIST)
        # after a long-running session with fragmented heap. EVENTs
        # emitted from the main loop (binding_fired, etc.) skip this
        # path so the input latency stays low.
        try:
            import gc as _gc
            _gc.collect()
        except Exception:
            pass
        try:
            if   t == "PING":              self._send({"type": "ACK", "id": mid, "fw": VERSION})
            elif t == "GET_DEVICE_INFO":   self._device_info(mid)
            elif t == "GET_GLOBAL":        self._get_global(mid, msg)
            elif t == "PUT_GLOBAL":        self._put_global(mid, msg)
            elif t == "LIST_PATCHES":      self._list_patches(mid, msg)
            elif t == "GET_PATCH":         self._get_patch(mid, msg)
            elif t == "PUT_PATCH":         self._put_patch(mid, msg)
            elif t == "PUT_BINDING":       self._put_binding(mid, msg)
            elif t == "DELETE_PATCH":      self._delete_patch(mid, msg)
            elif t == "SWITCH_PATCH":      self._switch_patch(mid, msg)
            elif t == "SAVE_NOW":          self._save_now(mid, msg)
            elif t == "DISCARD":           self._discard(mid, msg)
            elif t == "GET_DIRTY":         self._send({"type": "DIRTY", "id": mid, "patches": self.app.patches.dirty_ids()})
            elif t == "START_MIDI_LEARN":  self._start_learn(mid)
            elif t == "STOP_MIDI_LEARN":   self._stop_learn(mid)
            elif t == "GET_MIDI_LEARN":    self._get_midi_learn(mid, msg)
            elif t == "PUT_MIDI_LEARN":    self._put_midi_learn(mid, msg)
            elif t == "GET_MANIFEST":      self._get_manifest(mid)
            elif t == "STATS":             self._stats(mid)
            elif t == "PUT_FILE_BEGIN":    self._put_file_begin(mid, msg)
            elif t == "PUT_FILE_CHUNK":    self._put_file_chunk(mid, msg)
            elif t == "PUT_FILE_END":      self._put_file_end(mid, msg)
            elif t == "REBOOT":            self._reboot(mid)
            elif t == "LIST_PROFILES":     self._list_profiles(mid)
            elif t == "CREATE_PROFILE":    self._create_profile(mid, msg)
            elif t == "SWITCH_PROFILE":    self._switch_profile(mid, msg)
            elif t == "DELETE_PROFILE":    self._delete_profile(mid, msg)
            elif t == "RENAME_PROFILE":    self._rename_profile(mid, msg)
            elif t == "LIST_FONTS":        self._list_fonts(mid)
            elif t == "LED_PROBE":         self._led_probe(mid, msg)
            elif t == "LED_DUMP":          self._led_dump(mid, msg)
            else:                          self._send({"type": "ERROR", "id": mid, "error": "unknown_type", "of": t})
        except Exception as e:
            self._send({"type": "ERROR", "id": mid, "error": "exception", "detail": str(e), "of": t})

    # ---------- handlers ----------

    def _device_info(self, mid):
        active = ""
        try:
            active = config.active_profile_id()
        except Exception:
            pass
        self._send({
            "type": "DEVICE_INFO",
            "id": mid,
            "fw": VERSION,
            "device": self.app.device.get("device_name", "MIDI Captain"),
            "current": {"bank": self.app.current_bank, "slot": self.app.current_slot},
            "profile": active,
        })

    def _put_global(self, mid, msg):
        device = msg.get("device")
        if not isinstance(device, dict):
            self._send({"type": "ERROR", "id": mid, "error": "missing_device"})
            return
        # Cross-profile write: `profile` field targets a non-active
        # profile on disk and skips apply_global (which would mutate
        # the running state). Used by bulk import to seed a freshly
        # created profile without a SWITCH_PROFILE reboot.
        pid = msg.get("profile") or ""
        if pid:
            if not config.profile_exists(pid):
                self._send({"type": "ERROR", "id": mid, "error": "no_such_profile", "profile": pid})
                return
            config.save_device_for(device, pid)
        else:
            config.save_device(device)
            self.app.apply_global(device)
        self._send({"type": "ACK", "id": mid})

    # ---------- cross-profile read helpers ----------
    # When the editor adds `profile: "<id>"` to GET_GLOBAL / LIST_PATCHES
    # / GET_PATCH / GET_MIDI_LEARN, we read straight from disk for that
    # profile instead of returning the active profile's in-memory state.
    # This lets the editor's "Export all profiles" flow do its job
    # without a SWITCH_PROFILE-induced reboot per profile.

    def _resolve_profile(self, mid, msg):
        """Return (profile_id_to_read, use_active_state). When the editor
        omits `profile` we serve the active in-memory state (cheaper and
        avoids a stat() per call). Otherwise validate the requested
        profile exists and signal a disk read."""
        pid = msg.get("profile")
        if not pid:
            return (None, True)
        if not config.profile_exists(pid):
            self._send({"type": "ERROR", "id": mid, "error": "no_such_profile", "profile": pid})
            return (None, False)
        return (pid, False)

    def _get_global(self, mid, msg):
        pid, use_active = self._resolve_profile(mid, msg)
        if not use_active and pid is None:
            return                           # _resolve_profile already sent ERROR
        device = self.app.device if use_active else config.load_device_for(pid)
        self._send({"type": "GLOBAL", "id": mid, "device": device, "profile": pid or ""})

    def _list_patches(self, mid, msg):
        pid, use_active = self._resolve_profile(mid, msg)
        if not use_active and pid is None:
            return
        if use_active:
            patches = self.app.patches.list()
        else:
            # Cross-profile list: skip the name field (would require
            # opening every patch.json - export only needs bank+slot).
            # Empty string keeps the TypeScript type happy.
            patches = [{"bank": b, "slot": s, "name": ""}
                       for (b, s) in config.list_patches(profile=pid)]
        self._send({"type": "PATCH_LIST", "id": mid, "patches": patches, "profile": pid or ""})

    def _get_patch(self, mid, msg):
        bank, slot = msg["bank"], msg["slot"]
        pid, use_active = self._resolve_profile(mid, msg)
        if not use_active and pid is None:
            return
        try:
            patch = self.app.patches.get(bank, slot) if use_active else config.load_patch_for(bank, slot, pid)
        except OSError:
            self._send({"type": "ERROR", "id": mid, "error": "not_found", "bank": bank, "slot": slot})
            return
        self._send({"type": "PATCH", "id": mid, "bank": bank, "slot": slot, "patch": patch, "profile": pid or ""})

    def _get_midi_learn(self, mid, msg):
        pid, use_active = self._resolve_profile(mid, msg)
        if not use_active and pid is None:
            return
        table = self.app.midi_learn_table if use_active else config.load_midi_learn_for(pid)
        self._send({"type": "MIDI_LEARN", "id": mid, "table": table, "profile": pid or ""})

    def _put_patch(self, mid, msg):
        bank, slot, patch = msg["bank"], msg["slot"], msg["patch"]
        # Cross-profile write: `profile` field targets a non-active
        # profile on disk and skips the in-memory store update.
        pid = msg.get("profile") or ""
        if pid:
            if not config.profile_exists(pid):
                self._send({"type": "ERROR", "id": mid, "error": "no_such_profile", "profile": pid})
                return
            config.save_patch_for(bank, slot, patch, pid)
        else:
            self.app.put_patch(bank, slot, patch)
        self._send({"type": "ACK", "id": mid})

    def _put_binding(self, mid, msg):
        bank, slot, binding = msg["bank"], msg["slot"], msg["binding"]
        self.app.put_binding(bank, slot, binding)
        self._send({"type": "ACK", "id": mid})

    def _delete_patch(self, mid, msg):
        bank, slot = msg["bank"], msg["slot"]
        self.app.patches.delete(bank, slot)
        self._send({"type": "ACK", "id": mid})

    def _switch_patch(self, mid, msg):
        bank, slot = msg["bank"], msg["slot"]
        ok = self.app.switch_patch(bank, slot, source="editor")
        if ok:
            self._send({"type": "ACK", "id": mid})
        else:
            self._send({"type": "ERROR", "id": mid, "error": "not_found", "bank": bank, "slot": slot})

    def _save_now(self, mid, msg):
        bank, slot = msg.get("bank"), msg.get("slot")
        saved = self.app.patches.save_now(bank, slot)
        self._send({
            "type": "SAVED",
            "id": mid,
            "patches": [{"bank": b, "slot": s} for (b, s) in saved],
        })

    def _discard(self, mid, msg):
        bank, slot = msg.get("bank"), msg.get("slot")
        discarded = self.app.patches.discard(bank, slot)
        if (self.app.current_bank, self.app.current_slot) in discarded:
            self.app.reload_current_patch()
        self._send({"type": "ACK", "id": mid})

    def _start_learn(self, mid):
        self.app.midi_learn = True
        self._send({"type": "ACK", "id": mid})

    def _stop_learn(self, mid):
        self.app.midi_learn = False
        self._send({"type": "ACK", "id": mid})

    def _put_midi_learn(self, mid, msg):
        table = msg.get("table")
        if not isinstance(table, dict):
            self._send({"type": "ERROR", "id": mid, "error": "missing_table"})
            return
        # Cross-profile write: same pattern as _put_global / _put_patch.
        pid = msg.get("profile") or ""
        if pid:
            if not config.profile_exists(pid):
                self._send({"type": "ERROR", "id": mid, "error": "no_such_profile", "profile": pid})
                return
            config.save_midi_learn_for(table, pid)
        else:
            config.save_midi_learn(table)
            self.app.apply_midi_learn(table)
        self._send({"type": "ACK", "id": mid})

    def _write_bytes(self, data):
        """Write all of `data` to the port, tolerating partial writes (the
        host's RX window may not accept everything at once). Returns the count
        of bytes still unsent if the host stalled (0 == fully delivered)."""
        if self.port is None or not self.port.connected:
            return len(data)
        view = memoryview(data)
        stalls = 0
        while view and stalls < 8:
            n = self.port.write(view)
            if not n:
                stalls += 1
                continue
            view = view[n:]
            stalls = 0
        return len(view)

    def _get_manifest(self, mid):
        # The full manifest (every plugin's MESSAGE_TYPES + config/recipe
        # schemas) can exceed the RP2040 heap when json.dumps'd in one
        # allocation -> MemoryError, which left the editor with no plugins
        # (only "generic"). Stream it instead: emit the JSON piece by piece,
        # serializing a single field at a time and collecting garbage between
        # pieces, so peak allocation is one field rather than the whole tree.
        import gc
        if self.port is None or not self.port.connected:
            return
        try:
            gc.collect()
            w = self._write_bytes
            w(b'{"type":"MANIFEST","id":')
            w(json.dumps(mid).encode())
            w(b',"core_messages":')
            w(json.dumps(messages.CORE_MESSAGE_TYPES).encode())
            w(b',"plugins":{')
            first_plugin = True
            for name, entry in self.app.plugins.iter_manifest():
                w(b'' if first_plugin else b',')
                first_plugin = False
                w(json.dumps(name).encode())
                w(b':{')
                first_field = True
                for k, v in entry.items():
                    w(b'' if first_field else b',')
                    first_field = False
                    w(json.dumps(k).encode())
                    w(b':')
                    w(json.dumps(v).encode())
                    v = None
                    gc.collect()
                w(b'}')
                entry = None
                gc.collect()
            w(b'}}\n')
        except Exception as e:
            print("[protocol] _send EXC type=MANIFEST err=%s" % type(e).__name__)

    def _stats(self, mid):
        payload = {"type": "STATS", "id": mid}
        payload.update(self.app.stats())
        self._send(payload)

    # ---------- file upload (OTA) ----------

    def _put_file_begin(self, mid, msg):
        path = msg.get("path", "")
        if not path or not path.startswith("/"):
            self._send({"type": "ERROR", "id": mid, "error": "bad_path"})
            return
        parent = path.rsplit("/", 1)[0]
        if parent:
            try:
                _mkdir_p(parent)
            except OSError as e:
                self._send({"type": "ERROR", "id": mid, "error": "mkdir", "detail": str(e)})
                return
        old = self._uploads.pop(path, None)
        if old is not None:
            try: old.close()
            except Exception: pass
        try:
            f = open(path + ".tmp", "wb")
        except OSError as e:
            self._send({"type": "ERROR", "id": mid, "error": "open", "detail": str(e)})
            return
        self._uploads[path] = f
        self._send({"type": "ACK", "id": mid})

    def _put_file_chunk(self, mid, msg):
        path = msg.get("path", "")
        f = self._uploads.get(path)
        if f is None:
            self._send({"type": "ERROR", "id": mid, "error": "no_open_file"})
            return
        try:
            data = binascii.a2b_base64(msg.get("data_b64", ""))
            f.write(data)
        except Exception as e:
            self._send({"type": "ERROR", "id": mid, "error": "write", "detail": str(e)})
            return
        self._send({"type": "ACK", "id": mid})

    def _put_file_end(self, mid, msg):
        path = msg.get("path", "")
        f = self._uploads.pop(path, None)
        if f is None:
            self._send({"type": "ERROR", "id": mid, "error": "no_open_file"})
            return
        try:
            f.close()
            try:
                os.remove(path)
            except OSError:
                pass
            os.rename(path + ".tmp", path)
        except OSError as e:
            self._send({"type": "ERROR", "id": mid, "error": "rename", "detail": str(e)})
            return
        self._send({"type": "ACK", "id": mid})

    def _reboot(self, mid):
        self._send({"type": "ACK", "id": mid})
        import time
        time.sleep(0.1)  # let the ACK actually flush
        import microcontroller
        microcontroller.reset()

    # ---------- profile management ----------

    def _list_profiles(self, mid):
        self._send({
            "type": "PROFILE_LIST",
            "id": mid,
            "profiles": config.list_profiles(),
            "active": config.active_profile_id(),
        })

    def _create_profile(self, mid, msg):
        pid  = msg.get("profile_id") or msg.get("id")
        name = msg.get("name") or pid
        kind = msg.get("kind") or "unknown"
        # Seed the new profile with the matching plugin's default layout.
        # If no plugin matches the kind (e.g. "other"), default_layout is [].
        layout = []
        try:
            layout = self.app.plugins.default_layout(kind)
        except Exception:
            pass
        try:
            config.create_profile(pid, name, kind, default_layout=layout)
        except Exception as e:
            self._send({"type": "ERROR", "id": mid, "error": "create_profile", "detail": str(e)})
            return
        self._send({"type": "ACK", "id": mid, "profile_id": pid})

    def _switch_profile(self, mid, msg):
        pid = msg.get("profile_id") or msg.get("id")
        try:
            config.set_active_profile_id(pid)
        except Exception as e:
            self._send({"type": "ERROR", "id": mid, "error": "switch_profile", "detail": str(e)})
            return
        # Ack BEFORE reboot so the editor sees the result.
        self._send({"type": "ACK", "id": mid, "profile_id": pid})
        import time
        time.sleep(0.15)  # let the ACK and any buffered output flush
        import microcontroller
        microcontroller.reset()

    def _delete_profile(self, mid, msg):
        pid = msg.get("profile_id") or msg.get("id")
        try:
            config.delete_profile(pid)
        except Exception as e:
            self._send({"type": "ERROR", "id": mid, "error": "delete_profile", "detail": str(e)})
            return
        self._send({"type": "ACK", "id": mid})

    def _rename_profile(self, mid, msg):
        pid  = msg.get("profile_id") or msg.get("id")
        name = msg.get("name", "")
        try:
            config.rename_profile(pid, name)
        except Exception as e:
            self._send({"type": "ERROR", "id": mid, "error": "rename_profile", "detail": str(e)})
            return
        self._send({"type": "ACK", "id": mid})

    def _list_fonts(self, mid):
        """Return the list of *.bdf files in /fonts/ so the editor can offer
        them in the TFT Layout font picker. 'system' (terminalio) is implicit."""
        try:
            entries = os.listdir("/fonts")
        except OSError:
            entries = []
        fonts = [e for e in entries if e.endswith(".bdf") or e.endswith(".pcf")]
        fonts.sort()
        self._send({"type": "FONT_LIST", "id": mid, "fonts": fonts})

    def _led_probe(self, mid, msg):
        """Diagnostic: light a single NeoPixel index in bright red so the
        user can identify the physical switch that pixel sits under. Call
        with {type: "LED_PROBE", index: <0..29>}. After ack, normal LED
        rendering resumes on the next patch reload."""
        try:
            idx = int(msg.get("index", 0))
            strip = self.app.leds.strip
            n = len(strip)
            for i in range(n):
                strip[i] = (0, 0, 0)
            if 0 <= idx < n:
                strip[idx] = (255, 0, 0)
            strip.show()
            self._send({"type": "ACK", "id": mid, "index": idx})
        except Exception as e:
            self._send({"type": "ERROR", "id": mid, "error": "exception", "detail": str(e), "of": "LED_PROBE"})

    def _led_dump(self, mid, msg):
        """Diagnostic: return the current values of every NeoPixel as a
        list of [r, g, b] triples, plus the firmware's view of which
        switch owns which pixel indices. Lets us see exactly what was
        written vs. what the user reports seeing physically."""
        try:
            from .board import LED_INDEX_PER_SWITCH
            strip = self.app.leds.strip
            pixels = [list(strip[i]) for i in range(len(strip))]
            self._send({
                "type": "LED_DUMP", "id": mid,
                "pixels": pixels,
                "switch_indices": {k: list(v) for k, v in LED_INDEX_PER_SWITCH.items()},
                "current": {"bank": self.app.current_bank, "slot": self.app.current_slot},
            })
        except Exception as e:
            self._send({"type": "ERROR", "id": mid, "error": "exception", "detail": str(e), "of": "LED_DUMP"})
