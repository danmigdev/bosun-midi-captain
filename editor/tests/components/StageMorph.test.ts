import { describe, it, expect, vi } from "vitest";
import { render, screen, fireEvent, waitFor } from "@testing-library/svelte";
import StageMorph from "../../src/components/StageMorph.svelte";
import { morphButtonBinding, morphPercent, morphValue } from "../../src/lib/morph";

const context = (value = 83, revision = 1) => ({
  kemper_morph_value: value, kemper_morph_source: value < 0 ? "unknown" : "commanded",
  kemper_morph_ready: "on", kemper_morph_revision: revision,
});
const props = () => ({ context: context(), connected: true, ready: true,
  identity: "kemper/1/1/2", oncommand: vi.fn().mockResolvedValue(undefined) });

describe("Stage Morph", () => {
  it("labels the bar as a commanded position, and has no invented progress for unknown state", async () => {
    const { rerender } = render(StageMorph, { props: props() });
    expect(screen.getByText("Set 65%")).toBeInTheDocument();
    expect(screen.getByRole("meter")).toHaveAttribute("aria-valuenow", "65");
    await rerender({ context: context(-1, 2) });
    expect(screen.getByRole("meter")).toHaveAttribute("aria-valuetext", "Unknown");
    expect(screen.getByRole("meter")).not.toHaveAttribute("aria-valuenow");
  });

  it("sends explicit endpoints and percentage, with controls hidden until opened", async () => {
    const p = props(); render(StageMorph, { props: p });
    expect(screen.queryByRole("slider")).not.toBeInTheDocument();
    await fireEvent.click(screen.getByRole("button", { name: "Morph controls" }));
    await fireEvent.click(screen.getByRole("button", { name: "Base", exact: true }));
    await waitFor(() => expect(p.oncommand).toHaveBeenLastCalledWith("position", 0));
    await fireEvent.click(screen.getByRole("button", { name: "Morph", exact: true }));
    await waitFor(() => expect(p.oncommand).toHaveBeenLastCalledWith("position", 100));
    const slider = screen.getByRole("slider");
    await fireEvent.input(slider, { target: { value: "40" } });
    await fireEvent.change(slider, { target: { value: "40" } });
    await waitFor(() => expect(p.oncommand).toHaveBeenLastCalledWith("position", 40));
  });

  it("does not predict a ramp when triggering the Kemper button", async () => {
    const p = props(); const { rerender } = render(StageMorph, { props: p });
    await fireEvent.click(screen.getByRole("button", { name: "Morph controls" }));
    await fireEvent.click(screen.getByRole("button", { name: "Trigger Morph" }));
    expect(p.oncommand).toHaveBeenCalledWith("trigger", undefined);
    await rerender({ context: context(-1, 2) });
    expect(screen.getByRole("meter")).toHaveAttribute("aria-valuetext", "Unknown");
  });

  it("does not revive a cached command after reconnecting", async () => {
    const { rerender } = render(StageMorph, { props: props() });
    await rerender({ connected: false, ready: false });
    await rerender({ connected: true, ready: true });
    expect(screen.getByRole("meter")).toHaveAttribute("aria-valuetext", "Unknown");
    await rerender({ context: context(127, 2) });
    expect(screen.getByText("Set 100%")).toBeInTheDocument();
  });

  it("disables input while the rig is not ready", async () => {
    const p = { ...props(), ready: false }; render(StageMorph, { props: p });
    await fireEvent.click(screen.getByRole("button", { name: "Morph controls" }));
    expect(screen.getByRole("slider")).toBeDisabled();
    expect(screen.getByRole("button", { name: "Trigger Morph" })).toBeDisabled();
    expect(p.oncommand).not.toHaveBeenCalled();
  });

  it("shows failures without claiming the requested position succeeded", async () => {
    const p = props(); p.oncommand.mockRejectedValue(new Error("midi_send_failed"));
    render(StageMorph, { props: p });
    await fireEvent.click(screen.getByRole("button", { name: "Morph controls" }));
    await fireEvent.click(screen.getByRole("button", { name: "Morph", exact: true }));
    await waitFor(() => expect(screen.getByRole("alert")).toHaveTextContent("midi_send_failed"));
    expect(screen.getByText("Set 65%")).toBeInTheDocument();
  });

  it("ignores a delayed failure after changing rigs", async () => {
    let reject!: (error: Error) => void;
    const p = props(); p.oncommand.mockImplementation(() => new Promise((_, fail) => { reject = fail; }));
    const { rerender } = render(StageMorph, { props: p });
    await fireEvent.click(screen.getByRole("button", { name: "Morph controls" }));
    await fireEvent.click(screen.getByRole("button", { name: "Base", exact: true }));
    await rerender({ identity: "kemper/1/2/3", context: context(-1, 3) });
    reject(new Error("old request"));
    await fireEvent.click(screen.getByRole("button", { name: "Morph controls" }));
    expect(screen.queryByRole("alert")).not.toBeInTheDocument();
    expect(screen.getByRole("meter")).toHaveAttribute("aria-valuetext", "Unknown");
  });

  it("creates a paired physical button binding and preserves MIDI endpoints", () => {
    const binding = morphButtonBinding("3", 2);
    expect(binding.mode).toBe("momentary");
    expect(binding.actions.press.messages).toEqual([{ type: "kemper_morph_trigger", channel: 2, state: "on" }]);
    expect(binding.actions.release.messages).toEqual([{ type: "kemper_morph_trigger", channel: 2, state: "off" }]);
    expect([morphValue(0), morphValue(100), morphPercent(0), morphPercent(127)]).toEqual([0, 127, 0, 100]);
  });
});
