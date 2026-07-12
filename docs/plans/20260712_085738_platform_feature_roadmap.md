# Bosun platform feature roadmap

Date: 2026-07-12
Status: planning
Owner: Danilo

This plan captures the five features selected from the "killer app" analysis, plus
the decisions made while scoping them. It is the working document for the next
development arc; keep it updated as items ship (drop completed items into git
history, not this file).

## Scope decisions (already made)

Analysis produced a longer candidate list; the following were explicitly
**deselected** and are out of scope for this arc:

- Cross-platform editor: already solved. `release.yml` builds macOS `.dmg` and
  Linux AppImage on tag; only the `README.md` Requirements line is stale (see
  "Quick fix" below). Not a feature, just a doc edit.
- Logic/automation layer (toggle groups, sequences, conditions): deselected.
- Multi-device orchestration in one patch: deselected.
- Song/setlist prompter expansion: deselected.
- Community plugin/preset hub: deselected.
- More plugins (QC/Fractal/HX/etc.): deselected.
- Other MIDI Captain hardware variants: deselected.
- Cloud/sync backup: deselected.

### MIDI clock master: evaluated and REJECTED

A global tempo / MIDI clock master was considered and dropped after verifying the
firmware timing model. Recording the reasoning so it is not re-litigated:

- The firmware is a cooperative single-thread superloop: `while True: tick_once();
  time.sleep(0.005)` (`app.py:172`). A clock byte can only be emitted at a loop
  boundary, so the loop period (5 ms sleep + ~6-12 ms work) is the timing
  resolution. MIDI clock is 24 PPQN (20.8 ms/pulse at 120 BPM), so baseline
  quantization jitter is already several ms (10-25% instantaneous tempo wander).
- Full TFT renders block the loop for tens of ms. The entire deferred-render
  system (`_REFRESH_QUIET_MS = 40`, `_refresh_due_ms`) exists because a render
  starves USB-MIDI. During a render, zero clocks go out, then a late burst
  catches up: an audible tempo stumble on every patch change / tuner / marquee.
- No escape hatch on this stack: CircuitPython does not expose the RP2040 second
  core (`_thread` unavailable) and offers no practical ISR-driven MIDI TX. PIO
  could bit-bang a precise DIN clock but is a large lift and, decisively, cannot
  drive USB-MIDI. The flagship device (Kemper Player) is USB-MIDI only, so its
  clock must go through `usb_midi.PortOut.write()` (loop-bound, can short-count).
  There is no low-jitter path for the primary target.

Tempo was dropped entirely (no tap-tempo-broadcast substitute this arc either).

## Quick fix (do first, trivial)

`README.md` Requirements section says the editor is Windows 11 only. macOS/Linux
builds exist. Update that line to list all three platforms so the README stops
under-selling the product.

---

## Feature 1: Per-patch expression mapping  [IMPLEMENTED 2026-07-12]

Shipped (full firmware battery + editor check/build green). Built in parallel with
Features 4 and 5 (disjoint files). Firmware: `Captain._expression_for_patch(patch)`
merges a deep copy of `device["expression"]` with the patch's optional
`expression` list (per-jack `{jack, message, invert?}` overrides the target/invert,
keeps device calibration/curve/enabled); called in `switch_patch` and
`apply_global`, try/except falls back to device default so a bad override never
breaks patch load. `expression.py` unchanged. Editor: a collapsible "Expression"
block in `PatchEditor.svelte` (per-jack override toggle + target picker from
`continuousControlTypes(manifest)` + invert; persists via the existing patch-write
path; calibration/curve stay device-wide in Settings). `tools/expression_test.py`
extended (55 pass) covering merge, no-override, malformed-safe, jack-2-only.



**Goal.** Let the expression-pedal target change per patch (e.g. wah on a lead
patch, volume on a clean patch), instead of the single device-wide mapping today.

**Today.** Expression is device-wide. `expression.py` builds jacks from the
`device.json` `expression` list (`{jack, enabled, invert, calibration, curve,
message}`), `message` is a template with the live 0..127 value substituted into
`value`. `configure(expression_cfg)` rebuilds the jacks; `poll(now_ms)` returns
`(message_template, value127)` for moved jacks, dispatched in `_tick_body`
(`app.py:240`). Editor UI is `ExpressionPedals.svelte`, currently under Settings.

