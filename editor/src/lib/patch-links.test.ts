import { describe, it, expect } from "vitest";

import {
  resolveLinkedPatches, lockedSlots, isSlotLocked, toggledLock, applyLockToggle,
  retargetOnEnterBank,
} from "./patch-links";
import type { Patch, PatchSummary } from "./protocol";

function patch(extra: Partial<Patch> = {}): Patch {
  return { name: "p", bindings: [], ...extra };
}

function summary(bank: number, slot: number, name = ""): PatchSummary {
  return { bank, slot, name, dirty: false };
}

describe("lockedSlots / isSlotLocked", () => {
  const all = [summary(1, 1), summary(2, 1), summary(1, 2)];

  it("nothing locked without a config", () => {
    expect([...lockedSlots(undefined, all)]).toEqual([]);
    expect(isSlotLocked(1, undefined, all)).toBe(false);
  });

  it("explicit locked_slots", () => {
    const cfg = { locked_slots: [1, 3] };
    expect(isSlotLocked(1, cfg, all)).toBe(true);
    expect(isSlotLocked(2, cfg, all)).toBe(false);
    expect(isSlotLocked(3, cfg, all)).toBe(true);
  });

  it("legacy implicit_by_position is ignored - the default is unlinked", () => {
    const cfg = { implicit_by_position: true };
    expect([...lockedSlots(cfg, all)]).toEqual([]);
    expect(isSlotLocked(1, cfg, all)).toBe(false);
    expect(isSlotLocked(2, cfg, all)).toBe(false);
  });

  it("only explicit locked_slots lock, even alongside legacy implicit_by_position", () => {
    const cfg = { implicit_by_position: true, locked_slots: [2] };
    expect(isSlotLocked(1, cfg, all)).toBe(false);
    expect(isSlotLocked(2, cfg, all)).toBe(true);
  });
});

describe("toggledLock", () => {
  const all = [summary(1, 1), summary(2, 1), summary(1, 2)];

  it("adds a slot to an empty config", () => {
    expect(toggledLock(undefined, all, 1)).toEqual({ locked_slots: [1] });
  });

  it("removes an already-locked slot", () => {
    expect(toggledLock({ locked_slots: [1, 2] }, all, 1)).toEqual({ locked_slots: [2] });
  });

  it("drops legacy implicit_by_position: toggling starts from an unlocked base", () => {
    // implicit is ignored, so the base is empty; toggling slot 1 locks just it.
    expect(toggledLock({ implicit_by_position: true }, all, 1)).toEqual({ locked_slots: [1] });
  });

  it("returns a sorted list", () => {
    expect(toggledLock({ locked_slots: [3, 1] }, all, 2)).toEqual({ locked_slots: [1, 2, 3] });
  });
});

describe("resolveLinkedPatches (lock-driven)", () => {
  it("no links when the slot isn't locked", () => {
    const all = [summary(1, 1), summary(2, 1), summary(3, 1)];
    expect(resolveLinkedPatches({ bank: 1, slot: 1 }, patch(), all, undefined)).toEqual([]);
  });

  it("a locked slot links to the same slot in every other bank", () => {
    const all = [summary(1, 1), summary(2, 1), summary(3, 1), summary(2, 2)];
    const out = resolveLinkedPatches({ bank: 1, slot: 1 }, patch(), all, { locked_slots: [1] });
    expect(out).toEqual([{ bank: 2, slot: 1 }, { bank: 3, slot: 1 }]);
  });

  it("never returns the host patch itself", () => {
    const all = [summary(1, 1), summary(2, 1)];
    const out = resolveLinkedPatches({ bank: 1, slot: 1 }, patch(), all, { locked_slots: [1] });
    expect(out).not.toContainEqual({ bank: 1, slot: 1 });
  });

  it("legacy implicit_by_position no longer links anything", () => {
    const all = [summary(1, 1), summary(2, 1)];
    const out = resolveLinkedPatches({ bank: 1, slot: 1 }, patch(), all, { implicit_by_position: true });
    expect(out).toEqual([]);
  });

  it("ignores any legacy explicit linked_to on the patch", () => {
    const all = [summary(1, 1), summary(2, 3)];
    const out = resolveLinkedPatches(
      { bank: 1, slot: 1 },
      patch({ linked_to: [{ bank: 2, slot: 3 }] }),
      all,
      undefined,
    );
    expect(out).toEqual([]);
  });

  it("drops linked targets that don't exist", () => {
    const all = [summary(1, 1)]; // no bank 2
    const out = resolveLinkedPatches({ bank: 1, slot: 1 }, patch(), all, { locked_slots: [1] });
    expect(out).toEqual([]);
  });
});

