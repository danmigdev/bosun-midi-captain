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
after the native Screen Save test: 4.078 to 4.453 seconds, 150 correlated busy
responses, no duplicates, timeouts or link failures. Free heap was 3,880 bytes;
maximum tick was 2,495 ms, below the eight-second watchdog timeout.

Native Screen Save passed the changed-color operation and restoration,
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

The native desktop selector check confirmed all core and Kemper commands,
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

## Remaining issue at the native-C branch checkpoint

The final desktop can reopen its TCP connection after a hub restart, but
the complete bootstrap still intermittently times out on GET_GLOBAL under
concurrent Stage traffic. Streaming LIST_PROFILES removed its contiguous
catalog encoding, but did not eliminate the broader heap pressure. This
hardware case is still open; the migration branch must include it in its
acceptance tests. The C/C++ migration was explicitly requested after these
measurements. No experimental native firmware has been installed.
