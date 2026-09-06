# Native Captain hardware evidence, 2026-09-05/06

**Native firmware remains experimental; manual hardware acceptance is still pending.**
Candidate 08 with the Pi's DWC2 host driver passed two 600-second
production-hub runs and 600 concurrent bank transitions in total, including
a repeat after Pi reboot and independent USB integrity checks. Browser Stage,
configuration persistence through two Captain reboots and final configuration
comparison also passed. DWC2 remains persisted on the Pi. These finite checks
do not establish unlimited reliability or replace physical footswitch,
pedal and display validation. Earlier failures with the legacy host driver
and their diagnostic limits are retained below.

The tested candidate-06 UF2 has SHA-256 beginning `425ece31`. Installation
was verified after the Pi became reachable again on September 6. The cause
of the preceding loss of SSH, HTTP and neighbor reachability remains undetermined.

## Measured USB performance

The same Captain, Raspberry Pi 3 and Kemper were used for CircuitPython
9.2.7 / Bosun 0.6.4 and native. Each direct data-CDC baseline used two warmup
rounds and 30 sequential samples per command. The hub released the serial
port while ALSA MIDI routing remained available.

| Command | CircuitPython median / p95 (ms) | Native 06 median / p95 (ms) | Median ratio, CP / C |
| --- | ---: | ---: | ---: |
| `PING` | 83.50 / 94.75 | 4.88 / 6.16 | 17.1 |
| `GET_CONTEXT` | 110.22 / 117.68 | 18.79 / 20.07 | 5.9 |
| `GET_GLOBAL` | 2281.27 / 2315.93 | 52.59 / 54.44 | 43.4 |

These are host request-to-response times, including scheduling, USB transport
and firmware processing. They do not measure physical footswitch-to-MIDI
latency, TFT pixels, boot time or isolated CPU execution. No hub cache served
the direct reads; the context schemas differ.

Global configuration was equal as parsed JSON. Replies contained 2,215–2,216
bytes for CircuitPython and 2,233–2,234 for candidate 06. An earlier native
reply was about 2,508 bytes before a persistence rewrite compacted the JSON;
comparisons between candidates also depend on serialization size.

Both baselines preserved configuration and reported no counter growth or
uptime reset. Both selected the original CRUNCH bank 1 / rig 3. Their complete
durations were 90.61 seconds and 2.79 seconds respectively; equal sample counts
do not establish equal-duration stability.

## Completed checks and limits

| Candidate 06 check | Recorded result |
| --- | --- |
| Direct bank navigation | 12 confirmed transitions across all six stored patches in banks 1 and 2; original rig restored; counters unchanged |
| Configuration persistence | One color-bit change, exact JSON readback, normal reboot/readback, then original JSON restore and another reboot/readback; both globals, six patches and TFT projections matched |
| CDC reconnect | 20 sessions passed at CRUNCH 3; an earlier rig-3 assertion was a harness precondition error: all 20 replies correctly reported the actual ACOUSTIC 1 selection |
| Browser Stage | Three rig cycles, three external X-block OFF/ON cycles, five cold loads with CLEAN effect checks, nine viewport layouts passed |
| Concurrent endurance | Failed with bulk timeouts, including during a rerun with 99 confirmed bank transitions; subsequent diagnostics are detailed below |

Candidate 06 Stage's five cold-load navigation times ranged from 859 to 3,407 ms; the
slowest overlapped the read-only layout test. Effect checks required 500 ms
of stable state. Context, submitted LED frames and browser DOM agreed, with
one stable change per external effect edge. LED data does not measure emitted light.
The harness checks these saved ACOUSTIC/CLEAN rigs, the five known navigation
labels, B/R or BANK/RIG header prefixes, and MIDI channel 1. It does not prove
correctness for arbitrary user names, prefixes or all 16 MIDI channels.

Candidate 08 with runtime DWC2 repeated and passed the three rig cycles,
three external X-block OFF/ON cycles, five cold loads and nine viewport layouts.
The fresh capture below is byte-identical to the earlier Stage image.

![Real browser Stage on native candidate 08 with DWC2 host: CLEAN, bank 1, rig 2, FLANG on, BOOST off, VOL](ui-test-screenshots/native_stage.png)

