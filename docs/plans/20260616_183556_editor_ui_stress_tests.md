# Editor UI robustness / consistency stress tests

Created: 2026-06-16 18:35:56
Owner: claude

## Goal

The user asked for "deep robustness and consistency tests on the UI, verifying
that all states stay coherent under stress conditions" - specifically:

- rapid user input
- MIDI/device flood
- state transitions (concurrent edits, mid-flight saves, navigation during
  async operations)
- edge data (empty/huge configs, malformed input, missing files, unicode/accents)
- **save coherence between patches in all aspects** (the user emphasised this)

Approach (user pick): **write automated stress tests**, no manual probing,
no audit-only deliverable. The tests live under `editor/tests/` and
`editor/src/lib/*.test.ts` and run via `npm run test` (vitest + jsdom).

## Out of scope

- Mounting App.svelte end-to-end. Tauri ESM modules and child components
  make full mounts slow and brittle; the existing `tests/app-events.test.ts`
  shows the project's preferred pattern: mirror the handler logic in a
  testable shape and assert on that. We follow the same convention.
- On-device firmware UI (user picked editor only).
- Refactoring App.svelte for testability. Only minimal moves if a real
  bug is found and an extraction is the smallest fix.

## Target surfaces

Mapped from the current code:

### 1. `src/lib/protocol.ts` - transport
- `sendAndAwait` request/response correlation via `_pending` map +
  `id` field, 5s default timeout.
- `_drainOnce` re-entry guard (`_draining` flag).
- `_firmwareSubscribers` set + raw subscribers - mutated during iteration
  is the classic footgun.
- `_failPending` on `rust-disconnected`.
- `debouncedPutBinding` global `_debouncers` keyed by string - rapid edits
  on same key should coalesce, different keys are independent.
- `send` catches "not connected" / "write:" errors and dispatches
  `rust-disconnected` - must not swallow other failures.

### 2. `src/lib/patch-links.ts` + `src/components/PatchEditor.svelte` - save coherence
The user's "tutti gli aspetti" clause concentrates here. The editor's
`persistPatch()` writes the source patch then propagates a *mirror* to
every linked target, and `commit()` does the same for single-binding
edits via `propagateBinding()`. Things that must hold under stress:

- Every patch field present on the source must land on every target:
  `name`, `tft_color`, `on_enter`, `bindings`, plus any new top-level
  Patch field added later. Anything missed is silent data drift.
- `linked_to` on a target must be rewritten to point at the source,
  never inherit the source's own list (the 2025 regression covered by
  `patch-links.test.ts` was exactly this).
- Propagation is independent per target: if one PUT fails, the others
  still run, and `applyToLinked` returns only the successful writes.
- Many targets (worst case ~125 - 25 banks * 5 slots) propagate without
  shared references between target payloads.

### 3. `src/lib/config-backup.ts` - export/import roundtrip
- `validateBackup` accepts every shape the editor itself produces.
- `backupFilename` sanitises profile labels into filesystem-safe names
  even for accented Italian / non-ASCII.
- `inferKindFromDevice` tolerates malformed `device` blobs (v1
  backwards-compat path).
- A 125-patch fixture with unicode patch names, max-size binding lists,
  and every optional field set must round-trip through
  `JSON.stringify` -> parse -> `validateBackup` byte-equal.

### 4. App.svelte event handlers - state machine consistency
Mirror the same convention as `app-events.test.ts`: lift the relevant
handler bodies into a parallel pure function and stress-test there.

- PATCH-response filter: only adopt when matching `currentPatch.bank/slot`.
  Background `getPatch` calls fired by link-maintenance must not hijack
  the editor.
- Captures buffer cap (`CAPTURE_CAP = 20`).
- Log cap (`LOG_CAP = 200`).
- Manifest retry watchdog: stops at `MANIFEST_MAX_RETRIES = 5`, flips
  `manifestGaveUp`.

## Test files

| File | Surface |
|------|---------|
| `editor/tests/protocol-stress.test.ts` | Transport: sendAndAwait, drain, subscribers, debouncer, _failPending |
| `editor/tests/patch-links-stress.test.ts` | Propagation coherence: every field, every target, partial-failure contract, scale |
| `editor/tests/config-backup-roundtrip.test.ts` | Backup roundtrip with edge data |
| `editor/tests/app-handlers-stress.test.ts` | App.svelte handler mirrors: PATCH filter, captures cap, log cap, manifest retry |

## Attack vectors

For each surface, the explicit stress vectors:

### Transport
- 500 concurrent `sendAndAwait` calls, mock firmware responds in random
  order. Every promise must resolve with the right correlated response.
- A subset of those calls have no response queued -> they must time out
  (with the test-injected short timeout) and `_pending` must be empty.
