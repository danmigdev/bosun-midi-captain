/**
 * Backup/restore roundtrip stress.
 *
 * The save-coherence story extends across the export/import boundary:
 * when a user backs up a profile and restores it, every single patch
 * field must come back identical. Tests here exercise the
 * pure validation + naming helpers under conditions that would surface
 * silent drift:
 *  - 125-patch fixture with unicode/accented Italian patch names, deep
 *    binding trees, every optional Patch field populated
 *  - JSON.stringify -> JSON.parse -> validateBackup roundtrip must
 *    deep-equal the input
 *  - filename sanitization for edge labels (accents, slashes, emoji)
 *  - inferKindFromDevice tolerates malformed device blobs (v1 path)
 *  - v1 backup (no `kind`) accepted, v2 accepted, unknown versions
 *    rejected
 */
import { describe, it, expect } from "vitest";
import {
  validateBackup, inferKindFromDevice, backupFilename, timestampedFolderName,
  type ConfigBackup,
} from "../src/lib/config-backup";
import type { Patch, MidiLearnTable, Action, BindingMode } from "../src/lib/protocol";

const SWITCHES = ["1","2","3","4","up","A","B","C","D","down"] as const;
const MODES: BindingMode[] = ["tap","latched","momentary","long_press_alt","double_tap"];

/** A Patch with every optional field set, deliberately heavy. */
function richPatch(bank: number, slot: number): Patch {
  const onEnter: Action = {
    messages: [
      { type: "kemper_rig", rig: ((bank - 1) * 5 + slot) },
      { type: "cc", channel: 1, number: 7, value: 100 + slot },
    ],
  };
  return {
    name: `${nameSeed(bank, slot)} ${bank}/${slot}`,
    tft_color: `#${(0x100000 + bank * slot).toString(16).padStart(6, "0")}`,
    on_enter: onEnter,
    bindings: SWITCHES.map((sw, i) => ({
      switch: sw,
      mode: MODES[i % MODES.length],
      label: `${sw}-${bank}/${slot}`,
      led: { on: "#abcdef", off: "#000000" },
      auto_momentary: i % 2 === 0,
      actions: {
        press: {
          messages: [
            { type: "cc", channel: 1, number: 30 + i, value: 64 },
            { type: "pc", channel: 1, value: i },
          ],
        },
        ...(MODES[i % MODES.length] === "latched"
          ? { toggle_off: { messages: [{ type: "cc", channel: 1, number: 30 + i, value: 0 }] } }
          : {}),
      },
    })),
    // Cross-bank link to "same slot, next bank" - skipped when we'd
    // overflow the device. Captures the realistic shape used by the
    // implicit_by_position group.
    linked_to: bank < 25 ? [{ bank: bank + 1, slot }] : undefined,
  };
}

function nameSeed(bank: number, slot: number): string {
  // Rotate through a varied set of unicode strings so the fixture
  // covers accented Italian (the project's working language), German
  // umlauts, an Asian script, and ASCII edge characters.
  const seeds = [
    "Solo - Caña",
    "Bagliore d'autunno",
    "à è é ì ò ù",
    "Größenwahn",
    "中文 测试",
    "Heavy_FX (live)",
    "Verse + Chorus",
    "ABCDEF",
  ];
  return seeds[(bank + slot) % seeds.length];
}

function fullBackup(): ConfigBackup {
  const patches: ConfigBackup["patches"] = [];
  for (let bank = 1; bank <= 25; bank++) {
    for (let slot = 1; slot <= 5; slot++) {
      patches.push({ bank, slot, patch: richPatch(bank, slot) });
    }
  }
  const midi_learn: MidiLearnTable = {
    pc_to_patch: patches.slice(0, 30).map((p, i) => ({
      channel: 1,
      bank_msb: Math.floor(i / 16),
      pc: i % 128,
      captain_patch: `${String(p.bank).padStart(2, "0")}/${String(p.slot).padStart(2, "0")}`,
    })),
  };
  return {
    format: "bosun-config-backup",
    version: 2,
    generated_at: "2026-06-16T18:35:56.000Z",
    profile_label: "Production - Sàbato sera",
    kind: "kemper_player",
    device: {
      device_name: "MIDI Captain - test",
      kemper: { enabled: true, channel: 1, beacon: true },
      patch_link: { implicit_by_position: false },
    },
    patches,
    midi_learn,
  };
}

