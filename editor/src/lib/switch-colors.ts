// Per-switch default LED colors. When a new binding is created (either
// by expanding an unbound switch in PatchEditor or by spawning a fresh
// patch in PatchActions) the LED on-color comes from this map instead
// of being hardcoded to black. Same switch = same default color across
// every patch, so the visual identity of a footswitch stays consistent
// when it's used.
//
// The user can override per-binding via the LED color picker in the
// editor; this is just the default for newly-created bindings.

export const SWITCH_DEFAULT_COLOR: Record<string, string> = {
  // Bottom row - typically preset navigation or effect on/off.
  "1":    "#3a8eff",   // blue
  "2":    "#f5dc34",   // yellow
  "3":    "#e54848",   // red
  "4":    "#3ecb6e",   // green
  // Middle pair - typically bank up / bank down or extra effects.
  "up":   "#00bcd4",   // cyan
  "down": "#c08aff",   // purple
  // Top row - typically per-rig effect slots A-D.
  "A":    "#ff8a00",   // orange
  "B":    "#00e5ff",   // light cyan
  "C":    "#ff4081",   // pink
  "D":    "#76ff03",   // lime
};

const FALLBACK_DEFAULT = "#888888";

export function defaultLedFor(switchName: string): string {
  return SWITCH_DEFAULT_COLOR[switchName] ?? FALLBACK_DEFAULT;
}