**Design.**
- Patch model gains an optional `expression` override (same shape as the
  device-wide list, or a per-jack subset: at minimum the `message` target;
  calibration/curve should stay device-wide since they are physical to the jack).
- On patch load (`switch_patch` in `app.py`), if the active patch carries an
  expression override, call `expression.configure()` with the merged config
  (patch `message`/`invert` over device calibration); otherwise fall back to the
  device-wide config. Ensure the jack re-arms cleanly (configure already releases
  and rebuilds).
- Persist the override in the patch JSON via the existing patch put/save path.
- Editor: add an "Expression" block inside `PatchEditor.svelte` (per-patch),
  reusing the mapping controls from `ExpressionPedals.svelte`. Make clear which
  settings are per-patch (target message, invert) vs device-wide (calibration,
  curve), so users are not confused about where calibration lives.

**Risks.** Keep calibration device-wide (it is a property of the physical jack,
not the sound) or every patch would need recalibration. Migration: patches
without an override must behave exactly as today.

**Testing.** Extend `expression_test.py`: override present -> patch target used;
override absent -> device default used; patch switch reconfigures jacks; bad
override falls back safely.

**Effort.** Medium. Touches patch model + `app.py` switch_patch + `expression.py`
merge + editor. No plugin contract change.

---

## Feature 2: Virtual pedal simulator (editor-only)  [IMPLEMENTED 2026-07-12, pending HW verify for Live]

Shipped in this arc (all tests green; production build OK; Live mode not yet
hardware-verified). Built with parallel agents for the two foundation libs:
- Foundation (pure, unit-tested): `lib/led-color.ts` (port of `leds.py::_color_for`,
  12 tests) and `lib/switch-fsm.ts` (port of `bindings.py::SwitchFsm`, clean-edge /
  injected-clock, 12 tests mirroring `tools/fsm_test.py`).
- `components/PedalSimulator.svelte`: Model/Live toggle, both feeding the shared
  `PedalMap` via `colorFor` + `selected`, plus an action/MIDI log.
  - Model (offline): one `SwitchFsm` per bound switch, built with the same timings
    as `app.py` (long_press_ms / double_tap_window_ms / auto_momentary_ms, and
    per-binding `auto_momentary ?? device default`). Pointer down/up on a switch =
    press/release; a 20 ms ticker resolves long-press / double-tap. LEDs via
    `led-color.ts`; fired actions logged with `summarizeMessage()`. Nothing sent.
  - Live (mirror): subscribes via `onFirmwareMessage` to `switch_pressed`/
    `switch_released` (highlight), `binding_fired` (infer latched -> LED), and
    `midi` (decoded log). Enables the MIDI monitor while live+connected.
- `PedalMap.svelte` gained optional `onPress`/`onRelease` (pointer) and made
  `onSelect` optional; unchanged for the editor's existing click-to-jump use.
- Wired as a third "Simulate" tab in the editor (`App.svelte` editorTab).
- DEFERRED to a follow-up (documented v1 limits): the simulated-TFT-context preview
  (currently LED + log only, no TFT render), a firmware-streamed `led_state` for a
  pixel-faithful Live LED (v1 infers from `binding_fired`), and component-level
  tests (the two pure libs are covered; orchestration relies on typecheck).

**Goal.** Click the on-screen pedal, watch LEDs and the TFT preview react, with no
hardware attached. Doubles as an offline-editing demo and an onboarding hook.

**Today.** `PedalMap.svelte` already renders the 10-switch schematic with
labels/LED colours and click-to-jump. A dormant offline scaffold exists in
`lib/offline-config.ts` (unit-tested, not wired into the app).

Decision (2026-07-12): build BOTH an offline "Model" and a live "Mirror", sharing
one view. They differ only in where switch state comes from. NOTE: the old
`lib/offline-config.ts` scaffold was already dropped (commit fd6412a); the model
is a fresh build.

**Shared view.** `PedalMap.svelte` already takes `colorFor(sw)`, `selected` and
`onSelect` (its props), so both modes just compute a per-switch colour + a
highlighted switch and feed the same component. No new pedal drawing.

