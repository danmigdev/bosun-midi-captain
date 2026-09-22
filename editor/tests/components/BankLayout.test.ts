import { beforeEach, describe, expect, it, vi } from "vitest";
import { fireEvent, render, screen, waitFor } from "@testing-library/svelte";
import Settings from "../../src/components/Settings.svelte";
import PatchActions from "../../src/components/PatchActions.svelte";
import { getBankCount, getRigsPerBank, nextFreePatch } from "../../src/lib/bank-layout";

const commands = vi.hoisted(() => ({
  putGlobal: vi.fn(async (_device: Record<string, unknown>) => ({})), getGlobal: vi.fn(async () => ({})),
  putPatch: vi.fn(async () => ({})), listPatches: vi.fn(async () => ({})),
  switchPatch: vi.fn(async () => ({})),
}));
vi.mock("../../src/lib/protocol", async (original) => ({
  ...await original<typeof import("../../src/lib/protocol")>(), cmd: commands,
}));
vi.mock("@tauri-apps/api/core", () => ({ invoke: vi.fn() }));
vi.mock("@tauri-apps/api/event", () => ({ listen: vi.fn(async () => () => {}) }));
beforeEach(() => vi.clearAllMocks());

const patches = [1, 2, 3].map(slot => ({ bank: 1, slot, name: `Rig ${slot}`, dirty: false }));

