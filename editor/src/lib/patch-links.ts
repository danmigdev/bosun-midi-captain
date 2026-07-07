// Patch link resolution and propagation helpers.
//
// Linking is driven entirely by a per-column lock: when a slot is locked
// (device.patch_link.locked_slots), every patch at that slot number is
// linked across banks, so editing one propagates to all the others in the
// same column. The default is UNLINKED: with no explicit `locked_slots`,
// nothing is locked.
//
// The legacy `implicit_by_position` global toggle (treated as "every slot
// locked") is NO LONGER honoured - it was never settable from the current UI,
// so the only way it could appear was inside an old backup, where it silently
// linked every bank. Reading it is harmless (the field is kept in the type so
// old configs still parse) but it no longer locks anything; a config carrying
// it now starts unlinked, and the first lock toggle migrates it away by
// writing an explicit `locked_slots` list. Explicit per-patch `linked_to`
// links were removed.

import { type Patch, type PatchSummary } from "./protocol";

export interface LinkConfig {
  /** Legacy global toggle. No longer honoured (see file header): kept only so
   *  old configs that still carry it parse without error. It never locks a
   *  column and is dropped on the first lock toggle. */
  implicit_by_position?: boolean;
  /** Per-column lock: slot numbers whose patches are linked across banks
   *  (a closed padlock in the grid header / editor). */
  locked_slots?: number[];
}

/** The set of slot numbers whose columns are locked (linked across banks).
 *  Only explicit `locked_slots` lock a column; the default is unlinked. The
 *  legacy `implicit_by_position` flag is ignored (`patches` is unused now but
 *  kept for a stable signature). */
export function lockedSlots(
  linkConfig: LinkConfig | undefined,
  patches: PatchSummary[],
): Set<number> {
  void patches;
  if (linkConfig?.locked_slots) return new Set(linkConfig.locked_slots);
  return new Set();
}

/** Is this slot's column locked? */
export function isSlotLocked(
  slot: number,
  linkConfig: LinkConfig | undefined,
  patches: PatchSummary[],
): boolean {
  return lockedSlots(linkConfig, patches).has(slot);
}

/** New patch_link config after flipping `slot`'s lock. Always returns an
 *  explicit `locked_slots` list, migrating away from `implicit_by_position`. */
export function toggledLock(
  linkConfig: LinkConfig | undefined,
  patches: PatchSummary[],
  slot: number,
): LinkConfig {
  const set = lockedSlots(linkConfig, patches);
  if (set.has(slot)) set.delete(slot);
  else set.add(slot);
  return { locked_slots: Array.from(set).sort((a, b) => a - b) };
}

/** Apply a column-lock toggle to a device.json and persist it via `put`.
 *
 *  Correctness contract (regression-guarded by tests):
 *   - The input `device` is NEVER mutated and only the RETURNED device
 *     carries the new patch_link, so the caller can commit to UI state
 *     ONLY after `put` resolves. A failed `put` (e.g. "not connected")
 *     therefore can't leave the padlock showing a state the firmware
 *     never saved.
 *   - Every other device field (tft.layout, kemper, ...) is preserved -
 *     we only swap `patch_link`.
 *
 *  `put` throws on failure; this function propagates that so the caller
 *  can route "not connected" into a reconnect and anything else into an
 *  error toast. */
export async function applyLockToggle(
  device: Record<string, unknown>,
  patches: PatchSummary[],
  slot: number,
  put: (device: Record<string, unknown>) => Promise<void>,
): Promise<Record<string, unknown>> {
  const linkConfig = device.patch_link as LinkConfig | undefined;
  const next = { ...device, patch_link: toggledLock(linkConfig, patches, slot) };
  await put(next);
  return next;
}

/** Drop the host patch from its own link list and de-duplicate. */
function dedupAndDropSelf(
  links: Array<{ bank: number; slot: number }>,
  self: { bank: number; slot: number },
): Array<{ bank: number; slot: number }> {
  const seen = new Set<string>();
  const out: Array<{ bank: number; slot: number }> = [];
  for (const l of links) {
    if (l.bank === self.bank && l.slot === self.slot) continue;
    const k = `${l.bank}/${l.slot}`;
    if (seen.has(k)) continue;
    seen.add(k);
    out.push({ bank: l.bank, slot: l.slot });
  }
  return out;
}

/** The patches `current` is linked to: same slot in every other bank when
 *  this slot's column is locked, filtered to targets that actually exist.
 *  `patch` is unused now (kept for a stable signature) - linking no longer
 *  reads per-patch `linked_to`. */
export function resolveLinkedPatches(
  current: { bank: number; slot: number },
  patch: Patch,
  allPatches: PatchSummary[],
  linkConfig: LinkConfig | undefined,
): Array<{ bank: number; slot: number }> {
  void patch;
  const candidates: Array<{ bank: number; slot: number }> = [];
  if (isSlotLocked(current.slot, linkConfig, allPatches)) {
    for (const p of allPatches) {
      if (p.slot === current.slot) {
        candidates.push({ bank: p.bank, slot: p.slot });
      }
    }
  }
  const merged = dedupAndDropSelf(candidates, current);
  // Filter to targets that actually exist - we don't create patches as a
  // side effect of propagation.
  const existing = new Set(allPatches.map(p => `${p.bank}/${p.slot}`));
  return merged.filter(t => existing.has(`${t.bank}/${t.slot}`));
}

/** Retarget a mirrored patch's on_enter rig-select to a different bank.
 *  Linked patches share the same slot across banks, so when the source
 *  patch is copied into `targetBank` its on_enter messages must address that
 *  bank - otherwise the mirror loads the right patch on screen but tells the
 *  device to switch to the SOURCE bank (the "bank-up shows bank 2 but the
 *  Kemper stays on bank 1" bug). The rig-in-bank is unchanged (same slot).
 *  Mutates `patch` in place and returns it. Any on_enter message carrying a
 *  numeric `bank` field (e.g. kemper_rig) is retargeted, keeping it
 *  plugin-agnostic. */
export function retargetOnEnterBank(patch: Patch, targetBank: number): Patch {
  for (const m of (patch.on_enter?.messages ?? []) as Array<Record<string, unknown>>) {
    if (typeof m.bank === "number") m.bank = targetBank;
  }
  return patch;
}
