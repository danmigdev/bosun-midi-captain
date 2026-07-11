# Plan: Bank preview-then-jump (A) + Tuner exit-on-press (B)

Status: implemented + offline-tested + hardware-verified (autonomous parts); physical-press paths need a human

## Hardware verification (2026-07-11, live pedal COM4 + Kemper)
Flashed firmware/ via push_firmware.py --reboot (33 files incl. navigation.py). Autonomous checks ALL PASS:
- PING -> ACK fw 0.4.2 (boots clean).
- GET_MANIFEST core_messages exposes captain_preview_step/commit/cancel with correct
  schema (scope enum patch/bank, delta) -> editor will see them.
- STATS x2: loop_iters advancing (+119/s), uptime climbing, mem_free flat (no leak) ->
  the new per-tick code (tuner check + preview timeout) runs clean.
- SWITCH_PATCH to 1-2 "CLEAN" while capturing pedal USB-MIDI: emitted CC0=0, CC32=0,
  PC=1 (== (1-1)*5+(2-1)) -> the modified switch_patch (preview-clear at top) still
  drives the Kemper rig change correctly.
- Console (COM3) clean under a 4-cycle SWITCH_PATCH load: no loop error / traceback /
  plugin-hook failure.

STILL NEEDS A HUMAN (rig cannot actuate footswitches): the two press-driven behaviors -
(A) scrolling preview emits no MIDI + PREVIEW badge + commit/timeout loads;
(B) a stomp while the tuner is up dismisses it AND fires the switch action in one press.
Manual steps handed to the user.
Date: 2026-07-11
Source of demand: `docs/` market research (deep-research workflow) + feature analysis.
Two of the highest appeal-to-effort items, both firmware-side (Kemper HW rig available).

## Context / constraints discovered

- Source of truth firmware tree: `firmware/`.
- `editor/src-tauri/resources/firmware/` is a **gitignored** staging bundle synced
  manually at release (currently stale at 0.4.0). We edit `firmware/` only; the
  bundle re-sync is a release step (out of scope here, pre-existing drift).
- Core message schemas: `firmware/lib/captain/messages.py::CORE_MESSAGE_TYPES`,
  mirrored in `editor/src/lib/protocol.ts::CORE_MESSAGE_TYPES`. The set of core
  keys is asserted exactly in `editor/src/lib/manifest-fallback.test.ts` -> update it.
- Device settings flow through GET_GLOBAL/PUT_GLOBAL (device.json). New keys are
  read from `self.device` at use-time with defaults (like `preset_navigation`),
  so no loader change is needed; `apply_global` already refreshes `self.device`.
- Tests mock CircuitPython and import real modules. App-level integration is
  heavy to mock, so preview cursor math is factored into a **pure function**
  (`captain/navigation.py`) and unit-tested in `tools/nav_preview_test.py`.

---

## Feature B: Tuner exit-on-press (smaller, do first)

### Behavior
When the tuner splash is showing, the next footswitch PRESS should:
1. Immediately clear the local tuner context so the TFT returns to the patch
   view without waiting for the device echo (latency win).
