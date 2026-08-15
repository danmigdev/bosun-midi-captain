# Android Stage view / serial I/O silent-stall investigation

## Status: paused, ready to resume

Session 2026-08-15 fixed two reliability bugs (both committed, tested, and
validated live on hardware) and then found a third, separate one that needs
its own session. This doc is the handoff for that third one.

## What's DONE this session (committed, don't re-open)

1. **`ddbeaa3` - Fix footswitch presses lost during MIDI/protocol USB
   stalls.** Both `midi.py`'s USB-MIDI TX retry loop and `protocol.py`'s
   data-CDC `_send()` retry loop could block the main loop for tens to
   hundreds of ms (measured live: up to 449ms on a single tick) without
   polling switches, silently losing a footswitch tap that landed inside
   the stall. Fixed with a `poll_hook` interleave in both retry loops,
   wired through `Captain._poll_switches_mid_op` (queues triggers, drained
   next tick to avoid re-entrant firing). Added `max_tick_ms` /
   `slow_tick_count` / `section_max_ms` to STATS for future measurement.
   Verified: `countio` hardware edge-latching is NOT viable on this board
   (RP2040 lacks RISE_AND_FALL; 4/10 switch pins aren't on a PWM channel B
   pin). Tests: `tools/midi_tx_test.py` (new), `tools/protocol_test.py`,
   `tools/firmware_stability_test.py`.

2. **`c26c6fb` - Bypass android.media.midi for the Kemper<->Captain USB-MIDI
   bridge.** That framework has a well-documented, longstanding Android
   platform bug: a port's receive callback silently stops firing after an
   initial burst, no exception, no error - confirmed live (Kemper->Captain
   relayed exactly 3 messages then went dead for the rest of the session)
   and matches reports from other Android MIDI apps with no fix from
   Google. Replaced with a direct raw-USB transport: `UsbMidiPacketCodec.kt`
   (pure USB-MIDI 1.0 packet codec, 26 JVM unit tests) +
   `UsbMidiDevice.kt` (UsbDeviceConnection.bulkTransfer, bypasses
   android.media.midi) + rewritten `BosunMidiBridge.kt` (same JNI contract,
   unchanged toward Rust). Verified live: old bridge failed its own health
   check within ~15s every time; new one ran clean 4+ minutes through a hub
   disconnect/reconnect cycle - the exact scenario that exposed the bug.

Both are on real hardware right now: firmware v0.5.33 on the pedal, new APK
(versionCode 23) on the test Pixel 8 Pro. Full offline test battery + the
two Kotlin test suites all green.

## What's NOT done - the actual next-session task

**Symptom**: Stage view (`editor/src/components/StageView.svelte`) does not
receive live updates. Confirmed live: changed rig on both the Kemper and
the Captain directly, watched `adb logcat` the whole time, zero `CONTEXT`
push traffic and zero response traffic to the `GET_CONTEXT` pull request
Stage sends on mount. This reproduced BOTH with the MIDI bridge on and with
it explicitly turned off in the app - so it is very likely NOT related to
today's two fixes above, and NOT specific to the MIDI bridge.

**Key finding**: this is a PRE-EXISTING bug, not introduced today. The Rust
serial I/O module already has dated comments and partial fixes for the
identical symptom from **2026-08-14** (the day before this session):
`editor/src-tauri/src/serial/android.rs` around `WRITE_ONLY_STALL_THRESHOLD`
/ `is_write_only_stall()` - literally describes "GET_CONTEXT sent every 2s
for 5+ minutes straight with zero read activity, self-heal never triggered."

**Where the trail went cold**: the I/O thread (`android.rs`, the `'main:
loop` around line 342) has its own diagnostic `eprintln!()` calls for
exactly this - `[io] write ok`, `[io] stall detected`, `[io] write-only
stall detected (...)`, `[chunk] #N ...`. NONE of these appeared ANYWHERE in
`adb logcat` for the entire session, even though `[send]` lines (which are
logged by whatever enqueues a command into `outbox`, a DIFFERENT, earlier
layer - not the I/O thread itself) appeared normally. This means either:
  - the I/O thread's main loop never actually started this session (most
    likely - check how/when `connect()` spawns it, and whether the sentinel
    handshake at the top of `connect()` is silently failing before reaching
    the instrumented loop), or
  - it's stuck somewhere before any of its own eprintln calls run, in a way
    its own stall-detection can't see (worth checking `call_with_timeout`
    in `android_helpers.rs` and the sentinel-sync code just above the
    `'main: loop` for a gap in the instrumentation), or
  - (less likely, worth ruling out first since it's cheap to check) eprintln
    output from a `std::thread::spawn`'d thread isn't reaching the same
    logcat tag as the main-thread JNI dispatch's stdout/stderr capture -
    sanity check by adding one eprintln right at thread spawn, before
    anything else, and confirming it shows up.

## Suggested first steps next session

1. Re-attach to the same hardware setup (phone + Kemper + Captain via OTG
   hub; PC reaches the phone via `adb connect <ip>:<port>` over WiFi - see
   MEMORY.md, this was worked out live this session and isn't yet written
   down anywhere else. Ask the user for the current `Settings > Developer
   options > Wireless debugging` IP:port again if adb doesn't reconnect on
   its own).
2. Add a `eprintln!("[io] thread started")` as the very first line inside
   whatever spawns the I/O thread in `android.rs`'s `connect()`, rebuild
   (`tools\build-android.ps1 -Deploy -SkipFrontend`, Kotlin-only changes
   don't need `-SkipFrontend` off but a Rust change here does need Rust
   recompiled - do NOT pass `-SkipFrontend` if only testing this, but DO
   still skip if frontend/.svelte files are untouched), reconnect, and
   check whether that line ever appears. That single line resolves which
   of the three hypotheses above is right.
3. `editor/src-tauri/src/serial/android_helpers.rs` already has the right
   pattern for this file's own testing (pure functions, `#[test]` at the
   bottom, runs on host via `cargo test` without Android) - once the root
   cause is found, prefer fixing/testing at that layer if the bug is
   logic (not literally "the thread never started").
4. `python tools/run_all_tests.py` and the two Kotlin/JVM test suites
   (`.\gradlew.bat :app:testArmDebugUnitTest` from
   `editor/src-tauri/gen/android`) should stay green throughout - nothing
   from this session should need touching to fix this.
