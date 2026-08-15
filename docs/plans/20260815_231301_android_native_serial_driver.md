# Custom raw-USB CDC-ACM serial driver for Android (bypass tauri-plugin-serialplugin)

## Why

After fixing the three bugs in `docs/plans/20260815_190535_android_stage_serial_stall.md`
(all verified, committed as `de1919f` and `656ffd3`), live phone testing still shows
occasional genuine "io read hung: timed out after 8s" / "close hung: timed out after 8s"
events - roughly every 60-90s in one test window - each costing a ~10-16s visible
stall+recovery cycle. The app now *survives* these (self-heals instead of freezing
forever), but the frequency makes Stage feel "not usable" per live user feedback.

Root cause: `tauri-plugin-serialplugin`'s Android backend (`android-usb-serial` crate,
via `nusb`'s `linux_usbfs`-style ioctl path against a raw fd) explicitly sets a
per-call timeout (`nusb_transport.rs`: `set_read_timeout`/`transfer_blocking`), but
Android's own USB host stack does not reliably honor it in practice - matching a
well-documented, decade-old Android platform bug (`mik3y/usb-serial-for-android`
issue #159: `UsbDeviceConnection.requestWait()`/async USB requests can block
indefinitely with no OS-level timeout on API 17+).

This exact class of problem was already solved once tonight for the *MIDI* side:
`android.media.midi` had its own silent-failure bug, fixed by bypassing it entirely
with a direct `UsbDeviceConnection.bulkTransfer()` implementation
(`UsbMidiDevice.kt`/`UsbMidiPacketCodec.kt`/`BosunMidiBridge.kt`, commit `c26c6fb`).
`bulkTransfer()` is Android's official *synchronous* transfer API with real SDK-level
timeout enforcement - a different, better-trodden code path than whatever
`nusb`/`android-usb-serial` uses under the hood. Same fix, same reasoning, applied to
the data-CDC connection instead of the MIDI relay.

## Plan

1. **`BosunSerialDevice.kt`** (new) - raw CDC-ACM class, one instance = the data CDC
   interface pair (Communications + Data, per USB CDC-ACM IAD grouping). Finds the
   right interface pair by picking the CDC_DATA-class interface with the *highest*
   interface number (boot.py's `usb_cdc.enable(console=True, data=True)` enumerates
   console first at lower interface numbers, data second at higher ones - matches the
   existing `sort_ports_desc` heuristic in `android_helpers.rs`). Exposes:
   `open()/close()/read(buf, timeoutMs)/write(data, timeoutMs)/setDtr(on)`, all via
   `UsbDeviceConnection.bulkTransfer()` (data) and `controlTransfer()` (DTR via CDC's
   `SET_CONTROL_LINE_STATE`, 0x22).
2. **`BosunSerialBridge.kt`** (new) - JNI-facing singleton `object`, mirrors
   `BosunMidiBridge.kt`'s shape: `listPorts/open/close/read/write`, all `@JvmStatic`,
   reusing the same device-matching/permission patterns already proven there.
3. **`serial_android_native.rs`** (new, Rust) - JNI front-end mirroring
   `midi_android.rs`'s `with_jni` pattern, exposing Rust functions with signatures
   matching what `android.rs` already calls from `tauri_plugin_serialplugin::commands`
   (`open`, `close`, `write`, `read`, `available_ports`) so the diff in `android.rs`
   itself stays small - swap the import, drop the now-unused `SpState` plumbing.
4. Wire `android.rs` to call the new module instead. Keep every other line - the
   single I/O thread, `call_with_timeout` wrapping, sentinel sync, stall detection,
   idle keepalive, reconnect-on-timeout - unchanged. This is a transport swap, not an
   architecture change.
5. Build, deploy, and stress-test on the real phone rig exactly like the sessions
   above: bridge Kemper<->Captain, rapid rig changes, extended idle periods, watch
   logcat for any `[io] fatal read error`/`close hung` - compare frequency against
   tonight's baseline (roughly 1 per 60-90s) before declaring this reliable.

## Status: RESOLVED, 2026-08-16 (commit `07d83c1`)

Built and verified live on the phone rig. One follow-up bug found during
verification and fixed separately (commit `4390369`): `protocol.
emit_event()` had the same unprotected-`_send()` vulnerability as
`_push_context()` (fixed in `656ffd3`) - a `patch_switched` EVENT lost to
heap fragmentation right after a big Kemper SYSEX burst explained the
"Stage shows the wrong patch's switch labels" reports, separate from the
transport work in this doc. Verified live: 9 rapid switch presses, full
event chain every time, no gaps.