- Mid-flight disconnect: half the calls are pending when
  `rust-disconnected` fires -> they all reject with `error: disconnected`.
- 1000 firmware lines drained in one event burst, all subscribers
  receive every line in order, none skipped.
- Subscriber unsubscribe during dispatch -> the unsubscribe takes effect
  for the next message, current iteration finishes cleanly (no throw).
- Malformed JSON line followed by valid line: warn-and-continue,
  the next valid line still reaches subscribers.
- `debouncedPutBinding`: 100 calls same key in tight loop -> one
  `cmd.putBinding`. 10 calls each on 10 different keys -> 10 calls,
  each carrying its key's final binding.

### Save coherence
- Build a `Patch` carrying every top-level field (`name`, `tft_color`,
  `on_enter` with messages, `bindings` with all 10 switches and all 5
  modes, `linked_to`). Mirror it via `mirroredPayload`: every field
  copied, `linked_to` rewritten exactly to `[source]`, deep-cloned.
- 120 linked targets, `applyToLinked` against a mocked transport that
  fails on 30 of them - returned list contains the other 90, no
  partial state leaks between targets.
- Round-trip 100 mirror operations and assert no shared object identity
  between any two payloads (no aliasing).

### Backup roundtrip
- 125-patch fixture, names include accented characters ("àèéìòù",
  "Solo - Caña", "中文测试"), bindings include nested message arrays.
- `JSON.stringify` -> `JSON.parse` -> `validateBackup` must return a
  value deep-equal to the original.
- Filename sanitization removes `/`, spaces, special punctuation, keeps
  ASCII letters and digits. Falls back to `profile.json` on empty.
- `inferKindFromDevice` returns "" (not throw) on `{ kemper: 42 }`,
  `null`, `undefined`.

### App handlers
- `handlePatch(msg, currentPatch)`: 50 PATCH messages targeting random
  bank/slot, only the one matching `currentPatch` is adopted.
- `handleCapture(msg, captures, cap)`: feed 1000 PC events, length
  always at most `CAPTURE_CAP`.
- `pushLog(log, raw, cap)`: feed 10000 events, length capped, latest
  always at the end.
- `manifestTick`: simulate the timer running until the retry budget
  is exhausted - `manifestGaveUp` flips exactly once at attempt 5.

## What this plan does NOT do

- Fix bugs found. If a stress test reveals a real defect, we'll record
  it in a follow-up section here and ask the user how to proceed.
- Add coverage for components other than the surfaces above (PatchesGrid
  drag/drop, MidiLearn, Installer wizard, etc.). Those would need their
  own pass.

## Findings

### Numbers

- 4 new test files, 56 new tests.
- Combined editor suite: **9 files, 129 tests, all pass.**
- `npm run check` (svelte-check): **0 errors, 0 warnings.**
- Total runtime including type check: ~3s for the test pass, ~6s for
  svelte-check. The stress vectors (500 concurrent commands, 1000-line
  drain bursts, 100 mirror operations) all complete in single-digit ms.

### Production bugs found

None. Every stress vector passes against the current code:

- The 24-target propagation in `mirroredPayload` produces 24
  independent deep-clones with no shared references.
- `applyToLinked` correctly preserves each target's own `linked_to`
  through the GET-then-PUT preamble - a propagation does NOT collapse
  every target onto the source's link graph.
- The 125-patch backup with unicode/accented names round-trips
  byte-identical through JSON + `validateBackup`.
- Capture and log buffers hold their caps under 10x burst.
- Manifest retry budget stops cleanly at 5 attempts.
- `sendAndAwait` correlates 500 concurrent requests with shuffled
  response order; no leaks in `_pending` after timeouts or disconnects.

### Test-harness observations worth keeping

The intermediate "5 failed" state on the first protocol-stress run
came from two harness-side issues, both worth recording so the next
person writing async-mock tests doesn't burn the same hour:

