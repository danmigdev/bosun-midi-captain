// Theme management. The whole app shell + page chrome reads its colours
// from CSS variables declared in App.svelte. This module flips the
// `data-theme` attribute on <html> so those variables resolve to either
// the dark or the light palette, and persists the choice across launches.
//
// Components that haven't been migrated yet keep their hardcoded colours
// - they look correct in dark mode and acceptable in light mode (slightly
// out of place, but readable). Migration is incremental: replace each
// hex string with the matching `var(--<token>)` from App.svelte.

export type Theme = "dark" | "light";

const STORAGE_KEY = "BOSUN_THEME";

export function readSavedTheme(): Theme {
  try {
    const v = localStorage.getItem(STORAGE_KEY);
    if (v === "light" || v === "dark") return v;
  } catch {}
  return "dark";
}

export function applyTheme(theme: Theme): void {
  const root = document.documentElement;
  root.setAttribute("data-theme", theme);
  // `color-scheme` tells the WebView to pick the matching native form
  // controls (e.g. scrollbar colours) - the bit you can't style.
  root.style.colorScheme = theme;
}

export function saveTheme(theme: Theme): void {
  try { localStorage.setItem(STORAGE_KEY, theme); } catch {}
  applyTheme(theme);
}
