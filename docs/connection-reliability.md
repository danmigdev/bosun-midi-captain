# Connection reliability validation, 2026-09-05

The 0.6.4 follow-up covers the desktop TCP route through the Raspberry Pi 3
at 192.168.1.91 to a MIDI Captain running CircuitPython 9.2.7. The active
profile is Kemper Player, CLEAN at bank 1 / slot 2, with six stored patches.

## Reproduced faults

- Firmware TX committed USB writes before allocating their remaining suffix.
  An allocation failure could replay a prefix, corrupting a JSON response.
  Both direct and queued writes now retain the queue head's committed offset.
  Mutable buffers are retained safely across a stall, and nested events do
  not interleave with a streamed response. Injected failures previously
  produced a duplicated ACK prefix; both reproductions now pass.
- The hub checked its write-only watchdog before reading the response to the
  threshold write. It now drains that response first. A silent transport
  still fails; a transport answering that exact write stays connected.
- The Captain used the wrong namespace for `WatchDogMode`, silently leaving
  its watchdog disabled. The watchdog now uses the CircuitPython `watchdog`
  module, is armed after boot and fed after completed ticks. Raw-REPL recovery
  also feeds it during each bounded verification read.
- Screen Save could fail with `rx_oom` after opening the patch editor. On a
  spooled JSON parse allocation failure, the parser now releases the cached
  TFT frame and retries parsing once from the beginning. A local `finally`
  resumes rendering; the frame is rebuilt after save/apply. This adds no
  persistent protocol attribute or helper method, preserves OTA ownership
  of an already suspended screen, and never retries the write itself.
- Desktop TCP loss returned to Connect without attempting recovery. An
  established network session now retries its saved endpoint with backoff,
  then reloads the Captain state before mounting live pages. Automatic
  attempts stop after a bounded retry window; explicit Disconnect, profile
  operations, duplicate events and app teardown do not start competing loops.
- When Stage clients reconnected together, two immediate `background_busy`
  responses exhausted the desktop bootstrap's old single retry. Read-only
  bootstrap commands now back off at 250/500/1000 ms within a total two-timeout
  budget. Timeout errors still have one retry, permanent errors propagate,
  and no writes are replayed.
- `LIST_PROFILES` still encoded the entire catalog at once. The console
  confirmed `PROFILE_LIST` allocation failures and the desktop timed out
  during bootstrap. It now uses the existing streamed JSON response path;
  the hub includes it in its bounded bulk admission class. Tests cover a
  catalog over 4 KB, partial writes, concurrent PING and correlated failures.

## Verified checkpoints

The offset TX build passed ten read-only backpressure rounds: 330 uniquely
identified MANIFEST/LED_DUMP requests, with no duplicate replies, timeouts or
link failures. The 300 excess queued requests received correlated busy
responses as designed. Each round finished with a successful recovery PING;
duration was 5.750 to 7.531 seconds. Free heap at the end was 3,992 bytes and
the maximum recorded firmware tick was 896 ms. The USB-MIDI dropped-byte
counter was 24; it is separate from the JSON/CDC response validation.

The final compact parser build passed another five rounds (165 requests)
after the Windows Screen Save test: 4.078 to 4.453 seconds, 150 correlated busy
responses, no duplicates, timeouts or link failures. Free heap was 3,880 bytes;
maximum tick was 2,495 ms, below the eight-second watchdog timeout.

Windows Screen Save passed the changed-color operation and restoration,
including asynchronous GET_GLOBAL readback, in 10.196 and 15.419 seconds.
The final complete configuration equals the fresh initial snapshot. Stage
received the current context and displayed CLEAN / BANK 1 / RIG 2 / VOL.
Console capture still observed small transient allocation errors during
concurrent bootstrap traffic; this validation does not claim to eliminate
every low-memory event. The tested writes and subsequent read-only load
completed without resetting the Captain.

The hub deployment verified all seven installed modules by SHA-256, passed
the protocol health gate and reported zero service restarts. The Captain
accepted the version, protocol and application update through normal OTA,
rebooted and completed the runtime bootstrap at the original bank and slot.

The Windows desktop selector check confirmed all core and Kemper commands,
with no foreign plugin commands, in two existing switch action selectors.
The saved configuration was unchanged. Component tests cover Kemper, Ampero,
Headrush, late profile metadata, ambiguous metadata, saved foreign commands,
patch macros and expression commands. Existing foreign messages remain
readable and cannot be selected as new commands.

Local checks: 569 frontend tests, Svelte checking with zero errors (28 existing
CSS warnings), 164 hub tests, and the complete 26-suite firmware battery on
the offset TX checkpoint. The pinned CircuitPython compiler reproduced all
20 checked-in bytecode files. The final protocol has 164 protocol, 52 stability
and 12 soak checks passing. Portable folder and ZIP verification checked
all 110 firmware resources by inventory and hash.

