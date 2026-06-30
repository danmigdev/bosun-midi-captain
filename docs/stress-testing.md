# Stress-testing the MIDI Captain on real hardware

This guide walks through five categories of test. The first two need no
extra hardware. The third needs a single TRS-MIDI cable. The fourth runs
in the background while you do other things. The fifth needs an oscilloscope
or a logic analyzer.

## Setup

Identify the two `COM` ports the pedal exposes. Plug it in, open Windows
Device Manager → Ports. You'll see two new entries - typically named with
"USB Serial Device". The first is CircuitPython's REPL console; the second
is the firmware's JSON protocol. Tests target the **second** one.

If you can't tell which is which, open the lower-numbered port in PuTTY at
115200 baud - if you see a Python REPL prompt or `Captain x.y.z ready`,
that's the console. The other is the data port.

```
pip install pyserial          # only requirement for tools/stress.py
```

---

## A. Protocol stress (no extra hardware)

Verifies firmware can keep up with the editor under load and isn't dropping
messages. Run after major firmware changes.

### Smoke pass

Sanity check: every protocol command returns something coherent.

```
python tools/stress.py --port COM7 smoke
```

Expect to see a one-line status per command. Anything that throws an
exception is a bug worth investigating.

### Hammer mode

Fire `PUT_BINDING` in a tight loop and measure ACK round-trip latency.

```
python tools/stress.py --port COM7 hammer --count 500
```

What to look for:

- **p50 latency** under 50 ms is healthy.
- **p99** under 200 ms is acceptable.
- **errors > 0** means the firmware fell behind - either the main loop is
  too slow (something CPU-heavy crept in) or the autosave debounce is
  flushing too aggressively.

Vary the burst rate with `--interval 0.01` (100 req/s) to find the knee
point. The CDC RX buffer is small; sustained throughput above ~200 req/s
will likely start dropping.

### Spam mode

Fire-and-forget PINGs without waiting between sends. Tests whether the
CDC buffer + main loop can catch up.

```
python tools/stress.py --port COM7 spam --count 1000
```

If "received: 1000/1000" the firmware caught everything. If less, you've
found the CDC RX buffer ceiling for that batch size.

---

## B. Switch FSM logic (no hardware)

Pure-Python tests of the per-switch state machine - same FSM the firmware
runs, executed against synthetic time + pin levels.

```
python tools/fsm_test.py
```

Expected: `11/11 passed, 0 failed`. Run this before committing any change
to `firmware/lib/captain/bindings.py` - it catches debounce / long-press
boundary / double-tap window regressions in seconds.

To extend: add new `@test("description")` blocks at the bottom. Each gets a
fresh FSM via `fresh(**kwargs)`, drives `press(fsm)` / `release(fsm)`, and
polls with synthetic `now_ms`.

---

## B2. Firmware stability (no hardware)

Mocks the CircuitPython surface, constructs the real `captain.app.Captain`,
and hammers the protocol dispatch + main loop with valid and malformed
input. Catches the class of bug where an unhandled exception escapes the
main loop and kills `code.py` - on real hardware that drops the USB data
CDC and the editor reports "not connected".

```
python tools/firmware_stability_test.py
```

Expected: `15 passed, 0 failed`. Covers:

- `protocol.handle()` never raises and always answers across a barrage of
  valid + malformed messages (incl. `PUT_GLOBAL` with `patch_link` locks),
  plus a 2000-message fuzz.
- `Captain.tick_once()` survives an exception thrown by ANY sub-component
  (protocol.poll, MIDI parse, switch poll, autosave). Regression guard for
  the "bare main loop kills the connection" bug - only `handle()` used to
  be wrapped.
- `MidiParser.feed()` eats 20000 random byte bursts + SYSEX edge cases
  without throwing.

Run before releasing any change to `app.py` / `protocol.py` / `midi.py`.

---

## C. MIDI round-trip (one TRS cable)

Cheapest way to measure MIDI in/out latency without external test gear.

### Wiring

