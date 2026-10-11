// Overwriting the active profile writes each patch as an unsaved draft, and
// the firmware holds at most 128 drafts (BOSUN_DIRTY_PATCHES). A restore of a
// larger backup must save as it goes instead of failing at patch 129, after
// every existing patch has already been deleted.
import { describe, it, expect, vi, beforeEach } from "vitest";

const mocks = vi.hoisted(() => ({ sendAndAwait: vi.fn(), createProfile: vi.fn() }));
vi.mock("../src/lib/protocol", () => ({
  sendAndAwait: mocks.sendAndAwait,
  cmd: { createProfile: mocks.createProfile },
}));

import { importConfig, validateBackup, type ConfigBackup } from "../src/lib/config-backup";

const DRAFT_LIMIT = 128;

/** Firmware store for the active profile: saved patches plus a bounded
 *  draft table, with the same SAVE_NOW and DELETE_PATCH semantics. */
function fakeFirmware(existing: Array<{ bank: number; slot: number }>) {
  const saved = new Map(existing.map(p => [`${p.bank}/${p.slot}`, { name: "old" }]));
  const drafts = new Map<string, unknown>();
  mocks.sendAndAwait.mockImplementation(async (m: Record<string, any>) => {
    const key = `${m.bank}/${m.slot}`;
    switch (m.type) {
      case "PUT_GLOBAL": return { type: "ACK" };
      case "LIST_PATCHES": return { type: "PATCH_LIST", patches: [...new Set([...saved.keys(), ...drafts.keys()])]
        .map(k => ({ bank: Number(k.split("/")[0]), slot: Number(k.split("/")[1]) })) };
      case "DELETE_PATCH": saved.delete(key); drafts.delete(key); return { type: "ACK" };
      case "PUT_PATCH":
        if (m.profile) { saved.set(key, m.patch); return { type: "ACK" }; }
        if (!drafts.has(key) && drafts.size >= DRAFT_LIMIT) throw new Error("error: limit_exceeded");
        drafts.set(key, m.patch); return { type: "ACK" };
      case "SAVE_NOW":
        for (const [k, patch] of drafts) {
          if (m.bank === undefined || k === key) { saved.set(k, patch); drafts.delete(k); }
        }
        return { type: "SAVED" };
      default: throw new Error(`unexpected ${m.type}`);
    }
  });
  return { saved, drafts };
}

function backupWith(count: number): ConfigBackup {
  const patches = Array.from({ length: count }, (_, i) => ({
    bank: Math.floor(i / 10) + 1, slot: (i % 10) + 1, patch: { name: `P${i + 1}`, bindings: [] },
  }));
  return validateBackup({
    format: "bosun-config-backup", version: 2, generated_at: "2026-10-11T00:00:00.000Z",
    kind: "generic", device: { device_name: "MIDI Captain" }, patches,
  });
}

describe("restoring more patches than the firmware's draft table holds", () => {
  beforeEach(() => { mocks.sendAndAwait.mockReset(); mocks.createProfile.mockReset(); });

  it("overwrites the active profile with all 300 patches, saved", async () => {
    const store = fakeFirmware([{ bank: 40, slot: 1 }, { bank: 1, slot: 1 }]);
    await importConfig(backupWith(300));
    expect(store.saved.size).toBe(300);
    expect(store.drafts.size).toBe(0);
    expect(store.saved.has("40/1")).toBe(false);
    expect((store.saved.get("30/10") as { name: string }).name).toBe("P300");
  });

  it("saves only the patch it just wrote", async () => {
    fakeFirmware([]);
    await importConfig(backupWith(2));
    const calls = mocks.sendAndAwait.mock.calls.map(([m]) => m)
      .filter(m => m.type === "PUT_PATCH" || m.type === "SAVE_NOW")
      .map(m => `${m.type} ${m.bank}/${m.slot}`);
    expect(calls).toEqual(["PUT_PATCH 1/1", "SAVE_NOW 1/1", "PUT_PATCH 1/2", "SAVE_NOW 1/2"]);
  });

  it("writes a new profile's saved files directly, without SAVE_NOW", async () => {
    const store = fakeFirmware([]);
    await importConfig(backupWith(300), undefined,
      { asNewProfile: { profile_id: "restored", name: "Restored", kind: "generic" } });
    expect(store.saved.size).toBe(300);
    expect(mocks.sendAndAwait.mock.calls.some(([m]) => m.type === "SAVE_NOW")).toBe(false);
  });
});