2. Tell the target device to leave tuner mode (works for any plugin, not just
   Kemper's own rig-change-exits-tuner path).
3. Still fire the pressed switch's own binding (pass-through) - this is what
   users asked for (dismiss AND act in one stomp), not swallow-first.

Gated by device setting `tuner_exit_on_press` (bool, default true).

### Changes
- `plugin.py`: new optional hook `tuner_off(app)` on the registry, called for
  every plugin that declares it. A throwing plugin doesn't block others.
- `kemper.py`: `tuner_off(app)` -> send CC 31 = 0 (channel = device midi_channel).
- `ampero.py`: `tuner_off(app)` -> CC 60 = 0.
- `line6_helix.py`: `tuner_off(app)` -> CC 68 = 0.
- `generic_midi.py`: no hook (no tuner concept).
- `app.py`:
  - `_tuner_is_on()` helper: context tuner == "on" or kemper_tuner == "on".
  - `_exit_tuner()`: call `plugins.tuner_off(self)`, then
    `update_context({"tuner":"off","kemper_tuner":"off"})`.
  - In `_tick_body` switch loop: capture `tuner_on` once before the loop; on any
    `raw_edge == "press"` while `tuner_on` and the setting is enabled, call
    `_exit_tuner()` (before firing triggers). Idempotent if the switch's own
    action is also a tuner toggle.

### Editor
- `Settings.svelte`: add a "Tuner" toggle for `tuner_exit_on_press` (default on).

---

## Feature A: Bank preview-then-jump

### Behavior
Scroll a preview cursor across patches/banks seeing each on the TFT, WITHOUT
firing on_enter/on_exit or sending any device MIDI. Commit loads the previewed
patch for real; cancel (or inactivity timeout) returns to the current patch.

### New core message types (messages.py + protocol.ts + fallback test)
- `captain_preview_step`  { delta:int(-10..10, def 1), scope:enum["patch","bank"] def "patch" }
- `captain_preview_commit` { }  (no params)
- `captain_preview_cancel` { }  (no params)

These appear automatically in the binding message dropdown (schema-driven), so
a user maps them to switches (e.g. up/down to step, a switch to commit).

### App state + methods (`app.py`)
- `self._preview = None` normally. Active shape:
  `{ "bank":b, "slot":s, "until_ms":t, "saved_context":{...} }`.
- `preview_step(delta, scope)`:
  - `order = navigation.patch_order(self.patches.list())` (sorted (bank,slot)).
  - Determine start index: current preview target if active, else current patch.
  - `idx = navigation.step_index(order, start, delta, scope)`.
  - On first entry snapshot `dict(self.display_context)` into saved_context.
  - Set `self._preview` target; read target patch from store; write
    patch_name/bank/slot into context; set `preview="on"`; call optional plugin
    hook `on_preview(app, bank, slot)` (Kemper fills rig fields, no MIDI);
    `_mark_display_dirty()`. Never touches current_* / LEDs / MIDI.
  - `until_ms = now + preview.timeout_ms` (default 1500).
- `preview_commit()`: target=(bank,slot); `self._preview=None`;
  `switch_patch(bank, slot, source="binding")` (fires on_exit->on_enter).
- `preview_cancel()`: restore `saved_context`, `self._preview=None`,
  `_mark_display_dirty()`.
- `_tick_body`: if `self._preview` and `now >= until_ms`, resolve per
  `device.preview.on_timeout` ("commit"|"cancel", default "commit").

### Pure helper (`captain/navigation.py`) - unit tested
- `patch_order(patch_list) -> [(bank,slot), ...]` sorted.
- `step_index(order, start, delta, scope) -> (bank, slot)`:
  - scope "patch": index of start in order, +delta wrap, return order[idx].
  - scope "bank": move delta whole banks from start's bank (wrap over the sorted
    unique banks), keep slot if present in target bank else nearest lower/first.

### Display (`display.py`)
- Minimal preview badge: when `context.get("preview") == "on"`, draw a small
  "PREVIEW" tag (bottom center) over the normal layout so the user knows the
  shown patch is not yet loaded. Cheap; only added when previewing.

### Plugin hook
- `plugin.py`: registry `on_preview(app, bank, slot)` -> plugins that declare it.
- `kemper.py`: `on_preview` computes rig/bank/rig_in_bank from (bank,slot) and
  writes them to context WITHOUT sending MIDI (mirrors `update_context`).

### Editor
- `messages.py` + `protocol.ts`: add the 3 message schemas.
- `manifest-fallback.test.ts`: extend expected core key list.
- `Settings.svelte`: "Preset preview" section: `preview.timeout_ms` (number),
  `preview.on_timeout` (select commit/cancel).

---

## Files touched (firmware source of truth)
- firmware/lib/captain/messages.py        (A: 3 msg types)
- firmware/lib/captain/navigation.py       (A: NEW pure helper)
- firmware/lib/captain/app.py              (A + B)
- firmware/lib/captain/bindings.py         (A: dispatch 3 msg types)
- firmware/lib/captain/display.py          (A: preview badge)
- firmware/lib/captain/plugin.py           (A: on_preview, B: tuner_off)
- firmware/lib/plugins/kemper.py           (A: on_preview, B: tuner_off)
- firmware/lib/plugins/ampero.py           (B: tuner_off)
- firmware/lib/plugins/line6_helix.py      (B: tuner_off)

## Editor
- editor/src/lib/protocol.ts               (3 core msg schemas)
- editor/src/lib/manifest-fallback.test.ts (expected keys)
- editor/src/components/Settings.svelte    (tuner + preview settings)

## Tests
- tools/nav_preview_test.py                (NEW: navigation.py pure logic)
- tools/run_all_tests.py                   (register the new test)

## Verification
- Host: `python tools/nav_preview_test.py`, `python tools/fsm_test.py`,
  editor `npm test` (fallback test).
- Hardware (Kemper rig): flash, bind preview step/commit + confirm no MIDI
  fires while previewing (mido capture), tuner exit-on-press dismisses + acts.

## Out of scope (noted for later)
- Preview LEDs (leave current patch LEDs during preview).
- Re-sync of the stale resources/firmware bundle (release step).
- Editor "Preset preview" convenience binder (users bind via normal message dropdown).
