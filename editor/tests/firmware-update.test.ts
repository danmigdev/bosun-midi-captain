/**
 * Unit tests for compareVersions in src/lib/firmware-update.ts.
 *
 * This is the version-comparison brain behind the firmware update offers:
 * it decides whether the bundled release is newer than what the pedal
 * reports. Pure - no Tauri, no network - so it is cheap to lock down and
 * easy to regress on.
 */
import { describe, it, expect } from "vitest";

import { compareVersions } from "../src/lib/firmware-update";

describe("compareVersions", () => {
  it("orders by major, then minor, then patch", () => {
    expect(compareVersions("1.0.0", "0.9.9")).toBeGreaterThan(0);
    expect(compareVersions("0.4.0", "0.3.29")).toBeGreaterThan(0);
    expect(compareVersions("0.3.29", "0.4.0")).toBeLessThan(0);
    expect(compareVersions("0.3.2", "0.3.10")).toBeLessThan(0); // numeric, not lexical
  });

  it("returns 0 for equal versions", () => {
    expect(compareVersions("0.4.0", "0.4.0")).toBe(0);
  });

  it("strips a pre-release suffix before comparing", () => {
    expect(compareVersions("0.2.0-scaffold", "0.2.0")).toBe(0);
    expect(compareVersions("0.2.0-rc.1", "0.2.0")).toBe(0);
    expect(compareVersions("0.3.0-beta", "0.2.0")).toBeGreaterThan(0);
    expect(compareVersions("0.6.5", "0.6.5-native")).toBe(0);
  });

  it("treats missing segments as zero", () => {
    expect(compareVersions("1", "1.0.0")).toBe(0);
    expect(compareVersions("1.2", "1.2.0")).toBe(0);
    expect(compareVersions("1.2", "1.2.1")).toBeLessThan(0);
  });

  it("treats non-numeric junk segments as zero rather than NaN", () => {
    // parseInt("x") is NaN -> coerced to 0 by the `|| 0` guard.
    expect(compareVersions("x.y.z", "0.0.0")).toBe(0);
    expect(compareVersions("1.x.0", "1.0.5")).toBeLessThan(0);
  });
});