**Shared foundation (two pure, unit-tested libs; build first, they carry the risk):**
- `editor/src/lib/led-color.ts`: port of `leds.py::_color_for`, incl. the
  latched-off "dim the on-colour by /4 unless a real non-black off-colour is set"
  rule. Used by BOTH modes to map (binding, latched_on) -> hex. (`switch-colors.ts`
  only has default colours, not this logic.)
- `editor/src/lib/switch-fsm.ts`: faithful port of `bindings.py::SwitchFsm`
  (tap / latched+auto-momentary / momentary / long_press_alt / double_tap ->
  action keys press/release/toggle_on/toggle_off/long_press/double_tap), driven by
  an injected clock. Used by the OFFLINE mode only.

**Offline ("Model").** Click a switch -> `switch-fsm.ts` yields action keys ->
look up `binding.actions[key].messages` -> render a "would send" list via the
existing `summarizeMessage()` (nothing is transmitted). Track latched state in JS,
colour via `led-color.ts` -> `colorFor`. TFT: a simulated context of core fields
(patch name, bank/slot, setlist pos) into the existing TFT preview renderer;
plugin/device fields stay at sample values (documented limit). Timing (long-press /
double-tap) comes from UI gestures (tap vs hold vs double-click) into the clock.

**Live ("Mirror").** Subscribe (via `onFirmwareMessage`, same plumbing as the MIDI
monitor) to events the firmware already emits: `switch_pressed`/`switch_released`
(app.py:223) -> highlight; `binding_fired {switch, action}` (app.py:807) -> infer
latched (toggle_on/off) and colour via the same `led-color.ts`; `patch_switched`
(app.py:383) -> refresh. No firmware change needed for v1 (LED inferred, not
streamed). Inert when disconnected.

**Toggle.** A `PedalSimulator.svelte` wraps `PedalMap` + a Model/Live switch + a
log panel, mounted as a "Simulate" tab in `PatchEditor.svelte` (acts on the open
patch). Both branches set the same `colorFor`/`selected`.

**Risks / guard.** FSM divergence is the trap. Guard: `switch-fsm.test.ts` mirrors
the SAME scenarios as `tools/fsm_test.py` (cross-referenced; keep in sync), and
`led-color.test.ts` mirrors `_color_for` cases. Do NOT modify `fsm_test.py` (it's
green and drives the real pin-level FSM); mirror its cases in JS instead. Scope TFT
sim to the preview's fields, not a pixel-perfect device emulation.

**Sequencing.** (1) foundation libs + tests [parallelizable, independent], (2)
`PedalSimulator` offline + Simulate tab, (3) live branch (cheap, reuses monitor
events), (4) optional later: a firmware `led_state`/TFT-context event for a
pixel-faithful live mirror (v1 infers instead).

**Effort.** Medium. Editor-only for v1, zero firmware change. Largest sub-task is
the faithful FSM port.

---

## Feature 3: MIDI monitor (editor)  [IMPLEMENTED 2026-07-12, pending HW verify]

Shipped in this arc (all tests green, not yet hardware-verified):
- Firmware: `SET_MIDI_MONITOR {on}` protocol command (`protocol.py`) toggling
  `Captain.set_midi_monitor` (`app.py`), which wires a `tx_monitor` tap on
  `MidiEngine._tx` (`midi.py`) for outbound and emits inbound from the RX loop.
  Both go out as `EVENT event="midi" dir=in/out [port] raw=[bytes]`. Gated off by
  default; only emits while the panel is open. Tests: `protocol_test.py` (+2).
- Editor: `lib/midi-decode.ts` (pure decoder, 20 unit tests in
  `midi-decode.test.ts`), `components/MidiMonitor.svelte` (filter/pause/clear,
  1000-row cap), `cmd.setMidiMonitor` in `protocol.ts`, nav item + page in
  `App.svelte` under System.
- Remaining: verify on the live rig (COM4 + Kemper) that in/out decode and the
  open/close gating behave, then it powers the simulator's live mode (Feature 2).

**Goal.** A live rx/tx decode panel in the editor. Invaluable for debugging,
plugin authoring, MIDI Learn, and it feeds the simulator's live mode.

