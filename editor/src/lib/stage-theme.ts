// Stage View theme: per-section font/color/size overrides for the live
// "stage mode" screen (StageView.svelte). Same lightweight pattern as
// theme.ts (light/dark) and ui-scale.ts (global font scale) - a small
// localStorage-backed module the component reads on mount and re-applies
// whenever the user edits it in StageThemeEditor.svelte.
//
// Sizes are never replaced outright: StageView's CSS uses responsive
// clamp() expressions per section, so a section's `scale` is a multiplier
// layered on top via calc(), keeping the existing portrait/landscape
// responsive behaviour intact at every viewport.

export type StageSection =
  | "rigName"
  | "bank"
  | "bpm"
  | "tuner"
  | "switchLabel"
  | "switchId";

export const STAGE_SECTIONS: StageSection[] = [
  "rigName", "bank", "bpm", "tuner", "switchLabel", "switchId",
];

// CSS custom-property key per section, matching the kebab-case class names
// already used in StageView.svelte (.stage__rig-name, .stage__switch-id, ...).
const SECTION_CSS_KEY: Record<StageSection, string> = {
  rigName: "rig-name",
  bank: "bank",
  bpm: "bpm",
  tuner: "tuner",
  switchLabel: "switch-label",
  switchId: "switch-id",
};

export type StageSectionStyle = {
  fontFamily?: string;
  color?: string;
  scale?: number; // MIN_SECTION_SCALE - MAX_SECTION_SCALE, default 1.0
};

export type StageTheme = {
  version: 1;
  fontFamily?: string; // global fallback, used when a section has none of its own
  sections: Partial<Record<StageSection, StageSectionStyle>>;
};

export const MIN_SECTION_SCALE = 0.5;
export const MAX_SECTION_SCALE = 2.0;

// Curated system-font stacks only (no bundled web fonts): guarantees
// identical bundle size and no asset-packaging work across
// Windows/macOS/Android/iOS, at the cost of the exact glyph shape
// differing slightly per platform.
export const FONT_STACKS: Record<string, string> = {
  Default: '"Inter", -apple-system, sans-serif',
  "Sans-serif": '-apple-system, "Segoe UI", Roboto, Helvetica, Arial, sans-serif',
  Serif: 'Georgia, "Times New Roman", Times, serif',
  Monospace: '"Cascadia Code", "SF Mono", Consolas, "Courier New", monospace',
  Condensed: '"Arial Narrow", "Segoe UI", sans-serif',
};

const STORAGE_KEY = "BOSUN_STAGE_THEME";

export function clampSectionScale(n: number): number {
  if (!Number.isFinite(n)) return 1;
  if (n < MIN_SECTION_SCALE) return MIN_SECTION_SCALE;
  if (n > MAX_SECTION_SCALE) return MAX_SECTION_SCALE;
  // Round to 2 decimals to avoid floating-point drift from slider steps.
  return Math.round(n * 100) / 100;
}

function sanitizeSectionStyle(raw: unknown): StageSectionStyle {
  if (!raw || typeof raw !== "object") return {};
  const r = raw as Record<string, unknown>;
  const out: StageSectionStyle = {};
  if (typeof r.fontFamily === "string" && r.fontFamily) out.fontFamily = r.fontFamily;
  if (typeof r.color === "string" && r.color) out.color = r.color;
  if (typeof r.scale === "number") out.scale = clampSectionScale(r.scale);
  return out;
}

export function readSavedStageTheme(): StageTheme {
  const empty: StageTheme = { version: 1, sections: {} };
  try {
    const raw = localStorage.getItem(STORAGE_KEY);
    if (!raw) return empty;
    const parsed = JSON.parse(raw);
    if (!parsed || typeof parsed !== "object") return empty;
    const theme: StageTheme = { version: 1, sections: {} };
    const p = parsed as Record<string, unknown>;
    if (typeof p.fontFamily === "string" && p.fontFamily) theme.fontFamily = p.fontFamily;
    const rawSections = p.sections;
    if (rawSections && typeof rawSections === "object") {
      for (const section of STAGE_SECTIONS) {
        const style = sanitizeSectionStyle((rawSections as Record<string, unknown>)[section]);
        if (Object.keys(style).length > 0) theme.sections[section] = style;
      }
    }
    return theme;
  } catch {
    return empty;
  }
}

export function saveStageTheme(theme: StageTheme): void {
  try { localStorage.setItem(STORAGE_KEY, JSON.stringify(theme)); } catch {}
}

export function resetSection(theme: StageTheme, section: StageSection): StageTheme {
  const sections = { ...theme.sections };
  delete sections[section];
  return { ...theme, sections };
}

export function resetAllStageTheme(): StageTheme {
  return { version: 1, sections: {} };
}

/** Builds the inline `style` string for the `.stage` root: one CSS custom
 *  property per set field. Unset fields emit nothing, so the component's
 *  `var(--stage-x-y, <default>)` fallbacks keep today's hardcoded look. */
export function stageThemeToCssVars(theme: StageTheme): string {
  const parts: string[] = [];
  if (theme.fontFamily) parts.push(`--stage-font: ${theme.fontFamily}`);
  for (const section of STAGE_SECTIONS) {
    const s = theme.sections[section];
    if (!s) continue;
    const key = SECTION_CSS_KEY[section];
    if (s.fontFamily) parts.push(`--stage-${key}-font: ${s.fontFamily}`);
    if (s.color) parts.push(`--stage-${key}-color: ${s.color}`);
    if (s.scale != null) parts.push(`--stage-${key}-scale: ${s.scale}`);
  }
  return parts.join("; ");
}
