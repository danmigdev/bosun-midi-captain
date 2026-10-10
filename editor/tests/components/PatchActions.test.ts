import { beforeEach, describe, expect, it, vi } from "vitest";
import { fireEvent, render, screen, waitFor } from "@testing-library/svelte";
import PatchActions from "../../src/components/PatchActions.svelte";

const mock = vi.hoisted(() => {
  const calls: string[] = [];
  const record = (name: string) => vi.fn(async (...args: unknown[]) => { calls.push([name, ...args].join(" ")); });
  return {
    calls,
    cmd: {
      discard: record("discard"), deletePatch: record("deletePatch"),
      listPatches: record("listPatches"), switchPatch: record("switchPatch"), putPatch: record("putPatch"),
    },
  };
});
vi.mock("../../src/lib/protocol", async (original) => ({
  ...await original<typeof import("../../src/lib/protocol")>(), cmd: mock.cmd,
}));
vi.mock("@tauri-apps/api/core", () => ({ invoke: vi.fn() }));
vi.mock("@tauri-apps/api/event", () => ({ listen: vi.fn(async () => () => {}) }));
beforeEach(() => { vi.clearAllMocks(); mock.calls.length = 0; });

// Two other patches carry unsaved edits that a delete must leave alone.
const patches = [
  { bank: 1, slot: 1, name: "Clean", dirty: false },
  { bank: 2, slot: 3, name: "Lead", dirty: true },
  { bank: 4, slot: 2, name: "Ambient", dirty: true },
];
const current = { bank: 2, slot: 3, patch: { name: "Lead" } };

describe("PatchActions delete", () => {
  it("discards only the deleted patch's unsaved edits, then deletes it", async () => {
    render(PatchActions, { patches, currentPatchEnvelope: current });
    await fireEvent.click(screen.getByRole("button", { name: "Delete…" }));
    await fireEvent.click(screen.getByRole("button", { name: "Delete", exact: true }));
    await waitFor(() => expect(mock.cmd.switchPatch).toHaveBeenCalled());
    expect(mock.calls).toEqual(["discard 2 3", "deletePatch 2 3", "listPatches", "switchPatch 1 1"]);
  });

  it("explains the deletion without CircuitPython wording", async () => {
    render(PatchActions, { patches, currentPatchEnvelope: current });
    await fireEvent.click(screen.getByRole("button", { name: "Delete…" }));
    const dialog = screen.getByRole("dialog");
    expect(dialog).toHaveTextContent("including its unsaved edits");
    expect(dialog).not.toHaveTextContent("CIRCUITPY");
  });
});