After this Stage test, original CRUNCH bank 1 / rig 3 was restored with fresh
matching Kemper information. This records the end of that test, before later
persistence and host-configuration work.

## Candidate 08: two missing USB packets

Candidate 08, UF2 SHA-256 beginning `34912536`, adds observation points at
DCD completion, CDC receive callback and application reads; it is diagnostic code.

| Concurrent client | Result |
| --- | --- |
| Intended 600-second bulk run | Failed `GET_GLOBAL` after 164.20 seconds; 658 `PING`, 658 context and 657 global samples; counters unchanged |
| Bank transitions | Failed `GET_RIG_INFO` after 140 confirmed transitions in 236.93 seconds; `protocol_errors +1`; original rig restoration passed |

The final capture has 79,960 records, zero capture drops, 4,485 valid host
commands and 5,237 complete device JSON frames. Two requests lack replies:

- The entire 63-byte `GET_GLOBAL` packet is absent internally.
- The first 64 bytes of an 81-byte `GET_RIG_INFO` are absent; its remaining
  17 bytes arrive and cause the recorded `ERROR invalid_json`.

Both host OUT transfers completed successfully with their full lengths.
Removing only those 127 bytes makes all **92/92 RX fingerprint checkpoints**
match at all three internal observation points. Packet counts independently
show zero, one, then two missing packets across the corresponding intervals.
All 92 TX checkpoints match the unchanged host reply stream. The parser error
is a consequence of the second incomplete request.

These packets are missing before the instrumented DCD completion boundary,
which excludes the later USBD queue, CDC FIFO and application parser as their
loss point. It still leaves the earlier DCD interrupt path, controller behavior
and host interaction unresolved. The sticky, global SIE `DATA_SEQ_ERROR` bit
became set within the same three-second interval as the first omission; it
does not identify an endpoint or establish causality. No configuration,
interface or data-endpoint clear-halt operation was recorded; a separate
console-endpoint clear-halt occurred about 101 seconds earlier.

USBmon observes host-driver transfers, not physical USB tokens. FNV32,
lengths and packet counts are fingerprints, not internal payload captures;
diagnostic overhead may affect timing.

## Candidate 08 direct concurrent control

Two direct workers shared one CDC session with the original host mask `0xf`.
The bulk worker passed in 600.55 seconds, completing 5,871 `PING`, 5,871
`GET_CONTEXT` and 5,870 `GET_GLOBAL` samples. Its final request reached the
overall measurement deadline and is recorded as censored, not a completed
sample. The concurrent bank worker passed all 300 confirmed transitions in
470.02 seconds. Recorded error counters stayed unchanged; no uptime reset
was observed. Original CRUNCH bank 1 / rig 3 was restored with fresh Kemper
confirmation, and the subsequent read-only full configuration comparison passed.

The transport recorded 24,816 commands and a maximum of two outstanding
requests. All 24,816 have complete correlated replies in the final USB
capture, which contains 418,197 records and reports zero kernel capture
drops. All **200/200 application RX, DCD, CDC and TX fingerprint checkpoints**
match without excluding any request bytes; packet counts also match.
The final global response took 55.38 ms with only 41.60 ms of measurement
budget remaining, explaining the censored sample.

An old 64-byte in-flight prefix precedes the initial session-boundary LF;
it is retained as startup evidence. All 26,234 subsequent reply/event JSON
frames are valid. The only 17 canceled USB IN transfers coincide with the
intentional final DTR close. These startup/shutdown boundaries are distinct
from a missing command during sustained traffic.

This control does not reproduce the production hub's background reads,
context coalescing, exact request framing or scheduling. The failed hub trace
briefly reached four overlapping requests. At both omissions, all other
successfully answered requests had already completed. The later loss
occurred in an interval whose healthy request depth never exceeded two.
Consequently, the different direct result does not establish excessive
concurrency or the hub itself as the cause of the omission.

## Candidate 07 controls

The earlier diagnostic candidate 07 (`8581f485`) reproduced one missing
63-byte context request shared by both hub clients. Excluding that packet
matched all 39 application RX checkpoints; all 39 TX checkpoints matched.
The bulk and bank clients failed at 49.46 and 39.45 seconds respectively.

