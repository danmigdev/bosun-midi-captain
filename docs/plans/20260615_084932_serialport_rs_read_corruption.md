# Tauri editor: multi-KB responses silently corrupted (root cause = serialport-rs) - RESOLVED

Date: 2026-06-15 08:49 (diagnosis), resolved same day after crate swap to serial2 0.2.

## Resolution

Swapped `serialport = "4.3"` → `serial2 = "0.2"` in
`editor/src-tauri/Cargo.toml`. Verified end-to-end: 4291-byte MANIFEST
and 686-byte GLOBAL arrive with `valid: true` on the JS side, repeated
across multiple connect/disconnect cycles. No further corruption.

The architectural changes added during diagnosis (single-handle pattern,
doorbell + `drain_inbox` drain) survived the swap cleanly. With serial2
the single-handle pattern is even simpler - `serial2::SerialPort`
impls `Read + Write` for `&SerialPort`, so the reader thread and
`send_command` share an `Arc<SerialPort>` with no Mutex wrapping.

Diagnostic test commands (`debug_emit_synthetic`, `debug_emit_from_thread`,
`debug_emit_payload`) were removed from serial.rs/main.rs after the
swap. The `serial-trace` event has no Rust emitter anymore; the JS
listener for it in App.svelte is a no-op leftover and can be cleaned
up next time something is edited there.

---

Original diagnostic notes follow.

## Symptom

When the editor connects, the firmware's MANIFEST (~2 KB) and GLOBAL
(~600 B) responses arrive but JSON.parse fails. Small responses
(DEVICE_INFO, PATCH_LIST, DIRTY, MIDI_LEARN) arrive cleanly. Same
firmware behaves perfectly when read by pyserial (`tools/stress.py smoke`,
`tools/raw_manifest.py`) or by PowerShell `System.IO.Ports.SerialPort`.

## Root cause (definitive)

`serialport-rs 4.3` on Windows returns scrambled bytes from `read()`
when a multi-KB response is queued in the OS COM buffer. Diagnostic
trace from inside the reader thread, sampling byte 480 of a 1024-byte
read:

```
__TRACE__ read n=1024 accum_before=1 sample_at_480_len_64=[int", "max": 16, "label": "Chan 127, "lab]
```

The substring `"label": "Chan 127, "lab` mixes content from two
distinct offsets of the source MANIFEST (`"label": "Channel"...` and
`"max": 127, "label"...`). The bytes are corrupt **before** they reach
Tauri's IPC layer, App.svelte, or any JSON parser. Every fix attempt
beyond `serialport-rs` saw the same corruption.

The corruption is deterministic: same input bytes from firmware produce
the same scramble pattern. Position of first invalid byte stays around
321-326 of the decoded JSON regardless of payload size.

## What is NOT the cause (ruled out this session)

- **Tauri 2.11 / WebView2 IPC** - `debug_emit_synthetic` and
  `debug_emit_from_thread` emit synthetic 100–3000 byte strings (incl.
  back-to-back, including from spawned threads, including with
  interleaved `serial-trace` companions) and ALL arrive byte-for-byte
  intact at the JS listener.
- **Tauri `emit` vs Tauri `invoke`** - switching the reader to push
  lines into a `Mutex<VecDeque<String>>` and have the frontend pull
  them via `invoke("drain_inbox")` (doorbell + fetch) does not fix it;
  same corruption pattern.
- **Multi-emit interleaving** - removing the `serial-trace` companions
  (read + LINE) does not fix it.
- **Stale chunk bytes** - zeroing the read buffer between reads does
  not fix it.
- **Over-read past available bytes** - `port.bytes_to_read()` then
  read only that many does not fix it. serialport-rs corrupts even when
  asked for exactly the queued count.
- **Two-handle race** - `writer.try_clone()` for the reader thread vs
  the writer held by `send_command` was the strongest theory. Replaced
  with a single `Arc<Mutex<Box<dyn SerialPort + Send>>>`. Same corruption.
- **DTR / RTS not asserted** - explicitly setting both true after
  open() does not fix it.

## Workaround the next session needs to ship

Swap `serialport = "4.3"` for **`serial2 = "0.2"`** (or `tokio-serial`).
`serial2-rs` is the modern Rust serial crate with a clean
`CreateFile`/`ReadFile`-based Windows backend and is known to handle
multi-KB CDC streams correctly. Architecture-level changes already in
place will survive the swap:

- Single-handle pattern in `connect()` (port held by
  `Arc<Mutex<Box<dyn ...>>>`, both reader thread and `send_command` go
  through it)
- Doorbell + drain pattern (`firmware-data-ready` event + `drain_inbox`
  invoke); this is the better architecture regardless of crate choice
  and should stay
- Frontend listener API in `editor/src/lib/protocol.ts`:
  `onFirmwareMessage` and `onFirmwareRawLine`, both fed by
  `_drainOnce()` after the doorbell. Already wired through App.svelte.

## State of the tree at this checkpoint

- `editor/src-tauri/src/serial.rs`: instrumented reader with the
  diagnostic `__TRACE__` line (only fires when `n > 256`). Single-handle
  port, DTR/RTS asserted, `bytes_to_read()`+exact-read pattern. Doorbell
  + `drain_inbox` command in place. Three diagnostic commands
  (`debug_emit_synthetic`, `debug_emit_from_thread`,
  `debug_emit_payload`) registered.
- `editor/src/lib/protocol.ts`: rewritten to use the drain pattern.
  `_ensureDoorbell()`, `_drainOnce()`, `_firmwareSubscribers`,
  `_firmwareRawSubscribers`. `onFirmwareMessage` + `onFirmwareRawLine`.
- `editor/src/App.svelte`: raw payload listener (`window.__rawPayloads`)
  now feeds off `onFirmwareRawLine`. Exposes `window.__invoke`. Used by
  `tools/ui_debug.py` for CDP-driven testing.

The diagnostic plumbing should stay in place until the crate swap is
verified - it is what proved this was a serialport-rs bug. After the
swap and a clean MANIFEST round-trip, remove `__TRACE__` and the three
`debug_emit_*` commands.

## Reproduction one-liner (after swap, to verify)

```powershell
$env:Path = "$env:USERPROFILE\.cargo\bin;$env:Path"
$env:WEBVIEW2_ADDITIONAL_BROWSER_ARGUMENTS = "--remote-debugging-port=9222 --remote-allow-origins=*"
cd C:\Users\danmigdev\Desktop\midi_captain\editor
npm run tauri dev    # leave running
```

In a second shell:
```bash
python tools/ui_debug.py eval "(async () => {
  const m = await import('/src/lib/protocol.ts');
  try { await m.disconnect(); } catch (e) {}
  await new Promise(r => setTimeout(r, 800));
  await m.autoConnect();
  await new Promise(r => setTimeout(r, 800));
  window.__rawPayloads.length = 0;
  await m.cmd.getManifest();
  await m.cmd.getGlobal();
  await new Promise(r => setTimeout(r, 2500));
  return Array.from(window.__rawPayloads).map(p => ({len: p.len, valid: p.valid}));
})()"
```

Expected after swap: every entry has `valid: true`.
