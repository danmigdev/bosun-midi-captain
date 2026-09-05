# Native Captain hardware evidence, 2026-09-05/06

**Candidate 05 is experimental and has not passed final hardware acceptance.**
Its USB baseline, bounded bank transitions, persistence and browser checks
passed. Longer concurrent bank stress failed; the final endurance result could
not be recovered after the Pi became unreachable. The cause of that host
unreachability is undetermined.

The last verified installed UF2 was candidate 05, SHA-256 beginning
`a8969362`. Subsequent echo-handling changes are offline work, not another
hardware-validated installation.

## Measured USB performance

The same Captain, Raspberry Pi 3 and Kemper were used for CircuitPython
9.2.7 / Bosun 0.6.4 and native. Each direct data-CDC baseline used two warmup
rounds and 30 sequential samples per command. The hub released the serial
port while ALSA MIDI routing remained available.

| Command | CircuitPython median / p95 (ms) | Native 05 median / p95 (ms) | Median ratio, CP / C |
| --- | ---: | ---: | ---: |
| `PING` | 83.50 / 94.75 | 4.86 / 5.84 | 17.2 |
| `GET_CONTEXT` | 110.22 / 117.68 | 19.37 / 20.70 | 5.7 |
| `GET_GLOBAL` | 2281.27 / 2315.93 | 50.72 / 53.41 | 45.0 |

These are host request-to-response times, including scheduling, USB transport
and firmware processing. They do not measure physical footswitch-to-MIDI
latency, TFT pixels, boot time or isolated CPU execution. No hub cache served
the direct reads; the context schemas differ.

Global configuration was equal as parsed JSON. Replies contained 2,215–2,216
bytes for CircuitPython and 2,233–2,234 for candidate 05. Native's earlier
candidate-03 reply was about 2,508 bytes before a persistence rewrite compacted
the JSON: timing changes between native candidates also include serialization
size, not just executable changes.

Both baselines preserved configuration and reported no counter growth or
uptime reset. Their complete durations were 90.61 seconds and 2.76 seconds
respectively; equal sample counts do not establish equal-duration stability.

## Completed checks and limits

| Candidate 05 check | Recorded result |
| --- | --- |
| Direct bank navigation | 12 confirmed transitions across all six stored patches in banks 1 and 2; original rig restored; counters unchanged |
| Configuration persistence | One color-bit change, exact JSON readback, normal reboot/readback, then original JSON restore and another reboot/readback; both globals, six patches and TFT projections matched |
| CDC reconnect | 20 sessions passed without errors or discarded startup fragments |
| Browser Stage | Three rig cycles, three external X-block OFF/ON cycles, five cold loads with CLEAN effect checks, nine viewport layouts passed |
| Concurrent bank stress | Failed after 25 completed transitions in 49.45 seconds: timeout and `storage_errors +1` |
| Intended 600-second endurance run | Final artifact unavailable after loss of access to the Pi; no pass claimed |

Stage's cold-load navigation became ready in 844–875 ms. Its effect checks
required 500 ms of stable state. During active tests, correlated context, the
Captain's submitted LED frame and browser DOM agreed, with one stable change
per external effect edge. Submitted LED data does not measure emitted light.

![Real browser Stage on native candidate 05: CLEAN, bank 1, rig 2, FLANG on, BOOST off, VOL](ui-test-screenshots/native_stage.png)

The screenshot is from the connected devices. After this capture, the private
restore tool confirmed the original bank 1 / rig 3 and fresh matching Kemper
rig information. **That confirmation preceded another bank diagnostic and
the later network loss; the current rig is not verified.**

## Memory evidence

| Candidate 05 linker audit | Bytes |
| --- | ---: |
| Static RAM | 234,256 |
| Reserved stack | 16,384 |
| Unused RAM margin | 19,696 |
| Flash program image | 159,504 |

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

The original FAT volume spans `0x100000..0x800000` (7 MiB). Native littlefs
uses `0x780000..0x800000` (512 KiB), **inside that FAT volume**. Its 512
destination clusters were free in the captured FAT, but native installation
still changes their bytes. Exact rollback requires the complete saved 8 MiB
image, including the filesystem; a generic CircuitPython UF2 is insufficient.

The verified image, identity-checked restore script and machine-specific
instructions remain local. Preparing and validating that recovery image is
not a completed hardware restore-and-retest cycle. Raw snapshots and device
identifiers are not included in this documentation.

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
  malformed JSON. An instrumented rerun passed 80 bank changes and 201 seconds
  of bulk traffic; the first 2.727 MB of USB and serial captures matched.
  The capture tool later reported 44,647 dropped capture packets, which are
  not evidence of Captain packet loss. The corruption's cause remains unproven.
- Candidate 05 preserves queued CDC data and session identity during USB
  suspend. That correction does not prove it caused or resolved candidate
  04's JSON failure. Candidate 05's longer bank stress still failed.

Subsequent source changes extend the fixed bank-snapshot window from 1 to
2.5 seconds and accept a late final PC after fallback unless a newer selection
has superseded it. Successful candidate-04 USB captures already approached
the original one-second limit. Offline tests cover 1.1/2.4-second confirmations,
fallback, timer wrap and superseding selections; all 22 host suites passed.
The resulting candidate 06 was compiled and audited, **not installed**. Its
UF2 SHA-256 begins `425ece31`; static RAM is 234,260 bytes with a 19,692-byte
linker margin. This change fixes a reproducible delayed-echo case in tests;
it does not establish the cause or resolution of candidate 05's hardware failure.

Commit `4921909` passed
[native GCC/Clang sanitizers and ARM image checks](https://github.com/danmigdev/bosun-midi-captain/actions/runs/33998391237)
and [CircuitPython, hub, editor, Rust and Android CI](https://github.com/danmigdev/bosun-midi-captain/actions/runs/33998391244).
These checks coexist with the hardware failures above. Further bank-transition
diagnosis, a completed clean endurance run, and direct switch/pedal and display
observations remain necessary. The Pi's later SSH/HTTP/neighbor unreachability
has not been diagnosed, and final host logs were unavailable.

Reproduction tools:
[`live_hardware_benchmark.py`](../firmware-native/tests/live_hardware_benchmark.py)
and [`live_native_persistence.py`](../firmware-native/tests/live_native_persistence.py).
Both default to reads. Private artifacts retain command correlation, recent
context/rig state and malformed wire data without tolerating session corruption.

Source artifacts remain local: `cp-usb-baseline.json`, `cp-usb-rigs.json`,
`native05-usb-baseline.json`, `native05-usb-banks.json`,
`native05-persistence.json`, `native05-cdc-reconnect20.json`,
`native05-hub-banks-stress.json`, `native05-stage-*.txt`,
`native05-stage-original-rig-restored.json`,
`candidate-05/bosun_native-memory.json`, `backup-verification.json` and
`native-storage-fat-overlap.json`; earlier failed-candidate artifacts are
retained privately as well.