A separate direct-CDC control with one client and an unchanged rig passed
300.43 seconds: 3,702 `PING`, 3,701 `GET_CONTEXT` and 3,701 `GET_GLOBAL`
samples, with counters unchanged. All 100 RX/TX fingerprint checkpoints
matched. The main captured session had 11,856 requests and 11,855 complete
replies; the final context already had a 256-byte response prefix when its
remaining 9.965 ms overall budget expired and the port closed. The four
cleanup requests all received replies. The 224,095-record capture reported
zero drops; 34 canceled IN transfers coincide with the two final DTR falls,
with no failed OUT completion. This control does not exercise concurrent
clients or rig changes and does not overturn the earlier receive omission.

## Host controls: reboot and reproduced omission

Captain is a 12 Mbit/s device behind the Pi 3's internal 480 Mbit/s hub,
shared with Ethernet and Kemper. Testing `dwc_otg.fiq_fsm_mask=0xe` instead
of the baseline `0xf` ended with an unexpected Pi reboot around 07:41–07:42
UTC, before the 20-minute rollback deadline; the timer had not fired. Both benchmark artifacts were
empty; the last valid console record was at 165.23 seconds. No completed pass
can be claimed, and the reboot's cause is undetermined.

The timer was disabled and the exact original 169-byte boot command line
restored and SHA-256 verified. After reboot, runtime mask `0xf` was confirmed.
That return reproduced the failure: bulk timed out after 50.77 seconds on
`GET_GLOBAL`, and the bank worker after 71 confirmed transitions in 125.95
seconds on `PING`, with recorded error counters unchanged.

The successfully completed host OUT transfers contained 62 and 57 bytes
respectively. Excluding only these two complete packets matches all 74
application RX and all 75 DCD/CDC checkpoints; all 74 TX checkpoints match
without alteration. Packet counts independently show two missing packets.
The capture has 38,118 written records and zero kernel drops; 23 received
records were not written at shutdown, limiting only its final suffix. Both
omissions occur well before that suffix. Here the sticky SIE sequence-error
bit was already set 8.532 ms before the first omitted request was submitted,
so it cannot be assigned uniquely to that request.

The mask change remains an inconclusive system failure, not a verified
workaround.

## Candidate 08 with the DWC2 host driver

The control changed the Pi to `dwc2,dr_mode=host` while retaining the same
candidate 08 firmware, physical controller, internal hub, Ethernet, Captain
and Kemper. Actual platform-driver binding and root-hub topology confirmed
DWC2. After the first completed run, the boot configuration was persisted,
the Pi rebooted, and the production-hub workload passed again.

| Recorded result | First DWC2 run | Repeat after Pi reboot |
| --- | ---: | ---: |
| Bulk total elapsed, with 600-second measurement budget | 601.23 s | 601.25 s |
| Measured `PING` / `GET_CONTEXT` / `GET_GLOBAL` samples | 2,627 / 2,626 / 2,626 | 2,630 / 2,629 / 2,629 |
| Confirmed bank transitions | 300 in 482.09 s | 300 in 483.77 s |
| Captured commands with complete replies | 13,069 / 13,069 | 13,187 / 13,187 |
| Valid reply/event JSON frames | 14,604 | 14,733 |
| Matching application RX, DCD, CDC and TX checkpoints | 211 / 211 at every point | 209 / 209 at every point |
| Matching final OUT packet count | 15,301 | 15,421 |
| USB capture records / kernel drops | 231,986 / 0 | 233,157 / 0 |

Both bank workers restored CRUNCH 3 with fresh Kemper confirmation. Recorded
protocol, storage, MIDI and queue counters stayed unchanged. No missing reply,
malformed frame, diagnostic fault or failed USB completion occurred during
either captured load. No byte removal was needed to match the fingerprints.
Final bulk samples reached the overall duration boundary; every wire request
still has its complete reply. Three descriptor-probe control stalls in the
second capture preceded its data-session DTR rise by about 21 seconds;
they are outside the measured load.

