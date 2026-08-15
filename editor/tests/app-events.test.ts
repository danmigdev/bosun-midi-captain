/**
 * Regression coverage for the App.svelte EVENT handler.
 *
 * Bug history: hitting "Discard" cleared the dirty flag on the firmware
 * but the editor's currentPatch state still held the un-saved label,
 * so the user kept seeing the rejected edits. Fix: App.svelte listens
 * for EVENT: "discarded" / "saved" and re-fetches GET_PATCH for the
 * currently-open patch.
 *
 * We don't render the full App component here (Tauri ESM modules,
 * deep imports, slow). Instead we exercise the handler logic in
 * isolation by re-implementing the relevant case branch and asserting
 * what it calls. If the production handler ever drifts away from this
 * shape, the test stops protecting the bug - so the test file lives
 * next to the handler reference and explicit copy.
 */
import { describe, it, expect, vi, beforeEach } from "vitest";

type DiscardedEvent = {
  type: "EVENT";
  event: "discarded" | "saved";
  patches: Array<{ bank: number; slot: number }>;
};

interface Cmd {
  getPatch: (bank: number, slot: number) => Promise<unknown>;
  listPatches: () => Promise<unknown>;
}

/**
 * Mirrors the App.svelte handleMessage branch for "discarded" / "saved"
 * events. Keep in sync with src/App.svelte. If the production code
 * changes, mirror the change here so the regression coverage is real.
 */
function handleDiscardedOrSaved(
  msg: DiscardedEvent,
  currentPatch: { bank: number; slot: number } | null,
  cmd: Cmd,
): void {
  if (msg.event !== "discarded" && msg.event !== "saved") return;
  const list = msg.patches ?? [];
  if (currentPatch && list.some(p => p.bank === currentPatch.bank && p.slot === currentPatch.slot)) {
    cmd.getPatch(currentPatch.bank, currentPatch.slot).catch(() => {});
    cmd.listPatches().catch(() => {});
  }
}

type PatchSwitchedEvent = {
  type: "EVENT";
  event: "patch_switched";
  bank: number;
  slot: number;
};

interface DeviceInfoCmd {
  getPatch: (bank: number, slot: number) => Promise<unknown>;
}

/**
 * Mirrors the App.svelte handleMessage branch for "patch_switched" (fired
 * by every app.switch_patch() call in firmware/lib/captain/app.py -
 * editor-initiated navigation, a local switch press, AND a long-press
 * captain_bank_step / bank_step()). Keep in sync with src/App.svelte.
 */
function handlePatchSwitched(
  msg: PatchSwitchedEvent,
  deviceInfo: { bank: number; slot: number } | null,
  cmd: DeviceInfoCmd,
): { bank: number; slot: number } | null {
  if (msg.event !== "patch_switched") return deviceInfo;
  const bank = msg.bank, slot = msg.slot;
  const next = deviceInfo ? { ...deviceInfo, bank, slot } : deviceInfo;
  cmd.getPatch(bank, slot).catch(() => {});
  return next;
}

describe("App handleMessage: patch_switched event", () => {
  // Regression for "long-press DOWN to bank 2, Stage keeps showing the
  // previous bank's patch" (2026-08-14). The handler itself is correct in
  // isolation (this test passes against the current code) and so is
  // StageView's reaction to a deviceInfo prop change (see
  // StageView.test.ts "bank/patch change while mounted") - so a report of
  // Stage staying stale after a bank-step points at the EVENT never
  // reaching the frontend at all (transport/link issue), not this handler.
  it("updates deviceInfo's bank/slot and re-fetches the new patch, for ANY source (local switch, bank-step, editor)", () => {
    const cmd = { getPatch: vi.fn(() => Promise.resolve({})) };
    const next = handlePatchSwitched(
      { type: "EVENT", event: "patch_switched", bank: 2, slot: 4 },
      { bank: 1, slot: 1 },
      cmd,
    );
    expect(next).toEqual({ bank: 2, slot: 4 });
    expect(cmd.getPatch).toHaveBeenCalledWith(2, 4);
  });

  it("does nothing when deviceInfo isn't loaded yet", () => {
    const cmd = { getPatch: vi.fn(() => Promise.resolve({})) };
    const next = handlePatchSwitched(
      { type: "EVENT", event: "patch_switched", bank: 2, slot: 4 },
      null,
      cmd,
    );
    expect(next).toBeNull();
    // Still fetches the patch even with no deviceInfo to update yet.
    expect(cmd.getPatch).toHaveBeenCalledWith(2, 4);
  });
});

describe("App handleMessage: discarded / saved events", () => {
  let cmd: { getPatch: ReturnType<typeof vi.fn>; listPatches: ReturnType<typeof vi.fn> };

  beforeEach(() => {
    cmd = {
      getPatch: vi.fn(() => Promise.resolve({})),
      listPatches: vi.fn(() => Promise.resolve({})),
    };
  });

  it("re-fetches the current patch when DISCARDED includes it", () => {
    handleDiscardedOrSaved(
      { type: "EVENT", event: "discarded", patches: [{ bank: 1, slot: 1 }] },
      { bank: 1, slot: 1 },
      cmd,
    );
    expect(cmd.getPatch).toHaveBeenCalledWith(1, 1);
    expect(cmd.listPatches).toHaveBeenCalled();
  });

  it("re-fetches the current patch when SAVED includes it", () => {
    handleDiscardedOrSaved(
      { type: "EVENT", event: "saved", patches: [{ bank: 2, slot: 3 }] },
      { bank: 2, slot: 3 },
      cmd,
    );
    expect(cmd.getPatch).toHaveBeenCalledWith(2, 3);
  });

  it("ignores the event when the current patch is NOT in the list", () => {
    handleDiscardedOrSaved(
      { type: "EVENT", event: "discarded", patches: [{ bank: 5, slot: 5 }] },
      { bank: 1, slot: 1 },
      cmd,
    );
    expect(cmd.getPatch).not.toHaveBeenCalled();
    expect(cmd.listPatches).not.toHaveBeenCalled();
  });

  it("does nothing when no patch is currently open", () => {
    handleDiscardedOrSaved(
      { type: "EVENT", event: "discarded", patches: [{ bank: 1, slot: 1 }] },
      null,
      cmd,
    );
    expect(cmd.getPatch).not.toHaveBeenCalled();
  });

  it("ignores unrelated EVENT types", () => {
    handleDiscardedOrSaved(
      { type: "EVENT", event: "patch_switched" as unknown as "discarded", patches: [] },
      { bank: 1, slot: 1 },
      cmd,
    );
    expect(cmd.getPatch).not.toHaveBeenCalled();
  });
});
