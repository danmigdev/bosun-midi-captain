# Android Stage view / serial I/O silent-stall investigation

## Follow-up (2026-08-16): GET_MANIFEST/GET_GLOBAL blocked the whole main loop

After the native serial driver and `emit_event` fixes (see
`20260815_231301_android_native_serial_driver.md`), live testing still
showed GET_PATCH responses arriving 11s-60s late, or apparently never, right
after opening Stage or switching rigs. Added `[timing]` start/elapsed prints
around every `handle()` dispatch and captured real hardware timing: a single
`GET_MANIFEST` dispatch took **5.25 s** end to end, and a `GET_PATCH` sent
94 ms later didn't even start processing until the manifest finished -
because `_get_manifest`/`_get_global`/`_stream_value` write their entire
multi-KB response inline via `_write_bytes` with **no yielding at all**, so
`handle()` (and therefore `tick_once()`, and therefore `protocol.poll()`
picking up anything else already sitting in `_rx_buf`) doesn't return until
the whole thing is sent. Every GET_MANIFEST fetch - which fires on every
Stage/editor connect - froze footswitch polling, MIDI processing, and every
other queued protocol request for its full multi-second duration.

Fixed (commit follows this doc update) by converting `_get_manifest`,
`_get_global`, and the shared `_stream_value` helper into resumable
generators (`_get_manifest_gen`/`_get_global_gen`), yielding at the same
points that were already `gc.collect()`-ing for heap safety. `handle()` now
calls `protocol._start_background()`, which runs only the first slice
inline and stores the generator; `Captain._tick_body()` (app.py) calls the
new `protocol.pump_background()` once per tick, after `poll()`/`handle()`,
advancing one slice at a time so anything else queued gets serviced first.

This surfaced a second, more serious bug during testing (caught by
`firmware_stability_test.py`'s `test_protocol_barrage`, not by hand-testing):
since the background generator's response has no trailing newline until it
fully completes, any *other* `_send()` response written while a background
line was still open landed in the middle of it, corrupting both into
unparseable garbage on the editor's line-delimited JSON parser. Fixed by
making `_send()` queue (`self._pending_out`) instead of writing directly
whenever `self._bg_gen is not None`, and flushing the queue the instant the
background line closes (`pump_background`'s `StopIteration` path) or gets
sealed off with a forced newline if a second background request replaces an
unfinished one (`_start_background`).

Verified live on the desktop rig: GET_PATCH now dispatches and answers in
under 100 ms even while a GET_MANIFEST is mid-flight (previously blocked for
the manifest's entire ~5-7 s transfer), and all three responses (MANIFEST,
PATCH, CONTEXT) arrive as clean, correctly-parsed JSON with no corruption.
`tools/protocol_test.py` (41/41) and `tools/firmware_stability_test.py`
(17/17, after updating two tests to drain the background generator the same
way `_tick_body` does before reading back responses) both green. Firmware
bumped to 0.5.36.

Residual, accepted tradeoff: if GET_PATCH (or any other request) is sent
while GET_MANIFEST/GET_GLOBAL is in flight, its *answer* is still deferred
until the background response finishes - but the firmware itself (switches,
MIDI, Kemper comms) stays fully responsive throughout, which is the part
that mattered. GET_MANIFEST only fires once per connection in normal use, so
this should be rare outside of reconnect churn.

## Status: RESOLVED (2026-08-15, later same day)

Three separate, real bugs were chained together to produce the "Stage
view never updates" symptom. All three found and fixed live on hardware
in the resume session:

1. **`de1919f`** (`editor/src-tauri/src/serial/android.rs`) - the
   stall-recovery path (`close`/`open`/`available_ports`, sentinel
   PING/ACK) called the serial plugin directly with no timeout, unlike
   the main loop's write/read. When the Android USB stack wedged one of
   those calls during recovery, the whole I/O thread froze **forever** -
   confirmed live, twice, via a `[io] thread started` diagnostic added
   per this doc's own suggested next step. This was the literal
   permanent-freeze case. Wrapped every plugin call in the recovery and
   sentinel-sync phases in `call_with_timeout`, matching the main loop.
   Also added a lightweight idle keepalive PING (fired by the I/O thread
   itself once idle >6s) because StageView had separately stopped
   polling `GET_CONTEXT` on a timer earlier the same day, and the naive
   15s wall-clock stall check couldn't tell legitimate protocol
   idleness apart from a dead link - was forcing a disruptive
   reconnect/USB-re-enumeration cycle every ~15-30s.
2. **`656ffd3`** (`firmware/lib/captain/app.py`) - `_push_context()`
   committed its "delivered" fingerprint unconditionally, even when
   `protocol._send()` had silently no-op'd because the host hadn't
   opened the data port yet (the normal state for the first several
   ticks after every boot, and every self-heal reboots the RP2040 via
   DTR). Once that happened the very first push after any reboot got
   marked "sent" and Stage received nothing further until an unrelated
   field changed too.
3. Also found while verifying #2: `protocol._send()` never raises (it
   swallows every exception including `MemoryError` and only prints to
   the REPL), so a CONTEXT push failing from heap fragmentation right
   after a big Kemper rig-change SYSEX burst was just as silently
   "delivered" as far as `_push_context` knew. Fixed by dry-running the
   JSON encode first (with its own gc.collect-and-retry) and only
   committing the fingerprint on success.

Verified live on the desktop rig (Captain on COM4, Kemper direct via
`tools/midi_bridge.py`) across a dozen+ rig changes, including
immediately after fresh reboots and back-to-back changes during heavy
Kemper SYSEX bursts - CONTEXT now arrives within ~1s every time. Full
`python tools/run_all_tests.py` battery and the Rust
`android_helpers` unit tests both green.

One test-tooling lesson from this session, in case it recurs: a
throwaway Python script that redirects stdout to a file/pipe (not a
TTY) gets FULLY buffered by default - `print()` without `flush=True` (or
`sys.stdout.reconfigure(line_buffering=True)`) can sit invisibly in the
process's internal buffer indefinitely. Cost a significant chunk of this
session chasing a "CONTEXT never arrives" ghost that was actually just
unflushed output in the diagnostic watcher script itself.

## Status: paused, ready to resume (stale - see RESOLVED above)

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