These two passing windows include production-hub background metadata and
patch reads. They support the host USB path as an effective experimental
variable for this setup. They do not establish a universal root cause or a
guarantee for every device, kernel, cable or future workload. Manual footswitch,
pedal and physical-display acceptance remains outstanding.

The first persistence attempt through the hub stopped on its `background_busy`
admission response during metadata refresh after a global change. Initial
and final full configuration comparisons and original-rig restoration still
passed. A bounded harness correction retries only that read-only admission
response; it passed 45 focused tests on both Windows and Linux, with no
Captain firmware change.

The repeated persistence sequence passed with two real Captain reboots:
temporary JSON persisted, original JSON was restored and persisted, and both
globals, all six patches and TFT projections matched the original snapshot.
CRUNCH 3 was restored with fresh Kemper confirmation. Exactly two
`GET_DEVICE_INFO` admission retries were logged, each after 0.5 seconds, one
following each global write. USB errors, malformed JSON and write operations
are not covered by that retry rule.

The final read-only comparison again matched both globals, all six patches
and TFT projections. The session's entry selection, ACOUSTIC bank 1 / rig 1,
was restored with fresh Kemper confirmation. A final passive Stage check
passed with ACOUSTIC selected, HARM on and EQ/BOOST off. This final selection
is distinct from the CRUNCH 3 position in the original September 5 backup.

DWC2 boot configuration and actual binding were verified after the Pi reboot;
the original boot command line remains unchanged. All three temporary
startup/rollback units were disabled and removed. Hub and kiosk services
were active at handoff, with no benchmark writer or capture process left
running. The exact original Captain backup and Pi boot configuration remain
available for rollback.

## Memory evidence

| Linker audit | Candidate 06 | Candidate 08 diagnostic |
| --- | ---: | ---: |
| Static RAM | 234,260 | 234,396 |
| Reserved stack | 16,384 | 16,384 |
| Unused RAM margin | 19,692 | 19,556 |
| Flash program image | 159,616 | 161,292 |

No dynamic allocator symbols were linked. The Captain executes native code
without CircuitPython or its garbage-collected heap, but static RAM remains
heavily occupied. The margin is not a measured stack high-water mark.

CircuitPython's baseline `mem_free` readings were 6,952–6,984 bytes. These are
sampled heap values, not total physical free RAM or a maximum contiguous
allocation. Comparing them directly with the native linker margin would not
measure a memory saving. Native `STATS` provides no equivalent heap or live
stack-headroom measurement.

## Backup and exact rollback

Before installation, all **8 MiB of physical flash** were read and verified
with `picotool save -a -v`. A second local copy matched SHA-256; the recovery
UF2 was checked page by page against the full binary. Configuration was also
preserved through file extraction and protocol snapshots.

The original FAT spans `0x100000..0x800000`; native littlefs occupies its final
512 KiB, **inside that FAT volume**. Although those clusters were free in the
backup, their bytes change. Exact rollback requires the saved full 8 MiB image,
including the filesystem; a generic CircuitPython UF2 is insufficient.

On September 6, the complete original image was actually restored and
verified over all 8 MiB. After reboot, the read-only protocol comparison passed:
both `GET_GLOBAL` objects, all six `GET_PATCH` objects, firmware `0.6.4` and the
active profile matched the original snapshot. This establishes the completed
flash rollback and configuration readback, with the live tests below recorded
separately.

Before that rollback, candidate 06 was also backed up as a verified
8,388,608-byte image, SHA-256 beginning `15e33b40`, preserved on both the Pi
and the PC. The images, identity-checked restore script and machine-specific
instructions remain private; raw snapshots and device identifiers are not
included in this documentation. At the final handoff, the original backups
were rehashed on both Pi and PC and still matched their recorded hashes.

## Restored CircuitPython control

With the original firmware restored, the hub bulk client completed its
300-second measurement budget: 110 `PING`, 110 `GET_CONTEXT` and 109
`GET_GLOBAL` replies, with no `usb_tx_dropped` growth. Total runtime including
setup and final reads was 312.54 seconds. Its last `GET_GLOBAL` was cut off
by the remaining 0.74-second overall budget, recorded as censored rather than
an eight-second response timeout.

