# Plan: Editor usability sweep (6 features, parallel build)

Status: built + integrated + verified (svelte-check 0/0, 194 vitest tests, vite build OK)

## Outcome
All six built in parallel by agents (disjoint new files), integrated by the parent.
Pedal map reworked to a FULLY user-arrangeable layout (drag to rearrange, persisted) -
no fixed physical grid, per user direction. Setlist view (#6b) deferred.
New files: help-text.ts, HelpTip.svelte, snippets.ts, undo-stack.ts, recipes.ts,
QuickSetup.svelte, pedal-layout.ts, PedalMap.svelte, DeviceMirror.svelte (+ 5 test files).
Integrated into PatchEditor.svelte (pedal map, undo/redo + Ctrl+Z/Y, snippets, help),
App.svelte (Quick setup page + nav), Dashboard.svelte (live device mirror).
Date: 2026-07-11

Six usability improvements. Built in parallel by agents producing DISJOINT new
files with fixed contracts; the parent integrates them into the shared files
(App.svelte, PatchEditor.svelte, Dashboard.svelte, PatchesGrid.svelte) sequentially.

## Conventions for all new files
- Svelte 5 runes ($props/$state/$derived/$effect), TypeScript strict (svelte-check clean).
- Import domain types/helpers from `../lib/protocol` (Patch, Binding, MidiMessage,
  Manifest, MessageSchema, BindingMode, ACTION_KEYS_BY_MODE, flattenManifest,
  defaultMessageFromSchema, summarizeMessage).
- Match styling of Dashboard.svelte / Settings.svelte; reuse their CSS variables.
- No new npm dependencies. Vitest for pure logic (*.test.ts). Do NOT run the full
  svelte-check/npm test (concurrent builds interfere) - parent runs it after integration.

## Switch facts
- 10 switches, names: 1,2,3,4,up,A,B,C,D,down.
- Physical layout for the map: top row A B C D, bottom row 1 2 3 4, middle pair up/down.
- Default LED colors: editor/src/lib/switch-colors.ts (defaultLedFor).

## Agent deliverables (all NEW files)

### A. Inline help
- lib/help-text.ts: MODE_HELP (all 5 modes), PARAM_HELP (channel/cc/value/program/
  delta/scope/bank/slot/ms/note/velocity/bpm...), helpForMessageType(type).
- components/HelpTip.svelte: props { text; label? } -> small "?" popover, accessible.
- lib/help-text.test.ts.

### B. Snippets (reusable switch bindings)
- lib/snippets.ts: Snippet{id,name,binding,createdAt}; listSnippets/saveSnippet/
  deleteSnippet/renameSnippet; bindingFromSnippet(s, switchName). localStorage
  "BOSUN_SNIPPETS_V1", robust to malformed JSON.
- lib/snippets.test.ts (mock localStorage).

### C. Undo/redo
- lib/undo-stack.ts: History<T>{push,undo,redo,canUndo,canRedo,current,reset}, bounded,
  structuredClone snapshots.
- lib/undo-stack.test.ts.

### D. Core setup recipes
- lib/recipes.ts: Recipe{id,label,description,icon?,roles[],build(assign,ctx)}.
  Recipes: preview (core preview msgs), bank_nav (captain_bank_step +/-1),
  tuner (find *_tuner plugin msg -> latched on/off), tap_tempo (find *_tap_tempo -> tap).
  ctx = { activeKind, manifest }. Helpers to detect plugin tuner/tap types.
- components/QuickSetup.svelte: props { switches; manifest; activeKind; existing; onApply }.
- lib/recipes.test.ts.

### E. Visual pedal map
- components/PedalMap.svelte: props { bindings; selected?; onSelect; colorFor? }.
  Renders physical layout, per-switch label + LED color, click -> onSelect, dim empties.
- (optional light helper test)

### F. Live device mirror
- components/DeviceMirror.svelte: props { connected; current; patchName?; lastActivityMs?; stats? }.
  Compact "Live on device" card.

## Parent integration (sequential, after agents)
- HelpTip: into PatchEditor mode selector + Settings labels.
- Snippets: copy/paste buttons per switch row in PatchEditor.
- Undo/redo: wrap working-patch edits in PatchEditor; Ctrl+Z/Ctrl+Y.
- QuickSetup: new "Quick setup" page in App sidebar; onApply writes bindings via cmd.putBinding.
- PedalMap: top of PatchEditor; click selects/expands a switch row.
- DeviceMirror: into Dashboard; fed by patch_switched events + periodic STATS.
- Setlist view (#6b, lowest priority): search + duplicate-bank in PatchesGrid - parent, time permitting.

## Verify
- npm run check (svelte-check 0 errors), npm test (vitest), then a release-style smoke if time.