// -----------------------------------------------------------------------
// roundtrip
// -----------------------------------------------------------------------

describe("Backup roundtrip: 125 patches, every aspect preserved", () => {
  it("JSON serialize -> parse -> validate returns a deeply-equal backup", () => {
    const backup = fullBackup();
    const serialized = JSON.stringify(backup, null, 2);
    const parsed = JSON.parse(serialized) as unknown;
    const validated = validateBackup(parsed);
    expect(validated).toEqual(backup);
    expect(validated.patches).toHaveLength(125);
  });

  it("unicode patch names survive the JSON roundtrip byte-equal", () => {
    const backup = fullBackup();
    const serialized = JSON.stringify(backup);
    const parsed = JSON.parse(serialized) as ConfigBackup;
    for (let i = 0; i < backup.patches.length; i++) {
      expect(parsed.patches[i].patch.name).toBe(backup.patches[i].patch.name);
    }
  });

  it("every binding's modes, labels, LED colors, and message arrays round-trip identically", () => {
    const backup = fullBackup();
    const parsed = JSON.parse(JSON.stringify(backup)) as ConfigBackup;
    for (let i = 0; i < backup.patches.length; i++) {
      const orig = backup.patches[i].patch;
      const after = parsed.patches[i].patch;
      expect(after.bindings).toEqual(orig.bindings);
      expect(after.on_enter).toEqual(orig.on_enter);
      expect(after.tft_color).toBe(orig.tft_color);
      expect(after.linked_to).toEqual(orig.linked_to);
    }
  });

  it("midi_learn table is fully preserved", () => {
    const backup = fullBackup();
    const parsed = JSON.parse(JSON.stringify(backup)) as ConfigBackup;
    expect(parsed.midi_learn).toEqual(backup.midi_learn);
  });

  it("a v1-shape backup (no kind, no midi_learn) still validates", () => {
    const backup: ConfigBackup = {
      format: "bosun-config-backup",
      version: 1,
      generated_at: "2026-01-01T00:00:00.000Z",
      device: { device_name: "X" },
      patches: [{ bank: 1, slot: 1, patch: { name: "p", bindings: [] } }],
    };
    expect(validateBackup(backup)).toEqual(backup);
  });

  it("backup ordering (bank, slot) is preserved through the roundtrip", () => {
    const backup = fullBackup();
    const parsed = JSON.parse(JSON.stringify(backup)) as ConfigBackup;
    for (let i = 0; i < backup.patches.length; i++) {
      expect(parsed.patches[i].bank).toBe(backup.patches[i].bank);
      expect(parsed.patches[i].slot).toBe(backup.patches[i].slot);
    }
  });
});

// -----------------------------------------------------------------------
// validateBackup: rejection edges
// -----------------------------------------------------------------------

describe("validateBackup: rejection edges", () => {
  it("rejects an unknown version", () => {
    const b = fullBackup() as unknown as { version: number };
    b.version = 9;
    expect(() => validateBackup(b)).toThrow(/Unsupported backup version/);
  });

  it("rejects when `device` is missing or null", () => {
    const b = JSON.parse(JSON.stringify(fullBackup()));
    delete b.device;
    expect(() => validateBackup(b)).toThrow(/device/);
    b.device = null;
    expect(() => validateBackup(b)).toThrow(/device/);
  });

  it("rejects when `patches` is not an array", () => {
    const b = JSON.parse(JSON.stringify(fullBackup()));
    b.patches = "nope";
    expect(() => validateBackup(b)).toThrow(/patches/);
    b.patches = { 0: { bank: 1, slot: 1, patch: {} } };
    expect(() => validateBackup(b)).toThrow(/patches/);
  });

  it("rejects a non-object input", () => {
    expect(() => validateBackup("nope")).toThrow();
    expect(() => validateBackup(null)).toThrow();
    expect(() => validateBackup(42)).toThrow();
    expect(() => validateBackup(undefined)).toThrow();
  });
});

// -----------------------------------------------------------------------
// backupFilename: sanitization for awkward labels
// -----------------------------------------------------------------------