The concurrent bank client failed at 48.81 seconds after one confirmed
transition, and its automatic restoration confirmation also timed out.
The USB capture contains replies to all 401 captured CDC requests, with no
malformed JSON. The two timed-out context replies arrived after 2.221 and
2.355 seconds, exceeding their remaining confirmation budgets of 1.489 and
0.792 seconds. MIDI also shows the final Kemper rig selections arriving while
CircuitPython context retained the intermediate rig coordinates. These are
observed state and deadline failures, without a missing CDC reply in this
capture. Its 401 requests and lower throughput do not establish equivalent
load or absolute reliability against the 3,400-request native-06 trace.
The successful configuration comparison above preceded these navigation tests.

## Findings retained for review

- DIN UART transmission stalled when interrupts were enabled on an empty
  FIFO. The producer now primes the FIFO before interrupt-driven draining.
- CDC close/reopen handling now separates sessions and queued traffic.
  Serial-open input flushing can still discard an initial prefix; bounded
  PING synchronization records startup fragments, followed by strict parsing.
- Candidate 02 failed rig/name correlation. Candidate 03 passed the 15-target
  bank-1 sequence but mishandled an intermediate bank-change Program Change.
  Its 601.25-second endurance test failed with `storage_errors +2`.
- Candidate 04 passed short boundary tests but two concurrent clients rejected
  malformed JSON. A later instrumented rerun passed; its 44,647 capture-tool
  drops were not evidence of Captain packet loss. That earlier corruption
  has not been conclusively attributed.
- Candidate 05 preserves queued CDC data and session identity during USB
  suspend, but still failed longer bank stress with a timeout and storage
  error. Its empty bulk artifact and truncated console/MIDI captures do not
  establish endurance or explain the loss of Pi access.

Candidate 06 extends the bank-snapshot window to 2.5 seconds and accepts a
late final PC unless a newer selection superseded it. Offline tests cover
delayed confirmations, fallback, timer wrap and superseding selections.
This fixes a reproducible state-handling case; the later USB omissions require
separate investigation.

Commit `9e97cd2`, including the candidate-07 diagnostics, passed
[native GCC/Clang sanitizers and ARM image checks](https://github.com/danmigdev/bosun-midi-captain/actions/runs/34017142235)
and [CircuitPython, hub, editor, Rust and Android CI](https://github.com/danmigdev/bosun-midi-captain/actions/runs/34017142213).
The candidate-08 host checks also passed all 23 local sanitizer suites; the
45 focused Python checks passed on Windows and Linux. These offline results
coexist with the recorded hardware history and the two passing DWC2 hub runs.
Physical switch, pedal and display acceptance remains necessary.

Reproduction tools:
[`live_hardware_benchmark.py`](../firmware-native/tests/live_hardware_benchmark.py)
and [`live_native_persistence.py`](../firmware-native/tests/live_native_persistence.py).
Both default to reads. Private artifacts retain command correlation, recent
context/rig/error replies and malformed wire data without tolerating session corruption.

Private source artifacts include the `cp-usb-*`, `native06-*` and
`cp-restored-*` reports, `resume-cp-restore.log`, `backup-verification.json`
and `native-storage-fat-overlap.json`. Diagnostic conclusions are recorded in
`native07-missing-request-analysis.json`, `native07-direct-usb-analysis.json`,
`native08-usb-omission-proof.md`, `native08-missing-request-final-analysis.json`
and `native08-fingerprint-final.json`, alongside original captures, benchmark
reports and each candidate's linker audit. The incomplete host experiment is
retained as `native08-fiq14-*`. The return-to-baseline and request-pressure
analyses are `native08-fiq15-return-proof.md` and
`native08-fiq15-return-pressure-proof.md`; the direct control is
`native08-direct-concurrent.json` and `native08-direct-concurrent-proof.md`
with its final USB capture. `native08-dwc2-proof.md`,
`native08-dwc2-repeat-automated-report.json` and the final captures record the
two DWC2 hub passes. `native08-dwc2-stage-*`, the persistence reports,
`native08-final-config-compare.json` and
`native08-final-session-entry-restored.json` retain the remaining checks.
Raw configuration is not published.
