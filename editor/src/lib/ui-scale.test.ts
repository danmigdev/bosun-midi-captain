import { describe, it, expect } from "vitest";

import {
  MIN_SCALE, MAX_SCALE, DEFAULT_SCALE,
  clampScale, readSavedScale, saveScale, applyScale,
} from "./ui-scale";

describe("ui-scale", () => {
  it("clamps below MIN to MIN", () => {
    expect(clampScale(0.1)).toBe(MIN_SCALE);
    expect(clampScale(-5)).toBe(MIN_SCALE);
  });

  it("clamps above MAX to MAX", () => {
    expect(clampScale(99)).toBe(MAX_SCALE);
    expect(clampScale(MAX_SCALE + 0.5)).toBe(MAX_SCALE);
  });

  it("rounds to 2 decimals to avoid floating-point drift", () => {
    // Without the round, repeated +0.05 increments would produce
    // values like 1.0500000000000003 over time.
    expect(clampScale(1.0500000000000003)).toBe(1.05);
    expect(clampScale(1.234567)).toBe(1.23);
  });

  it("readSavedScale returns DEFAULT_SCALE when nothing is stored", () => {
    expect(readSavedScale()).toBe(DEFAULT_SCALE);
  });

  it("readSavedScale rejects out-of-range stored values", () => {
    localStorage.setItem("BOSUN_UI_SCALE", "999");
    expect(readSavedScale()).toBe(DEFAULT_SCALE);
    localStorage.setItem("BOSUN_UI_SCALE", "not-a-number");
    expect(readSavedScale()).toBe(DEFAULT_SCALE);
  });

  // jsdom normalises CSS values (e.g. "120.00%" -> "120%"), so we
  // compare against the parsed numeric value instead of the exact
  // string produced by applyScale().
  function fontSizePercent(): number {
    const s = document.documentElement.style.fontSize;
    return parseFloat(s);
  }

  it("saveScale persists and applies", () => {
    saveScale(1.2);
    expect(readSavedScale()).toBe(1.2);
    expect(fontSizePercent()).toBe(120);
  });

  it("applyScale alone sets the html font-size", () => {
    applyScale(0.85);
    expect(fontSizePercent()).toBe(85);
  });
});
