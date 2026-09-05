"""Allocation-bounded dynamic MANIFEST fallback.

This module is intentionally imported lazily by ``captain.protocol``.  The
normal shipped-plugin path streams the prebuilt manifest tail and must not pay
the RP2040 heap cost of loading this fallback's bytecode.  Dependencies already
owned by the protocol module are passed explicitly to preserve its tested
monkey-patching and avoid additional eager imports here.
"""


def get_manifest_gen(self, mid, gc, json, messages, bytearray, print):
    """Stream a live registry manifest without allocating the full object."""
    # The full manifest (every plugin's MESSAGE_TYPES + config/recipe
    # schemas) can exceed the RP2040 heap when json.dumps'd in one
    # allocation -> MemoryError, which left the editor with no plugins
    # (only "generic"). Stream it instead: emit the JSON piece by piece,
    # serializing a single field at a time and collecting garbage between
    # pieces, so peak allocation is one field rather than the whole tree.
    #
    # A generator - see _start_background/pump_background: yields once
    # per gc.collect() point so the ~22 KB response (measured 5.25 s of
    # main-loop-blocking write time, 2026-08-16) doesn't stall every
    # other queued request for its whole duration.
    if self.port is None or not self.port.connected:
        return
    chunk = None
    state = None
    try:
        gc.collect()
        state = [0, 0]  # bytes since yield, yields since collection
        try:
            chunk = bytearray(self._MANIFEST_CHUNK_SIZE)
            used = [0]

            def w(data):
                for octet in data:
                    chunk[used[0]] = octet
                    used[0] += 1
                    state[0] += 1
                    if used[0] == len(chunk):
                        self._write_bytes(chunk)
                        used[0] = 0
        except MemoryError:
            chunk = None
            used = None

            def w(data):
                state[0] += len(data)
                self._write_bytes(data)

        w(b'{"type":"MANIFEST","id":')
        yield from self._stream_value(w, mid, gc, budget=state)
        w(b',"core_messages":')
        yield from self._stream_value(
            w, messages.CORE_MESSAGE_TYPES, gc, budget=state)
        w(b',"plugins":{')
        first_plugin = True
        loaded = getattr(self.app.plugins, "_plugins", None)
        if isinstance(loaded, dict):
            # The on-device registry exposes loaded modules. Do not call
            # iter_manifest(): it first allocates a seven-field entry dict
            # for every plugin, which is exactly the low-heap failure this
            # streaming path must avoid.
            for name, module in loaded.items():
                w(b'' if first_plugin else b',')
                first_plugin = False
                yield from self._stream_value(
                    w, name, gc, budget=state)
                w(b':{"label":')
                yield from self._stream_value(
                    w, getattr(module, "LABEL", name), gc, budget=state)
                w(b',"version":')
                yield from self._stream_value(
                    w, getattr(module, "VERSION", "0"), gc, budget=state)
                message_types = getattr(module, "MESSAGE_TYPES", None)
                factory = getattr(module, "manifest_message_types", None)
                if callable(factory):
                    message_types = factory()
                w(b',"messages":')
                if message_types:
                    yield from self._stream_value(
                        w, message_types, gc, budget=state)
                else:
                    w(b'{}')
                w(b',"default_layout":')
                layout = getattr(module, "DEFAULT_LAYOUT", ())
                yield from self._stream_value(
                    w, layout, gc, budget=state)
                w(b',"tft_fields":')
                if hasattr(module, "TFT_FIELDS"):
                    tft_fields = module.TFT_FIELDS
                    yield from self._stream_value(
                        w, tft_fields, gc, budget=state)
                else:
                    tft_fields = None
                    w(b'{}')
                w(b',"config_schema":')
                yield from self._stream_value(
                    w, getattr(module, "CONFIG_SCHEMA", None), gc,
                    budget=state)
                w(b',"recipe_schema":')
                yield from self._stream_value(
                    w, getattr(module, "RECIPE_SCHEMA", None), gc,
                    budget=state)
                w(b'}')
                module = None
                message_types = None
                factory = None
                layout = None
                tft_fields = None
        else:
            # Compatibility for host-side/test registries.
            for name, entry in self.app.plugins.iter_manifest():
                w(b'' if first_plugin else b',')
                first_plugin = False
                yield from self._stream_value(
                    w, name, gc, budget=state)
                w(b':')
                yield from self._stream_value(
                    w, entry, gc, budget=state)
                entry = None
        w(b'}}')
        if chunk is None:
            w(b'\n')
        elif used[0]:
            # JSON permits trailing whitespace. Pad the final packet and
            # put its newline in byte 64, avoiding both a tail slice and a
            # bytes(memoryview(...)) allocation on the tight heap.
            while used[0] < len(chunk) - 1:
                w(b' ')
            w(b'\n')
        else:
            self._write_bytes(b'\n')
    except Exception as e:
        # Protocol.pump_background centrally queues the correlated error after
        # sealing the damaged line. Keeping it there gives every streamed
        # request identical failure semantics and avoids duplicate responses.
        gc.collect()
        print("[protocol] _send EXC type=MANIFEST err=%s" % type(e).__name__)
        raise
