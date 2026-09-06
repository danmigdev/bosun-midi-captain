# Raspberry Pi 3 hub latency

Measured on 6 September 2026 with the MIDI Captain native candidate 08, a
connected Kemper Player and the Pi's DWC2 USB host driver. This change keeps
the hub in Python and wakes its upstream worker when a command enters the TX
queue. The Captain firmware and Stage bundle are unchanged.

## Results

The complete TCP/USB comparison matched all 388 request/reply exchanges in
each version, including all 360 measured requests. There were no ambiguous
matches, incomplete replies, TCP retransmissions or reported capture drops.
Each cell below pools 60 actual samples; timings are the kernel-captured TCP
round trip in milliseconds, with setup and warmup excluded.

| Scenario | Request | Before p50 / p95 | After p50 / p95 |
|---|---|---:|---:|
| Burst | `PING` | 64.046 / 65.502 | 7.582 / 8.809 |
| Burst | `GET_CONTEXT` | 77.398 / 78.723 | 19.956 / 21.135 |
| Burst | `GET_GLOBAL` | 109.453 / 112.106 | 52.947 / 55.534 |
| Quiet | `PING` | 39.133 / 64.849 | 7.636 / 9.405 |
| Quiet | `GET_CONTEXT` | 45.959 / 75.690 | 20.332 / 22.359 |
| Quiet | `GET_GLOBAL` | 72.916 / 103.646 | 52.510 / 55.241 |

In burst, the median interval before USB fell from 58.207–59.105 ms to
1.327–1.438 ms. The USB/device interval remained about 5 ms for PING, 18 ms
for context and 49–50 ms for global. This identifies the removed host wait;
it does not predict the benefit of rewriting the hub in C. Phase medians
are calculated independently and must not be added to reconstruct the RTT median.

A separate control ran the original client helper without tcpdump, the
resource sampler or browser instrumentation, using 30 samples per request:

| Request | Before client p50 / p95 ms | After client p50 / p95 ms |
|---|---:|---:|
| `PING` | 64.457 / 65.680 | 6.730 / 7.770 |
| `GET_CONTEXT` | 78.100 / 79.659 | 19.353 / 21.454 |
| `GET_GLOBAL` | 110.983 / 113.935 | 52.140 / 54.262 |

All 90 measured requests and final checks passed in each control. This
independently corroborates the latency reduction; periodic stats requests and
different browser process histories prevent isolating instrumentation cost
from the difference between the two tables.

## Why wake the worker

Before this change, a command could arrive while the worker was blocked in
the serial read's 50 ms timeout, followed by a 10 ms idle sleep. In the measured
back-to-back request/reply workload, the median interval from the kernel's
loopback TCP command capture to USB OUT submission was about 58–59 ms.
That interval includes scheduling and host processing; it is not 58–59 ms of
Python CPU execution.

The worker now waits on both the upstream descriptor and a separate control
socket. Queue admission notifies that nonblocking socket and the worker's
short backpressure wait. A pending notification survives admission before the
wait, and a full notification buffer coalesces wakeups. Notification does not
alter serial data or DTR. The existing receive-first draining, partial-write
FIFO, watchdog and reconnect queue purge remain in place.

MIDI between Captain and Kemper continues through the kernel ALSA sequencer.
These measurements concern the hub's JSON state/editor channel, not audio or
physical footswitch latency.

## Measurement method

Before and after use the same Pi, Captain firmware, DWC2 driver, configured
context and client harness. Each version has two sequential runs of 30 samples
for each of `PING`, `GET_CONTEXT` and `GET_GLOBAL` in each scenario: 360 measured
requests plus setup, warmup and final checks. The second run reverses scenario
order. Burst means one outstanding request followed immediately by the next;
quiet means a seeded 200–250 ms pause. Neither is a two-client load test.

The client connects to `127.0.0.1:9876`, using the default TCP settings, while
the production hub remains the sole owner of the Captain's data CDC port.
Simultaneous loopback TCP and usbmon captures separate time before USB,
USB submission to complete reply, and return to TCP. Requests and complete
response bodies are correlated uniquely; identifiers alone are insufficient.
`GET_GLOBAL` is forwarded to the Captain. No retained context cache is used.

Raw hardware logs, packet captures and configuration snapshots remain in the
private `dist/rpi3-performance-20260906` and
`dist/rpi3-hub-wakeup-20260906` evidence directories. The first after capture
has a truncated TCP suffix: all 388 exchanges are present in the client and
USB logs, but only 372 complete exchanges in TCP. It is retained and excluded
from the complete TCP comparison. The repeated capture adds three quiet
seconds after the final request so libpcap can drain before capture shutdown.
The complete repeat also verifies identical GLOBAL and CONTEXT bodies before
and after, excluding only request IDs, with the same USB byte totals in both
versions: 23,806 outbound and 380,806 inbound.

## CPU and memory

CPU values below are percentages of one core, derived from `/proc` ticks.
Only intervals fully contained in the relevant phase are included. The Pi has
four cores. Memory uses proportional set size (PSS), sampled every ten seconds.