**Today.** The protocol already has an event channel: `protocol.emit_event(event,
**fields)` sends `{"type": "EVENT", ...}` line-JSON on the data CDC, already used
for `switch_pressed`/`switch_released`. MIDI counters are in STATS. `midi.py` is
the single choke point for TX (`send_cc/pc/note/sysex`) and RX (`poll`).

**Design.**
- Firmware: emit MIDI events over the existing EVENT channel. On RX (`midi.poll`
  in `_tick_body`, `app.py:229`) and on each TX in `midi.py`, call
  `emit_event("midi_in"/"midi_out", port=..., channel=..., status=..., data=...)`.
  **Gate it behind an opt-in flag** (a protocol command to enable/disable the
  monitor, default off) because emitting an EVENT per MIDI byte-group during a
  rig-change burst adds loop work and CDC traffic exactly when the loop is busy.
  Only emit while the editor's monitor panel is open.
- Consider a lightweight rate cap / coalescing so a SYSEX flood cannot swamp the
  CDC. Decoding (CC name, PC number, SYSEX hex) happens editor-side.
- Editor: a monitor panel subscribing to the EVENT stream (the editor already
  consumes EVENTs for the live mirror). Show timestamp, direction, decoded
  message; add a pause/clear and a filter.

**Risks.** Performance under burst. Mitigate with the opt-in gate + coalescing.
Do not let monitor traffic interfere with the deferred-render MIDI drain logic.

**Testing.** `protocol_test.py`: enable flag -> midi_in/out EVENTs emitted;
disabled -> none. Editor: decode unit tests (CC/PC/note/SYSEX -> human string).

**Effort.** Small-to-medium. Build this before/with the simulator: its live EVENT
stream is what powers the simulator's "mirror the real pedal" mode.

---

## Feature 4: Pull device data in (Kemper rig names + colours)  [IMPLEMENTED 2026-07-12, needs HW verify]

