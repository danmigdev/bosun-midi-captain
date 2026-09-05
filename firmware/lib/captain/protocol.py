import gc
import json
import os
import time

import usb_cdc

from . import VERSION, config, messages


class Protocol:
    _MAX_BG_QUEUE = 8
    _MAX_PENDING_CHUNKS = 128
    _MAX_PENDING_BYTES = 65536
    # usb_cdc is configured non-blocking below. Positive partial progress can
    # therefore be consumed inline without resetting a 200 ms timeout; the
    # first zero queues the exact unsent tail for a later main-loop tick.
    _MAX_DIRECT_WRITE_STALLS = 1
    # USB full-speed CDC has a 64-byte bulk-IN max packet. An exact 64-byte
    # write does not terminate the transfer with a short packet, so TinyUSB's
    # flush path waits before the host receives it. Raw-REPL measurement for
    # 8192 bytes: 63-byte chunks = 0.066 s; 64/128/256/512 = ~0.35 s. Keep the
    # manifest packet deliberately short: it preserves the low-heap bound and
    # is over 5x faster than every exact multiple of the endpoint packet.
    _MANIFEST_CHUNK_SIZE = 63
    _MANIFEST_STRING_CHARS = 8
    _MANIFEST_YIELD_BYTES = 1024
    _MANIFEST_GC_YIELDS = 4
    _MANIFEST_TAIL_PATH = "/lib/captain/manifest-tail.json"
    _MANIFEST_TAIL_PREFIX = b',"core_messages":'
    _MANIFEST_TAIL_SUFFIX = b'}}\n'
    _STATIC_MANIFEST_PLUGINS = (
        "kemper_player", "headrush_core", "ampero_ii_stage",
        "line6_helix", "generic_midi",
    )
    """Line-delimited JSON over the secondary USB CDC data port.
    Editor speaks here; primary CDC console keeps the REPL.
    Construct with the Captain app - handlers reach back into store, switch
    array, MIDI engine via the app reference."""

    def __init__(self, app):
        self.app = app
        self.port = usb_cdc.data
        self._rx_buf = bytearray()
        # A USB read consumes bytes before bytearray.extend() gets a chance to
        # grow the parser buffer.  On the Captain's fragmented heap that grow
        # can MemoryError; dropping the already-read chunk then turns the next
        # suffix/newline into an apparently malformed JSON command.  Retain an
        # uncommitted chunk (and the exact partial offset, defensively) until
        # the buffer can accept it.
        self._rx_pending = None
        self._rx_pending_offset = 0
        self._rx_pending_count = 0
        self._rx_failures = 0
        self._rx_file = None
        self._rx_size = 0
        self._rx_discard = False
        self._rx_mid = None
        self._rx_read_buf = bytearray(self._RX_READ_MAX)
        self._rx_octet = bytearray(1)
        self._rx_path = config.CONFIG_ROOT + "/.bosun-rx.tmp"
        # This one private staging file is never configuration. A reset in
        # the middle of reception must not leave an abandoned upload behind.
        self._close_rx_file()
        self._uploads = {}                        # path -> open file
        self._upload_sizes = {}                   # path -> expected byte count
        # In-flight resumable background response (GET_MANIFEST/GET_GLOBAL/
        # GET_PATCH - see _start_background/pump_background). At most one
        # actively advancing at a time; anything else queues in _bg_queue
        # rather than preempting it (see _start_background).
        self._bg_gen = None
        self._bg_mid = None
        self._bg_request_type = None
        self._bg_queue = []
        # Other _send() responses queued because they arrived while a
        # background line was still open on the wire (no trailing newline
        # yet) - writing them immediately would land mid-line and corrupt
        # both messages. Flushed the instant the background line closes.
        # See _send()'s _bg_gen check and _flush_pending.
        self._pending_out = []
        self._pending_bytes = 0
        # Complete protocol lines deferred while a streamed response owns the
        # wire.  Kept separate from _pending_out (which is the continuation of
        # that open line), otherwise an ACK could be inserted mid-JSON.
        self._deferred_out = []
        self._deferred_bytes = 0
        # Recovery barrier for a background generator that failed after
        # opening a JSON line.  1 means the delimiter still has to be sent;
        # 2 means that delimiter is itself queued behind a stalled write.
        # While non-zero, _send() and _start_background() must keep treating
        # the wire as owned even though the failed generator is already gone.
        self._bg_line_seal = 0
        self._was_connected = bool(self.port is not None and self.port.connected)
        if self.port is not None:
            # Never wait inside usb_cdc.write. CircuitPython may return a
            # positive partial count *after* waiting the entire timeout; an
            # inline retry then starts that timeout again and a single 63-byte
            # chunk can occupy a firmware tick for seconds. With zero timeout
            # each positive prefix is immediate, while _write_direct stops at
            # the first zero and the queue resumes it on a later tick.
            try:
                self.port.write_timeout = 0
            except Exception:
                pass
            try:
                # A fixed-size readinto must return the currently available
                # bytes, never wait for the rest of the 256-byte buffer.
                self.port.timeout = 0
            except Exception:
                pass

    # ---------- io ----------

    # Hard cap on _rx_buf to prevent a misbehaving host from exhausting
    # RAM by sending a huge no-newline blob. 64 KiB is well above any
    # real protocol message (the largest expected payload is a manifest
    # response, but that's outgoing, not incoming).
    _RX_BUF_MAX = 65536
    # Preallocate the USB input buffer at boot. readinto() can fill a whole
    # packet even when a fresh read(256) allocation cannot fit the heap.
    _RX_READ_MAX = 256
    _RX_SPOOL_AT = 512

    def _close_rx_file(self):
        rx_file = self._rx_file
        self._rx_file = None
        try:
            if rx_file is not None:
                rx_file.close()
        finally:
            try:
                os.remove(self._rx_path)
            except OSError:
                pass

    def _remember_rx_id(self):
        """Recover complete leading metadata, without scanning the payload.

        New clients send id before their payload. Older clients still parse
        normally, but an id at the end of a failed line cannot be recovered.
        Only a successfully decoded root object prefix supplies an error id.
        """
        if self._rx_mid is not None:
            return
        try:
            prefix = bytes(self._rx_buf[:128])
            for end in range(len(prefix)):
                if prefix[end] != 44:  # comma terminating a leading field
                    continue
                try:
                    header = json.loads(prefix[:end] + b"}")
                except (ValueError, TypeError):
                    continue
                if isinstance(header, dict):
                    mid = header.get("id")
                    if isinstance(mid, (str, int)):
                        self._rx_mid = mid
                        return
        except MemoryError:
            pass

    def _rx_error(self, error, discard=True):
        self._remember_rx_id()
        mid = self._rx_mid
        self._rx_mid = None
        self._rx_buf = bytearray()
        self._rx_size = 0
        self._rx_failures = 0
        self._rx_discard = discard
        try:
            self._close_rx_file()
        except (OSError, MemoryError):
            pass
        gc.collect()
        response = {"type": "ERROR", "error": error}
        if mid is not None:
            response["id"] = mid
        self._send(response)

    def _write_rx(self, end):
        """Spool bytes unchanged: a USB boundary may bisect a UTF-8 codepoint."""
        if not self._rx_size:
            self._remember_rx_id()
        if self._rx_file is None:
            self._rx_file = open(self._rx_path, "wb")
        offset = 0
        while offset < end:
            view = memoryview(self._rx_buf)[offset:min(end, offset + self._RX_READ_MAX)]
            try:
                try:
                    count = self._rx_file.write(view)
                except TypeError:
                    # Older builds may accept bytes but not buffer views.
                    count = self._rx_file.write(bytes(view))
                if count is None or count <= 0 or count > len(view):
                    raise OSError("rx_write")
                offset += count
            finally:
                view = None
        self._rx_size += end

    def _consume_rx_buffer(self, nl):
        # Retain an already-read following line without slicing/copying it.
        # The retained allocation is bounded by one read for a spooled line.
        if nl >= 0:
            if self._rx_pending is not None:
                # A partially successful extend may already include a full
                # line. Rewind its original chunk over the committed tail.
                self._rx_pending_offset -= len(self._rx_buf) - nl - 1
                if self._rx_pending_offset == self._rx_pending_count:
                    self._rx_pending = None
                    self._rx_pending_offset = 0
                    self._rx_pending_count = 0
            elif nl + 1 < len(self._rx_buf):
                self._rx_pending = self._rx_buf
                self._rx_pending_offset = nl + 1
                self._rx_pending_count = len(self._rx_buf)
        self._rx_buf = bytearray()

    def _append_rx_chunk(self, chunk, offset, count):
        """Commit an already-consumed CDC chunk without ever losing bytes."""
        # CircuitPython 9.2.7 py/objarray.c: array_append sets free before
        # m_renew; an allocation failure corrupts its capacity, so retrying
        # append can write beyond the buffer. array_extend updates capacity
        # only AFTER allocation. Use a preallocated one-byte buffer to keep
        # that safe path small, including retries at offset zero.
        # Respect readinto's count, not the scratch buffer's stale suffix.
        i = offset
        while i < count:
            try:
                self._rx_octet[0] = chunk[i]
                self._rx_buf.extend(self._rx_octet)
            except MemoryError:
                self._rx_pending = chunk
                self._rx_pending_offset = i
                self._rx_pending_count = count
                self._recover_rx_append()
                return False
            i += 1
        self._rx_pending = None
        self._rx_pending_offset = 0
        self._rx_pending_count = 0
        self._rx_failures = 0
        return True

    def _recover_rx_append(self):
        gc.collect()
        self._rx_failures += 1
        if self._rx_failures < 2 or self._rx_buf.find(b"\n") >= 0:
            return
        if not self._rx_buf:
            self._rx_error("rx_oom")
            return
        # Retry a transient allocation once, then free the existing buffer
        # through the same spill path. Never pin an unread suffix forever.
        try:
            self._write_rx(len(self._rx_buf))
            self._rx_buf = bytearray()
            self._rx_failures = 0
        except MemoryError:
            self._rx_error("rx_oom")
        except OSError:
            self._rx_error("rx_io")

    def poll(self):
        self._sync_connection()
        if self.port is None or not self.port.connected:
            return None
        chunk = self._rx_pending
        offset = self._rx_pending_offset
        count = self._rx_pending_count
        ready = self._rx_buf.find(b"\n") >= 0
        if ready:
            chunk = None
        if chunk is None and not ready:
            try:
                avail = self.port.in_waiting
            except Exception:
                avail = 0
            if not avail:
                return None
            try:
                count = self.port.readinto(self._rx_read_buf) or 0
                if count < 0 or count > len(self._rx_read_buf):
                    raise OSError("rx_read")
                chunk = self._rx_read_buf
            except MemoryError:
                if not self._rx_discard:
                    self._rx_error("rx_oom")
                return None
            except Exception:
                if not self._rx_discard:
                    self._rx_error("rx_io")
                return None
        if not count and not ready:
            return None
        if self._rx_discard:
            nl = offset
            while nl < count and chunk[nl] != 10:
                nl += 1
            if nl < count:
                self._rx_discard = False
                offset = nl + 1
            else:
                offset = count
            if offset < count:
                self._rx_pending = chunk
                self._rx_pending_offset = offset
                self._rx_pending_count = count
            else:
                self._rx_pending = None
                self._rx_pending_offset = 0
                self._rx_pending_count = 0
            return None
        if chunk is not None and not self._append_rx_chunk(chunk, offset, count):
            return None
        # No consumed USB chunk or large raw JSON allocation may survive
        # into json.load(). A batched next command remains bounded by 256 B.
        chunk = None
        nl = self._rx_buf.find(b"\n")
        end = nl if nl >= 0 else len(self._rx_buf)
        if self._rx_size + end > self._RX_BUF_MAX:
            self._remember_rx_id()
            self._consume_rx_buffer(nl)
            self._rx_error("rx_overflow", discard=nl < 0)
            return None
        if nl < 0:
            if self._rx_file is not None or len(self._rx_buf) >= self._RX_SPOOL_AT:
                try:
                    self._write_rx(len(self._rx_buf))
                    self._rx_buf = bytearray()
                except MemoryError:
                    self._rx_error("rx_oom")
                except OSError:
                    self._rx_error("rx_io")
            return None
        msg = None
        err = None
        rx_view = None
        consumed = False
        try:
            if self._rx_file is not None or nl >= self._RX_SPOOL_AT:
                self._write_rx(nl)
                self._rx_file.close()
                self._rx_file = None
                self._consume_rx_buffer(nl)
                consumed = True
                gc.collect()
                with open(self._rx_path, "r") as source:
                    msg = json.load(source)
            else:
                rx_view = memoryview(self._rx_buf)[:nl]
                try:
                    msg = json.loads(rx_view)
                except TypeError:
                    # CPython and some CircuitPython builds reject a view.
                    # This compatibility copy is now at most 512 bytes.
                    msg = json.loads(bytes(rx_view))
        except ValueError:
            err = "bad_json"
        except MemoryError:
            err = "rx_oom"
        except OSError:
            err = "rx_io"
        finally:
            rx_view = None
            if not consumed:
                if err:
                    self._remember_rx_id()
                self._consume_rx_buffer(nl)
            if consumed or self._rx_file is not None:
                try:
                    self._close_rx_file()
                except (OSError, MemoryError):
                    err = err or "rx_io"
            self._rx_size = 0
        if err:
            self._rx_error(err, discard=False)
            return None
        self._rx_mid = None
        return msg

    def emit_event(self, event, **fields):
        payload = {"type": "EVENT", "event": event}
        payload.update(fields)
        # _send() never raises - it swallows every exception, including
        # MemoryError, and only prints to the REPL (see its own comment
        # below) - so a send failing from heap pressure right after a big
        # inbound Kemper SYSEX burst (exactly when a footswitch-triggered
        # patch_switched is most likely to fire) silently drops this EVENT
        # with zero visible signal, and the editor never learns the patch
        # actually changed - e.g. Stage's switch labels then keep showing
        # the previous patch's bindings indefinitely (2026-08-15/16:
        # confirmed live). Dry-run the encode first, with a
        # gc.collect()-and-retry - the identical fix already applied to
        # _push_context() for the same failure class. If it still can't be
        # encoded even after that, there is no queue to retry this into,
        # so drop it - but that at least makes the COMMON case (encoding
        # succeeds) reliable instead of silently racing heap fragmentation
        # on every single event.
        try:
            json.dumps(payload)
        except MemoryError:
            try:
                gc.collect()
            except Exception:
                pass
            try:
                json.dumps(payload)
            except MemoryError:
                return
        self._send(payload)

    def _send(self, obj):
        if self.port is None:
            return False
        try:
            if not self.port.connected:
                self._sync_connection()
                return False
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
            if self._bg_gen is not None or self._bg_line_seal:
                # A background response (GET_MANIFEST/GET_GLOBAL - see
                # _start_background/pump_background) has an open line on the
                # wire right now: it has written some bytes but not yet its
                # own trailing newline, because pump_background() only
                # advances it one slice per tick. Writing this response's
                # bytes immediately would land in the MIDDLE of that
                # unterminated line, corrupting BOTH into unparseable
                # garbage on the editor's line-delimited JSON parser
                # (confirmed 2026-08-16 by firmware_stability_test.py:
                # test_protocol_barrage - a PUT_GLOBAL ACK sent while a
                # GET_GLOBAL background line was open vanished entirely).
                # Queue it - pump_background() flushes everything queued
                # here the instant the background line closes.
                return self._queue_deferred(data)
            # CircuitPython's usb_cdc.write is configured non-blocking above
            # and may return a partial byte count when the host's RX buffer
            # cannot accept everything immediately. Large responses - notably
            # MANIFEST, which can run
            # into the multi-KB range once a plugin like kemper expands
            # MESSAGE_TYPES + TFT_FIELDS + CONFIG_SCHEMA - would
            # otherwise get silently truncated and the editor would
            # never see the trailing newline. Loop until either all
            # bytes are flushed or we stall (host stopped reading).
            if self._pending_out:
                if not self._queue_output(data):
                    return False
                self._flush_pending()
                return not self._pending_out
            left = self._write_bytes(data)
            return left == 0
        except Exception as e:
            # Host disconnected mid-write, buffer full, or attribute
            # missing on older CP. Never propagate from _send - a
            # protocol-layer error must not crash the main loop. Print
            # the exception class to the REPL so a stuck send is at
            # least observable.
            print("[protocol] _send EXC type=%s err=%s" % (
                obj.get("type", "?"), type(e).__name__))
            return False

    def _sync_connection(self):
        """Discard session-local work when USB disconnects.

        A new CDC session must never receive the tail of a JSON line or a
        command response requested by the previous host.
        """
        connected = bool(self.port is not None and self.port.connected)
        if self._was_connected and not connected:
            try:
                if self._bg_gen is not None:
                    self._bg_gen.close()
            except Exception:
                pass
            self._bg_gen = None
            self._bg_mid = None
            self._bg_request_type = None
            self._bg_queue = []
            self._pending_out = []
            self._pending_bytes = 0
            self._deferred_out = []
            self._deferred_bytes = 0
            self._bg_line_seal = 0
            self._rx_buf = bytearray()
            self._rx_pending = None
            self._rx_pending_offset = 0
            self._rx_pending_count = 0
            self._rx_failures = 0
            self._rx_size = 0
            self._rx_mid = None
            self._rx_discard = False
            self._close_uploads()
            self._release_ota()
            try:
                self._close_rx_file()
            except (OSError, MemoryError):
                pass
        self._was_connected = connected

    def _queue_output(self, data):
        if (len(self._pending_out) >= self._MAX_PENDING_CHUNKS or
                self._pending_bytes + len(data) > self._MAX_PENDING_BYTES):
            return False
        chunk = bytes(data)
        self._pending_out.append(chunk)
        self._pending_bytes += len(chunk)
        return True

    def _queue_deferred(self, data):
        if (len(self._deferred_out) >= self._MAX_PENDING_CHUNKS or
                self._pending_bytes + self._deferred_bytes + len(data) >
                self._MAX_PENDING_BYTES):
            return False
        chunk = bytes(data)
        self._deferred_out.append(chunk)
        self._deferred_bytes += len(chunk)
        return True

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
            elif t == "GET_GLOBAL":        self._start_background(self._get_global_gen(mid, msg), mid, t)
            elif t == "PUT_GLOBAL":        self._put_global(mid, msg)
            elif t == "LIST_PATCHES":      self._list_patches(mid, msg)
            elif t == "GET_PATCH":         self._start_background(self._get_patch_gen(mid, msg), mid, t)
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
            elif t == "GET_MANIFEST":      self._start_background(self._get_manifest_gen(mid), mid, t)
            elif t == "STATS":             self._stats(mid)
            elif t == "SET_MIDI_MONITOR":  self._set_midi_monitor(mid, msg)
            elif t == "PUT_FILE_BEGIN":    self._put_file("begin", mid, msg)
            elif t == "PUT_FILE_CHUNK":    self._put_file("chunk", mid, msg)
            elif t == "PUT_FILE_END":      self._put_file("end", mid, msg)
            elif t == "REBOOT":            self._reboot(mid)
            elif t == "LIST_PROFILES":     self._list_profiles(mid)
            elif t == "CREATE_PROFILE":    self._create_profile(mid, msg)
            elif t == "SWITCH_PROFILE":    self._switch_profile(mid, msg)
            elif t == "DELETE_PROFILE":    self._delete_profile(mid, msg)
            elif t == "RENAME_PROFILE":    self._rename_profile(mid, msg)
            elif t == "LIST_FONTS":        self._list_fonts(mid)
            elif t == "LED_PROBE":         self._led_probe(mid, msg)
            elif t == "LED_DUMP":          self._led_dump(mid, msg)
            elif t == "GET_RIG_INFO":      self._get_rig_info(mid, msg)
            elif t == "GET_CONTEXT":       self._start_background(self._get_context_gen(mid), mid, t)
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
        device = getattr(self.app, "device", {})
        if not isinstance(device, dict):
            device = {}
        response = {
            "type": "DEVICE_INFO",
            "id": mid,
            "fw": VERSION,
            "device": device.get("device_name", "MIDI Captain"),
            "current": {"bank": self.app.current_bank, "slot": self.app.current_slot},
            "profile": active,
        }
        # Stage only needs this small device-config subtree to map the lower
        # rig row.  Supplying it here avoids waiting for the much larger,
        # streamed GLOBAL response without bloating the fast bootstrap reply.
        preset_navigation = device.get("preset_navigation")
        # Presence is also the capability marker used by Stage: legacy
        # firmware omits this field, while current firmware sends an empty
        # object for a profile with no usable mapping and need not fall back
        # to the heavyweight GLOBAL stream.
        if not isinstance(preset_navigation, dict):
            preset_navigation = {}
        response["preset_navigation"] = preset_navigation
        # Stage's fast bootstrap only needs field colors, not all of GLOBAL.
        colors = {}
        labels = {}
        tft = device.get("tft")
        layout = tft.get("layout") if isinstance(tft, dict) else None
        if isinstance(layout, list):
            for entry in layout:
                if not isinstance(entry, dict):
                    continue
                field = entry.get("field")
                color = entry.get("color")
                if (isinstance(field, str) and field and
                        isinstance(color, str) and field not in colors):
                    colors[field] = color
                if (field in ("bank", "kemper_bank", "kemper_rig_in_bank",
                              "kemper_rig", "slot") and field not in labels):
                    prefix = entry.get("prefix") or ""
                    suffix = entry.get("suffix") or ""
                    labels[field] = {
                        "prefix": prefix if isinstance(prefix, str) else "",
                        "suffix": suffix if isinstance(suffix, str) else "",
                    }
        response["tft_colors"] = colors
        response["tft_labels"] = labels
        # Never hand the whole object to _send(): on the fragmented RP2040
        # heap, adding preset_navigation made even this bootstrap response
        # fail both contiguous json.dumps() attempts.  Stream scalar leaves
        # and the navigation maps instead.  With no nested navigation entries
        # this generator completes synchronously in _start_background(); with
        # a real map it yields at bounded GC points and cannot be interleaved
        # with another protocol line.
        self._start_background(
            self._json_line_gen(response, depth=1), mid, "GET_DEVICE_INFO")

    def _json_line_gen(self, response, depth=0):
        """Emit one JSON response without encoding its whole container.

        The background owner prevents another response from being inserted
        before this line's newline.  Exceptions deliberately propagate to
        pump_background(), which seals any partial record before releasing
        deferred ACKs/events.
        """
        import gc
        if self.port is None or not self.port.connected:
            return
        gc.collect()
        yield from self._stream_value(
            self._write_bytes, response, gc, depth=depth)
        self._write_bytes(b"\n")

    def _set_midi_monitor(self, mid, msg):
        on = bool(msg.get("on"))
        self.app.set_midi_monitor(on)
        self._send({"type": "ACK", "id": mid, "on": on})

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
        if not pid:
            self.emit_event("global_changed")

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

    def _get_global_gen(self, mid, msg):
        pid, use_active = self._resolve_profile(mid, msg)
        if not use_active and pid is None:
            return                           # _resolve_profile already sent ERROR
        device = self.app.device if use_active else config.load_device_for(pid)
        # Stream the device dict field by field rather than json.dumps'ing the
        # whole thing: the full config (TFT layout + expression + 25-bank preset
        # colours + all settings) can exceed a single contiguous allocation on
        # the RP2040 heap and MemoryError, which left the editor with no device
        # config at all (empty Screen-layout / Settings, hanging layout saves).
        # Same fix as _get_manifest_gen. See _stream_value.
        import gc
        if self.port is None or not self.port.connected:
            return
        try:
            gc.collect()
            w = self._write_bytes
            w(b'{"type":"GLOBAL","id":')
            w(json.dumps(mid).encode())
            w(b',"profile":')
            w(json.dumps(pid or "").encode())
            w(b',"device":')
            yield from self._stream_value(w, device, gc)
            w(b'}\n')
        except Exception as e:
            print("[protocol] _send EXC type=GLOBAL err=%s" % type(e).__name__)
            raise

    def _start_background(self, gen, mid=None, request_type=None):
        """Register a resumable generator-based response (GET_MANIFEST,
        GET_GLOBAL, GET_PATCH - see _get_manifest_gen/_get_global_gen/
        _get_patch_gen) to be advanced from the main loop instead of run to
        completion inline. All three stream their response via
        _write_bytes/_stream_value rather than one json.dumps() call, to
        avoid needing one large contiguous allocation on the fragmented
        RP2040 heap (GET_PATCH's own previous single-dumps()
        dry-run-and-retry still gave up completely - no response at all -
        if that retry also MemoryError'd; confirmed live 2026-08-16).
        GET_MANIFEST/GET_GLOBAL can additionally run to multiple KB with no
        yielding at all, which measured 5.25 s of dead time on real
        hardware for a 22 KB manifest (2026-08-16) - during which
        tick_once() never returns, so protocol.poll() never runs again and
        every other queued request sits unread in _rx_buf for the entire
        duration, plus footswitch polling and MIDI processing are frozen
        too. If one is already in flight, queue this one rather than
        abandoning the first: an abandoned generator's response has no
        trailing newline yet, so replacing it would corrupt the wire the
        same way an interleaved _send() would (see _send()'s _bg_gen
        check) - confirmed live 2026-08-16 once GET_PATCH became
        background-driven too: a burst of simultaneous GET_PATCH requests
        kept preempting an in-flight GET_MANIFEST, truncating it every
        single time. Queueing costs nothing for the common case (most
        GET_PATCH/GET_GLOBAL responses finish within a single slice - see
        pump_background's drain-the-queue loop) and just means a genuine
        retry (e.g. the editor's GET_MANIFEST watchdog - getManifestAwait
        in protocol.ts) waits its turn instead of stepping on the
        original."""
        if self._bg_gen is not None or self._bg_line_seal:
            if len(self._bg_queue) < self._MAX_BG_QUEUE:
                self._bg_queue.append((gen, mid, request_type))
            else:
                try:
                    gen.close()
                except Exception:
                    pass
                # A bounded queue is required on the RP2040, but silently
                # discarding the first request beyond that bound leaves its
                # caller waiting forever. The current streamed line still
                # owns the wire, so _send() defers this complete correlated
                # rejection until that line has ended.
                if request_type is not None:
                    self._send({
                        "type": "ERROR", "id": mid,
                        "error": "background_busy", "of": request_type,
                    })
            return
        self._bg_gen = gen
        self._bg_mid = mid
        self._bg_request_type = request_type
        self.pump_background()

    def pump_background(self):
        """Advance the in-flight background generator (if any) by one step;
        once it (and anything ahead of it) fully completes, start the next
        queued one and give IT a turn too, in the same call. Most GET_PATCH/
        GET_GLOBAL responses finish inside a single slice, so a burst of
        several drains within one tick_once() instead of each costing its
        own idle tick; a genuinely large one (GET_MANIFEST) still only gets
        one slice per call, so it can't starve whatever's queued behind it
        for more than a tick at a time. Call once per main-loop tick, after
        protocol.poll()/handle() so a request that arrived this tick is
        already queued (or dispatched, if nothing was in flight) before we
        spend more time on background work."""
        self._sync_connection()
        if self._bg_line_seal:
            if not self._flush_background_seal():
                return
            self._release_deferred()
            if self._pending_out:
                return
        if self._pending_out:
            self._flush_pending()
            if self._pending_out:
                return
            if self._bg_gen is None and self._deferred_out:
                self._release_deferred()
                # Releasing complete deferred lines can itself stall. Keep a
                # queued background generator from appending its JSON behind
                # those bytes until they have physically drained.
                if self._pending_out:
                    return
        while True:
            if self._bg_gen is None:
                if not self._bg_queue:
                    return
                queued = self._bg_queue.pop(0)
                self._bg_gen = queued[0]
                self._bg_mid = queued[1]
                self._bg_request_type = queued[2]
            try:
                next(self._bg_gen)
                return
            except StopIteration:
                # The generator may have queued the tail (including its final
                # newline) after a partial CDC write. Keep it marked active
                # until that tail is physically flushed; otherwise a PING/ACK
                # received in the next tick can be inserted into the open JSON
                # line (observed live at the new 192-byte buffer boundary).
                self._flush_pending()
                if self._pending_out:
                    return
                self._bg_gen = None
                self._bg_mid = None
                self._bg_request_type = None
                self._release_deferred()
                # _release_deferred may itself hit CDC backpressure. Do not
                # start serialising the next background response into the
                # pending byte queue while those complete earlier lines are
                # still stalled; wait for the next pump to drain them first.
                if self._pending_out:
                    return
                # One completed generator is the per-pump scheduling unit.
                # Draining every synchronously completing queued diagnostic
                # here let a resumed MANIFEST run all eight queued LED_DUMPs
                # in the same firmware tick (dozens of USB writes), starving
                # MIDI/switch processing even though each individual write
                # obeyed its stall budget. The next request starts next tick.
                return
            except Exception as e:
                failed_mid = self._bg_mid
                failed_type = self._bg_request_type
                self._bg_gen = None
                self._bg_mid = None
                self._bg_request_type = None
                # The failed generator may already have written a JSON prefix.
                # Terminate that damaged record before releasing queued ACKs /
                # events; otherwise the next valid object is concatenated onto
                # it and becomes malformed too.
                self._bg_line_seal = 1
                if failed_type is not None:
                    if failed_type == "GET_MANIFEST":
                        error = {
                            "type": "ERROR", "id": failed_mid,
                            "error": "manifest_failed",
                        }
                    else:
                        try:
                            detail = str(e)
                        except Exception:
                            detail = type(e).__name__
                        error = {
                            "type": "ERROR", "id": failed_mid,
                            "error": "exception", "detail": detail,
                            "of": failed_type,
                        }
                    self._send(error)
                try:
                    print("[protocol] background gen EXC err=%s" % type(e).__name__)
                except Exception:
                    # Recovery must not depend on allocating a diagnostic
                    # string while the heap is already exhausted.
                    pass
                if self._flush_background_seal():
                    self._release_deferred()
                return

    def _flush_background_seal(self):
        """Finish the delimiter that quarantines a failed streamed line.

        A normal queue entry cannot be used blindly here: the failed
        generator may already have filled the pending queue with the tail of
        its damaged record. Drain that tail first, then reserve/write the
        newline. If the host is still stalled, keep the barrier raised so no
        complete response or later background generator can cross it.
        """
        if not self._bg_line_seal:
            return True
        if self._pending_out:
            self._flush_pending()
            if self._pending_out:
                return False
            if self._bg_line_seal == 2:
                self._bg_line_seal = 0
                return True
        elif self._bg_line_seal == 2:
            # Defensive: an explicit _flush_pending() may have drained the
            # queued delimiter between pump calls.
            self._bg_line_seal = 0
            return True

        left = self._write_bytes(b"\n")
        if left:
            if self._pending_out:
                self._bg_line_seal = 2
            return False
        self._bg_line_seal = 0
        return True

    def _release_deferred(self):
        # `_deferred_out` already owns immutable complete protocol lines.
        # Do not copy every one through _queue_output here: this path is also
        # used immediately after a streamed response failed under low heap,
        # and another bytes() allocation could both raise and lose the whole
        # list (the old code cleared `_deferred_out` before attempting the
        # first copy).  Drain any older continuation first, then swap the two
        # existing list objects so releasing deferred replies allocates
        # nothing and cannot silently discard a correlated response.
        if self._pending_out:
            self._flush_pending()
            if self._pending_out:
                return False
        deferred_bytes = self._deferred_bytes
        self._pending_out, self._deferred_out = (
            self._deferred_out, self._pending_out)
        self._pending_bytes = deferred_bytes
        self._deferred_bytes = 0
        return self._flush_pending()

    def _flush_pending(self):
        """Write out every _send() response that queued while the
        background line was open (see _send()'s _bg_gen check), now that
        the line has closed (or been sealed off - see _start_background)
        and the wire is safe to write to again."""
        if not self._pending_out:
            return
        while self._pending_out:
            data = self._pending_out[0]
            left = self._write_direct(data)
            sent = len(data) - left
            self._pending_bytes -= sent
            if left:
                self._pending_out[0] = data[sent:]
                return False
            self._pending_out.pop(0)
        self._pending_bytes = 0
        return True

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
        # A full bank list can exceed the largest contiguous block left on a
        # long-running RP2040 heap.  Stream one patch summary per background
        # slice; DEVICE_INFO and ACK/EVENT lines remain ordered behind it.
        self._start_background(self._json_line_gen({
            "type": "PATCH_LIST", "id": mid,
            "patches": patches, "profile": pid or "",
        }), mid, "LIST_PATCHES")

    def _get_patch_gen(self, mid, msg):
        bank, slot = msg["bank"], msg["slot"]
        pid, use_active = self._resolve_profile(mid, msg)
        if not use_active and pid is None:
            return
        try:
            patch = self.app.patches.read(bank, slot) if use_active else config.load_patch_for(bank, slot, pid)
        except OSError:
            self._send({"type": "ERROR", "id": mid, "error": "not_found", "bank": bank, "slot": slot})
            return
        # Stream the response field-by-field (same mechanism as
        # GET_MANIFEST/GET_GLOBAL - see _stream_value) instead of one
        # json.dumps(obj) call for the whole patch. A patch with many
        # bindings/actions can be multi-KB, and a single dumps() needs ONE
        # contiguous allocation of that size - on the fragmented RP2040
        # heap this can MemoryError even with plenty of *total* free
        # memory. The previous fix here (dry-run encode + one
        # gc.collect()-and-retry, matching _push_context/emit_event, see
        # git history) still gave up completely - no ERROR, no PATCH,
        # nothing - if that retry also failed, which is a genuine
        # fragmentation problem a single collect doesn't always fix.
        # Confirmed live 2026-08-16: a real patch on the test rig
        # reproduced silent, total non-response deterministically, not
        # just delay.
        import gc
        if self.port is None or not self.port.connected:
            return
        try:
            gc.collect()
            try:
                # A growing 192-byte bytearray needed a ~232-byte contiguous
                # allocation and failed on the live fragmented RP2040 heap.
                # Reuse one fixed short-packet chunk instead; scalar leaves
                # are the only other allocations and the buffer never grows.
                chunk = bytearray(self._MANIFEST_CHUNK_SIZE)
                state = [0, 0, 0]  # bytes/yields budget, then chunk fill

                def w(data):
                    for octet in data:
                        chunk[state[2]] = octet
                        state[2] += 1
                        state[0] += 1
                        if state[2] == len(chunk):
                            self._write_bytes(chunk)
                            state[2] = 0
            except MemoryError:
                chunk = None
                state = [0, 0]

                def w(data):
                    state[0] += len(data)
                    self._write_bytes(data)
            w(b'{"type":"PATCH","id":')
            w(json.dumps(mid).encode())
            w(b',"bank":')
            w(json.dumps(bank).encode())
            w(b',"slot":')
            w(json.dumps(slot).encode())
            w(b',"profile":')
            w(json.dumps(pid or "").encode())
            w(b',"patch":')
            yield from self._stream_value(
                w, patch, gc, budget=state)
            w(b'}\n')
            if chunk is not None and state[2]:
                tail = bytes(memoryview(chunk)[:state[2]])
                self._write_bytes(tail)
        except Exception as e:
            print("[protocol] _send EXC type=PATCH err=%s" % type(e).__name__)
            raise

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
        try:
            self.app.delete_patch(bank, slot)
        except OSError:
            # Never ACK a deletion that the filesystem rejected. In
            # particular, CIRCUITPY can be read-only while USB mass storage is
            # mounted; PatchStore has preserved the RAM/dirty value intact.
            self._send({
                "type": "ERROR", "id": mid, "error": "delete_failed",
                "bank": bank, "slot": slot,
            })
            return
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
        if self._pending_out:
            if not self._queue_output(data):
                # Continuing would make the generator believe this chunk
                # reached the wire; a later newline would then bless a
                # silently truncated JSON record as complete.
                raise RuntimeError("tx_queue_full")
            return len(data)
        left = self._write_direct(data)
        if left:
            if not self._queue_output(
                    bytes(memoryview(data)[len(data) - left:])):
                raise RuntimeError("tx_queue_full")
        return left

    def _write_direct(self, data):
        """Low-level non-blocking write; unlike _write_bytes it never queues."""
        view = memoryview(data)
        stalls = 0
        while view and stalls < self._MAX_DIRECT_WRITE_STALLS:
            n = self.port.write(view)
            if not n:
                stalls += 1
                try:
                    self.app._poll_switches_mid_op()
                except Exception:
                    pass
                continue
            view = view[n:]
            stalls = 0
        return len(view)

    def _write_json_scalar(self, w, value, bounded):
        if bounded and isinstance(value, str):
            w(b'"')
            offset = 0
            while offset < len(value):
                encoded = json.dumps(value[
                    offset:offset + self._MANIFEST_STRING_CHARS]).encode()
                w(encoded[1:-1])
                offset += self._MANIFEST_STRING_CHARS
            w(b'"')
        else:
            w(json.dumps(value).encode())

    def _stream_value(self, w, v, gc, depth=0, budget=None):
        """Stream JSON with one generator frame and a depth-only stack."""
        stack = []
        value = v
        value_depth = depth
        bounded = budget is not None
        while True:
            kind = -1
            if isinstance(value, dict):
                w(b'{')
                iterator = iter(value.items())
                close = b'}'
                kind = 1
            elif isinstance(value, (list, tuple)):
                w(b'[')
                iterator = iter(value)
                close = b']'
                kind = 0
            else:
                self._write_json_scalar(w, value, bounded)

            if kind >= 0:
                try:
                    item = next(iterator)
                except StopIteration:
                    w(close)
                else:
                    stack.append((iterator, close, kind, value_depth))
                    if kind:
                        key, value = item
                        self._write_json_scalar(w, key, bounded)
                        w(b':')
                    else:
                        value = item
                    value_depth += 1
                    continue

            if bounded and budget[0] >= self._MANIFEST_YIELD_BYTES:
                budget[0] = 0
                budget[1] += 1
                if budget[1] >= self._MANIFEST_GC_YIELDS:
                    budget[1] = 0
                    gc.collect()
                yield

            while stack:
                iterator, close, kind, parent_depth = stack[-1]
                if not bounded and parent_depth < 2:
                    gc.collect()
                    yield
                try:
                    item = next(iterator)
                except StopIteration:
                    stack.pop()
                    w(close)
                    continue
                w(b',')
                if kind:
                    key, value = item
                    self._write_json_scalar(w, key, bounded)
                    w(b':')
                else:
                    value = item
                value_depth = parent_depth + 1
                break
            else:
                return

    def _get_manifest_gen(self, mid):
        """Select the zero-serialization shipped manifest when it is safe."""
        loaded = getattr(self.app.plugins, "_plugins", None)
        if isinstance(loaded, dict) and len(loaded) == len(
                self._STATIC_MANIFEST_PLUGINS):
            complete = True
            for name in self._STATIC_MANIFEST_PLUGINS:
                if name not in loaded:
                    complete = False
                    break
            if complete:
                try:
                    size = os.stat(self._MANIFEST_TAIL_PATH)[6]
                    if size >= (len(self._MANIFEST_TAIL_PREFIX) +
                                len(self._MANIFEST_TAIL_SUFFIX)):
                        return self._get_static_manifest_gen(mid)
                except OSError:
                    pass
        # Missing/custom plugin sets retain the allocation-bounded dynamic
        # implementation, so third-party modules are never hidden by a shipped
        # cache and an older installation remains backward compatible.
        # Keep its sizeable bytecode out of the normal static-manifest heap.
        from . import manifest_dynamic
        return manifest_dynamic.get_manifest_gen(
            self, mid, gc, json, messages, bytearray, print)

    def _get_static_manifest_gen(self, mid):
        """Stream the build-verified manifest tail directly from flash.

        The RP2040 otherwise spends tens of seconds executing Python JSON
        recursion and per-byte staging for a ~21 KB response.  ``readinto``
        reuses one 63-byte short-packet buffer, removing both serialization and
        per-byte Python work while preserving USB backpressure and main-loop
        fairness.
        """
        if self.port is None or not self.port.connected:
            return
        manifest_file = None
        chunk = None
        try:
            gc.collect()
            manifest_file = open(self._MANIFEST_TAIL_PATH, "rb")
            size = os.stat(self._MANIFEST_TAIL_PATH)[6]
            head = manifest_file.read(len(self._MANIFEST_TAIL_PREFIX))
            manifest_file.seek(size - len(self._MANIFEST_TAIL_SUFFIX))
            tail = manifest_file.read(len(self._MANIFEST_TAIL_SUFFIX))
            if (head != self._MANIFEST_TAIL_PREFIX or
                    tail != self._MANIFEST_TAIL_SUFFIX):
                raise ValueError("invalid static manifest framing")
            head = None
            tail = None
            manifest_file.seek(0)

            left = self._write_bytes(b'{"type":"MANIFEST","id":')
            if left:
                yield
            # IDs are normally a few bytes. Keep the same bounded string path
            # as the dynamic encoder so an adversarially long ID cannot demand
            # one contiguous allocation.
            state = [0, 0]
            yield from self._stream_value(
                self._write_bytes, mid, gc, budget=state)
            if self._pending_out:
                yield

            try:
                chunk = bytearray(self._MANIFEST_CHUNK_SIZE)
            except MemoryError:
                chunk = None
            sent_since_yield = 0
            yields_since_gc = 0
            while True:
                if chunk is None:
                    data = manifest_file.read(self._MANIFEST_CHUNK_SIZE)
                    if not data:
                        break
                    count = len(data)
                else:
                    count = manifest_file.readinto(chunk)
                    if not count:
                        break
                    if count == len(chunk):
                        data = chunk
                    else:
                        data = memoryview(chunk)[:count]
                left = self._write_bytes(data)
                data = None
                sent_since_yield += count
                if (left or
                        sent_since_yield >= self._MANIFEST_YIELD_BYTES):
                    if sent_since_yield >= self._MANIFEST_YIELD_BYTES:
                        sent_since_yield = 0
                        yields_since_gc += 1
                        if yields_since_gc >= self._MANIFEST_GC_YIELDS:
                            yields_since_gc = 0
                            gc.collect()
                    yield
        except Exception as e:
            # pump_background owns the correlated error response so every
            # streamed request has identical failure semantics and no stream
            # can accidentally emit the error twice.
            gc.collect()
            print("[protocol] _send EXC type=MANIFEST err=%s" %
                  type(e).__name__)
            raise
        finally:
            if manifest_file is not None:
                try:
                    manifest_file.close()
                except Exception:
                    pass

    def _get_context_gen(self, mid):
        """Stream the full context without one contiguous JSON allocation.

        Do not try a monolithic ``json.dumps`` first and do not accumulate a
        staging ``bytearray``.  On the live RP2040 both paths can fail after a
        few rig changes even with several KiB free, because neither the JSON
        string nor the 192-byte staging buffer can find a sufficiently large
        contiguous heap block.  A failed GET_CONTEXT is especially damaging:
        Stage deliberately clears old-rig effect state on ``patch_switched``
        and relies on this snapshot to restore unchanged blocks (for example
        CLEAN's X/FLANG remaining ON across a rig change).

        ``_stream_value`` only serialises one scalar leaf at a time.  Its tiny
        writes are coalesced into one
        reusable 64-byte buffer: on the real Captain a direct leaf-by-leaf
        snapshot took about 1.1 seconds and multiple Stage clients could queue
        enough of them to delay ``patch_switched`` by several seconds.  The
        fixed buffer keeps the largest contiguous allocation far below the
        184/232-byte allocations observed failing on the fragmented live heap.
        With that buffer available the context (a flat set of display scalar
        fields) is emitted after one initial GC, without the generic stream's
        per-field GC/yield.  Those ~20 collections were measured taking over
        5 seconds on a fragmented live heap even though no exception occurred.
        If even 64 bytes are unavailable we retain the allocation-minimal
        direct writer *and* its per-field collections as a safe fallback.

        Background wire ownership keeps subsequent partial CONTEXT pushes
        ordered behind the snapshot, so changes that arrive while it is being
        streamed still follow it authoritatively.
        """
        import gc
        if self.port is None or not self.port.connected:
            return
        gc.collect()
        try:
            chunk = bytearray(64)
            used = [0]

            def w(data):
                # Copy without slicing `data`: a bytes slice would allocate a
                # second transient chunk and defeat the bounded-heap design.
                for octet in data:
                    chunk[used[0]] = octet
                    used[0] += 1
                    if used[0] == len(chunk):
                        self._write_bytes(chunk)
                        used[0] = 0
        except MemoryError:
            chunk = None
            used = None
            w = self._write_bytes
        try:
            w(b'{"type":"CONTEXT","id":')
            w(json.dumps(mid).encode())
            w(b',"context":')
            yield from self._stream_value(
                w, getattr(self.app, "display_context", {}), gc,
                # depth >= 2 disables the generic stream's per-entry GC/yield.
                # Keep those conservative collection points on the no-buffer
                # emergency path where heap pressure is demonstrably extreme.
                depth=2 if chunk is not None else 0)
            w(b'}\n')
            if chunk is not None and used[0]:
                # CircuitPython 9.2.7 does not support deleting a bytearray
                # slice (the CPython tests do), so an in-place shrink raises
                # TypeError and loses the trailing newline.  The sole copy is
                # bounded to 63 bytes and therefore remains below the live
                # heap's observed 184-byte contiguous-allocation failure.
                tail = bytes(memoryview(chunk)[:used[0]])
                self._write_bytes(tail)
        except Exception as e:
            print("[protocol] context stream EXC err=%s" % type(e).__name__)
            raise

    def _stats(self, mid):
        expression = getattr(self.app, "expression", None)
        if (hasattr(self.app, "iter_stats_fields") and
                hasattr(expression, "stats_jacks")):
            self._start_background(
                self._stats_live_gen(mid, expression), mid, "STATS")
            return

        # Compatibility for small host-side fakes and third-party app stubs.
        # The real Captain always takes the allocation-light path above.
        payload = {"type": "STATS", "id": mid}
        payload.update(self.app.stats())
        # STATS contains nested section timings and expression readings and is
        # commonly ~0.8 KB.  Encoding it as one string can require two large
        # contiguous allocations (JSON text, then bytes) that are unavailable
        # on a fragmented RP2040 heap.  Use the same bounded background stream
        # as DEVICE_INFO/PATCH_LIST so diagnostic polling cannot disappear.
        self._start_background(self._json_line_gen(payload), mid, "STATS")

    def _stats_live_gen(self, mid, expression):
        if self.port is None or not self.port.connected:
            return
        gc.collect()
        # Coalesce the many tiny scalar fragments.  Yielding once per field
        # made a live STATS response take ~1.2 s and held CONTEXT/EVENT behind
        # its open line for that whole interval.  The normal 64-byte path has
        # no yields and therefore completes in the initial background slice.
        # If even this small allocation fails, retain the old direct writer
        # and its per-field yield/GC points as the low-heap escape path.
        try:
            chunk = bytearray(64)
            used = [0]

            def w(data):
                for octet in data:
                    chunk[used[0]] = octet
                    used[0] += 1
                    if used[0] == len(chunk):
                        self._write_bytes(chunk)
                        used[0] = 0
        except MemoryError:
            chunk = None
            used = None
            w = self._write_bytes
        w(b'{"type":"STATS","id":')
        w(json.dumps(mid).encode())
        for key, value in self.app.iter_stats_fields():
            w(b',')
            w(json.dumps(key).encode())
            w(b':')
            if isinstance(value, dict):
                # The only live nested field here is section_max_ms and its
                # values are scalars.  Finish it in this slice: yielding while
                # the main loop can add a new timing key would invalidate the
                # live dict iterator and truncate the JSON record.
                w(b'{')
                first = True
                for subkey, subvalue in value.items():
                    w(b'' if first else b',')
                    first = False
                    w(json.dumps(subkey).encode())
                    w(b':')
                    w(json.dumps(subvalue).encode())
                w(b'}')
            else:
                w(json.dumps(value).encode())
            value = None
            if chunk is None:
                gc.collect()
                yield

        w(b',"current":{"bank":')
        w(json.dumps(self.app.current_bank).encode())
        w(b',"slot":')
        w(json.dumps(self.app.current_slot).encode())
        w(b'},"expression":[')
        first = True
        for jack in expression.stats_jacks():
            w(b'' if first else b',')
            first = False
            w(b'{"jack":')
            w(json.dumps(jack.jack).encode())
            w(b',"raw":')
            w(json.dumps(jack.raw).encode())
            w(b',"armed":')
            w(json.dumps(jack.armed).encode())
            w(b',"present":')
            w(json.dumps(jack.present).encode())
            w(b',"value":')
            value = jack.value
            w(json.dumps(value if value >= 0 else 0).encode())
            w(b'}')
            jack = None
            value = None
            if chunk is None:
                gc.collect()
                yield
        w(b']}\n')
        if chunk is not None and used[0]:
            # CircuitPython 9.2.7 cannot shrink bytearray slices in place.
            # This sole tail copy is bounded to 63 bytes.
            tail = bytes(memoryview(chunk)[:used[0]])
            self._write_bytes(tail)

    # ---------- file upload (OTA) ----------

    def _put_file(self, operation, mid, msg):
        captain_ota = None
        succeeded = False
        try:
            if operation == "begin" and not self._uploads:
                display = getattr(self.app, "display", None)
                if display is not None:
                    display.suspend()
            # Loading this cold module also needs RAM. Keep import failures
            # inside cleanup so the TFT resumes even before BEGIN can open.
            import captain_ota
            succeeded = getattr(captain_ota, operation)(self, mid, msg)
        except Exception:
            self._close_uploads()
            raise
        finally:
            # Drop the local reference before removing the root module cache.
            # Root placement avoids a second reference on captain.__dict__.
            captain_ota = None
            if not succeeded or not self._uploads:
                self._release_ota()

    def _close_uploads(self):
        for path, upload in self._uploads.items():
            try:
                upload.close()
            except Exception:
                pass
            try:
                os.remove(path + ".tmp")
            except OSError:
                pass
        self._uploads.clear()
        self._upload_sizes.clear()

    def _release_ota(self):
        import sys
        sys.modules.pop("captain_ota", None)
        gc.collect()
        if not self._uploads:
            display = getattr(self.app, "display", None)
            if display is not None and display.resume():
                self.app._mark_display_dirty()

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
        color = msg.get("color")
        # Seed the new profile with the matching plugin's default layout.
        # If no plugin matches the kind (e.g. "other"), default_layout is [].
        layout = []
        try:
            layout = self.app.plugins.default_layout(kind)
        except Exception:
            pass
        try:
            config.create_profile(pid, name, kind, default_layout=layout, color=color)
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
        response = {"type": "FONT_LIST", "id": mid, "fonts": fonts}
        self._start_background(
            self._json_line_gen(response), mid, "LIST_FONTS")

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

    def _active_plugin(self):
        """Return the plugin module backing the active profile's kind, or None.
        Kept device-agnostic: we match the app's active_kind against each
        plugin's NAME and never reach into any plugin's internals here - the
        core stays plugin-neutral and this works for any future device that
        exposes the same rig-info hook. Uses the registry's private _plugins map
        (there is no public by-name getter); tolerant of its absence."""
        kind = getattr(self.app, "active_kind", "") or ""
        if not kind:
            return None
        plugins = getattr(self.app, "plugins", None)
        registry = getattr(plugins, "_plugins", None)
        if not registry:
            return None
        for module in registry.values():
            if getattr(module, "NAME", None) == kind:
                return module
        return None

    def _get_rig_info(self, mid, msg):
        """Read the active device's current rig name (and best-effort colour)
        for the editor's "Import rig name from device" flow. Pure ROUTING: the
        core knows nothing about Kemper - it just asks the active plugin for its
        `get_rig_info(app, request=...)` result if the plugin implements it.
        Errors safely (no crash) when the active profile has no such device.

        `request` (default True): also ask the device to refresh the value; the
        editor can pass False to read the cache only."""
        module = self._active_plugin()
        fn = getattr(module, "get_rig_info", None) if module is not None else None
        if fn is None:
            # No active device that can report a rig name (e.g. generic profile,
            # or a plugin without the hook). No-op-safe: report it as an error
            # the editor can show, don't raise.
            self._send({"type": "ERROR", "id": mid, "error": "no_rig_info",
                        "of": "GET_RIG_INFO"})
            return
        want_request = msg.get("request", True)
        try:
            info = fn(self.app, request=bool(want_request))
        except Exception as e:
            self._send({"type": "ERROR", "id": mid, "error": "exception",
                        "detail": str(e), "of": "GET_RIG_INFO"})
            return
        if info is None:
            self._send({"type": "ERROR", "id": mid, "error": "no_rig_info",
                        "of": "GET_RIG_INFO"})
            return
        self._send({
            "type": "RIG_INFO",
            "id": mid,
            "name":  info.get("name", ""),
            "rig":   info.get("rig"),
            "color": info.get("color"),
            "fresh": bool(info.get("fresh", False)),
        })

    def _led_dump(self, mid, msg):
        """Diagnostic: return the current values of every NeoPixel as a
        list of [r, g, b] triples, plus the firmware's view of which
        switch owns which pixel indices. Lets us see exactly what was
        written vs. what the user reports seeing physically."""
        self._start_background(self._led_dump_gen(mid), mid, "LED_DUMP")

    def _led_dump_gen(self, mid):
        """Stream LED diagnostics without retaining or encoding containers."""
        if self.port is None or not self.port.connected:
            return
        gc.collect()
        try:
            from .board import LED_INDEX_PER_SWITCH
            strip = self.app.leds.strip
            try:
                chunk = bytearray(self._MANIFEST_CHUNK_SIZE)
                used = [0]

                def w(data):
                    for octet in data:
                        chunk[used[0]] = octet
                        used[0] += 1
                        if used[0] == len(chunk):
                            self._write_bytes(chunk)
                            used[0] = 0
            except MemoryError:
                chunk = None
                used = None
                w = self._write_bytes

            scalar = self._write_json_scalar
            w(b'{"type":"LED_DUMP","id":')
            scalar(w, mid, True)
            w(b',"pixels":[')
            for i in range(len(strip)):
                if i:
                    w(b',')
                pixel = strip[i]
                w(b'[')
                scalar(w, pixel[0], True)
                w(b',')
                scalar(w, pixel[1], True)
                w(b',')
                scalar(w, pixel[2], True)
                w(b']')
                pixel = None
                if chunk is None:
                    gc.collect()
                    yield

            w(b'],"switch_indices":{')
            first = True
            for name, indices in LED_INDEX_PER_SWITCH.items():
                if not first:
                    w(b',')
                first = False
                scalar(w, name, True)
                w(b':[')
                for i in range(len(indices)):
                    if i:
                        w(b',')
                    scalar(w, indices[i], True)
                w(b']')
                if chunk is None:
                    gc.collect()
                    yield

            w(b'},"current":{"bank":')
            scalar(w, self.app.current_bank, True)
            w(b',"slot":')
            scalar(w, self.app.current_slot, True)
            w(b'}}\n')
            if chunk is not None and used[0]:
                self._write_bytes(bytes(memoryview(chunk)[:used[0]]))
        except Exception as e:
            gc.collect()
            try:
                print("[protocol] LED_DUMP stream EXC err=%s" %
                      type(e).__name__)
            except Exception:
                pass
            raise
