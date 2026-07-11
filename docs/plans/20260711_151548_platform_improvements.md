# Platform improvements sweep (0.4.0)

Branch: `feature/scrolling-text-tft` (0.4.0 already bumped for the scrolling-text
work; this sweep rides the same 0.4.0 release).

Big multi-stream improvement pass agreed with the user ("procedi con tutto, vai
anche in parallelo"). Streams are split so they touch disjoint files and can run
in parallel. Owners in brackets.

## Stream 1 - Expression pedals [ME / firmware core]
The two ADC jacks (`board.EXP1_ADC=GP27`, `EXP2_ADC=GP28`) were dead. Wire them.

- New `firmware/lib/captain/expression.py`: `ExpressionArray` owns up to 2
  `analogio.AnalogIn` (guarded import so a board without it still boots).
  Per jack: read 16-bit, apply calibration `{min,max}` + `invert` + deadband,
  produce 0..127. `poll(now_ms)` returns `[(jack, value127), ...]` only for jacks
  whose mapped value CHANGED (throttled ~10 ms, small deadband to avoid MIDI flood).
- device.json `expression` entry schema (extended, back-compat):
  `{jack:1, enabled:false, invert:false, calibration:{min,max}, curve:"linear",
    message:{type,...}}` where `message` is a template; the live 0..127 is
  substituted into its `value` field before dispatch.
  `message` examples: `{"type":"cc","channel":1,"cc":11,"value":0}` or
  `{"type":"kemper_wah","channel":1,"value":0}` or `kemper_volume`/`kemper_morph`.
- `BindingRunner.run_message(msg)` public wrapper over `_dispatch` (additive).
- app.py: construct `ExpressionArray` from `device.expression`; in `_tick_body`
  poll it and dispatch changed values via runner (cc) / plugins (plugin msgs).
  Rebuild on `apply_global`. Expose live raw+value in `stats()` under
  `expression:[{jack,raw,value}]` so the editor can show a live bar + calibrate.
- Default `enabled:false` so nothing changes until the user configures a jack.

## Stream 2 - Generalized tuner UI [ME / firmware]
Tuner render was hard-gated on `kemper_tuner`. Generalize so any plugin can drive it.
- display.py `render`: trigger tuner when `context.get("tuner")=="on"` OR legacy
  `kemper_tuner=="on"`. `_render_tuner` reads generic `tuner_note`/`tuner_deviance`
  with fallback to `kemper_tuner_note`/`kemper_tuner_deviance`; degrades gracefully
  (note "-", centred needle) when a plugin has no pitch feedback.
- ampero.py: publish generic `tuner` on/off from `ampero_tuner` so Ampero gets a
  tuner screen (text-only, no needle - Ampero sends no pitch data).
- kemper.py: also publish generic `tuner`/`tuner_note`/`tuner_deviance` aliases
  next to the kemper_* ones (keep kemper_* for existing layouts).

## Stream 3 - Orphan cleanup [ME / firmware]
- `on_exit`: fire the leaving patch's `on_exit` chain in `switch_patch` right
  before loading the target, gated the same as `on_enter` (`fire_on_enter`, not an
  echo). Editor gets on_exit editing (Stream 5).
- profile `color`: `create_profile(..., color=None)` writes it into manifest.json;
  wire through protocol CREATE_PROFILE. Editor renders a colour chip (Stream 5).
- `linked_to`: NOT an orphan - it is editor-owned link metadata passed through
  firmware. Leave as-is; documented.

## Stream 4 - New device plugins [AGENT B / new files only]
New self-contained files under `firmware/lib/plugins/`, modelled on ampero.py.
Do NOT edit kemper.py/ampero.py/core.
- `generic_midi.py`: kind "generic_midi"; convenience message types
  (`program_change_bank` = CC0 MSB + CC32 LSB + PC in one; `cc_toggle` on/off);
  a sensible DEFAULT_LAYOUT (patch_name/bank/slot). Lets any MIDI device get a
  profile out of the box.
- `line6_helix.py`: kind "line6_helix"; snapshot select, setlist/preset
  (CC0/32 + PC), footswitch CC, tap tempo, tuner (publish generic `tuner`).
  Cite the Line 6 Helix MIDI reference in comments.
- Each must `py_compile` clean and follow the plugin contract (NAME, VERSION,
  LABEL, MESSAGE_TYPES, dispatch, optional update_context/DEFAULT_LAYOUT/
  TFT_FIELDS/CONFIG_SCHEMA).

## Stream 5 - Editor [AGENT A / editor/* only]
Priority order:
1. Manifest fallback: if GET_MANIFEST fails all retries, fall back to a
   core-messages-only schema so Patches/Editor stay usable (don't hard-wall).
2. Expression config UI in Settings matching Stream 1 schema: per-jack enable,
   invert, calibration (live bar + "capture min/max" from STATS `expression`),
   message-target picker (cc / plugin continuous msg). This EXPOSES Stream 1.
3. on_exit editing in PatchEditor (mirror on_enter).
4. Patch search/filter box in PatchesGrid.
5. Profile colour chip (set on create, show in ProfilePicker/grid).
6. Strip leftover temporary `debug_log` instrumentation in installer.rs.
7. Stretch: offline/disconnected editing (in-memory config from an imported
   backup, "push to device" on connect). Land only if it does NOT regress the
   connected path; otherwise scaffold + document as follow-up.
Keep `npm run check` and `npm test` green.

## Stream 6 - Firmware tests + CI [AGENT C / new files only]
- Follow existing `tools/*_test.py` mocking pattern. Fill the gap: unit tests for
  the MIDI parser (`midi.py` MidiParser: running status, realtime interleave,
  SYSEX collect/abort) and the plugin bank<->rig remap math.
- `.github/workflows/ci.yml`: on push/PR run editor `npm ci && npm run check &&
  npm test`, plus `py_compile` over firmware and the `tools/*_test.py` suite.
- Do NOT edit firmware core or plugins.

## Version
Everything ships under 0.4.0 (already bumped in the 4 files for scrolling text).
No further bump this sweep.

## Verify
- Firmware: `py_compile` all of firmware/ + tools/*_test.py green.
- Editor: `npm run check` + `npm test` green.
- Hardware: expression + tuner + new plugins NOT yet hardware-verified (no device
  this session) - flag in the release notes.

## Status
- [x] S1 expression firmware (expression.py + app wiring + stats + config schema); compiles
- [x] S2 tuner generalization (display + kemper aliases + ampero); bilateral tests updated, green
- [x] S3 orphan cleanup (on_exit fires in switch_patch; profile color via create_profile/CREATE_PROFILE)
- [x] S4 new plugins: generic_midi.py + line6_helix.py (Helix FS mapping corrected against
      helixhelp.com spec: FS1-5=CC49-53, FS7-11=CC54-58, FS6/CC59/CC60 excluded); smoke-tested
- [ ] S5 editor (agent a948bc2eff44020cd running)
- [x] S6 tests + CI: midi_parser_test.py 36/36; ci.yml; fixed stale protocol_test iter_manifest fixture

## Testing sweep (follow-up user request: "test a tappetto" + hours-of-use robustness)
New firmware suites (tools/, offline, mocked CircuitPython):
- `expression_test.py` (29) - calibration/clamp/invert/curves, deadband, throttle,
  reconfigure-releases-pins, inert/disabled jacks, read-error robustness, no-analogio.
- `plugins_test.py` (14) - generic_midi + line6_helix dispatch, Helix FS gap; plus a
  CROSS-PLUGIN self-consistency loop over all 4 plugins (summary placeholders resolve,
  enum defaults valid, every message type dispatchable with its own defaults).
- `soak_test.py` (12) - ENDURANCE: builds the real Captain + real plugins, seeds a
  Kemper profile, drives 60k iterations (~20 sim min; `--long` = 600k ~3.3h) of
  footswitch/MIDI/expression/protocol workload on a fast-forwarded clock, then asserts
  ZERO escaped exceptions and NO leaks (live object count flat; every long-lived
  dict/list/set - bank trackers, marquee, dirty set, kemper block/published caches,
  binding index - stays bounded).
- `run_all_tests.py` now runs ALL 10 suites (was missing protocol/midi_parser); CI
  (`ci.yml`) calls the aggregator so every suite runs on GitHub push/PR.

Editor (vitest): agent added `protocol-endurance.test.ts` (connect/disconnect churn,
100k-message flux, timeout cleanup, debouncer hygiene - all assert bookkeeping returns
to baseline) + `firmware-update.test.ts`; a non-invasive `__getInternalSizes()` getter
in protocol.ts. No leaks found.

## Final verification
- Firmware battery: `python tools/run_all_tests.py` -> BATTERY PASSED (10 suites):
  fsm 12, midi_parser 36, protocol 38, expression 29, plugins 14, kemper_plugin 20,
  kemper_bank_change 6, bilateral 64, firmware_stability 15, soak 12. compileall OK.
- Editor: `npm run check` 0 errors (150 files); `npm test` 149 passed (13 files);
  `cargo check` compiles (2 pre-existing unrelated warnings in serial.rs).
- Versions aligned at 0.4.0 across the 4 files. README updated (expression, devices).
- NOT hardware-verified this session (no device): expression jacks, tuner screen,
  the two new device plugins on real gear.