Shipped (compiles, battery + editor green), but HARDWARE-DEPENDENT and unverified.
Firmware: `plugins/kemper.py` gained `_RIG_INFO` cache, `request_rig_info(app)` (a
$41 string-param request, page 0x00/addr 0x01, reusing existing Kemper framing under
the beacon), and `get_rig_info(app, request=True)` -> `{name, rig, color, fresh}`; the
existing $03 string-response handler seeds the cache. `protocol.py` `GET_RIG_INFO` is
pure routing to the active plugin's `get_rig_info` (no Kemper specifics in core, no
app-class change). Editor: `cmd.getRigInfo`, new `DeviceImport.svelte` ("read current
rig name -> use as patch name", optional position-colour), wired into the editor
Switches tab gated to `activeKind === "kemper_player"`. ASSUMPTIONS TO VERIFY ON HW:
(1) whether an on-demand $41 string request is answered while the beacon is active
(falls back to the last broadcast name if not); (2) Player has NO real per-rig colour
param, so `color` is the best-effort position chart (RIG_COLORS), not device truth;
(3) capture the request frame once with `device.kemper.debug`. Bulk scanning omitted
by design (reading rig N requires selecting it, which changes the sound). NOTE: this
does NOT yet auto-fill the 25-bank colour chart TODO; it is single-rig name import.



**Goal.** Read the Kemper's real rig names and colours over SYSEX to auto-name
patches and fill LED/bank colours, instead of hand-mapping them.

**Today.** `plugins/kemper.py` (864 lines) already runs the bidirectional beacon
(function $7E, 10 s lease, re-sent every 5 s; see the bidirectional-beacon memory)
and mirrors device state via `on_midi_in`. `RIG_COLORS` maps only ~15 of 125 rigs;
`device.json` `preset_navigation.bank_colors` uses a non-official cyclic palette.
There is a parked TODO to hand-enter a 125-rig / 25-bank colour CSV.

**Design.**
- Use the Kemper SYSEX string-parameter requests (rig name, and rig/bank colour
  where the Profiler exposes it) under the active beacon subscription to read the
  current rig's name/colour, and optionally sweep to cache names for auto-follow.
- Feed rig name into the display context (it already overrides `patch_name` per
  the rig-name-overrides memory) and into an editor action: "Import names/colours
  from device" that fills patch names and LED/bank colours from live reads.
- This **retires the parked colour-chart TODO**: read colours from the device
  rather than transcribing a chart image into a CSV. Prefer live data; keep a
  static fallback table only for rigs the device cannot report.

**Risks.** Kemper-specific (stays entirely in `plugins/kemper.py`, honoring the
core/plugin separation rule). SYSEX string requests must respect the beacon lease
and not flood the loop. Confirm which colour fields the Player actually reports vs
the full Profiler (Player is USB-only and feature-reduced).

**Testing.** Extend `kemper_plugin_test.py`: simulated SYSEX name/colour responses
parse into the context; import action maps reads to patch fields. Hardware verify
against the live rig (HW test rig available per memory).

**Effort.** Medium, plugin-scoped. High payoff: also closes an existing TODO.

---

## Feature 5: Richer TFT  [IMPLEMENTED 2026-07-12 (v1: big-name + big-tuner), needs visual HW check]

Shipped v1 (editor check/build green; rendering is visual so needs an on-pedal look).
Built in parallel with Features 1 and 4. `display.py`: bigger tuner splash (note scale
6->8 centered; wider/thicker deviance bar + center tick; footer now `<< FLAT n` /
`SHARP n >>` / `IN TUNE`, green when in tune) driven by the same generic tuner context
fields, in-place update preserved. `TftLayout.svelte`: label Size max raised 6->12
(preview already scales; `render()` already applies scale with no upper cap), so a
patch-name label can be genuinely large. Backward compatible. Per-patch image
deliberately OUT of scope (flash/decode risk). Human check: enter tuner (big note,
clear bar, FLAT/SHARP/IN TUNE footer); set a name label to size 8-10 and confirm
on-device matches the editor preview.



**Goal.** Make the 240x240 screen match what players expect: a large patch-name
mode, a large tuner, and optionally a per-patch image.

**Today.** `display.py` (`render(context, layout)` + marquee `tick`) renders
user-defined labels from the layout. Renders are the loop's slow blocking op
(deferred-render machinery). Tuner already updates the splash in place.

**Design.**
- Big patch-name / big tuner: new layout presets or a "size: xl" style plus a
  large-tuner render path, driven from the existing layout system so it stays
  user-configurable in the Screen layout editor. Mostly additive to `display.py`
  + the layout schema + editor preview.
- Per-patch image (stretch): requires storing image assets on the pedal (flash
  space is the constraint on the RP2040) and a decode/blit path. Scope this
  carefully: start with a small palette-limited format or a few shared images
  referenced by patches, not arbitrary full-colour bitmaps per patch. Decide
  feasibility before committing (flash budget + render cost).

**Risks.** Render cost and flash space. Any new render path must stay compatible
with the deferred-render/marquee timing so it does not worsen MIDI drain. Image
support may be deferred if the flash/decode budget is too tight; big-name and
big-tuner are the safe wins.

**Testing.** Display renders are hardware/visual (human in the loop per the HW
memory). Add layout-schema unit tests for the new field/size options.

**Effort.** Medium for big-name/big-tuner; image support is a separate, riskier
sub-project (may split out).

---

## Suggested sequencing

Two independent tracks that can run in parallel:

- **Editor track (zero firmware risk):** MIDI monitor (Feature 3) first, because
  its EVENT stream powers the simulator; then the virtual pedal simulator
  (Feature 2).
- **Firmware track:** per-patch expression (Feature 1) -> pull Kemper data
  (Feature 4) -> richer TFT (Feature 5, big-name/big-tuner first, image last or
  deferred).

Rationale: the monitor is the fast confidence-builder and a dependency of the
simulator's live mode; per-patch expression is a clean, well-bounded core change;
pull-Kemper-data also retires a parked TODO; richer TFT's image sub-feature is the
riskiest and is sequenced last so it can be descoped without blocking anything.

## Version / release note

Per the version-alignment rule, any firmware-touching item (1, 3-firmware, 4, 5)
bumps all four version files in lockstep and resyncs `resources/firmware` before a
release build. Editor-only work (2, 3-editor) can ship in the same bump.