Linux CI required pyserial 3.5 for the firmware-tool tests, an executable
Gradle wrapper and explicit successful exits after the PowerShell deployment
tests' expected failing subprocesses. The four firmware-tool suites also
passed under Ubuntu/Python 3.12 (53 checks).
Android CI also generates its ignored Gradle/Kotlin bindings through a Cargo
Android check before Gradle configuration. A clean archive passed all 186
Gradle tasks, 39 Bosun native tests and application/library lint. All five
CI jobs passed on `f179b1c`.

These are bounded regression checks, not an endurance certification. The Pi
reported historical power/throttling flags (`0x70000`), with no current flag
set during this validation.

## CircuitPython plugin memory correction

At commit `46aedb5`, desktop TCP recovery still intermittently timed out on
GET_GLOBAL with concurrent Stage traffic. Even an isolated GET_GLOBAL took
11.812 seconds. Streaming LIST_PROFILES removed its contiguous encoding,
but all five plugins still allocated their complete editor message schemas
at boot, although normal MANIFEST responses already stream a generated file.

Commit `e0a2276` changes the bundled plugins to the existing
`MESSAGE_TYPE_NAMES` / `manifest_message_types()` contract. Runtime dispatch
registers the names without constructing the schema dictionaries. The host
manifest builder and dynamic fallback still obtain the same schemas when
needed. The manifest artifact is byte-identical (21,010 bytes, SHA-256
`161383e9` prefix); commands, parameter defaults, MIDI behavior, layouts and
saved configuration remain compatible. Regression tests reject schema
allocation during registration and check that the factories do not retain
their returned dictionaries.

After deploying only the five plugin bytecode files through normal OTA,
free heap increased from 4,232 to 7,640 bytes and the sampled current tick
fell from 269 to 36 ms. The runtime boot check found the original CLEAN
bank 1 / slot 2 and all six patches. These are measured samples, not a
guarantee that every subsequent tick or heap sample has the same value.

With two Stage clients and the Windows desktop connected, four hub restart
checks passed full desktop recovery, including configuration reload, in
15.656, 15.422, 14.078 and 14.578 seconds. Each final configuration matched
its fresh baseline. The captured console contained no allocation errors.
Screen Layout Save after opening the patch editor passed the changed-color
write and restoration in 5.814 and 6.111 seconds, including readback. The
complete configuration matched the baseline after restoration.

Ten further backpressure rounds passed 330 uniquely identified requests,
including 300 correlated busy responses and a successful recovery PING per
round. There were no duplicate replies, timeouts or link failures. Rounds
took 1.219 to 2.016 seconds (median 1.320); final free heap was 7,848 bytes.
The recorded maximum tick was 1,853 ms. The USB-MIDI TX dropped counter was
33 after the stress run, so these CDC checks do not establish zero MIDI
loss. The newly packaged desktop also completed bootstrap during this load.

Two complete CLEAN/CRUNCH navigation cycles reported WAH for CRUNCH with
block A on, and VOL for CLEAN with block A off. The context settled in
1.297 to 2.672 seconds. The original CLEAN bank 1 / slot 2 was restored.
An additional normal navigation cycle left the USB-MIDI TX dropped counter
unchanged (delta zero), separating ordinary navigation from the stress run.
Screen Save from the packaged `cp-stable` executable then passed a further
changed-color write (6.039 seconds) and restoration (5.802 seconds), with
the entire configuration matching the baseline and Stage showing the
saved title, bank, rig and expression colors.

A normal Captain reboot returned its ACK and completed bootstrap with a
matching GET_GLOBAL in 13.281 seconds. CLEAN bank 1 / slot 2, all six
patches and the full configuration survived. The post-reboot sample had
7,720 bytes free, a maximum recorded tick of 875 ms and zero USB-MIDI TX
drops since boot.