describe("applyLockToggle (commit only after a successful write)", () => {
  const patches = [summary(1, 1), summary(2, 1), summary(1, 2)];

  it("on success returns a NEW device with the slot toggled, input untouched", async () => {
    const device = { patch_link: { locked_slots: [1] }, tft: { layout: [{ field: "patch_name" }] } };
    let putArg: Record<string, unknown> | null = null;
    const next = await applyLockToggle(device, patches, 2, async (d) => { putArg = d; });
    expect((next.patch_link as { locked_slots: number[] }).locked_slots).toEqual([1, 2]);
    // The firmware received exactly the device we return.
    expect(putArg).toBe(next);
    // Other device fields ride along - the lock toggle must not drop the TFT layout.
    expect(next.tft).toEqual(device.tft);
    // Input device is never mutated.
    expect((device.patch_link as { locked_slots: number[] }).locked_slots).toEqual([1]);
  });

  it("REPRO: a failing write ('not connected') does NOT commit - input stays as-is", async () => {
    const device = { patch_link: { locked_slots: [1] }, tft: { layout: [{ field: "patch_name" }] } };
    const before = JSON.parse(JSON.stringify(device));
    await expect(
      applyLockToggle(device, patches, 2, async () => { throw new Error("not connected"); }),
    ).rejects.toThrow("not connected");
    // The bug was an optimistic update that left the padlock toggled even
    // though the pedal never saved it. With the fix the caller only assigns
    // globalDevice on success, and the input is untouched here.
    expect(device).toEqual(before);
  });

  it("toggling an unlocked slot locks it; toggling again unlocks it", async () => {
    const device: Record<string, unknown> = { patch_link: {} };
    const noop = async () => {};
    const locked = await applyLockToggle(device, patches, 1, noop);
    expect((locked.patch_link as { locked_slots: number[] }).locked_slots).toEqual([1]);
    const unlocked = await applyLockToggle(locked, patches, 1, noop);
    expect((unlocked.patch_link as { locked_slots: number[] }).locked_slots).toEqual([]);
  });
});

describe("retargetOnEnterBank (mirror across banks fixes the rig-select bank)", () => {
  it("REPRO: a locked patch mirrored to bank 2 must address bank 2, not the source bank", () => {
    // The bug: editing slot-4 in bank 1 propagated its on_enter verbatim to
    // bank 2, so patch 2/4 fired kemper_rig bank=1. A long-press bank-up then
    // showed bank 2 on the TFT but told the Kemper to load bank 1 rig 4.
    const p = patch({
      name: "HEAVY",
      on_enter: { messages: [{ type: "kemper_rig", bank: 1, rig: 4, channel: 1 }] },
    });
    retargetOnEnterBank(p, 2);
    expect(p.on_enter!.messages[0]).toEqual({ type: "kemper_rig", bank: 2, rig: 4, channel: 1 });
  });

  it("leaves the rig-in-bank untouched (linked targets share the slot)", () => {
    const p = patch({ on_enter: { messages: [{ type: "kemper_rig", bank: 5, rig: 3, channel: 1 }] } });
    retargetOnEnterBank(p, 9);
    expect(p.on_enter!.messages[0]).toMatchObject({ bank: 9, rig: 3 });
  });

  it("retargets every bank-bearing message and ignores ones without a bank", () => {
    const p = patch({
      on_enter: { messages: [
        { type: "kemper_rig", bank: 1, rig: 2, channel: 1 },
        { type: "kemper_effect_toggle", slot: "A", value: "on", channel: 1 },
        { type: "kemper_rig", bank: 1, rig: 5, channel: 1 },
      ] },
    });
    retargetOnEnterBank(p, 3);
    expect(p.on_enter!.messages.map((m: Record<string, unknown>) => m.bank)).toEqual([3, undefined, 3]);
  });

  it("no-ops when the patch has no on_enter", () => {
    const p = patch();
    expect(() => retargetOnEnterBank(p, 4)).not.toThrow();
    expect(p.on_enter).toBeUndefined();
  });
});