Connect the pedal's MIDI **OUT** jack to its own MIDI **IN** jack with a
single TRS-MIDI cable. Anything the firmware transmits on DIN comes
straight back in.

### Run

```
python tools/stress.py --port COM7 midi-rt --presses 20
```

The script puts the firmware into MIDI Learn mode, then for each iteration:

1. Overwrites the active patch with one whose `on_enter` sends a unique PC.
2. Calls `SWITCH_PATCH` to trigger the macro.
3. Times how long until the matching `midi_in_captured` event arrives.

What to look for:

- p50 round-trip should land between **5-15 ms** on a healthy build.
  Anything > 30 ms suggests the main loop is doing too much per tick.
- p95 spikes beyond 50 ms point at autosave flushes or GC pauses.
- If "0 round trips completed" - the cable is wired wrong, or `auto_follow_pc`
  is consuming the inbound PC before the learn capture (set `auto_follow_pc:
  false` in `device.json` for this test).

### Caveat

This test does NOT measure switch-press-to-MIDI latency. The press itself
is bypassed (we trigger via `SWITCH_PATCH`). For real switch latency you
need test E.

---

## D. Soak (background, hours)

Hunts memory leaks and slow degradation.

```
python tools/stress.py --port COM7 soak --duration 21600 --interval 30 > soak.csv
```

Runs for 6 hours, samples STATS every 30 s, writes CSV. Open `soak.csv` in
Excel / a plotter when done:

- **`mem_free`** should oscillate around a baseline. A monotonic decrease
  over hours = leak. The first 100 KB or so of free RAM disappearing in
  the first minute is normal (lazy init).
- **`iters_per_s`** should be roughly constant. If it falls over time
  something is accumulating per-tick work.
- **`midi_rx_count`** and **`midi_tx_count`** rise only when MIDI flows.
  Spurious increases when no MIDI is connected indicate noise on the
  UART line (cable issue, not firmware).

For really long soaks, run with `--duration 86400` (24 h) overnight.

### Real-world soak

Do soak runs while *actually using the pedal* (or while the protocol
hammer is going in another window). Pure idle doesn't exercise the
allocator hot paths.

---

## E. Hardware-only: switch press latency

Real switch → MIDI byte latency. Needs an oscilloscope, logic analyzer,
or `Saleae`-style USB capture probe.

### Setup

- **Channel 1**: tap onto one footswitch's GPIO pin (or use a probe on the
  switch contact). Goes LOW on press.
- **Channel 2**: tap the UART TX line (GP16) or USB MIDI activity.

Configure a binding on that switch that fires a single, distinctive raw
`note_on` so the MIDI byte is unambiguous on the scope.

### Measure

Press the switch. Read the time from the falling edge on CH1 to the start
bit on CH2. Expected:

- 5-15 ms typical on this firmware design (5 ms debounce + ~5 ms main loop).
- If > 30 ms consistently: main loop too slow, profile what runs per tick.
- If < 5 ms: you've optimized debounce too aggressively and may be
  chattering. Compare with the `debounce` FSM test in B.

### What you can't measure without hardware

- USB MIDI driver-side latency on the host PC (DAW-dependent).
- DIN MIDI clock jitter (needs precise UART probe).

For these you'd want a dedicated test rig - not worth it for hobby use.

---

## Quick reference

| Test | Hardware | When to run |
|---|---|---|
| `python tools/fsm_test.py` | none | Every firmware change to `bindings.py` |
| `python tools/firmware_stability_test.py` | none | Every change to `app.py` / `protocol.py` / `midi.py` |
| `python tools/stress.py … smoke` | pedal connected | After firmware update on the pedal |
| `… hammer` | pedal connected | Before releasing a new firmware version |
| `… spam` | pedal connected | Same |
| `… midi-rt` | TRS loopback cable | When changing the MIDI parser or main loop |
| `… soak --duration 21600` | pedal connected | Overnight, on a release candidate |
| Scope on GPIO + UART | scope or LA | Once per hardware iteration |