Both Windows and Linux passed the complete 26-suite firmware battery. An
independent review confirmed identical schemas for all five plugins and
passed all 36 plugin and 164 Kemper checks. Hub Linux passed all 164 tests;
the two Windows bind failures were caused by another local application
owning TCP port 9899, also reproduced with a standalone socket bind. No
user process was stopped to free that port. All five CI jobs on the plugin
correction `e0a2276` passed, including Android native tests and lint:
[CI run 33965353475](https://github.com/danmigdev/bosun-midi-captain/actions/runs/33965353475).

The initial schema-only canonical and `cp-stable` packages both passed inventory and
SHA-256 verification of all 110 firmware resources, with resource digest
`51f1b5a53a1b1bcd2285bc1c6c1eed0794f7bd0c5a13114adf0aac3d5568302c`.
The native C port is suspended in a separate worktree at the user's request.
No experimental native firmware has been installed.

## USB-MIDI first attempt correction

The longer hardware read test exposed further increments in the USB-MIDI
TX dropped-byte counter while CDC, configuration and Stage remained stable.
That ten-minute run completed 93 read requests and 31 desktop checks in
603.109 seconds, with no reset or connection failure. Free heap stayed
between 7,720 and 7,752 bytes and returned to its initial value; GET_GLOBAL
took at most 2.390 seconds. MIDI RX advanced by 5,253 messages, while the
USB-MIDI TX dropped-byte counter advanced by 50. These measurements separate
the resolved CDC read failure from the remaining MIDI timing defect.
Inspection found a separate reproducible timer defect: `_tx_usb` calculated
its deadline and checked it before attempting any write. A pause of 10 ms
or longer between the two clock samples could therefore discard an entire
message without ever trying an available endpoint. Controlled-clock tests
reproduced this with a complete 13-byte Kemper beacon and pauses of 10, 12
and 40 ms. Garbage collection is a possible source of such a pause on the
device; these tests do not establish the source of each hardware counter
increment.

Commit `039f5bc` guarantees one initial attempt, retaining the original
10 ms retry budget for a full endpoint. Four added regressions cover the
initial pause, bounded retries, short writes without duplicated bytes, and
counting only the unsent remainder. All 11 MIDI TX checks and the complete
26-suite firmware battery pass. The pinned compiler reproduced all 20
bytecode files; the updated `midi.mpy` is 2,202 bytes, SHA-256
`6c47a3913693ef7ae81ca2dc1377c409e57a50a07cccca01dff78ad0b97e6cf6`.

The final canonical and `cp-stable` portable folders and ZIPs passed verification of all
110 firmware resources, with resource digest
`066eeba87ce25dde275240a219dbe1b48a54f66353be782261a08198eec02775`.

Normal OTA transferred the final MIDI module successfully and rebooted the
Captain with an ACKed PING. The deployment's subsequent runtime gate hit an
explicit `background_busy` on GET_DEVICE_INFO as the two Stage clients
reconnected. A read-only follow-up confirmed the complete configuration and
healthy runtime without reuploading or resetting again. Commit `bb6bf70`
fixes this separate host-tool false failure: only the four bootstrap reads
retry an explicit busy refusal, with distinct IDs, 100 ms backoff and the
original overall deadline. Eight local TCP cases pass, including stale
reply rejection, a persistent-busy deadline and immediate permanent errors.

Using the final canonical Windows executable, Screen Save after opening
the editor passed both the changed-color write (6.063 seconds) and complete
restoration (6.289 seconds). Automatic recovery after a hub restart passed
in 16.453 seconds. CRUNCH reported WAH with block A on in 2.234 seconds;
CLEAN reported VOL on return in 2.797 seconds. The complete configuration
matched the original snapshot, and normal navigation added zero USB-MIDI
TX drops. All five CI jobs on the final firmware source commit `039f5bc`
passed: [CI run 33966120110](https://github.com/danmigdev/bosun-midi-captain/actions/runs/33966120110).

The Captain deployment regressions are now part of CI alongside the hub
and Stage deployment checks. All five jobs on `0e21947` passed, including
the new Captain test under Linux/PowerShell and Android tests/lint:
[CI run 33966771612](https://github.com/danmigdev/bosun-midi-captain/actions/runs/33966771612).

## Final hardware acceptance

The final firmware completed another uninterrupted 603.078-second run
after Screen Save, hub recovery and rig navigation, with two Stage clients,
the packaged Windows desktop and a separate read-only probe connected.
All 93 reads and 31 desktop checks passed. There were no resets,
disconnections or configuration differences; the console capture contained
only the terminal title, with no errors. Free heap ranged from 7,176 to
7,208 bytes and ended at its initial
7,176 bytes. GET_GLOBAL took at most 2.406 seconds. MIDI RX advanced by
5,179 messages and the loop by 21,767 iterations. The USB-MIDI TX dropped
counter stayed at zero throughout, including after the preceding normal
navigation checks.

The Captain remains on CLEAN bank 1 / slot 2 with all six patches, and the
final canonical desktop remains connected. No REPL session or extra reset
was used after this acceptance run. Remote OTA confirmed the MIDI file's
2,202-byte size and successful completion; the SHA-256 above identifies
the local compiled/package artifact, not a remote byte-for-byte readback.

Delivery artifact: `dist/Bosun-0.6.4-portable-x64.zip`, SHA-256
`e1c50cf8780bde990b75c581c1dcc2787fdd5a6fb5a45d693b89767f0e8ff071`.
Detailed local evidence is in `dist/cp-final-soak-result.json`, its sample
and console logs, `dist/cp-final-native-save.log`,
`dist/cp-final-native-reconnect.log` and `dist/cp-final-navigation.json`.