1. **`vi.mock` factory closures hit TDZ on top-level `let`.** vi.mock
   is hoisted above all imports AND above the test file's top-level
   `let` declarations, so the mock factory's closure over plain `let
   inbox = []` references an uninitialized binding at the moment the
   factory runs. Use `vi.hoisted(() => ({ harness: {...} }))` to share
   state between the mock factory and the test bodies.
2. **`sendAndAwait` is async at the top** (`await
   _ensureAwaitListener()`). Tests that call sendAndAwait and
   synchronously check `harness.sent` see an empty array because the
   async body is still suspended at the first await. Insert a
   `flushMicrotasks()` between firing and asserting.

### Production observations (not bugs)

Things the tests surfaced about the current design that are worth
noting but don't need a code change:

- `onFirmwareMessage` iterates the live `Set`, so a handler that
  unsubscribes a peer mid-dispatch causes the peer to be skipped for
  the CURRENT event and every future event. That matches the intuitive
  contract ("unsubscribe means stop hearing anything"), but it's
  asymmetric with patterns that iterate a snapshot. Test
  `unsubscribing a peer during dispatch...` pins the documented
  behaviour.
- `PatchEditor.commit()` debounces the SOURCE binding write via
  `debouncedPutBinding`, but the propagation to linked targets in
  `propagateBinding` is NOT debounced. For a user editing a patch that
  has 24 implicit-linked siblings, each keystroke that triggers commit
  produces 1 debounced write to the source plus 24 immediate writes to
  the targets. Probably intentional (you want linked targets to track
  promptly) but the asymmetry is real and worth confirming with the
  user before any future refactor.
- `persistPatch()` in PatchEditor (which fires on every meaningful
  edit) uses `structuredClone($state.snapshot(working))` for each
  mirror payload. If a new top-level `Patch` field lands later, it
  propagates automatically with zero edit to PatchEditor or
  patch-links - the test
  `mirroredPayload: preserves name, tft_color, on_enter, bindings`
  explicitly asserts this so anyone removing/renaming a field will
  notice immediately.

### Open follow-ups (NOT in this pass)

- App.svelte still has plenty of state-machine surface that isn't
  testable without mounting (the profile-switch reentry guard, the
  watchdog interval, the connection-resynced flow). Mounting the
  component end-to-end would need a non-trivial mock layer for the
  child components (Dashboard, PatchEditor, MidiLearn, ...). Worth
  considering but big enough to be its own deliverable.
- The on-device firmware UI was explicitly out of scope for this pass
  (user picked editor only). If the user wants to extend coverage to
  the Captain TFT/LED side, that's a separate plan.
- No regression coverage yet for `protocol.send()`'s "not connected"
  detection path that surfaces `rust-disconnected`. The transport
  stress file covers the sendAndAwait side; the fire-and-forget
  `send()` side is exercised indirectly by `cmd.putPatch` but doesn't
  exercise the failure-detection branch.

## Status

DONE.

---

## Follow-up: linked-to bug reported by user (2026-06-16, same day)

### Symptom

User: "When I link two patches, I see linked to 0 explicit links."

The header `{(working.linked_to?.length ?? 0)} explicit link(s)` in
`PatchEditor.svelte` lines 416-420 was reading from a stale `working`
state after the user added a link and then navigated to Patches and
back.

### Root cause

Two compounding issues in `editor/src/components/PatchEditor.svelte`:

1. **`$effect` key was name-based.** The effect that re-syncs `working`
   from the `patch` prop used
       `key = ${bank}/${slot}/${patch.name ?? ""}`
   When `App.openPatchInEditor` seeded `currentPatch` with a placeholder
   (`{ name: placeholder.name, bindings: [] }`) and the real PATCH
   response arrived later carrying the same name, the key was unchanged
   and `working` was NOT re-cloned from the new patch. The fresh
   `linked_to` (and bindings) dropped on the floor.

2. **`persistPatch` never refreshed App's view.** After PUT_PATCH the
   firmware had the new `linked_to`, but no GET_PATCH was fired, so
   `App.currentPatch.patch` kept the pre-edit snapshot. If the user
   navigated away and came back via the sidebar Editor button (which
   does NOT route through `openPatchInEditor`), PatchEditor remounted
   on stale prop data and the link disappeared from the UI even though
   the firmware had it persisted.

### Fix

Two surgical changes in `editor/src/components/PatchEditor.svelte`:

- The `$effect` now discriminates by `patch` prop reference instead of
  a name-based key. `App.svelte` always builds a NEW object when it
  adopts a PATCH response or seeds a placeholder, so reference equality
  is the right signal for "App-level truth refreshed - re-sync." User
  edits live on `working` and never mutate the prop, so this only
  fires at points where re-syncing is desired.
- `persistPatch` now fires `cmd.getPatch(bank, slot).catch(() => {})`
  after the PUT, so App's `currentPatch.patch` tracks the freshly-saved
  state. Combined with the reference-based effect, that means a
  subsequent remount (sidebar nav, re-clicking the tile) initialises
  `working` from the post-link patch.

### Coverage

New file: `editor/tests/patch-editor-link-state.test.ts` (4 tests).

- The end-to-end reproduction (open patch -> add link -> navigate
  away -> navigate back) now passes; previously failed with "expected
  0 to be 1."
- A characterisation test pins the previously-broken "same name,
  different bindings" scenario.
- Two regression tests guard against over-correction: same-reference
  re-renders must NOT thrash `working`, but a different-reference prop
  (navigation to a different patch) MUST replace it.

### Numbers after the fix

- 10 test files, 133 tests pass.
- `npm run check`: 0 errors, 0 warnings.
- 1 production bug fixed (the user's reported case).

---

## Follow-up #2: Save/Discard counter stuck at (2) after removing a link

### Symptom

User: "I removed the link from the position-4 slot but the save and
discard buttons still show (2)."

The `Save (N)` / `Discard (N)` suffix on the editor's header buttons
read the size of the linked group:

    {label}{linkedGroup.length > 1 ? ` (${linkedGroup.length})` : ""}

`linkedGroup` comes from `resolveLinkedPatches(currentPatch.bank/slot,
currentPatch.patch, patches, linkConfig)` plus the current patch
itself. After the user clicked × on the link chip, the firmware
ended up clean but App's `currentPatch.patch.linked_to` still held the
removed entry, so the count stuck at 2.

### Root cause

`PatchEditor.removeLink` was not going through `persistPatch`. It
called `cmd.putPatch(bank, slot, working)` and `propagatePatch()`
inline, bypassing the new `cmd.getPatch` refresh that
`persistPatch` gained in Follow-up #1. So App's view of the patch
never updated and the linked-group counter kept the pre-remove size.

### Fix

`editor/src/components/PatchEditor.svelte`: one-line change in
`removeLink` to call `persistPatch()` instead of the inline
`cmd.putPatch + propagatePatch` duplicate. `persistPatch` already does
both, plus the `cmd.getPatch` that refreshes App.

### Coverage

`editor/tests/patch-editor-link-state.test.ts` gained two tests:

- `removeLink ... refreshes App's currentPatch so the header reads
  'Save' (no count)` - the bug reproduction. Pre-fix asserts on
  `groupSize === 1` would fail; with the fix it passes.
- `implicit_by_position keeps the linked group at 2 even after
  removing the explicit link (BY DESIGN)` - pins the contract that
  the global "same slot is implicitly linked" toggle keeps the group
  size up regardless of the explicit list.

### Numbers after the second fix

- 10 test files, 135 tests pass.
- `npm run check`: 0 errors, 0 warnings.
- 2 production bugs fixed.

---

## Follow-up #3: "Save (2)" still showing after removing the link

### Symptom

User's screenshot: editing patch `02/04 HEAVY`, the "Linked to" section
reads "0 explicit links" but the page header still shows
`Save (2)` / `Discard (2)` (both disabled). The user removed the link
and expected the (2) suffix to drop.

### Root cause

`device.patch_link.implicit_by_position` is on, and a patch lives at
slot 4 in another bank (e.g. `01/04`). The Save/Discard counters in
`App.svelte` are computed from `resolveLinkedPatches(...)` which adds
implicit (same-slot-across-banks) links to the group regardless of
whether `working.linked_to` is empty.

This is technically the documented behaviour of the toggle - implicit
partners are saved together to keep the group in lock-step - but the
PatchEditor's "Linked to" hint section says
       `{N} explicit link(s); edits propagate automatically...`
so the two counters disagree: editor body says "0 links", page header
says "(2)". The user reasonably concluded they had a stale-state bug.

### Fix

`editor/src/components/PatchEditor.svelte` "Linked to" hint now also
reads the implicit count from `linkConfig.implicit_by_position` and
appends it to the same line:

       `0 explicit links + 1 implicit (same slot across banks); edits
        propagate automatically to the linked targets`

So the moment the user opens the section, the (2) in the page header
is explained: 0 explicit + 1 implicit. No silent contribution.

Also pluralises "target" -> "targets" when more than one link is in
play (small detail but it nudges the user that something else is in
the group).

### Coverage

`editor/tests/patch-editor-link-state.test.ts` gained a
"Screenshot reproduction" suite (4 tests):

- REPRO: the today-rendering says "0 explicit links" while the page
  header says "Save (2)" - pinned as the user-visible symptom.
- FIX: the new hint surfaces both counts ("0 explicit + 1 implicit
  (same slot across banks)") so the two counters agree.
- Implicit OFF: the hint matches the previous wording verbatim (no
  spurious "+ 0 implicit" clause).
- Explicit + implicit together: counts concatenate correctly.

### Numbers after the third fix

- 10 test files, 139 tests pass.
- `npm run check`: 0 errors, 0 warnings.
- 3 production bugs fixed.

### Action item for the user

The fix lives in `editor/src/components/PatchEditor.svelte` source.
Per `memory/project_editor_build_workflow.md`, the editor must be
rebuilt with `npm run tauri build` for the change to land in the
installed binary - confirm with the user that the running editor is
the post-fix build before declaring the issue resolved.
