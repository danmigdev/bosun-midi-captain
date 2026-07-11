import { describe, it, expect } from "vitest";
import { sessionFromBackup, summariesOf, planPush } from "./offline-config";
import type { ConfigBackup } from "./config-backup";

function backup(overrides: Partial<ConfigBackup> = {}): ConfigBackup {
  return {
    format: "bosun-config-backup",
    version: 2,
    generated_at: "2026-01-01T00:00:00Z",
    profile_label: "Live Kemper",
    kind: "kemper_player",
    device: { kemper: { enabled: true }, midi_channel: 1 },
    patches: [
      { bank: 1, slot: 2, patch: { name: "Lead", bindings: [] } },
      { bank: 1, slot: 1, patch: { name: "Clean", bindings: [] } },
    ],
    midi_learn: { pc_to_patch: [{ channel: 1, bank_msb: 0, pc: 0, captain_patch: "01/01" }] },
    ...overrides,
  };
}

describe("sessionFromBackup", () => {
  it("builds an editable session keyed by BB/SS", () => {
    const s = sessionFromBackup(backup());
    expect(s.label).toBe("Live Kemper");
    expect(s.kind).toBe("kemper_player");
    expect(s.patches.get("01/01")?.name).toBe("Clean");
    expect(s.patches.get("01/02")?.name).toBe("Lead");
    expect(s.device.midi_channel).toBe(1);
  });

  it("rejects a non-backup blob", () => {
    expect(() => sessionFromBackup({ foo: 1 })).toThrow();
  });

  it("tolerates a v1 backup with no kind or midi_learn", () => {
    const s = sessionFromBackup(backup({ version: 1, kind: undefined, midi_learn: undefined }));
    expect(s.kind).toBe("");
    expect(s.midiLearn).toEqual({ pc_to_patch: [] });
  });
});

describe("summariesOf", () => {
  it("returns bank/slot-sorted summaries, all dirty", () => {
    const s = sessionFromBackup(backup());
    const sums = summariesOf(s);
    expect(sums.map(x => `${x.bank}/${x.slot}`)).toEqual(["1/1", "1/2"]);
    expect(sums.every(x => x.dirty)).toBe(true);
    expect(sums[0].name).toBe("Clean");
  });
});

describe("planPush", () => {
  it("orders device -> patches -> midi_learn", () => {
    const s = sessionFromBackup(backup());
    const plan = planPush(s);
    expect(plan[0]).toEqual({ kind: "device" });
    expect(plan.slice(1, 3)).toEqual([
      { kind: "patch", bank: 1, slot: 1 },
      { kind: "patch", bank: 1, slot: 2 },
    ]);
    expect(plan[plan.length - 1]).toEqual({ kind: "midi_learn" });
  });

  it("omits midi_learn when the table is empty", () => {
    const s = sessionFromBackup(backup({ midi_learn: { pc_to_patch: [] } }));
    const plan = planPush(s);
    expect(plan.some(step => step.kind === "midi_learn")).toBe(false);
  });
});