describe("profile bank layout", () => {
  it.each([undefined, null, 0, -1, 126, 2.5, "2", true, NaN, Infinity])(
    "keeps all 125 banks for missing or invalid stored limit %s", value => {
      expect(getBankCount({ bank_count: value })).toBe(125);
    },
  );

  it("saves a two-bank limit per profile and rejects invalid input", async () => {
    const view = render(Settings, { device: { rigs_per_bank: 3, custom: { keep: 42 } } });
    const input = screen.getByLabelText("Number of banks");
    const save = screen.getByRole("button", { name: "Save settings" });
    expect(input).toHaveValue(125);
    for (const value of ["0", "126", "1.5", ""]) {
      await fireEvent.input(input, { target: { value } });
      expect(save).toBeDisabled();
      await fireEvent.click(save);
      expect(commands.putGlobal).not.toHaveBeenCalled();
    }
    await fireEvent.input(input, { target: { value: "2" } });
    await fireEvent.click(save);
    await waitFor(() => expect(commands.putGlobal).toHaveBeenCalledOnce());
    expect(commands.putGlobal.mock.calls[0][0]).toMatchObject({ bank_count: 2, rigs_per_bank: 3, custom: { keep: 42 } });
    await view.rerender({ device: { bank_count: 2 } });
    expect(input).toHaveValue(2);
    await view.rerender({ device: { kemper: {} }, activeKind: "kemper_player" });
    expect(input).toHaveValue(125);
  });

  it("stops New and Clone at the configured bank limit", async () => {
    const full = [1, 2].flatMap(bank => patches.map(p => ({ ...p, bank })));
    expect(nextFreePatch(full, 3, 2)).toBeNull();
    const view = render(PatchActions, { patches: full, rigsPerBank: 3, bankCount: 2,
      currentPatchEnvelope: { bank: 1, slot: 1, patch: { name: "Clean" } } });
    for (const name of ["+ New patch", "Clone…"]) {
      await fireEvent.click(screen.getByRole("button", { name }));
      expect(screen.getByRole("alert")).toHaveTextContent("All bank slots are occupied");
      expect(screen.queryByRole("dialog")).not.toBeInTheDocument();
      expect(commands.putPatch).not.toHaveBeenCalled();
    }
    await view.rerender({ patches });
    await fireEvent.click(screen.getByRole("button", { name: "+ New patch" }));
    const bank = screen.getByLabelText("Bank");
    expect(bank).toHaveValue(2);
    expect(bank).toHaveAttribute("max", "2");
    await fireEvent.input(bank, { target: { value: "3" } });
    expect(screen.getByRole("button", { name: "Create", exact: true })).toBeDisabled();
  });

  it.each([undefined, null, 0, 11, 3.5, "3", true, NaN, Infinity])(
    "keeps the five-rig default for missing or invalid stored value %s", value => {
      expect(getRigsPerBank({ rigs_per_bank: value })).toBe(5);
    },
  );

  it("saves three rigs with the current profile settings and loads five for another profile", async () => {
    const device = { device_name: "Three-rig device", midi_channel: 7,
      preset_navigation: { switches: { A: 1, B: 2, C: 3 } }, custom: { keep: 42 } };
    const view = render(Settings, { device });
    const select = screen.getByLabelText("Rigs per bank");
    expect(select).toHaveValue("5");
    await fireEvent.change(select, { target: { value: "3" } });
    await fireEvent.click(screen.getByRole("button", { name: "Save settings" }));
    await waitFor(() => expect(commands.putGlobal).toHaveBeenCalledOnce());
    expect(commands.putGlobal.mock.calls[0][0]).toMatchObject({ ...device, rigs_per_bank: 3 });
    expect(device).not.toHaveProperty("rigs_per_bank");
    await view.rerender({ device: { ...device, rigs_per_bank: 3 } });
    expect(select).toHaveValue("3");
    await view.rerender({ device: { device_name: "Kemper", kemper: {} }, activeKind: "kemper_player" });
    expect(select).toHaveValue("5");
  });

  it("starts a new bank after rig three and rejects a manually entered fourth slot", async () => {
    render(PatchActions, { patches, currentPatchEnvelope: null, rigsPerBank: 3 });
    await fireEvent.click(screen.getByRole("button", { name: "+ New patch" }));
    expect(screen.getByLabelText("Bank")).toHaveValue(2);
    const slot = screen.getByLabelText("Slot");
    expect(slot).toHaveValue(1);
    expect(slot).toHaveAttribute("max", "3");
    const create = screen.getByRole("button", { name: "Create", exact: true });
    for (const value of ["4", "0", "1.5", ""]) {
      await fireEvent.input(slot, { target: { value } });
      expect(create).toBeDisabled();
      await fireEvent.click(create);
      expect(commands.putPatch).not.toHaveBeenCalled();
    }
    await fireEvent.input(slot, { target: { value: "1" } });
    await fireEvent.click(create);
    await waitFor(() => expect(commands.putPatch).toHaveBeenCalledWith(2, 1, expect.any(Object)));
    expect(commands.switchPatch).toHaveBeenCalledWith(2, 1);
  });

  it("waits for the profile configuration before offering a new patch", async () => {
    const view = render(PatchActions, { patches, currentPatchEnvelope: null, ready: false });
    const add = screen.getByRole("button", { name: "+ New patch" });
    expect(add).toBeDisabled();
    await fireEvent.click(add);
    expect(screen.queryByRole("dialog")).not.toBeInTheDocument();
    await view.rerender({ ready: true, rigsPerBank: 3 });
    await fireEvent.click(add);
    expect(screen.getByLabelText("Bank")).toHaveValue(2);
    expect(screen.getByLabelText("Slot")).toHaveValue(1);
  });

  it("clones into the next three-rig bank without changing saved MIDI assignments", async () => {
    const patch = { name: "Lead", on_enter: { messages: [{ type: "pc", channel: 7, program: 12 }] }, bindings: [] };
    render(PatchActions, { patches, rigsPerBank: 3, currentPatchEnvelope: { bank: 1, slot: 3, patch } });
    await fireEvent.click(screen.getByRole("button", { name: "Clone…" }));
    await fireEvent.click(screen.getByRole("button", { name: "Clone", exact: true }));
    await waitFor(() => expect(commands.putPatch).toHaveBeenCalledWith(2, 1, { ...patch, name: "Lead copy" }));
    expect(patch.name).toBe("Lead");
  });

  it("does not overwrite the first patch when all configured bank slots are full", async () => {
    const full = Array.from({ length: 125 }, (_, index) => ({ bank: index + 1, slot: 1, name: "Full", dirty: false }));
    expect(nextFreePatch(full, 1)).toBeNull();
    render(PatchActions, { patches: full, currentPatchEnvelope: null, rigsPerBank: 1 });
    await fireEvent.click(screen.getByRole("button", { name: "+ New patch" }));
    expect(screen.getByRole("alert")).toHaveTextContent("All bank slots are occupied");
    expect(screen.queryByRole("dialog")).not.toBeInTheDocument();
    expect(commands.putPatch).not.toHaveBeenCalled();
  });

  it("uses all ten supported slots and fills gaps before advancing", () => {
    expect(nextFreePatch(patches, 5)).toEqual({ bank: 1, slot: 4 });
    expect(nextFreePatch([{ bank: 1, slot: 1 }, { bank: 1, slot: 3 }], 3)).toEqual({ bank: 1, slot: 2 });
    const nine = Array.from({ length: 9 }, (_, index) => ({ bank: 1, slot: index + 1 }));
    expect(nextFreePatch(nine, 10)).toEqual({ bank: 1, slot: 10 });
    expect(nextFreePatch([...nine, { bank: 1, slot: 10 }], 10)).toEqual({ bank: 2, slot: 1 });
  });
});
