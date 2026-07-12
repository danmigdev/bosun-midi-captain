# Remove the tempo feature completely

Date: 2026-07-12
Status: DONE. Firmware battery 13/13, fsm_test 14/14, editor svelte-check 0
errors, vitest 237/237. Hold indicator + LED dim + memory fixes untouched.
Owner: Danilo

## Goal

User asked to remove the tempo functionality entirely ("togli del tutto la
funzionalita del tempo"). The tempo feature was added uncommitted on top of
0.4.9, bundled in the same working tree with two OTHER features that must be
KEPT: the auto-momentary hold indicator (`hold_effect`/`hold_text`) and the LED
`dim` setting, plus a batch of RP2040 memory (gc/import-order/deferred-render)
fixes. This is a surgical removal of tempo only.

## Scope decision (what is tempo vs pre-existing)

Confirmed via `git diff HEAD`:
- REMOVE (new tempo feature): core `captain_tap_tempo` message, per-patch
  `tempo_bpm`, TFT `tempo_dot` field + metronome dot, app `set_tempo`/`tap_tempo`/
  `_apply_patch_tempo`, `bpm` in display_context/status telemetry, the
  `on_tempo` plugin hook, kemper auto-follow (`_request_bpm`, `on_tempo`,
  `app.set_tempo(...)` call, link-up + rig-settle bpm requests), and the tempo
  tests.
- KEEP (pre-existing device features, committed before this work): plugin
  `kemper_tap_tempo`/`kemper_set_tempo`/`ampero_*`/`helix_tap_tempo` messages,
  the `kemper_bpm` TFT readout + page 0x04 decode (`_ADDR_BPM`,
  `_PAGE_RIG_PARAMETERS` still used at the decode site), the `tap_tempo` recipe,
  the `bpm` help-text, README "Tap tempo" recipe mentions.
- KEEP (other bundled features): hold indicator, LED dim, memory fixes,
  ColorField refactor.

## Files / edits

Firmware:
- app.py: drop `_tap_times`, `set_tempo`/`tap_tempo`/`_apply_patch_tempo` +
  its call in the patch-change path, `bpm` in status. Keep `_last_hold_effect`.
- display.py: drop `_TEMPO_FLASH_MS`, tempo-dot state init, render context-bpm
  reset + `tempo_dot` build branch, `set_tempo`/`_build_tempo_dot`/
  `_tick_tempo_dot`, the tick() call + docstring mention. Keep `import gc`.
- bindings.py: drop `captain_tap_tempo` dispatch. Keep `is_momentary_active`.
- messages.py: drop `captain_tap_tempo`.
- plugin.py: drop `on_tempo`. Keep import-order/gc fixes.
- plugins/kemper.py: drop `_request_bpm`, `on_tempo`, link-up seed, rig-settle
  request, and the `app.set_tempo(...)` auto-follow call (keep the `kemper_bpm`
  publish + decode).

Editor:
- protocol.ts: drop `tempo_bpm` and `captain_tap_tempo`. Keep `hold_text`.
- PatchEditor.svelte: drop `clearTempo`, the Tempo (BPM) input, its CSS. Keep
  hold_text input + ColorField.
- TftLayout.svelte: drop `tempo_dot` field option + preview branch. Keep
  `hold_effect`.
- manifest-fallback.test.ts: drop `captain_tap_tempo`; count 12 -> 11.

Tests:
- tools/kemper_plugin_test.py: remove the 5 tempo tests + FakeMidi.cc.
- tools/fsm_test.py: no change (only hold-indicator tests there).

## Verify
- Firmware offline battery + fsm_test.
- Editor svelte-check + vitest.
