# Tempo dot + auto-momentary hold indicator

Date: 2026-07-12
Status: TEMPO HALF REMOVED 2026-07-12 (Feature A backed out at user request, see
20260712_184946_remove_tempo_feature.md). Feature B (auto-momentary hold
indicator) and the memory/OOM fixes remain in place.
Owner: Danilo

## Update 2026-07-12 (post-flash): memory OOM regression found + fixed

Flashing this + the LED-dim feature on top of 0.4.9 dropped free heap ~33.4k->30.8k, and a
PUT_GLOBAL then wedged the pedal ("loop error: memory allocation failed, allocating 2011 bytes",
PING/PUT_GLOBAL returning None). Root cause: display.render() builds a new displayio.Group while the
old one is still root_group (~2x peak), stacked on apply_global's reindex/expression/LED churn on the
tight RP2040 heap. Fixed in 3 edits, flashed + verified on COM4:
- display.py: `import gc` + `gc.collect()` immediately before `group = displayio.Group()` in render().
- app.py apply_display_layout(): defer render via `_mark_display_dirty()` (was inline `_refresh_display()`)
  so the PUT_GLOBAL ACK goes out first and a render OOM becomes a caught skipped frame, not a wedge.
- app.py apply_global(): `gc.collect()` after the churn, before the deferred layout render.
Verified: 5 rapid PUT_GLOBAL (dim 0/100/50/5/25) all ACK, mem_free stable ~32.0k, GET_MANIFEST all 4
plugins, 3400 loop iters clean. Offline battery still 13 suites + fsm 14/14. Remaining = visual checks
(dot blink / Kemper auto-follow / hold_text on TFT / LED dim look), then bump 0.4.10 + release.

Implemented 2026-07-12. Firmware battery 13/13, fsm_test 14/14 (added is_momentary_active
cases), editor svelte-check 0 errors + vitest 237/237. Files touched:
- Firmware: messages.py (captain_tap_tempo), bindings.py (dispatch + SwitchFsm.is_momentary_active),
  app.py (tempo model: set_tempo/tap_tempo/_apply_patch_tempo, hold-indicator detection in
  _tick_body, context hold_effect init), kemper.py (app.set_tempo on device BPM), display.py
  (set_tempo, tempo_dot filled-circle build + blink in tick).
- Editor: protocol.ts (captain_tap_tempo core msg, Patch.tempo_bpm, Binding.hold_text),
  TftLayout.svelte (hold_effect + tempo_dot core fields + preview dot), PatchEditor.svelte
  (patch tempo_bpm input, per-switch hold_text input shown for latched+auto-momentary).
- Tests: fsm_test.py (+2), manifest-fallback.test.ts (core-msg count 11->12).
Remaining: flash + HW verify (dot blinks/follows Kemper; hold_text shows during auto-momentary
hold), then bump + release. Also still pending HW: the earlier LED dim_percent feature.

Two TFT features requested by the user, with the design choices they confirmed.

## Feature A: Per-patch tempo + metronome dot (auto-follow the device)

**What:** A dot on the TFT flashes on each beat at the current tempo. Tempo can be
set manually per patch, by tapping a switch, and (auto-follow) from the connected
device's own reported tempo.

**Tempo model (single source: `context["bpm"]`, a number).** Priority / who writes it:
1. Device auto-follow: the Kemper already decodes its BPM (kemper.py, page 0x04
   addr 0x00 = value/64). It will write `context["bpm"]` on each report -> the dot
   tracks the amp.
2. Tap tempo: a new core message `captain_tap_tempo` records tap timestamps in the
   app, computes BPM from the median recent interval, and sets `context["bpm"]`.
   (The user pairs it with the plugin tap, e.g. `kemper_tap_tempo`, so the device
   is tapped too; the Quick-setup "Tap tempo" recipe can emit both.)
3. Per-patch manual: a patch may carry `tempo_bpm`; on patch load the app sets
   `context["bpm"]` to it (unless/until the device or a tap overrides).

**The dot (display.py).** New layout field `tempo_dot`. When the layout contains it,
`render()` creates a persistent small filled dot (a bullet label) at the field's
position/size/colour and remembers it. `display.tick(now_ms)` flashes it in place
(no full render): ON for a short flash at the start of each beat period
(`60000/bpm` ms), OFF otherwise. BPM comes from the last-rendered `context["bpm"]`
stashed on the Display. bpm <= 0 or missing -> dot hidden/steady-off. This mirrors
the existing in-place tuner/marquee update so it does not trigger blocking renders.

**Editor.** Patch-level `tempo_bpm` input in PatchEditor; `tempo_dot` offered as a
core field in the Screen-layout field list (+ preview handling); `captain_tap_tempo`
added to the core message dropdown; optional: extend the tap-tempo recipe.

## Feature B: Auto-momentary hold indicator (per-switch hold text)

**What (scoped per the user):** ONLY while a latched + auto-momentary switch is held
PAST the auto-momentary threshold (i.e. it is acting momentarily and will revert on
release) does the TFT show that switch's effect. A quick tap (permanent toggle)
shows nothing; a normal latched-on state shows nothing. The text shown is a NEW,
separate per-switch `hold_text` (not the button label), and only if set.

**Firmware.**
- `bindings.py` `SwitchFsm.is_momentary_active(now_ms, mode)`: True iff
  `mode == "latched"` and `auto_momentary_on_hold` and currently held
  (`not _stable`) and `(now_ms - _press_start_ms) >= auto_momentary_ms`.
- `app.py` `_tick_body`, after the switch poll: compute the hold text of the first
  momentary-active switch (its binding's `hold_text`, if non-empty), and when it
  CHANGES, write `context["hold_effect"]` and mark a refresh. Cleared (empty) on
  release. `_binding_index[sw]` holds the binding dict for the lookup.

**Editor.** A `hold_text` input on the switch/binding editor row; `hold_effect`
offered as a core field in the Screen-layout field list.

## Cross-cutting
- New core TFT fields: `tempo_dot` (special, dot) and `hold_effect` (plain text).
  Both are "core" fields (added alongside patch_name/bank/slot/setlist_pos in the
  editor's core field list and in any firmware default-field advertising).
- New core message: `captain_tap_tempo` (messages.py CORE_MESSAGE_TYPES + editor
  CORE_MESSAGE_TYPES in protocol.ts + dispatch in bindings.py -> app.tap_tempo()).
- Patch schema: optional `tempo_bpm` (number). Binding schema: optional `hold_text`
  (string). Both persist through existing PUT_PATCH.

## Testing / rollout
- Offline: firmware battery (extend fsm/display/app tests where practical - the dot
  blink and TFT are visual, so cover the pure bits: tap-BPM math, is_momentary_active,
  the merge of context["bpm"], hold-effect change detection). Editor svelte-check +
  vitest.
- Hardware (pending pedal power-cycle): flash, verify the dot blinks at a set BPM and
  follows the Kemper, and that holding a latched+auto-momentary boost shows its
  hold_text until release. Then bump + release (0.4.10).

## Note
Pedal is currently unresponsive on USB (awaiting a physical power-cycle). All of this
is offline-testable; hardware flash/verify happens once it re-enumerates.
