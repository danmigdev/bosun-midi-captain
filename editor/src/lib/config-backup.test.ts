import { describe, it, expect } from "vitest";

import {
  validateBackup, inferKindFromDevice, backupFilename, timestampedFolderName,
  type ConfigBackup,
} from "./config-backup";

function v2Backup(overrides: Partial<ConfigBackup> = {}): ConfigBackup {
  return {
    format: "bosun-config-backup",
    version: 2,
    generated_at: "2026-06-16T08:00:00.000Z",
    profile_label: "Test",
    kind: "kemper_player",
    device: { device_name: "MIDI Captain", kemper: { enabled: true } },
    patches: [{ bank: 1, slot: 1, patch: { name: "p", bindings: [] } as never }],
    ...overrides,
  };
}

describe("validateBackup", () => {
  it("accepts a clean v2 backup", () => {
    const b = v2Backup();
    expect(validateBackup(b)).toEqual(b);
  });

  it("accepts a legacy v1 backup (no kind)", () => {
    const b = v2Backup({ version: 1, kind: undefined });
    expect(validateBackup(b)).toEqual(b);
  });

  it("rejects an unknown version", () => {
    expect(() => validateBackup(v2Backup({ version: 9 as unknown as 2 })))
      .toThrow(/Unsupported backup version/);
  });

  it("rejects wrong format string", () => {
    expect(() => validateBackup({ ...v2Backup(), format: "not-bosun" } as unknown))
      .toThrow(/Not a Bosun backup/);
  });

  it("rejects missing device section", () => {
    expect(() => validateBackup({ ...v2Backup(), device: undefined } as unknown))
      .toThrow(/Missing 'device'/);
  });

  it("rejects non-array patches", () => {
    expect(() => validateBackup({ ...v2Backup(), patches: "nope" } as unknown))
      .toThrow(/Missing or invalid 'patches'/);
  });

  it("rejects non-object input", () => {
    expect(() => validateBackup(null)).toThrow();
    expect(() => validateBackup(42)).toThrow();
  });
});

describe("inferKindFromDevice", () => {
  it("returns kemper_player when device.kemper is present", () => {
    expect(inferKindFromDevice({ kemper: { enabled: true } })).toBe("kemper_player");
  });

  it("returns ampero_ii_stage when device.ampero is present", () => {
    expect(inferKindFromDevice({ ampero: { enabled: false } })).toBe("ampero_ii_stage");
  });

  it("returns empty string when no known plugin section is present", () => {
    expect(inferKindFromDevice({ device_name: "x" })).toBe("");
  });
});

describe("backupFilename + timestampedFolderName", () => {
  it("sanitises profile label into a safe filename", () => {
    const name = backupFilename({ profile_label: "Kemper / Live!" } as ConfigBackup);
    expect(name).toMatch(/\.json$/);
    expect(name).not.toContain("/");
    expect(name).not.toContain(" ");
  });

  it("falls back to 'profile' when no label is set", () => {
    expect(backupFilename({} as ConfigBackup)).toBe("profile.json");
  });

  it("timestampedFolderName follows the bosun-export_YYYY-MM-DD_HH-MM-SS pattern", () => {
    const f = timestampedFolderName();
    expect(f).toMatch(/^bosun-export_\d{4}-\d{2}-\d{2}_\d{2}-\d{2}-\d{2}$/);
  });

  it("respects a custom prefix", () => {
    expect(timestampedFolderName("snapshot")).toMatch(/^snapshot_/);
  });
});