| Observation | Hub | Chromium | Resource sampler |
|---|---:|---:|---:|
| Before, 60.029 s idle | 0.433% | 0.183% | 2.032% |
| After, 59.039 s idle from final repeat | 0.542% | 0.203% | 2.033% |
| Two clients, 223.102 s excluding Stage profiling | 22.232% | 0.224% | 2.219% |
| Two clients and three Stage reloads, 4.000 s | 33.498% | 137.242% | 1.750% |

The first after run independently measured the same 0.542% idle hub CPU.
The concurrent phase outside Stage profiling completed 42.46 measured requests
per second. Faster burst completion increases work per second, so raw busy CPU
percentages are not a comparison at equal load. Fully contained after-burst
samples cover less than two seconds per capture; they do not establish a CPU
efficiency gain. The sampler cost is shown separately and does not include all
instrumentation or client overhead.

During the concurrent phase outside Stage profiling, median/max hub PSS was
19.764 MiB; Chromium was 456.392/458.293 MiB. Across the whole concurrent test,
minimum sampled `MemAvailable` was 342.145 MiB and maximum temperature was
68.218 °C. Throttle flags were `0x0` at the start and end. The final single-client
repeat measured hub PSS of 19.927 MiB during requests and 20.406 MiB at idle.

The hub and browser were restarted between the baseline and after measurements,
and Chromium's resident state differs. Memory deltas therefore cannot be
attributed to the patch. PSI was unavailable, swap occupancy does not measure
swap I/O, and these short runs do not establish long-term memory behavior.

## Concurrent clients and Stage

Two independent TCP clients each ran for 300 seconds, with 299.852 seconds of
overlapping measured activity. They completed 6,349 and 6,353 measured requests
(12,702 total), while the real on-Pi Stage performed three page reloads, a
60-second idle profile and a 10-second animation-frame scheduling check.

Both clients passed all final protocol checks. Four initial/final global and
context snapshots were identical, observed error counters did not increase,
and hub and kiosk process identities remained stable throughout the 310-second
resource sample. No request exhausted the normal eight-second timeout. One
last request per client was right-censored by the overall 300-second boundary:
its remaining timeout was only 62.3 or 40.4 ms. These two incomplete samples
are recorded separately and are not counted as completed replies.

Under this concurrent workload, each client's median PING was about 14.5 ms,
context read about 68.5 ms and global read about 56.8–56.9 ms. The slowest
completed measured request was 217.2 ms. This workload includes queueing behind
the other client's requests; it is distinct from the single-client table.

Chromium profiling used the existing kiosk, with a temporary loopback-only
debug port. Instrumentation was enabled before the concurrent resource sample
and removed after it; no kiosk restart occurred within that sample. The
original launcher, arguments and service configuration were restored, with the
debug listener closed and rollback timer stopped. The hub process remained unchanged.

During the three reloads, navigation and the grid were available in
428.7–562.6 ms; first contentful paint was 236–332 ms. The subsequent
60.019-second browser-idle phase, while both TCP clients kept running, used
0.537% of one core on the renderer main thread, with no observed JavaScript
errors, long tasks, layout or style recalculation. The 601 animation-frame
intervals had median 16.7 ms, p95 16.8 ms and maximum 16.8 ms. This checks browser
scheduling on the 1920×440 headless Wayland output; it is not a physical-display
frame-rate measurement. The earlier Stage profile had no competing TCP clients,
so these results establish behavior under load rather than an isolated UI speedup.

## Regression coverage and deployment

The full hub suite passes on Windows (201 passed, one POSIX-only skip) and
Linux (202 passed). New tests exercise blocked and early wakeups, concurrent
receive data, partial writes and backpressure, a saturated notification socket,
old-session admission during reconnect, idle waiting and bounded shutdown.
Actual TCP sockets and a Linux pyserial pseudoterminal cover the transport
paths as well as deterministic fakes.

The PowerShell deploy-helper regression checks also pass. Their fixture now
normalizes CRLF to LF before extracting shell/Python snippets, matching the
production deploy encoder on Windows checkouts.

Before deployment, the entire working hub package and deployment context were
backed up on the Pi and PC, with independent SHA-256 verification. Only
`bosun_hub/link.py` differs in the installed seven-module package. The existing
atomic deploy helper passed both independent health gates and installed-file
hash checks. The original backup and restore instructions remain available.

To repeat the portable suite, run `python -m pytest -q` in `tools/rpi-hub`.
For a bounded hardware read-only check, copy
`firmware-native/tests/live_hardware_benchmark.py` to the Pi and run:

```sh
python3 live_hardware_benchmark.py --host 127.0.0.1 --port 9876 \
  --samples 30 --duration 60 --timeout 8 --warmup 2 --stats-every 5 \
  --output new-hub-readonly-result.json
```

These are finite regression checks. Physical footswitches, expression pedal,
TFT/LED output and audio still require the manual acceptance described in the
[native firmware hardware report](native-firmware-hardware-validation.md).
