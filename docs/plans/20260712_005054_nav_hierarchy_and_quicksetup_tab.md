# Nav hierarchy + Quick setup as an Editor tab

## Motivation
User feedback: the flat 10-item sidebar mixes unrelated concerns, and "Quick
setup" as a top-level peer of "Editor" doesn't communicate that it acts on the
patch currently open in the Editor ("when do I use it?").

User decisions:
- Grouping: grouped sidebar with always-visible section headers (Build / Device / System).
- Quick setup: move it INTO the Editor as a tab (Switches | Quick setup).

## Changes (editor/src/App.svelte only)

1. CORE_NAV: drop the `quicksetup` item; add a `group` field to each item.
   - Home -> "" (standalone, above the groups)
   - Build:  Patches, Editor, Setlist
   - Device: Screen layout, MIDI Learn, Settings (+ plugin recipe pages)
   - System: Maintenance, Log

2. visibleNav: assign plugin recipes to the `device` group; keep the
   active-kind filter.

3. navGroups derived: bucket visibleNav by group in a fixed order, drop empty
   groups. Sidebar renders a `.navgroup-label` header before each labelled
   group, then its items (dirty badge / learning pulse unchanged).

4. Editor page: add `editorTab` state ("switches" | "quicksetup"). Inside the
   `currentPatch && manifest` branch, render a tab bar and switch between
   <PatchEditor> and <QuickSetup>. Remove the standalone `page === "quicksetup"`
   block. After applying a recipe, jump back to the Switches tab so the user
   sees the result.

5. CSS: `.navgroup-label`, `.editor-tabs`, `.etab`.

## Verification
- svelte-check 0 errors, `npm test` green.
- Live drive via uidrv: sidebar shows 3 headers; Editor shows the two tabs;
  Quick setup tab writes bindings to the open patch. Re-shoot 40 (quicksetup)
  + 42 (editor) + 43 (home)/02 sidebar screenshots at 100%.
