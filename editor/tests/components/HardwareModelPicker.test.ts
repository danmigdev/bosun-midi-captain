import { beforeEach, describe, expect, it, vi } from "vitest";
import { fireEvent, render, screen, waitFor } from "@testing-library/svelte";
import HardwareModelPicker from "../../src/components/HardwareModelPicker.svelte";
import PatchActions from "../../src/components/PatchActions.svelte";
import { CAPTAIN_10, MINI_6, type HardwareLayout } from "../../src/lib/hardware";

const protocol = vi.hoisted(() => ({
  cmd: {
    setHardware: vi.fn(async (_model: string): Promise<{ type: "ACK"; reboot?: boolean }> => ({ type: "ACK", reboot: false })),
    getDeviceInfo: vi.fn(async () => {}),
    putPatch: vi.fn(async () => ({})),
    listPatches: vi.fn(async () => ({})),
    switchPatch: vi.fn(async () => ({})),
  },
  waitForReboot: vi.fn(async (_budget?: number) => true),
}));
vi.mock("../../src/lib/protocol", async (original) => ({
  ...await original<typeof import("../../src/lib/protocol")>(), ...protocol,
}));
vi.mock("@tauri-apps/api/core", () => ({ invoke: vi.fn() }));
vi.mock("@tauri-apps/api/event", () => ({ listen: vi.fn(async () => () => {}) }));
beforeEach(() => vi.clearAllMocks());

/** What a native 0.8 pedal without a hardware record reports. */
const unconfirmed: HardwareLayout = { ...CAPTAIN_10, configured: false, models: ["captain10", "mini6"] };

describe("HardwareModelPicker", () => {
  it("explains that older firmware only runs as the 10-switch Captain", () => {
    render(HardwareModelPicker, { hardware: CAPTAIN_10 });
    expect(screen.queryAllByRole("radio")).toHaveLength(0);
    expect(screen.getByText(/always runs as the MIDI Captain \(10 switches\)/)).toBeInTheDocument();
  });

  it("confirms the running model without restarting the pedal", async () => {
    const onApplied = vi.fn();
    render(HardwareModelPicker, { hardware: unconfirmed, onApplied });
    expect(screen.getAllByRole("radio")).toHaveLength(2);
    expect(screen.getByText("current, not confirmed")).toBeInTheDocument();
    expect(screen.getByRole("radio", { name: /MIDI Captain \(10 switches\)/ })).toBeChecked();
    await fireEvent.click(screen.getByRole("button", { name: "Confirm model" }));
    await waitFor(() => expect(onApplied).toHaveBeenCalledWith(expect.objectContaining({ model: "captain10" })));
    expect(protocol.cmd.setHardware).toHaveBeenCalledWith("captain10");
    expect(protocol.waitForReboot).not.toHaveBeenCalled();
    expect(protocol.cmd.getDeviceInfo).toHaveBeenCalled();
  });

  it("switches to the Mini 6 and waits for the pedal to restart", async () => {
    protocol.cmd.setHardware.mockResolvedValueOnce({ type: "ACK", reboot: true });
    const onApplied = vi.fn(); const onBusy = vi.fn();
    render(HardwareModelPicker, { hardware: unconfirmed, onApplied, onBusy });
    await fireEvent.click(screen.getByRole("radio", { name: /MIDI Captain Mini 6 \(6 switches\)/ }));
    await fireEvent.click(screen.getByRole("button", { name: "Apply and restart" }));
    await waitFor(() => expect(onApplied).toHaveBeenCalledWith(MINI_6));
    expect(protocol.cmd.setHardware).toHaveBeenCalledWith("mini6");
    expect(protocol.waitForReboot).toHaveBeenCalledWith(15000);
    expect(onBusy.mock.calls).toEqual([[true], [false]]);
    expect(screen.getByRole("status")).toHaveTextContent("set up as a MIDI Captain Mini 6 (6 switches)");
  });

  it("has nothing to do for a confirmed model", () => {
    render(HardwareModelPicker, { hardware: { ...MINI_6, configured: true, models: ["captain10", "mini6"] } });
    expect(screen.getByText("current")).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Model confirmed" })).toBeDisabled();
  });

  it("reports a refused or unanswered change", async () => {
    protocol.cmd.setHardware.mockRejectedValueOnce(new Error("unsupported_hardware"));
    const onApplied = vi.fn();
    render(HardwareModelPicker, { hardware: unconfirmed, onApplied });
    await fireEvent.click(screen.getByRole("button", { name: "Confirm model" }));
    await waitFor(() => expect(screen.getByRole("status")).toHaveTextContent("Could not change the model"));
    expect(onApplied).not.toHaveBeenCalled();

    protocol.cmd.setHardware.mockResolvedValueOnce({ type: "ACK", reboot: true });
    protocol.waitForReboot.mockResolvedValueOnce(false);
    await fireEvent.click(screen.getByRole("radio", { name: /Mini 6/ }));
    await fireEvent.click(screen.getByRole("button", { name: "Apply and restart" }));
    await waitFor(() => expect(screen.getByRole("status")).toHaveTextContent("did not come back"));
    expect(onApplied).not.toHaveBeenCalled();
  });

  it("is inert while its host is busy", () => {
    render(HardwareModelPicker, { hardware: unconfirmed, disabled: true });
    expect(screen.getByRole("button", { name: "Confirm model" })).toBeDisabled();
    for (const radio of screen.getAllByRole("radio")) expect(radio).toBeDisabled();
  });
});

describe("new patches follow the pedal model", () => {
  it("binds exactly the Mini 6 switches", async () => {
    render(PatchActions, { patches: [], currentPatchEnvelope: null, hardware: MINI_6 });
    await fireEvent.click(screen.getByRole("button", { name: "+ New patch" }));
    await fireEvent.click(screen.getByRole("button", { name: "Create", exact: true }));
    await waitFor(() => expect(protocol.cmd.putPatch).toHaveBeenCalled());
    const patch = (protocol.cmd.putPatch.mock.calls[0] as unknown[])[2] as { bindings: Array<{ switch: string }> };
    expect(patch.bindings.map((b) => b.switch)).toEqual(["1", "2", "3", "A", "B", "C"]);
  });

  it("keeps binding all ten switches on the 10-switch Captain", async () => {
    render(PatchActions, { patches: [], currentPatchEnvelope: null });
    await fireEvent.click(screen.getByRole("button", { name: "+ New patch" }));
    await fireEvent.click(screen.getByRole("button", { name: "Create", exact: true }));
    await waitFor(() => expect(protocol.cmd.putPatch).toHaveBeenCalled());
    const patch = (protocol.cmd.putPatch.mock.calls[0] as unknown[])[2] as { bindings: Array<{ switch: string }> };
    expect(patch.bindings.map((b) => b.switch)).toEqual(CAPTAIN_10.switches);
  });
});