describe("backupFilename: sanitisation", () => {
  function name(label: string | undefined): string {
    return backupFilename({ profile_label: label } as ConfigBackup);
  }

  it("strips path separators on both POSIX and Windows", () => {
    expect(name("Kemper/Live")).not.toContain("/");
    expect(name("Kemper\\Live")).not.toContain("\\");
  });

  it("collapses spaces and shell-active characters", () => {
    const n = name("Sàbato sera; rm -rf /");
    expect(n).toMatch(/\.json$/);
    expect(n).not.toContain(" ");
    expect(n).not.toContain(";");
    expect(n).not.toContain("/");
  });

  it("falls back to 'profile.json' when label is empty / undefined / null-ish", () => {
    expect(name(undefined)).toBe("profile.json");
    expect(name("")).toBe("profile.json");
  });

  it("preserves ASCII letters, digits, and underscore/hyphen", () => {
    const n = name("Kemper_Live-2026");
    expect(n).toBe("Kemper_Live-2026.json");
  });

  it("does not return an empty filename even for label = ' / / / '", () => {
    const n = name(" / / / ");
    expect(n.length).toBeGreaterThan(".json".length);
    expect(n).toMatch(/\.json$/);
  });
});

// -----------------------------------------------------------------------
// timestampedFolderName
// -----------------------------------------------------------------------

describe("timestampedFolderName", () => {
  it("follows the YYYY-MM-DD_HH-MM-SS pattern for the default prefix", () => {
    expect(timestampedFolderName()).toMatch(
      /^bosun-export_\d{4}-\d{2}-\d{2}_\d{2}-\d{2}-\d{2}$/,
    );
  });

  it("respects a custom prefix", () => {
    expect(timestampedFolderName("install-backup")).toMatch(
      /^install-backup_\d{4}-\d{2}-\d{2}_\d{2}-\d{2}-\d{2}$/,
    );
  });

  it("two consecutive calls within a second can be identical (no monotonic counter needed)", () => {
    // The function is timestamp-only; we don't promise uniqueness within
    // a single second. Just check both calls produced the documented
    // shape rather than asserting inequality (which would be flaky).
    const a = timestampedFolderName();
    const b = timestampedFolderName();
    expect(a).toMatch(/^bosun-export_/);
    expect(b).toMatch(/^bosun-export_/);
  });
});

// -----------------------------------------------------------------------
// inferKindFromDevice: malformed device tolerance
// -----------------------------------------------------------------------

describe("inferKindFromDevice: malformed/unusual device blobs", () => {
  it("returns kemper_player when device.kemper is an object (any shape)", () => {
    expect(inferKindFromDevice({ kemper: {} })).toBe("kemper_player");
    expect(inferKindFromDevice({ kemper: { enabled: false } })).toBe("kemper_player");
    expect(inferKindFromDevice({ kemper: { whatever: 1, more: "stuff" } })).toBe("kemper_player");
  });

  it("returns ampero_ii_stage when device.ampero is an object", () => {
    expect(inferKindFromDevice({ ampero: {} })).toBe("ampero_ii_stage");
  });

  it("returns '' when both plugin keys are missing", () => {
    expect(inferKindFromDevice({ device_name: "x" })).toBe("");
    expect(inferKindFromDevice({})).toBe("");
  });

  it("ignores plugin keys that are non-object scalars (treats them as absent)", () => {
    // A v1 backup could in principle have a stub like { kemper: true }
    // if some external tooling produced it; the inference is "object"
    // shaped, so a bare boolean is rejected.
    expect(inferKindFromDevice({ kemper: true } as unknown as Record<string, unknown>)).toBe("");
    expect(inferKindFromDevice({ kemper: 1 } as unknown as Record<string, unknown>)).toBe("");
    expect(inferKindFromDevice({ kemper: "yes" } as unknown as Record<string, unknown>)).toBe("");
  });

  it("does NOT throw on null/undefined plugin keys", () => {
    expect(() => inferKindFromDevice({ kemper: null } as unknown as Record<string, unknown>)).not.toThrow();
    expect(inferKindFromDevice({ kemper: null } as unknown as Record<string, unknown>)).toBe("");
  });
});
