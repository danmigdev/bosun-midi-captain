/**
 * Unit tests for the pure helpers in src/lib/firmware-update.ts and the
 * humanBytes formatter in src/lib/firmware-push.ts.
 *
 * These are the version-comparison brains behind the "Update firmware"
 * affordance: compareVersions decides whether a GitHub release is newer
 * than what the pedal reports, evaluateUpdate wraps that with the
 * missing-data guards the UI relies on, and humanBytes formats the file
 * sizes shown in the push log. All pure - no Tauri, no network - so they
 * are cheap to lock down and easy to regress on.
 */
import { describe, it, expect } from "vitest";

import { compareVersions, evaluateUpdate, type FirmwareRelease } from "../src/lib/firmware-update";
import { humanBytes } from "../src/lib/firmware-push";

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

describe("evaluateUpdate", () => {
  const rel = (version: string): FirmwareRelease => ({
    version,
    tag: `v${version}`,
    htmlUrl: "https://example.test/releases/latest",
    publishedAt: "2026-01-01T00:00:00Z",
  });

  it("returns true when the release is newer than the installed version", () => {
    expect(evaluateUpdate("0.3.0", rel("0.4.0"))).toBe(true);
  });

  it("returns false when installed is equal or newer", () => {
    expect(evaluateUpdate("0.4.0", rel("0.4.0"))).toBe(false);
    expect(evaluateUpdate("0.5.0", rel("0.4.0"))).toBe(false);
  });

  it("returns null when either side is missing", () => {
    expect(evaluateUpdate(null, rel("0.4.0"))).toBeNull();
    expect(evaluateUpdate("", rel("0.4.0"))).toBeNull();
    expect(evaluateUpdate("0.3.0", null)).toBeNull();
  });

  it("returns null when the release carries no version string", () => {
    expect(evaluateUpdate("0.3.0", rel(""))).toBeNull();
  });
});

describe("humanBytes", () => {
  it("formats bytes under 1 KiB as raw byte counts", () => {
    expect(humanBytes(0)).toBe("0 B");
    expect(humanBytes(512)).toBe("512 B");
    expect(humanBytes(1023)).toBe("1023 B");
  });

  it("formats KiB with one decimal", () => {
    expect(humanBytes(1024)).toBe("1.0 KB");
    expect(humanBytes(1536)).toBe("1.5 KB");
  });

  it("formats MiB with two decimals", () => {
    expect(humanBytes(1024 * 1024)).toBe("1.00 MB");
    expect(humanBytes(1024 * 1024 * 2.5)).toBe("2.50 MB");
  });
});
