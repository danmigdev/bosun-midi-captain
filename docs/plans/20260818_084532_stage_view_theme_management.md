# Stage View theme management

## Goal

Let the user customize the visual appearance of Stage View per section — font
family, color, and relative size — and persist that choice across launches,
following the same lightweight pattern already used for the app's light/dark
theme (`editor/src/lib/theme.ts`) and UI scale (`editor/src/lib/ui-scale.ts`).

## Non-goals

- No firmware/TFT changes. Stage View (`editor/src/components/StageView.svelte`)
  is a Tauri/Svelte full-screen view rendered entirely on the desktop/mobile
  app; the on-device TFT is a separate, much smaller display driven by
  `TFT_FIELDS` in `firmware/lib/plugins/kemper.py` and is out of scope.
- No change to the LED/border colors on the footswitch grid
  (`ledColorFor`, `onColor`/`offColor` in the template). Those encode
  functional state (which binding/patch color is assigned, latched on/off)
  and must keep reflecting the pedal's actual LEDs, not a decorative choice.
- No layout/spacing changes (grid geometry, landscape/portrait breakpoints,
  marquee scrolling). Only typography (family, color, relative size) is
  themeable.

## Current state (as found in StageView.svelte)

All text is currently hardcoded in the component's scoped `<style>` block:
a single `font-family: "Inter", -apple-system, sans-serif` on the `.stage`
root, and per-section colors/sizes hardcoded per selector
(`.stage__rig-name`, `.stage__bank`, `.stage__bpm`, `.stage__tuner`,
`.stage__switch-label`, `.stage__switch-id`), duplicated once for portrait
and once inside the `@media (orientation: landscape)` block. Sizes are all
responsive `clamp(min, vw-or-vh, max)` expressions — there is no existing
theming hook of any kind for Stage View today.

## Themeable sections

Six sections, matching the DOM structure 1:1:

| Section       | Element(s)                          | Themeable today (hardcoded) |
|---------------|--------------------------------------|------------------------------|
| `rigName`     | `.stage__rig-name`                   | color `#ffffff`, size |
| `bank`        | `.stage__bank`                       | color `#ffffff`, size |
| `bpm`         | `.stage__bpm` (+ `small` sub-label)  | color `var(--text)`, size |
| `tuner`       | `.stage__tuner`                      | color `#4ade80`, size |
| `switchLabel` | `.stage__switch-label`               | color `#ffffff`, size |
| `switchId`    | `.stage__switch-id`                  | color `var(--text-dim)`, size |

Each section gets: `fontFamily` (string, optional — falls back to the global
stage font), `color` (CSS color string, optional — falls back to today's
hardcoded default), `scale` (number, 0.5–2.0, default 1.0 — multiplies the
section's existing `clamp()` size rather than replacing it, so responsive
behavior at every viewport/orientation is preserved for free).

Plus one global fallback: `fontFamily` for `.stage` itself (replaces
`"Inter", -apple-system, sans-serif` when set).

## Data model & storage

New module `editor/src/lib/stage-theme.ts`, mirroring `theme.ts`/`ui-scale.ts`:

```ts
export type StageSection = "rigName" | "bank" | "bpm" | "tuner" | "switchLabel" | "switchId";

export type StageSectionStyle = {
  fontFamily?: string;
  color?: string;
  scale?: number; // 0.5 - 2.0, default 1.0
};

export type StageTheme = {
  version: 1;
  fontFamily?: string; // global fallback
  sections: Partial<Record<StageSection, StageSectionStyle>>;
};
```

- `STORAGE_KEY = "BOSUN_STAGE_THEME"`, JSON blob in `localStorage`.
- `readSavedStageTheme(): StageTheme` — parses + validates (unknown keys
  dropped, `scale` clamped, malformed JSON falls back to `{ version: 1,
  sections: {} }`), same defensive `try/catch` pattern as `theme.ts`.
- `saveStageTheme(theme: StageTheme): void` — persists and applies.
- `clampSectionScale(n: number): number` — reuse the clamp/round approach
  from `ui-scale.ts`'s `clampScale`.
- A `resetSection(section)` / `resetAll()` helper for the "restore default"
  actions in the UI.

## Apply mechanism

Rather than inline `style` per element (would fight the existing responsive
`clamp()` rules), expose the theme as CSS custom properties set once on the
`.stage` root div, and reference them from the existing scoped `<style>`
rules with `var(..., <existing-default>)` so an unset property is a true
no-op:

```svelte
<div class="stage" style={stageThemeVars}>
```

```css
.stage__rig-name {
  font-size: calc(clamp(3.6rem, 14vw, 8rem) * var(--stage-rigName-scale, 1));
  color: var(--stage-rigName-color, #ffffff);
  font-family: var(--stage-rigName-font, var(--stage-font, "Inter", -apple-system, sans-serif));
}
```

`stageThemeVars` is a `$derived` string built from the loaded `StageTheme`,
computed once in the component (or in `stage-theme.ts` as a pure helper
`toCssVars(theme): string` so it's unit-testable without mounting Svelte).
Same substitution needs to happen in both the portrait rules and the
`@media (orientation: landscape)` block, since sizes differ but the CSS
variables are shared.

## Editing UI

Per the existing IA conventions in this codebase (contextual placement, no
new always-visible chrome — see `[[feedback_editor_ia_grouped_contextual]]`
and `[[feedback_topbar_actions_only_when_needed]]`), the theme editor should
not be a new top-level nav item. Proposed placement: a small settings
(gear) icon next to the existing `.stage__exit` ✕ button, same
tap-to-reveal treatment, opening an overlay panel while Stage View keeps
rendering live behind/under it so changes preview instantly.

Panel contents, per section: font family (dropdown — system stack plus
whatever web fonts are already bundled with the editor, needs a quick check
of `editor/src-tauri/resources` / `editor/public` for what's actually
shipped versus relying on OS-installed fonts), color (native `<input
type=color>`, consistent with any existing color pickers in the binding
editor), size scale (slider, 0.5x–2.0x, live label). A "Reset section" and
a single "Reset all" action, matching the safety-net pattern already used
elsewhere (no destructive action without an obvious undo).

## Testing

- `editor/src/lib/stage-theme.test.ts`: read/save/clamp/`toCssVars` pure-
  function tests, mirroring the structure of `ui-scale.test.ts`.
- Extend `editor/tests/components/StageView.test.ts`: verify a saved theme's
  CSS variables land on the `.stage` root and that an empty/default theme
  renders identically to today's hardcoded output (regression guard).

## Decisions

1. **Font source: system fonts only.** The family dropdown sticks to a
   curated system-font stack (extending the current `"Inter", -apple-system,
   sans-serif` default) rather than bundling web font files. No app-size
   increase, no asset-packaging work; accept that the exact rendered glyph
   can differ slightly across Windows/macOS/Android/iOS.
2. **Header sections stay independent.** `rigName` and `bank` remain two
   separate themeable sections (own font/color/scale each), as originally
   scoped in the section table above — not merged into one combined
   "header" control.
3. **Scope: per-install, `localStorage`.** Matches `theme.ts` and
   `ui-scale.ts` exactly — the Stage theme lives in the browser/webview
   profile on the machine/device running the editor, not on the pedal
   itself. Following the pedal (device.json + protocol sync) is explicitly
   out of scope; revisit only if a real cross-device need shows up later.
