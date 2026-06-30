// UI font scale. Bumps / shrinks the entire editor by adjusting the
// html root font-size - every rem-based size in the CSS follows. Driven
// by Ctrl+ / Ctrl- / Ctrl+0 keyboard shortcuts (wired up in App.svelte).
//
// Persisted across launches in localStorage so the user doesn't have to
// re-bump after a restart.

export const MIN_SCALE = 0.8;
export const MAX_SCALE = 1.5;
export const STEP = 0.05;
export const DEFAULT_SCALE = 1.0;

const STORAGE_KEY = "BOSUN_UI_SCALE";

export function readSavedScale(): number {
  try {
    const v = localStorage.getItem(STORAGE_KEY);
    if (v) {
      const n = parseFloat(v);
      if (Number.isFinite(n) && n >= MIN_SCALE && n <= MAX_SCALE) return n;
    }
  } catch {}
  return DEFAULT_SCALE;
}

export function applyScale(scale: number): void {
  // html font-size scales every rem in the stylesheets. Browser default
  // is 16px - 100% keeps it there, 110% bumps to 17.6px, etc.
  document.documentElement.style.fontSize = `${(scale * 100).toFixed(2)}%`;
}

export function saveScale(scale: number): void {
  try { localStorage.setItem(STORAGE_KEY, String(scale)); } catch {}
  applyScale(scale);
}

export function clampScale(s: number): number {
  if (s < MIN_SCALE) return MIN_SCALE;
  if (s > MAX_SCALE) return MAX_SCALE;
  // Round to 2 decimals to avoid floating-point drift after many steps.
  return Math.round(s * 100) / 100;
}
