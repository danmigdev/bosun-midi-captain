// Component tests for the PatchesGrid: bank/slot tile grid, search filter,
// dirty/live badges and lock toggles.
//
// PatchesGrid transitively imports src/lib/protocol.ts, which pulls in the
// Tauri runtime bindings at module scope; mock them so any accidental IPC
// during a render surfaces as a mock call instead of a real invoke.

import { describe, it, expect, vi } from "vitest";
import { render, screen, fireEvent } from "@testing-library/svelte";
import userEvent from "@testing-library/user-event";
import PatchesGrid from "../../src/components/PatchesGrid.svelte";
import type { PatchSummary } from "../../src/lib/protocol";
import type { LinkConfig } from "../../src/lib/patch-links";

vi.mock("@tauri-apps/api/core");

function patch(bank: number, slot: number, name: string): PatchSummary {
  return { bank, slot, name, dirty: false };
}

function renderGrid(
  patches: PatchSummary[],
  opts: {
    deviceInfo?: { bank: number; slot: number } | null;
    dirtyIds?: Array<{ bank: number; slot: number }>;
    linkConfig?: LinkConfig;
    onToggleLock?: (slot: number) => void;
    onOpen?: (bank: number, slot: number) => void;
    onCreate?: (bank: number, slot: number) => void;
  } = {},
) {
  return render(PatchesGrid, {
    patches,
    deviceInfo: opts.deviceInfo ?? null,
    dirtyIds: opts.dirtyIds ?? [],
    linkConfig: opts.linkConfig,
    onToggleLock: opts.onToggleLock,
    onOpen: opts.onOpen ?? vi.fn(),
    onCreate: opts.onCreate ?? vi.fn(),
  });
}

describe("PatchesGrid", () => {
  it("renders bank headers and one tile per patch", () => {
    renderGrid([
      patch(1, 1, "Lead"),
      patch(1, 2, "Clean"),
      patch(2, 3, "Reverb"),
    ]);

    // Bank row headers.
    expect(screen.getByText("B01")).toBeInTheDocument();
    expect(screen.getByText("B02")).toBeInTheDocument();

    // Each patch tile shows its id and name.
    expect(screen.getByText("01/01")).toBeInTheDocument();
    expect(screen.getByText("Lead")).toBeInTheDocument();
    expect(screen.getByText("01/02")).toBeInTheDocument();
    expect(screen.getByText("Clean")).toBeInTheDocument();
    expect(screen.getByText("02/03")).toBeInTheDocument();
    expect(screen.getByText("Reverb")).toBeInTheDocument();

    // Unused slots render "create" placeholders, not empty holes.
    expect(screen.getByTitle("Create patch at 1/3")).toBeInTheDocument();
    expect(screen.getByTitle("Create patch at 2/1")).toBeInTheDocument();
  });

  it("shows an empty state when there are no patches", () => {
    const { container } = renderGrid([]);

    // No bank rows, no tiles, no placeholders - just the column headers.
    expect(container.querySelectorAll(".tile")).toHaveLength(0);
    expect(screen.queryByText("B01")).not.toBeInTheDocument();
    expect(screen.queryByText("01/01")).not.toBeInTheDocument();
    expect(screen.queryByTitle(/Create patch at/)).not.toBeInTheDocument();
  });

  it("opens a patch when its tile is clicked", () => {
    const onOpen = vi.fn();
    renderGrid([patch(1, 1, "Lead")], { onOpen });

    fireEvent.click(screen.getByText("Lead"));
    expect(onOpen).toHaveBeenCalledWith(1, 1);
  });

  it("creates a patch when a placeholder is clicked", () => {
    const onCreate = vi.fn();
    renderGrid([patch(1, 1, "Lead")], { onCreate });

    fireEvent.click(screen.getByTitle("Create patch at 1/2"));
    expect(onCreate).toHaveBeenCalledWith(1, 2);
  });

  it("filters by name, dims non-matches and hides placeholders", async () => {
    const user = userEvent.setup();
    const { container } = renderGrid([
      patch(1, 1, "Lead"),
      patch(1, 2, "Clean"),
    ]);

    await user.type(screen.getByLabelText("Filter patches by name"), "lead");

    // Match count is reported; the matching cell stays full-opacity.
    expect(screen.getByText("1 match")).toBeInTheDocument();
    expect(screen.getByText("Lead").closest(".cell")).not.toHaveClass("dimmed");

    // The non-matching tile is still rendered, but its cell is dimmed.
    const cleanCell = screen.getByText("Clean").closest(".cell");
    expect(cleanCell).toHaveClass("dimmed");

    // Create-placeholders hide while a filter is active.
    expect(container.querySelectorAll(".tile.placeholder")).toHaveLength(0);

    // Clearing the filter restores the full grid.
    fireEvent.click(screen.getByLabelText("Clear filter"));
    expect(screen.getByText("Clean").closest(".cell")).not.toHaveClass("dimmed");
    expect(screen.getByTitle("Create patch at 1/3")).toBeInTheDocument();
  });

  it("shows a dirty badge only on the dirty patches", () => {
    const { container } = renderGrid([
      patch(1, 1, "Lead"),
      patch(1, 2, "Clean"),
    ], { dirtyIds: [{ bank: 1, slot: 1 }] });

    const dirtyDots = container.querySelectorAll(".dot.dirty");
    expect(dirtyDots).toHaveLength(1);
    // The badge sits inside the 01/01 tile.
    expect(screen.getByTitle("unsaved").closest(".tile")).toHaveTextContent("Lead");
    expect(screen.getByText("Clean").closest(".tile").querySelector(".dot.dirty")).toBeNull();
  });

  it("shows a live badge on the active patch", () => {
    const { container } = renderGrid([patch(1, 1, "Lead")], {
      deviceInfo: { bank: 1, slot: 1 },
    });
    expect(container.querySelectorAll(".dot.live")).toHaveLength(1);
    expect(screen.getByTitle("live").closest(".tile")).toHaveTextContent("Lead");
  });

  it("shows a locked padlock for locked slots and toggles a lock", () => {
    const onToggleLock = vi.fn();

    // Without linkConfig no column is locked.
    const { container } = renderGrid([patch(1, 1, "Lead")], { onToggleLock });
    expect(container.querySelector(".lock.locked")).toBeNull();
    expect(screen.getByLabelText("Lock slot 1 across banks")).toHaveAttribute("aria-pressed", "false");

    // With slot 1 locked, its padlock is closed and toggle fires with the slot.
    renderGrid([patch(1, 1, "Lead")], { onToggleLock, linkConfig: { locked_slots: [1] } });
    expect(screen.getByLabelText("Unlock slot 1")).toHaveAttribute("aria-pressed", "true");

    fireEvent.click(screen.getByLabelText("Unlock slot 1"));
    expect(onToggleLock).toHaveBeenCalledWith(1);
  });
});
