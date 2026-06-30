import { describe, it, expect } from "vitest";

import { SWITCH_DEFAULT_COLOR, defaultLedFor } from "./switch-colors";

// Regression coverage for the "LED off after toggling an unbound switch"
// bug: PatchEditor.toggle() used to seed new bindings with #000000, so a
// fresh binding was effectively invisible. The fix routes through
// defaultLedFor(switchName) which must return a real color per switch.

describe("switch-colors", () => {
  const CANONICAL_SWITCHES = ["1", "2", "3", "4", "up", "A", "B", "C", "D", "down"];

  it("defines a default color for every canonical switch", () => {
    for (const sw of CANONICAL_SWITCHES) {
      expect(SWITCH_DEFAULT_COLOR[sw], `switch ${sw} missing`).toBeDefined();
      expect(SWITCH_DEFAULT_COLOR[sw]).toMatch(/^#[0-9a-fA-F]{6}$/);
    }
  });

  it("never returns black for a canonical switch (regression)", () => {
    for (const sw of CANONICAL_SWITCHES) {
      expect(defaultLedFor(sw), `switch ${sw} defaulted to black`).not.toBe("#000000");
    }
  });

  it("returns the same color for the same switch across calls", () => {
    // The whole point of per-switch defaults is identity stability.
    // If `defaultLedFor("4")` returned a different color on different
    // patches we'd lose that guarantee.
    for (const sw of CANONICAL_SWITCHES) {
      expect(defaultLedFor(sw)).toBe(defaultLedFor(sw));
    }
  });

  it("returns a neutral fallback for an unknown switch name", () => {
    expect(defaultLedFor("unknown")).toMatch(/^#[0-9a-fA-F]{6}$/);
    // The fallback must NOT be black either - same regression spirit.
    expect(defaultLedFor("unknown")).not.toBe("#000000");
  });

  it("assigns distinct colors to switches in the same row", () => {
    // Bottom row 1-4: at least 3 distinct colors so adjacent presets
    // are visually separable.
    const bottom = ["1", "2", "3", "4"].map(defaultLedFor);
    expect(new Set(bottom).size).toBeGreaterThanOrEqual(3);
    // Top row A-D: same.
    const top = ["A", "B", "C", "D"].map(defaultLedFor);
    expect(new Set(top).size).toBeGreaterThanOrEqual(3);
  });
});
