# Backlog drain on reconnect + firmware hang fixes

Date: 2026-06-15 10:08

Two related issues fixed in this pass.

## Issue 1 (editor): stale responses leak across editor sessions

When a Reconnect happens before the firmware's USB CDC TX buffer has
fully drained, the new editor session sees responses to commands sent
by the **previous** session - with stale id values, but the right
`type` - and they overwrite freshly-fetched state. Visible symptom:
"after reconnect the editor shows a MANIFEST/GLOBAL/PATCH that doesn't
match what I just asked for."

### Fix

`connect()` now writes a sentinel PING with a per-process id like
`__sync_<pid>_<ms>` and synchronously reads from the port until it
sees that exact id in the response stream - only then is the reader
thread spawned. Everything received before the sentinel ACK is
backlog and gets discarded.

- Hard cap: 10s. After that, we move on (avoids blocking the UI on
  a hung firmware - the editor's per-command timeouts will fire).
- After matching, 50ms tail drain catches anything written next to
  the ACK in the same TX burst.

Implementation: `editor/src-tauri/src/serial.rs::connect()` block
marked "Sentinel sync".

### Verification

```javascript
// Session 1: connect, fire 6 commands, disconnect mid-response.
await m.autoConnect();
await m.cmd.getDeviceInfo(); /* ... 5 more ... */
await m.disconnect();

// Session 2: reconnect cleanly.
await m.autoConnect();                  // ~2.3s - probe + sentinel drain
window.__rawPayloads.length = 0;
await m.cmd.getDeviceInfo(); /* ... 5 more ... */
// 6 valid responses arrive with sequential fresh ids. No stale leaks.
```

Observed: 7 valid responses, ids 8–14 (sequential), MANIFEST 4291 B,
GLOBAL 687 B, PATCH 2361 B - all intact, no session-1 backlog.

## Issue 2 (firmware): hangs after editor disconnect

The firmware became unresponsive on the next reconnect after some
disconnect sequences. Symptom: pyserial PING also timed out, so it
was firmware-side, not editor-side.

### Two root causes found

**a) `poll()` skipped the rx buffer when `in_waiting == 0`.**
When the editor wrote `\n{json}\n` (e.g. our sentinel PING with its
leading newline), the firmware's `port.read(in_waiting)` slurped
everything into `_rx_buf`. `poll()` processed the empty line (bad_json
ERROR sent), returned. **On the next iteration `in_waiting` was 0,
so `poll()` returned None immediately** - leaving the buffered JSON
line stuck in `_rx_buf` forever, until more bytes happened to arrive.
The sentinel was never ACK'd, the editor sat for the full 15s cap.

Fix in `firmware/lib/captain/protocol.py::poll()`: only **read** new
bytes when `in_waiting > 0`; **always** check `_rx_buf` for a complete
line afterward.

**b) `_send` would crash the main loop on edge conditions.**
`self.port.connected` attribute access could raise on some CP states,
and `self.port.write` could raise when the host had no handle open.
Neither was wrapped - the exception propagated to `app.run()` and
killed the main loop. After that, the firmware looked alive on USB
(CDC still enumerated) but nothing was polling, so pyserial PINGs
timed out too.

Fix: `_send` is now fully defensive - single try/except around the
connected-check, json encode, and write. Also `usb_cdc.data.write_timeout`
is capped at 200 ms in `Protocol.__init__()` so a wedged host can't
block a write indefinitely.

### Verification

Same flow as Issue 1's verification. The firmware now stays
responsive across many connect/disconnect cycles without REPL or
physical resets.

## What's not done

- Investigate _why_ the firmware was wedging on certain disconnect
  sequences in the first place. With the fixes above it's robust
  in the cases I could reproduce, but if it hangs again, look at:
  - Did `usb_cdc.data.connected` return False mid-write and we hit
    the new `if not self.port.connected: return` branch correctly?
  - Are there other paths to `port.write` outside `_send`?
- The Tauri editor's diagnostic listener `serial-trace` is no longer
  emitted by Rust; the JS side already stopped listening but the
  `__serialTrace` window global is gone. Anything in dev tooling
  expecting it should switch to `__rawPayloads`.
