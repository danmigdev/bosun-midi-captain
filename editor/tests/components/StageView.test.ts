// Component tests for StageView: the live "stage mode" screen showing the
// 2x5 pedal grid, the current rig/patch header, and live Kemper context
// (rig name, BPM, tuner) streamed from the firmware via CONTEXT messages.
//
// StageView transitively imports src/lib/protocol.ts, which pulls in the
// Tauri runtime bindings at module scope. The transport drains firmware
// lines via invoke("drain_inbox") after the listen("firmware-data-ready")
// doorbell, and sends commands via invoke("send_command", ...). Both are
// mocked here; the doorbell handler is captured so tests can feed CONTEXT
// messages into the subscriber bus exactly like the real firmware would.

import { describe, it, expect, vi, beforeEach, afterEach } from "vitest";
import { render, screen, fireEvent, waitFor, within } from "@testing-library/svelte";
import StageView from "../../src/components/StageView.svelte";
import { DEFAULT_LAYOUT } from "../../src/lib/pedal-layout";
import { FONT_STACKS } from "../../src/lib/stage-theme";
import {
  fallbackManifest,
  type Binding,
  type Manifest,
  type PatchSummary,
} from "../../src/lib/protocol";

// --- Tauri IPC shims ----------------------------------------------------

const eventHandlers = vi.hoisted(() => new Map<string, () => void>());
const invokeMock = vi.hoisted(() => vi.fn());

vi.mock("@tauri-apps/api/core", () => ({
  invoke: invokeMock,
}));

vi.mock("@tauri-apps/api/event", () => ({
  listen: vi.fn((event: string, handler: () => void) => {
    // Keep the doorbell handler for the whole file: protocol.ts registers
    // it once per process and the unlisten only detaches the native
    // callback, so tests can keep triggering drains across renders.
    eventHandlers.set(event, handler);
    return Promise.resolve(() => {});
  }),
}));

/** Firmware lines the next invoke("drain_inbox") call will return. */
let inboxLines: string[] = [];

beforeEach(() => {
  inboxLines = [];
  invokeMock.mockReset();
  invokeMock.mockImplementation((cmd: string) => {
    if (cmd === "drain_inbox") return Promise.resolve(inboxLines);
    return Promise.resolve(undefined);
  });
  // Stage theme persists to localStorage (see stage-theme.ts) - clear it so
  // one test's edits can't bleed into the next.
  localStorage.clear();
});

afterEach(() => { vi.useRealTimers(); });

/** Feed a firmware message through the mocked doorbell into the protocol
 *  subscriber bus (StageView subscribes on mount while connected). */
async function pushFirmwareMessage(msg: unknown): Promise<void> {
  const doorbell = await waitFor(() => {
    const handler = eventHandlers.get("firmware-data-ready");
    if (!handler) throw new Error("doorbell handler not registered yet");
    return handler;
  });
  inboxLines = [JSON.stringify(msg)];
  doorbell();
  // Let the drain promise chain (await invoke -> subscribers -> Svelte
  // state flush) settle before the test asserts.
  await new Promise((r) => setTimeout(r, 0));
}

// --- fixtures ------------------------------------------------------------

type DeviceInfo = {
  fw: string;
  device: string;
  bank: number;
  slot: number;
  profile?: string;
  stage_input?: boolean;
};

type StageProps = {
  deviceInfo: DeviceInfo | null;
  manifest: Manifest | null;
  device: Record<string, unknown> | null;
  connected: boolean;
  patches: PatchSummary[];
  onExit: () => void;
};

/** The ten switch IDs in DOM order (matches DEFAULT_LAYOUT row-major).
 * Lowercase firmware switch names; the component DISPLAYS them upper-cased
 * (see StageView.displaySwitch), so DOM assertions use DISPLAY_ORDER. */
const SWITCH_ORDER = ["1", "2", "3", "4", "up", "A", "B", "C", "D", "down"];
const DISPLAY_ORDER = ["1", "2", "3", "4", "UP", "A", "B", "C", "D", "DOWN"];

const DEVICE = { fw: "0.5.4", device: "midi_captain_10" };

it("routes Morph controls with the current profile and rig generation", async () => {
  renderStage({ deviceInfo: { ...DEVICE, bank: 1, slot: 1, profile: "kemper", stage_input: true } });
  const context = { bank: 1, slot: 1, kemper_generation: 7, kemper_morph_source: "commanded",
    kemper_morph_value: 83, kemper_morph_revision: 1, kemper_morph_ready: "on" };
  await pushFirmwareMessage({ type: "CONTEXT", context });
  expect(screen.getByText("Set 65%")).toBeInTheDocument();
  await fireEvent.click(screen.getByRole("button", { name: "Morph controls" }));
  await fireEvent.click(screen.getByRole("button", { name: "Base", exact: true }));
  const command = await waitFor(() => {
    const calls = invokeMock.mock.calls.filter(([name]) => name === "send_command")
      .map(([, args]) => JSON.parse(args.line));
    const message = calls.find(m => m.type === "MORPH_CONTROL");
    expect(message).toBeDefined(); return message;
  });
  expect(command).toMatchObject({ profile: "kemper", bank: 1, slot: 1, generation: 7, action: "position", percent: 0 });
  await pushFirmwareMessage({ type: "ACK", id: command.id });
  await pushFirmwareMessage({ type: "CONTEXT", context: { ...context, kemper_morph_value: 0, kemper_morph_revision: 2 } });
  expect(screen.getByText("Set 0%")).toBeInTheDocument();
});

function patch(bank: number, slot: number, name: string): PatchSummary {
  return { bank, slot, name, dirty: false };
}

function binding(sw: string, overrides: Partial<Binding> = {}): Binding {
  return { switch: sw, mode: "tap", actions: {}, ...overrides };
}

function renderStage(overrides: Partial<StageProps> = {}) {
  return render(StageView, {
    deviceInfo: null,
    manifest: null,
    device: null,
    connected: true,
    patches: [],
    onExit: vi.fn(),
    ...overrides,
  });
}

function switchEls(container: HTMLElement): HTMLElement[] {
  return Array.from(container.querySelectorAll(".stage__switch"));
}

function labelOf(el: HTMLElement): string | null {
  return el.querySelector(".stage__switch-label")?.textContent ?? null;
}

function idOf(el: HTMLElement): string | null {
  return el.querySelector(".stage__switch-id")?.textContent ?? null;
}

function switchById(container: HTMLElement, id: string): HTMLElement | undefined {
  return switchEls(container).find((el) => idOf(el) === id);
}

type SentCommand = { type: string; id: string; bank?: number; slot?: number };

function sentCommands(type: string): SentCommand[] {
  return invokeMock.mock.calls
    .filter(([command]) => command === "send_command")
    .map(([, args]) => JSON.parse(args.line) as SentCommand)
    .filter(command => command.type === type);
}

async function navigationRequest(type: string, index = 0): Promise<SentCommand> {
  return waitFor(() => {
    const command = sentCommands(type)[index];
    if (!command) throw new Error(`Missing ${type} request ${index + 1}`);
    return command;
  });
}

function deviceReply(bank: number, slot: number) {
  return { type: "DEVICE_INFO", ...DEVICE, current: { bank, slot } };
}

async function replyTo(command: SentCommand, response: Record<string, unknown>) {
  await pushFirmwareMessage({ ...response, id: command.id });
}

async function navigateWithFreshState(
  direction: "Previous bank" | "Next bank", bank: number, slot: number, inventory: PatchSummary[],
) {
  await fireEvent.click(screen.getByRole("button", { name: direction }));
  await replyTo(await navigationRequest("GET_DEVICE_INFO"), deviceReply(bank, slot));
  await replyTo(await navigationRequest("LIST_PATCHES"), { type: "PATCH_LIST", patches: inventory });
}

async function confirmNavigation(command: SentCommand) {
  await replyTo(command, { type: "ACK" });
  await replyTo(await navigationRequest("GET_DEVICE_INFO", 1), deviceReply(command.bank!, command.slot!));
}

// --- tests ---------------------------------------------------------------

describe("StageView", () => {
  describe("switch input", () => {
    const info = { ...DEVICE, bank: 2, slot: 3, profile: "kemper", stage_input: true };
    const inventory = [patch(2, 3, "Current"), patch(9, 3, "Other bank")];
    const latchedBinding = (sw: string) => binding(sw, {
      mode: "latched", label: "Delay",
      actions: {
        toggle_on: { messages: [{ type: "cc", channel: 1, cc: 22, value: 127 }] },
        toggle_off: { messages: [{ type: "cc", channel: 1, cc: 22, value: 0 }] },
      },
    });
    async function loadInput(overrides: Partial<StageProps> = {}, inputBindings = [latchedBinding("1")]) {
      const view = renderStage({ deviceInfo: info, patches: inventory, ...overrides });
      const position = overrides.deviceInfo ?? info;
      await pushFirmwareMessage({
        type: "PATCH", bank: position.bank, slot: position.slot, profile: "", active_profile: position.profile,
        patch: { name: "Current", bindings: inputBindings },
      });
      return view;
    }

    it.each([["1", "1"], ["UP", "up"], ["A", "A"], ["DOWN", "down"]])("sends one atomic tap for %s using firmware ID %s and the displayed coordinates", async (display, firmwareId) => {
      const { container } = await loadInput({}, [latchedBinding(firmwareId)]);
      const tile = switchById(container, display)!;
      expect(tile).toHaveRole("button");
      expect(tile).toBeEnabled();
      await fireEvent.click(tile.querySelector(".stage__switch-label")!);
      const command = await navigationRequest("ACTIVATE_SWITCH");
      expect(command).toMatchObject({ switch: firmwareId, bank: 2, slot: 3, profile: "kemper" });
      expect(sentCommands("ACTIVATE_SWITCH")).toHaveLength(1);
      // Coordinates are guarded by the firmware. No extra preflight adds lag.
      expect(sentCommands("GET_DEVICE_INFO")).toHaveLength(0);
      await replyTo(command, { type: "ACK" });
      await waitFor(() => expect(sentCommands("GET_DEVICE_INFO")).toHaveLength(1));
      expect(tile).toBeEnabled();
      expect(tile).toHaveAttribute("aria-pressed", "false");
    });

    it("omits the profile argument when the device does not supply one", async () => {
      const { container } = await loadInput({ deviceInfo: { ...info, profile: undefined } });
      await fireEvent.click(switchById(container, "1")!);
      const command = await navigationRequest("ACTIVATE_SWITCH");
      expect(command).not.toHaveProperty("profile");
      await replyTo(command, { type: "ACK" });
    });

    it.each([undefined, false])("requires stage_input:true even on a native firmware, capability=%s", async stage_input => {
      const { container } = await loadInput({ deviceInfo: { ...info, fw: "0.6.5-native", stage_input } });
      const tile = switchById(container, "1")!;
      expect(tile).toBeDisabled();
      await fireEvent.click(tile);
      expect(sentCommands("ACTIVATE_SWITCH")).toHaveLength(0);
      expect(screen.getByText("Update Captain firmware to control switches from Stage.")).toBeInTheDocument();
      expect(screen.getByRole("button", { name: "Stage appearance" })).toBeEnabled();
      expect(screen.getByRole("button", { name: "Next bank" })).toBeEnabled();
    });

    it("waits for a matching patch before enabling bindings or navigation tiles", async () => {
      const { container } = renderStage({ deviceInfo: info, patches: inventory });
      for (const tile of switchEls(container)) expect(tile).toBeDisabled();
      await pushFirmwareMessage({ type: "PATCH", bank: 9, slot: 3, profile: "", active_profile: "kemper", patch: { bindings: [latchedBinding("1")] } });
      expect(switchById(container, "1")).toBeDisabled();
      await pushFirmwareMessage({ type: "PATCH", bank: 2, slot: 3, profile: "", active_profile: "kemper", patch: { bindings: [latchedBinding("1")] } });
      expect(switchById(container, "1")).toBeEnabled();
      expect(switchById(container, "2")).toBeDisabled();
      expect(sentCommands("ACTIVATE_SWITCH")).toHaveLength(0);
    });

    it("requests the active patch and rejects saved or stale-profile replies before accepting unsaved bindings", async () => {
      const { container } = renderStage({ deviceInfo: info, patches: inventory });
      const request = await navigationRequest("GET_PATCH");
      expect(request).toMatchObject({ bank: 2, slot: 3 });
      expect(request).not.toHaveProperty("profile");
      const saved = { type: "PATCH", bank: 2, slot: 3, profile: "kemper", active_profile: "kemper",
        patch: { bindings: [{ ...latchedBinding("1"), label: "Saved action" }] } };
      await pushFirmwareMessage(saved);
      expect(switchById(container, "1")).toBeDisabled();
      expect(container).not.toHaveTextContent("Saved action");
      await pushFirmwareMessage({ ...saved, profile: "", active_profile: "midi" });
      expect(switchById(container, "1")).toBeDisabled();
      await pushFirmwareMessage({ ...saved, profile: "", active_profile: undefined });
      expect(switchById(container, "1")).toBeDisabled();
      await replyTo(request, { type: "PATCH", bank: 2, slot: 3, profile: "", active_profile: "kemper", dirty: true,
        patch: { bindings: [{ ...latchedBinding("up"), label: "Unsaved action" }] } });
      expect(switchById(container, "UP")).toBeEnabled();
      expect(switchById(container, "UP")).toHaveTextContent("Unsaved action");
      expect(switchById(container, "1")).toBeDisabled();
      // Delayed saved-file replies must not overwrite the live binding map.
      await pushFirmwareMessage(saved);
      await pushFirmwareMessage({ ...saved, profile: "", active_profile: "midi" });
      expect(switchById(container, "UP")).toBeEnabled();
      expect(switchById(container, "UP")).toHaveTextContent("Unsaved action");
      expect(switchById(container, "1")).toBeDisabled();
    });

    it("uses the native switch action for mapped presets and keeps empty slots inert", async () => {
      const { container } = await loadInput({
        device: { preset_navigation: { switches: { A: 1, D: 10 } } },
        patches: [...inventory, patch(2, 1, "Acoustic")],
      });
      expect(switchById(container, "A")).toBeEnabled();
      expect(switchById(container, "D")).toBeDisabled();
      await fireEvent.click(switchById(container, "D")!);
      expect(sentCommands("ACTIVATE_SWITCH")).toHaveLength(0);
      await fireEvent.click(switchById(container, "A")!);
      const command = await navigationRequest("ACTIVATE_SWITCH");
      // The action targets switch A on the current patch, not a guessed slot.
      expect(command).toMatchObject({ switch: "A", bank: 2, slot: 3, profile: "kemper" });
      expect(sentCommands("SWITCH_PATCH")).toHaveLength(0);
      await replyTo(command, { type: "ACK" });
      expect(switchById(container, "A")).toHaveAttribute("aria-pressed", "false");
    });

    it("blocks repeated clicks, other switches and bank changes until ACK, then waits for firmware state to paint", async () => {
      const { container } = await loadInput({}, [latchedBinding("1"), latchedBinding("up")]);
      const tile = switchById(container, "1")!;
      const other = switchById(container, "UP")!;
      const contexts = sentCommands("GET_CONTEXT").length;
      await Promise.all([fireEvent.click(tile), fireEvent.click(tile), fireEvent.click(other)]);
      const command = await navigationRequest("ACTIVATE_SWITCH");
      expect(sentCommands("ACTIVATE_SWITCH")).toHaveLength(1);
      expect(tile).toHaveAttribute("aria-busy", "true");
      expect(tile).toBeDisabled();
      expect(other).toBeDisabled();
      const bank = screen.getByRole("button", { name: "Next bank" });
      expect(bank).toBeDisabled();
      const bankPicker = screen.getByRole("button", { name: "Choose bank" });
      expect(bankPicker).toBeDisabled();
      await fireEvent.click(bankPicker);
      expect(screen.queryByRole("dialog", { name: "Choose bank" })).not.toBeInTheDocument();
      await fireEvent.click(bank);
      expect(sentCommands("GET_DEVICE_INFO")).toHaveLength(0);
      await pushFirmwareMessage({ type: "ACK", id: "unrelated-switch" });
      expect(tile).toBeDisabled();
      expect(tile).not.toHaveClass("stage__switch--active");
      await replyTo(command, { type: "ACK" });
      await waitFor(() => expect(sentCommands("GET_CONTEXT").length).toBeGreaterThan(contexts));
      expect(tile).toBeEnabled();
      expect(bank).toBeEnabled();
      expect(bankPicker).toBeEnabled();
      expect(tile).toHaveAttribute("aria-pressed", "false");
      await pushFirmwareMessage({ type: "EVENT", event: "binding_fired", switch: "1", action: "toggle_on" });
      expect(tile).toHaveClass("stage__switch--active");
      expect(tile).toHaveAttribute("aria-pressed", "true");
      await pushFirmwareMessage({ type: "PATCH", bank: 2, slot: 3, profile: "", active_profile: "kemper",
        patch: { bindings: [latchedBinding("1"), latchedBinding("up")] } });
      expect(tile).toHaveAttribute("aria-pressed", "true");
      // Re-reading the same patch preserves confirmed state; an actual
      // patch switch at those coordinates still starts a new binding state.
      await pushFirmwareMessage({ type: "EVENT", event: "patch_switched", bank: 2, slot: 3 });
      expect(tile).toHaveAttribute("aria-pressed", "false");
    });

    it("blocks switch activation while bank navigation is pending", async () => {
      const { container } = await loadInput();
      const tile = switchById(container, "1")!;
      expect(tile).toBeEnabled();
      await fireEvent.click(screen.getByRole("button", { name: "Next bank" }));
      const freshInfo = await navigationRequest("GET_DEVICE_INFO");
      expect(tile).toBeDisabled();
      await fireEvent.click(tile);
      expect(sentCommands("ACTIVATE_SWITCH")).toHaveLength(0);
      await replyTo(freshInfo, { ...deviceReply(2, 3), profile: "kemper" });
      await replyTo(await navigationRequest("LIST_PATCHES"), { type: "PATCH_LIST", profile: "kemper", patches: inventory });
      const switchPatch = await navigationRequest("SWITCH_PATCH");
      expect(tile).toBeDisabled();
      await confirmNavigation(switchPatch);
      expect(sentCommands("ACTIVATE_SWITCH")).toHaveLength(0);
    });

    it.each(["firmware ERROR", "send failure", "unexpected reply"])("reports %s without retrying or painting a successful action", async failure => {
      const { container } = await loadInput();
      if (failure === "send failure") {
        const original = invokeMock.getMockImplementation()!;
        invokeMock.mockImplementation((command, args) => command === "send_command" && JSON.parse(args.line).type === "ACTIVATE_SWITCH"
          ? Promise.reject(new Error("write: connection lost")) : original(command, args));
      }
      const tile = switchById(container, "1")!;
      await fireEvent.click(tile);
      const command = await navigationRequest("ACTIVATE_SWITCH");
      if (failure !== "send failure") await replyTo(command, failure === "firmware ERROR"
        ? { type: "ERROR", error: "stale_state" } : { type: "CONTEXT", context: {} });
      expect(await screen.findByRole("alert")).toHaveTextContent("Switch action not confirmed.");
      expect(tile).toBeEnabled();
      expect(tile).toHaveAttribute("aria-pressed", "false");
      vi.useFakeTimers();
      await vi.advanceTimersByTimeAsync(6000);
      expect(sentCommands("ACTIVATE_SWITCH")).toHaveLength(1);
      expect(sentCommands("GET_DEVICE_INFO")).toHaveLength(0);
    });

    it("times out once without replaying a tap that may already have executed", async () => {
      const { container } = await loadInput();
      vi.useFakeTimers();
      await fireEvent.click(switchById(container, "1")!);
      await vi.advanceTimersByTimeAsync(0);
      expect(sentCommands("ACTIVATE_SWITCH")).toHaveLength(1);
      await vi.advanceTimersByTimeAsync(5001);
      expect(screen.getByRole("alert")).toHaveTextContent("Switch action not confirmed.");
      expect(switchById(container, "1")).toBeEnabled();
      await vi.advanceTimersByTimeAsync(6000);
      expect(sentCommands("ACTIVATE_SWITCH")).toHaveLength(1);
    });

    it("disables stale bindings until the patch at the new coordinates arrives", async () => {
      const { container, rerender } = await loadInput();
      await rerender({ deviceInfo: { ...info, slot: 4 } });
      await pushFirmwareMessage({ type: "PATCH", bank: 2, slot: 3, profile: "", active_profile: "kemper", patch: { bindings: [latchedBinding("1")] } });
      expect(switchById(container, "1")).toBeDisabled();
      await fireEvent.click(switchById(container, "1")!);
      expect(sentCommands("ACTIVATE_SWITCH")).toHaveLength(0);
      await pushFirmwareMessage({ type: "PATCH", bank: 2, slot: 4, profile: "", active_profile: "kemper", patch: { bindings: [latchedBinding("1")] } });
      expect(switchById(container, "1")).toBeEnabled();
    });

    it("invalidates old-profile bindings at identical coordinates and rejects their delayed PATCH", async () => {
      const { container, rerender } = await loadInput();
      await rerender({ deviceInfo: { ...info, profile: "midi" } });
      expect(switchById(container, "1")).toBeDisabled();
      await pushFirmwareMessage({ type: "PATCH", bank: 2, slot: 3, profile: "", active_profile: "kemper", patch: { bindings: [latchedBinding("1")] } });
      expect(switchById(container, "1")).toBeDisabled();
      await pushFirmwareMessage({ type: "PATCH", bank: 2, slot: 3, profile: "", active_profile: "midi", patch: { bindings: [latchedBinding("up")] } });
      expect(switchById(container, "1")).toBeDisabled();
      expect(switchById(container, "UP")).toBeEnabled();
    });

    it.each(["firmware-disconnected", "firmware-reconnecting"])("does not refresh or replay the action after %s overtakes its ACK", async event => {
      const { container, rerender } = await loadInput();
      await fireEvent.click(switchById(container, "1")!);
      const command = await navigationRequest("ACTIVATE_SWITCH");
      eventHandlers.get(event)!();
      await rerender({ connected: false });
      expect(switchById(container, "1")).toBeDisabled();
      await rerender({ connected: true });
      const contexts = sentCommands("GET_CONTEXT").length;
      await replyTo(command, { type: "ACK" });
      expect(sentCommands("ACTIVATE_SWITCH")).toHaveLength(1);
      expect(sentCommands("GET_DEVICE_INFO")).toHaveLength(0);
      expect(sentCommands("GET_CONTEXT")).toHaveLength(contexts);
    });

    it("does not refresh or emit another action after Stage closes with an ACK pending", async () => {
      const { container, unmount } = await loadInput();
      await fireEvent.click(switchById(container, "1")!);
      const command = await navigationRequest("ACTIVATE_SWITCH");
      const contexts = sentCommands("GET_CONTEXT").length;
      unmount();
      await replyTo(command, { type: "ACK" });
      expect(sentCommands("ACTIVATE_SWITCH")).toHaveLength(1);
      expect(sentCommands("GET_DEVICE_INFO")).toHaveLength(0);
      expect(sentCommands("GET_CONTEXT")).toHaveLength(contexts);
    });
  });

  describe("bank navigation", () => {
    it.each([
      ["Next bank", 2, 1], ["Previous bank", 1, 2],
      ["Next bank", 3, 1], ["Previous bank", 3, 2],
    ] as const)("%s from %i wraps within two configured banks to %i", async (direction, bank, targetBank) => {
      const inventory = [1, 2, 3].map(b => patch(b, 3, `Bank ${b}`));
      renderStage({ deviceInfo: { ...DEVICE, bank, slot: 3 }, device: { bank_count: 2 }, patches: inventory });
      await navigateWithFreshState(direction, bank, 3, inventory);
      const request = await navigationRequest("SWITCH_PATCH");
      expect(request).toMatchObject({ bank: targetBank, slot: 3 });
      await confirmNavigation(request);
    });

    it.each([{ banks: [1, 2, 3] }, { banks: [2, 3] }])("disables navigation with no other eligible bank ($banks)", async ({ banks }) => {
      renderStage({ deviceInfo: { ...DEVICE, bank: 1, slot: 1 }, device: { bank_count: 1 },
        patches: banks.map(bank => patch(bank, 1, `Bank ${bank}`)) });
      expect(screen.getByRole("button", { name: "Next bank" })).toBeDisabled();
      expect(screen.getByRole("button", { name: "Previous bank" })).toBeDisabled();
      expect(sentCommands("SWITCH_PATCH")).toHaveLength(0);
    });

    it("uses the freshly read bank limit if another client changed it", async () => {
      const inventory = [1, 2, 3].map(bank => patch(bank, 3, `Bank ${bank}`));
      renderStage({ deviceInfo: { ...DEVICE, bank: 2, slot: 3 }, device: { bank_count: 99 }, patches: inventory });
      await fireEvent.click(screen.getByRole("button", { name: "Next bank" }));
      await replyTo(await navigationRequest("GET_DEVICE_INFO"), { ...deviceReply(2, 3), bank_count: 2 });
      await replyTo(await navigationRequest("LIST_PATCHES"), { type: "PATCH_LIST", patches: inventory });
      const request = await navigationRequest("SWITCH_PATCH");
      expect(request).toMatchObject({ bank: 1, slot: 3 });
      await confirmNavigation(request);
    });

    it("keeps rig three when changing a generic device's three-rig bank", async () => {
      const inventory = [1, 2].flatMap(bank => [1, 2, 3].map(slot => patch(bank, slot, `${bank}/${slot}`)));
      renderStage({ deviceInfo: { ...DEVICE, bank: 1, slot: 3 },
        device: { rigs_per_bank: 3 }, patches: inventory });
      await navigateWithFreshState("Next bank", 1, 3, inventory);
      const request = await navigationRequest("SWITCH_PATCH");
      expect(request).toMatchObject({ bank: 2, slot: 3 });
      await confirmNavigation(request);
    });

    const inventory = [
      patch(9, 7, "Nine seven"), patch(25, 3, "Twenty-five"),
      patch(2, 3, "Two"), patch(9, 3, "Nine three"), patch(25, 1, "Twenty-five one"),
    ];

    it.each([
      ["Next bank", 2, 9], ["Next bank", 25, 2],
      ["Previous bank", 9, 2], ["Previous bank", 2, 25],
    ] as const)("%s from %i skips empty banks and wraps to %i", async (direction, bank, targetBank) => {
      renderStage({ deviceInfo: { ...DEVICE, bank, slot: 3 }, patches: inventory });
      await navigateWithFreshState(direction, bank, 3, inventory);
      const request = await navigationRequest("SWITCH_PATCH");
      expect(request).toMatchObject({ bank: targetBank, slot: 3 });
      await confirmNavigation(request);
      expect(sentCommands("SWITCH_PATCH")).toHaveLength(1);
    });

    it("uses the lowest existing destination slot when the current slot is absent", async () => {
      const sparse = [patch(9, 8, "Eight"), patch(2, 5, "Current"), patch(9, 2, "Two")];
      renderStage({ deviceInfo: { ...DEVICE, bank: 2, slot: 5 }, patches: sparse });
      await navigateWithFreshState("Next bank", 2, 5, sparse);
      const request = await navigationRequest("SWITCH_PATCH");
      expect(request).toMatchObject({ bank: 9, slot: 2 });
      await confirmNavigation(request);
    });

    it("resolves from fresh device position and inventory instead of the cached destination", async () => {
      renderStage({ deviceInfo: { ...DEVICE, bank: 2, slot: 3 }, patches: inventory });
      const fresh = [patch(9, 7, "Current now"), patch(40, 2, "New bank")];
      await navigateWithFreshState("Next bank", 9, 7, fresh);
      const request = await navigationRequest("SWITCH_PATCH");
      expect(request).toMatchObject({ bank: 40, slot: 2 });
      await confirmNavigation(request);
    });

    it("does not switch if the other bank disappeared from the fresh inventory", async () => {
      const { rerender } = renderStage({ deviceInfo: { ...DEVICE, bank: 2, slot: 3 }, patches: inventory });
      await navigateWithFreshState("Next bank", 2, 3, [patch(2, 3, "Only remaining patch")]);
      expect(screen.getByRole("button", { name: "Next bank" })).toBeDisabled();
      expect(screen.getByRole("button", { name: "Previous bank" })).toBeDisabled();
      expect(sentCommands("SWITCH_PATCH")).toHaveLength(0);
      expect(screen.queryByRole("alert")).not.toBeInTheDocument();
      await rerender({ patches: [...inventory] });
      expect(screen.getByRole("button", { name: "Next bank" })).toBeEnabled();
      expect(screen.getByRole("button", { name: "Previous bank" })).toBeEnabled();
    });

    it.each([
      ["inventory belongs to another profile", "kemper", "midi"],
      ["another client changed the active profile", "midi", "midi"],
    ])("refuses navigation when %s", async (_reason, infoProfile, listProfile) => {
      renderStage({ deviceInfo: { ...DEVICE, bank: 2, slot: 3, profile: "kemper" }, patches: inventory });
      await fireEvent.click(screen.getByRole("button", { name: "Next bank" }));
      await replyTo(await navigationRequest("GET_DEVICE_INFO"), { ...deviceReply(2, 3), profile: infoProfile });
      await replyTo(await navigationRequest("LIST_PATCHES"), { type: "PATCH_LIST", profile: listProfile, patches: inventory });
      expect(await screen.findByRole("alert")).toHaveTextContent("Bank change not confirmed");
      expect(sentCommands("SWITCH_PATCH")).toHaveLength(0);
      expect(screen.getByRole("button", { name: "Next bank" })).toBeEnabled();
    });

    it("supports legacy patch inventories that omit the profile name", async () => {
      renderStage({ deviceInfo: { ...DEVICE, bank: 2, slot: 3, profile: "kemper" }, patches: inventory });
      await fireEvent.click(screen.getByRole("button", { name: "Next bank" }));
      await replyTo(await navigationRequest("GET_DEVICE_INFO"), { ...deviceReply(2, 3), profile: "kemper" });
      await replyTo(await navigationRequest("LIST_PATCHES"), { type: "PATCH_LIST", profile: "", patches: inventory });
      const request = await navigationRequest("SWITCH_PATCH");
      expect(request).toMatchObject({ bank: 9, slot: 3 });
      await confirmNavigation(request);
      expect(screen.queryByRole("alert")).not.toBeInTheDocument();
    });

    it.each([
      ["disconnected", { connected: false }],
      ["unknown device", { deviceInfo: null }],
      ["one bank", { patches: [patch(2, 3, "Only bank")] }],
      ["empty inventory", { patches: [] }],
    ] as [string, Partial<StageProps>][])("keeps both controls disabled with %s", async (_reason, overrides) => {
      renderStage({ deviceInfo: { ...DEVICE, bank: 2, slot: 3 }, patches: inventory, ...overrides });
      for (const name of ["Previous bank", "Next bank"]) {
        const button = screen.getByRole("button", { name });
        expect(button).toBeDisabled();
        await fireEvent.click(button);
      }
      expect(sentCommands("GET_DEVICE_INFO")).toHaveLength(0);
      expect(sentCommands("LIST_PATCHES")).toHaveLength(0);
      expect(sentCommands("SWITCH_PATCH")).toHaveLength(0);
    });

    it("ignores repeated clicks and paints no destination before authoritative state arrives", async () => {
      const { container, rerender } = renderStage({ deviceInfo: { ...DEVICE, bank: 2, slot: 3 }, patches: inventory });
      const next = screen.getByRole("button", { name: "Next bank" });
      const previous = screen.getByRole("button", { name: "Previous bank" });
      await Promise.all([fireEvent.click(next), fireEvent.click(next), fireEvent.click(previous)]);
      const info = await navigationRequest("GET_DEVICE_INFO");
      expect(sentCommands("GET_DEVICE_INFO")).toHaveLength(1);
      expect(next).toBeDisabled();
      expect(previous).toBeDisabled();
      // An unrelated reply cannot release the correlation fence.
      await pushFirmwareMessage({ ...deviceReply(9, 3), id: "unrelated-navigation" });
      expect(sentCommands("LIST_PATCHES")).toHaveLength(0);
      await replyTo(info, deviceReply(2, 3));
      await replyTo(await navigationRequest("LIST_PATCHES"), { type: "PATCH_LIST", patches: inventory });
      const request = await navigationRequest("SWITCH_PATCH");
      expect(request).toMatchObject({ bank: 9, slot: 3 });
      expect(container.querySelector(".stage__bank-number")).toHaveTextContent("BANK 2");
      await fireEvent.click(next);
      expect(sentCommands("SWITCH_PATCH")).toHaveLength(1);
      await replyTo(request, { type: "ACK" });
      const confirmation = await navigationRequest("GET_DEVICE_INFO", 1);
      expect(next).toBeDisabled();
      expect(container.querySelector(".stage__bank-number")).toHaveTextContent("BANK 2");
      await replyTo(confirmation, deviceReply(9, 3));
      // App/Kiosk owns these props and applies DEVICE_INFO from the bus.
      // Stage itself must never assign the requested destination optimistically.
      await rerender({ deviceInfo: { ...DEVICE, bank: 9, slot: 3 } });
      expect(container.querySelector(".stage__bank-number")).toHaveTextContent("BANK 9");
      expect(next).toBeEnabled();
      expect(previous).toBeEnabled();
      expect(sentCommands("SWITCH_PATCH")).toHaveLength(1);
    });

    it.each(["firmware ERROR", "send failure", "unexpected reply"])("reports %s without changing bank or retrying the switch", async failure => {
      const { container } = renderStage({ deviceInfo: { ...DEVICE, bank: 2, slot: 3 }, patches: inventory });
      if (failure === "send failure") {
        const original = invokeMock.getMockImplementation()!;
        invokeMock.mockImplementation((command, args) => {
          if (command === "send_command" && JSON.parse(args.line).type === "SWITCH_PATCH") {
            return Promise.reject(new Error("write: USB connection lost"));
          }
          return original(command, args);
        });
      }
      await navigateWithFreshState("Next bank", 2, 3, inventory);
      const request = await navigationRequest("SWITCH_PATCH");
      if (failure !== "send failure") {
        await replyTo(request, failure === "firmware ERROR"
          ? { type: "ERROR", error: "Patch unavailable" }
          : { type: "CONTEXT", context: {} });
      }
      expect(await screen.findByRole("alert")).toHaveTextContent("Bank change not confirmed");
      expect(container.querySelector(".stage__bank-number")).toHaveTextContent("BANK 2");
      expect(screen.getByRole("button", { name: "Next bank" })).toBeEnabled();
      vi.useFakeTimers();
      await vi.advanceTimersByTimeAsync(6000);
      expect(sentCommands("SWITCH_PATCH")).toHaveLength(1);
      expect(sentCommands("GET_DEVICE_INFO")).toHaveLength(1);
    });

    it.each([
      ["a different bank", deviceReply(25, 3)],
      ["a different slot", deviceReply(9, 7)],
      ["no current position", { type: "DEVICE_INFO", ...DEVICE }],
    ])("does not treat ACK as confirmation when the device reports %s", async (_reason, response) => {
      const { container } = renderStage({ deviceInfo: { ...DEVICE, bank: 2, slot: 3 }, patches: inventory });
      await navigateWithFreshState("Next bank", 2, 3, inventory);
      await replyTo(await navigationRequest("SWITCH_PATCH"), { type: "ACK" });
      await replyTo(await navigationRequest("GET_DEVICE_INFO", 1), response as Record<string, unknown>);
      expect(await screen.findByRole("alert")).toHaveTextContent("Bank change not confirmed");
      expect(container.querySelector(".stage__bank-number")).toHaveTextContent("BANK 2");
      expect(screen.getByRole("button", { name: "Next bank" })).toBeEnabled();
      expect(sentCommands("SWITCH_PATCH")).toHaveLength(1);
    });

    it("stops before switching when the connection goes down during inventory refresh", async () => {
      const { rerender } = renderStage({ deviceInfo: { ...DEVICE, bank: 2, slot: 3 }, patches: inventory });
      await fireEvent.click(screen.getByRole("button", { name: "Next bank" }));
      await replyTo(await navigationRequest("GET_DEVICE_INFO"), deviceReply(2, 3));
      const request = await navigationRequest("LIST_PATCHES");
      await rerender({ connected: false });
      await replyTo(request, { type: "PATCH_LIST", patches: inventory });
      expect(sentCommands("SWITCH_PATCH")).toHaveLength(0);
      expect(screen.getByRole("button", { name: "Next bank" })).toBeDisabled();
    });

    it.each(["firmware-disconnected", "firmware-reconnecting"])("cancels navigation after %s even when the connection returns before the reply", async event => {
      const { rerender } = renderStage({ deviceInfo: { ...DEVICE, bank: 2, slot: 3 }, patches: inventory });
      await fireEvent.click(screen.getByRole("button", { name: "Next bank" }));
      await replyTo(await navigationRequest("GET_DEVICE_INFO"), deviceReply(2, 3));
      const request = await navigationRequest("LIST_PATCHES");
      const lostConnection = eventHandlers.get(event);
      expect(lostConnection).toBeTypeOf("function");
      lostConnection!();
      await rerender({ connected: false });
      await rerender({ connected: true });
      await replyTo(request, { type: "PATCH_LIST", patches: inventory });
      expect(sentCommands("SWITCH_PATCH")).toHaveLength(0);
      expect(screen.getByRole("button", { name: "Next bank" })).toBeEnabled();
    });

    it("does not continue navigation after Stage is closed during the first read", async () => {
      const { unmount } = renderStage({ deviceInfo: { ...DEVICE, bank: 2, slot: 3 }, patches: inventory });
      await fireEvent.click(screen.getByRole("button", { name: "Next bank" }));
      const request = await navigationRequest("GET_DEVICE_INFO");
      unmount();
      await replyTo(request, deviceReply(2, 3));
      expect(sentCommands("LIST_PATCHES")).toHaveLength(0);
      expect(sentCommands("SWITCH_PATCH")).toHaveLength(0);
    });
  });

  describe("direct bank selection", () => {
    const info = { ...DEVICE, bank: 2, slot: 3, profile: "kemper" };
    const inventory = [patch(2, 3, "Current"), patch(9, 3, "Middle"), patch(25, 3, "Destination")];
    const replyInfo = (bank = 2, slot = 3, profile = "kemper") => ({ ...deviceReply(bank, slot), profile });
    const replyInventory = (patches = inventory, profile = "kemper") => ({ type: "PATCH_LIST", profile, patches });

    async function openPicker(patches = inventory, bank = 2, slot = 3) {
      const trigger = screen.getByRole("button", { name: "Choose bank" });
      trigger.focus();
      await fireEvent.click(trigger);
      await replyTo(await navigationRequest("GET_DEVICE_INFO"), replyInfo(bank, slot));
      await replyTo(await navigationRequest("LIST_PATCHES"), replyInventory(patches));
      return screen.getByRole("dialog", { name: "Choose bank" });
    }

    async function selectBank(bank: number, fresh = inventory, currentBank = 2, currentSlot = 3) {
      await fireEvent.click(screen.getByRole("button", { name: `Bank ${bank}`, exact: true }));
      await replyTo(await navigationRequest("GET_DEVICE_INFO", 1), replyInfo(currentBank, currentSlot));
      await replyTo(await navigationRequest("LIST_PATCHES", 1), replyInventory(fresh));
    }

    async function confirmSelection(command: SentCommand) {
      await replyTo(command, { type: "ACK" });
      await replyTo(await navigationRequest("GET_DEVICE_INFO", 2), replyInfo(command.bank!, command.slot!));
    }

    it("refreshes before offering a sorted list of existing banks, deduplicating patches and rejecting invalid coordinates", async () => {
      renderStage({ deviceInfo: info, patches: inventory });
      const trigger = screen.getByRole("button", { name: "Choose bank" });
      await fireEvent.click(trigger);
      const dialog = screen.getByRole("dialog", { name: "Choose bank" });
      expect(dialog).toHaveTextContent("Loading banks");
      expect(within(dialog).queryAllByRole("button", { name: /^Bank \d+$/ })).toHaveLength(0);
      await replyTo(await navigationRequest("GET_DEVICE_INFO"), replyInfo());
      await replyTo(await navigationRequest("LIST_PATCHES"), replyInventory([
        patch(99, 10, "Last"), patch(2, 3, "Current"), patch(25, 3, "Lead"),
        patch(25, 7, "Solo"), patch(25, 7, "Duplicate"),
        patch(0, 1, "Invalid bank"), patch(100, 1, "Invalid bank"),
        patch(3.5, 1, "Fractional bank"), patch(8, 0, "Invalid slot"), patch(9, 11, "Invalid slot"),
      ]));
      const banks = within(dialog).getAllByRole("button", { name: /^Bank \d+$/ });
      expect(banks.map(bank => bank.getAttribute("aria-label"))).toEqual(["Bank 2", "Bank 25", "Bank 99"]);
      expect(within(dialog).getByRole("button", { name: "Bank 2", exact: true })).toHaveAttribute("aria-current", "true");
      expect(within(dialog).getByRole("button", { name: "Bank 25", exact: true })).not.toHaveAttribute("aria-current", "true");
      expect(sentCommands("SWITCH_PATCH")).toHaveLength(0);
    });

    it("offers only banks within the configured limit in the Stage picker", async () => {
      const limited = [1, 2, 25].map(bank => patch(bank, 3, `Bank ${bank}`));
      renderStage({ deviceInfo: info, device: { bank_count: 2 }, patches: limited });
      const dialog = await openPicker(limited);
      const banks = within(dialog).getAllByRole("button", { name: /^Bank \d+$/ });
      expect(banks.map(bank => bank.getAttribute("aria-label"))).toEqual(["Bank 1", "Bank 2"]);
      expect(sentCommands("SWITCH_PATCH")).toHaveLength(0);
    });

    it("offers every existing bank through 99 without truncating a large inventory", async () => {
      renderStage({ deviceInfo: info, patches: inventory });
      const allBanks = Array.from({ length: 99 }, (_, index) => patch(99 - index, 3, `Rig ${99 - index}`));
      const dialog = await openPicker(allBanks);
      const banks = within(dialog).getAllByRole("button", { name: /^Bank \d+$/ });
      expect(banks).toHaveLength(99);
      expect(banks[0]).toHaveAccessibleName("Bank 1");
      expect(banks[98]).toHaveAccessibleName("Bank 99");
      // Browser coverage checks actual scrolling and full-screen geometry;
      // this verifies that the component does not silently drop later banks.
      expect(sentCommands("SWITCH_PATCH")).toHaveLength(0);
    });

    it("can open an initially empty cached inventory and displays an empty state when the fresh list is empty", async () => {
      renderStage({ deviceInfo: info, patches: [] });
      const dialog = await openPicker([]);
      expect(dialog).toHaveTextContent("No banks available.");
      expect(within(dialog).queryAllByRole("button", { name: /^Bank \d+$/ })).toHaveLength(0);
      expect(within(dialog).getByRole("button", { name: "Close bank selection" })).toBeEnabled();
      expect(sentCommands("SWITCH_PATCH")).toHaveLength(0);
    });

    it("keeps the bank picker disabled while disconnected", async () => {
      renderStage({ deviceInfo: info, patches: inventory, connected: false });
      const trigger = screen.getByRole("button", { name: "Choose bank" });
      expect(trigger).toBeDisabled();
      await fireEvent.click(trigger);
      expect(screen.queryByRole("dialog", { name: "Choose bank" })).not.toBeInTheDocument();
      expect(sentCommands("GET_DEVICE_INFO")).toHaveLength(0);
      expect(sentCommands("LIST_PATCHES")).toHaveLength(0);
    });

    it("reports an inventory read failure without showing cached destinations or sending a mutation", async () => {
      renderStage({ deviceInfo: info, patches: inventory });
      await fireEvent.click(screen.getByRole("button", { name: "Choose bank" }));
      await replyTo(await navigationRequest("GET_DEVICE_INFO"), replyInfo());
      await replyTo(await navigationRequest("LIST_PATCHES"), { type: "ERROR", error: "storage_busy" });
      const dialog = screen.getByRole("dialog", { name: "Choose bank" });
      expect(await within(dialog).findByRole("alert")).toHaveTextContent("Unable to load banks.");
      expect(within(dialog).queryAllByRole("button", { name: /^Bank \d+$/ })).toHaveLength(0);
      expect(sentCommands("SWITCH_PATCH")).toHaveLength(0);
    });

    it("rejects an inventory from a different profile before offering its bank numbers", async () => {
      renderStage({ deviceInfo: info, patches: inventory });
      await fireEvent.click(screen.getByRole("button", { name: "Choose bank" }));
      await replyTo(await navigationRequest("GET_DEVICE_INFO"), replyInfo());
      await replyTo(await navigationRequest("LIST_PATCHES"), replyInventory([patch(80, 3, "Other profile")], "midi"));
      const dialog = screen.getByRole("dialog", { name: "Choose bank" });
      expect(await within(dialog).findByRole("alert")).toHaveTextContent("Unable to load banks.");
      expect(within(dialog).queryAllByRole("button", { name: /^Bank \d+$/ })).toHaveLength(0);
      expect(sentCommands("SWITCH_PATCH")).toHaveLength(0);
    });

    it("retries only the inventory after a failed read and requires an explicit destination choice", async () => {
      renderStage({ deviceInfo: info, patches: inventory });
      await fireEvent.click(screen.getByRole("button", { name: "Choose bank" }));
      await replyTo(await navigationRequest("GET_DEVICE_INFO"), replyInfo());
      await replyTo(await navigationRequest("LIST_PATCHES"), { type: "ERROR", error: "storage_busy" });
      const dialog = screen.getByRole("dialog", { name: "Choose bank" });
      await fireEvent.click(within(dialog).getByRole("button", { name: "Retry" }));
      await replyTo(await navigationRequest("GET_DEVICE_INFO", 1), replyInfo());
      await replyTo(await navigationRequest("LIST_PATCHES", 1), replyInventory());
      expect(within(dialog).queryByRole("alert")).not.toBeInTheDocument();
      expect(within(dialog).getByRole("button", { name: "Bank 25", exact: true })).toBeEnabled();
      expect(sentCommands("SWITCH_PATCH")).toHaveLength(0);
    });

    it.each(["close button", "Escape"])("cancels a loading dialog with %s and ignores the delayed reply", async method => {
      renderStage({ deviceInfo: info, patches: inventory });
      const trigger = screen.getByRole("button", { name: "Choose bank" });
      trigger.focus();
      await fireEvent.click(trigger);
      const firstRead = await navigationRequest("GET_DEVICE_INFO");
      const dialog = screen.getByRole("dialog", { name: "Choose bank" });
      if (method === "close button") {
        await fireEvent.click(within(dialog).getByRole("button", { name: "Close bank selection" }));
      } else {
        // Browsers translate Escape on a modal <dialog> into cancel.
        // jsdom does not perform that default keyboard action.
        await fireEvent(dialog, new Event("cancel", { cancelable: true }));
      }
      await waitFor(() => expect(screen.queryByRole("dialog", { name: "Choose bank" })).not.toBeInTheDocument());
      await replyTo(firstRead, replyInfo());
      expect(sentCommands("LIST_PATCHES")).toHaveLength(0);
      expect(sentCommands("SWITCH_PATCH")).toHaveLength(0);
      await waitFor(() => expect(trigger).toHaveFocus());
    });

    it("closes when the current bank is selected without reloading or reselecting the current rig", async () => {
      renderStage({ deviceInfo: info, patches: inventory });
      await openPicker();
      await fireEvent.click(screen.getByRole("button", { name: "Bank 2", exact: true }));
      expect(screen.queryByRole("dialog", { name: "Choose bank" })).not.toBeInTheDocument();
      expect(sentCommands("GET_DEVICE_INFO")).toHaveLength(1);
      expect(sentCommands("LIST_PATCHES")).toHaveLength(1);
      expect(sentCommands("SWITCH_PATCH")).toHaveLength(0);
      await waitFor(() => expect(screen.getByRole("button", { name: "Choose bank" })).toHaveFocus());
    });

    it.each([
      ["the same slot from fresh current position", 7, [patch(25, 2, "First"), patch(25, 7, "Same slot")], 7],
      ["the lowest available slot when that slot is absent", 7, [patch(25, 8, "Last"), patch(25, 2, "First")], 2],
    ] as const)("jumps directly to a non-adjacent bank using %s", async (_reason, currentSlot, destinations, targetSlot) => {
      const { container, rerender } = renderStage({ deviceInfo: info, patches: inventory });
      await openPicker();
      await selectBank(25, [patch(9, currentSlot, "Now current"), ...destinations], 9, currentSlot);
      const request = await navigationRequest("SWITCH_PATCH");
      expect(request).toMatchObject({ bank: 25, slot: targetSlot });
      expect(sentCommands("SWITCH_PATCH")).toHaveLength(1);
      expect(container.querySelector(".stage__bank-number")).toHaveTextContent("BANK 2");
      await replyTo(request, { type: "ACK" });
      const confirmation = await navigationRequest("GET_DEVICE_INFO", 2);
      expect(screen.getByRole("dialog", { name: "Choose bank" })).toBeInTheDocument();
      expect(container.querySelector(".stage__bank-number")).toHaveTextContent("BANK 2");
      await replyTo(confirmation, replyInfo(25, targetSlot));
      await waitFor(() => expect(screen.queryByRole("dialog", { name: "Choose bank" })).not.toBeInTheDocument());
      // Stage relies on App/Kiosk's authoritative DEVICE_INFO propagation.
      await rerender({ deviceInfo: { ...info, bank: 25, slot: targetSlot } });
      expect(container.querySelector(".stage__bank-number")).toHaveTextContent("BANK 25");
      expect(sentCommands("GET_DEVICE_INFO")).toHaveLength(3);
      expect(sentCommands("LIST_PATCHES")).toHaveLength(2);
    });

    it("does not substitute another bank if the chosen bank disappears before switching", async () => {
      renderStage({ deviceInfo: info, patches: inventory });
      await openPicker();
      await selectBank(25, [patch(2, 3, "Current"), patch(9, 3, "Only other bank")]);
      const dialog = screen.getByRole("dialog", { name: "Choose bank" });
      expect(await within(dialog).findByRole("alert")).toHaveTextContent("Bank is no longer available.");
      expect(within(dialog).queryByRole("button", { name: "Bank 25", exact: true })).not.toBeInTheDocument();
      expect(sentCommands("SWITCH_PATCH")).toHaveLength(0);
    });

    it("does not reselect a rig if another controller has already moved to the chosen bank", async () => {
      renderStage({ deviceInfo: info, patches: inventory });
      await openPicker();
      await selectBank(25, [...inventory, patch(25, 7, "Already selected physically")], 25, 7);
      await waitFor(() => expect(screen.queryByRole("dialog", { name: "Choose bank" })).not.toBeInTheDocument());
      expect(sentCommands("SWITCH_PATCH")).toHaveLength(0);
      expect(sentCommands("GET_DEVICE_INFO")).toHaveLength(2);
    });

    it("blocks repeated choices, close and Escape while a bank change is awaiting confirmation", async () => {
      renderStage({ deviceInfo: info, patches: inventory });
      const dialog = await openPicker();
      const destination = within(dialog).getByRole("button", { name: "Bank 25", exact: true });
      await Promise.all([fireEvent.click(destination), fireEvent.click(destination)]);
      const read = await navigationRequest("GET_DEVICE_INFO", 1);
      expect(sentCommands("GET_DEVICE_INFO")).toHaveLength(2);
      for (const bank of within(dialog).getAllByRole("button", { name: /^Bank \d+$/ })) expect(bank).toBeDisabled();
      expect(within(dialog).getByRole("combobox", { name: "Bank selection" })).toBeDisabled();
      const close = within(dialog).getByRole("button", { name: "Close bank selection" });
      expect(close).toBeDisabled();
      await fireEvent.click(close);
      await fireEvent(dialog, new Event("cancel", { cancelable: true }));
      expect(dialog).toBeInTheDocument();
      await replyTo(read, replyInfo());
      await replyTo(await navigationRequest("LIST_PATCHES", 1), replyInventory());
      const request = await navigationRequest("SWITCH_PATCH");
      await fireEvent.click(destination);
      expect(sentCommands("SWITCH_PATCH")).toHaveLength(1);
      await confirmSelection(request);
      await waitFor(() => expect(screen.queryByRole("dialog", { name: "Choose bank" })).not.toBeInTheDocument());
    });

    it("keeps the dialog open with an error on rejected selection without painting or retrying it", async () => {
      const { container } = renderStage({ deviceInfo: info, patches: inventory });
      await openPicker();
      await selectBank(25);
      const request = await navigationRequest("SWITCH_PATCH");
      await replyTo(request, { type: "ERROR", error: "storage_busy" });
      const dialog = screen.getByRole("dialog", { name: "Choose bank" });
      expect(await within(dialog).findByRole("alert")).toHaveTextContent("Bank change not confirmed.");
      expect(container.querySelector(".stage__bank-number")).toHaveTextContent("BANK 2");
      expect(within(dialog).getByRole("button", { name: "Close bank selection" })).toBeEnabled();
      vi.useFakeTimers();
      await vi.advanceTimersByTimeAsync(6000);
      expect(sentCommands("SWITCH_PATCH")).toHaveLength(1);
      expect(sentCommands("GET_DEVICE_INFO")).toHaveLength(2);
    });

    it("keeps the error visible when ACK is followed by a different actual bank", async () => {
      renderStage({ deviceInfo: info, patches: inventory });
      await openPicker();
      await selectBank(25);
      await replyTo(await navigationRequest("SWITCH_PATCH"), { type: "ACK" });
      await replyTo(await navigationRequest("GET_DEVICE_INFO", 2), replyInfo(9, 3));
      const dialog = screen.getByRole("dialog", { name: "Choose bank" });
      expect(await within(dialog).findByRole("alert")).toHaveTextContent("Bank change not confirmed.");
      expect(sentCommands("SWITCH_PATCH")).toHaveLength(1);
    });

    it.each(["disconnect", "profile change"])("closes and abandons a pending selection on %s", async change => {
      const { rerender } = renderStage({ deviceInfo: info, patches: inventory });
      await openPicker();
      await fireEvent.click(screen.getByRole("button", { name: "Bank 25", exact: true }));
      await replyTo(await navigationRequest("GET_DEVICE_INFO", 1), replyInfo());
      const inventoryRead = await navigationRequest("LIST_PATCHES", 1);
      if (change === "disconnect") await rerender({ connected: false });
      else await rerender({ deviceInfo: { ...info, profile: "midi" } });
      expect(screen.queryByRole("dialog", { name: "Choose bank" })).not.toBeInTheDocument();
      await replyTo(inventoryRead, replyInventory());
      expect(sentCommands("SWITCH_PATCH")).toHaveLength(0);
    });

    it.each(["firmware-disconnected", "firmware-reconnecting"])("rejects delayed selection replies after %s even when connection returns", async event => {
      const { rerender } = renderStage({ deviceInfo: info, patches: inventory });
      await openPicker();
      await fireEvent.click(screen.getByRole("button", { name: "Bank 25", exact: true }));
      await replyTo(await navigationRequest("GET_DEVICE_INFO", 1), replyInfo());
      const inventoryRead = await navigationRequest("LIST_PATCHES", 1);
      const lostConnection = eventHandlers.get(event);
      expect(lostConnection).toBeTypeOf("function");
      lostConnection!();
      await rerender({ connected: false });
      await rerender({ connected: true });
      await replyTo(inventoryRead, replyInventory());
      expect(screen.queryByRole("dialog", { name: "Choose bank" })).not.toBeInTheDocument();
      expect(sentCommands("SWITCH_PATCH")).toHaveLength(0);
    });

    describe("bank preselection", () => {
      const modeKey = "BOSUN_STAGE_BANK_SELECTION";
      const rigs = [
        patch(25, 7, "Solo"), patch(2, 3, "Current"), patch(25, 1, "Clean"),
        patch(9, 3, "Middle"), patch(25, 3, "Crunch"),
      ];
      const nativeInfo = { ...info, stage_input: true };
      const nav = { preset_navigation: { switches: { A: 1, B: 3, C: 7 } } };

      async function renderPreselection(inventory = rigs, device: Record<string, unknown> | null = nav) {
        const view = renderStage({ deviceInfo: nativeInfo, patches: inventory, device });
        await pushFirmwareMessage({
          type: "PATCH", bank: 2, slot: 3, profile: "", active_profile: "kemper",
          patch: { name: "Live current", bindings: [
            binding("1", { mode: "latched", label: "Live delay", actions: {
              toggle_on: { messages: [{ type: "cc", channel: 1, cc: 22, value: 127 }] },
              toggle_off: { messages: [{ type: "cc", channel: 1, cc: 22, value: 0 }] },
            } }),
            binding("C", { label: "Live boost", actions: {
              press: { messages: [{ type: "cc", channel: 1, cc: 80, value: 127 }] },
            } }),
          ] },
        });
        return view;
      }

      async function openModePicker(inventory = rigs) {
        const infoIndex = sentCommands("GET_DEVICE_INFO").length;
        const listIndex = sentCommands("LIST_PATCHES").length;
        const trigger = screen.getByRole("button", { name: "Choose bank" });
        trigger.focus();
        await fireEvent.click(trigger);
        await replyTo(await navigationRequest("GET_DEVICE_INFO", infoIndex), replyInfo());
        await replyTo(await navigationRequest("LIST_PATCHES", listIndex), replyInventory(inventory));
        return screen.getByRole("dialog", { name: "Choose bank" });
      }

      async function preselectBank(bank = 25, inventory = rigs) {
        await openModePicker(inventory);
        await changeSelectionMode("Preselect bank");
        await completeBankPreselection(bank, inventory);
      }

      async function changeSelectionMode(name: "Load rig immediately" | "Preselect bank") {
        await fireEvent.click(screen.getByRole("combobox", { name: "Bank selection" }));
        const options = screen.getByRole("listbox", { name: "Bank selection mode" });
        await fireEvent.click(within(options).getByRole("option", { name, exact: true }));
      }

      async function completeBankPreselection(bank = 25, inventory = rigs) {
        const infoIndex = sentCommands("GET_DEVICE_INFO").length;
        const listIndex = sentCommands("LIST_PATCHES").length;
        await fireEvent.click(screen.getByRole("button", { name: `Bank ${bank}`, exact: true }));
        if (bank !== 2) {
          await replyTo(await navigationRequest("GET_DEVICE_INFO", infoIndex), replyInfo());
          await replyTo(await navigationRequest("LIST_PATCHES", listIndex), replyInventory(inventory));
        }
        await waitFor(() => expect(screen.queryByRole("dialog", { name: "Choose bank" })).not.toBeInTheDocument());
      }

      async function confirmPreselectedRig(command: SentCommand) {
        const nextInfo = sentCommands("GET_DEVICE_INFO").length;
        await replyTo(command, { type: "ACK" });
        await replyTo(await navigationRequest("GET_DEVICE_INFO", nextInfo), replyInfo(command.bank!, command.slot!));
      }

      async function chooseStageRig(container: HTMLElement, fresh = rigs, currentBank = 2, currentSlot = 3) {
        await fireEvent.click(within(container).getByRole("button", { name: "Rig 7: Solo", exact: true }));
        await replyTo(await navigationRequest("GET_DEVICE_INFO", 2), replyInfo(currentBank, currentSlot));
        await replyTo(await navigationRequest("LIST_PATCHES", 2), replyInventory(fresh));
      }

      it("cancels an excluded preselection when the configured bank limit is reduced", async () => {
        const { rerender } = await renderPreselection();
        await preselectBank();
        expect(screen.getByRole("button", { name: "Cancel bank preselection" })).toBeInTheDocument();
        await rerender({ device: { ...nav, bank_count: 2 } });
        expect(screen.queryByRole("button", { name: "Cancel bank preselection" })).not.toBeInTheDocument();
        expect(sentCommands("SWITCH_PATCH")).toHaveLength(0);
      });

      it("defaults to immediate selection and leaves the existing direct-bank behavior unchanged", async () => {
        await renderPreselection();
        await openModePicker();
        expect(screen.getByRole("combobox", { name: "Bank selection" })).toHaveAttribute("data-mode", "immediate");
        await selectBank(25, rigs);
        const request = await navigationRequest("SWITCH_PATCH");
        expect(request).toMatchObject({ bank: 25, slot: 3 });
        await confirmPreselectedRig(request);
        expect(sentCommands("SWITCH_PATCH")).toHaveLength(1);
      });

      it("closes the chooser and previews the selected bank's rigs on Stage without changing the live rig", async () => {
        const { container } = await renderPreselection();
        expect(switchById(container, "C")).toHaveTextContent("Live boost");
        await preselectBank();
        expect(screen.queryByRole("dialog", { name: "Choose bank" })).not.toBeInTheDocument();
        expect(container.querySelector(".stage__bank-number")).toHaveTextContent("BANK 25");
        expect(container.querySelector(".stage__rig-name")).toHaveTextContent("Live current");
        for (const name of ["Rig 1: Clean", "Rig 3: Crunch", "Rig 7: Solo"]) {
          const rig = screen.getByRole("button", { name, exact: true });
          expect(rig).toBeEnabled();
          expect(rig).not.toHaveClass("stage__switch--active");
          expect(rig).toHaveAttribute("aria-pressed", "false");
        }
        expect(screen.getByRole("button", { name: "Cancel bank preselection" })).toBeEnabled();
        expect(sentCommands("SWITCH_PATCH")).toHaveLength(0);
        expect(sentCommands("ACTIVATE_SWITCH")).toHaveLength(0);
        expect(sentCommands("GET_DEVICE_INFO")).toHaveLength(2);
        expect(sentCommands("LIST_PATCHES")).toHaveLength(2);
      });

      it("uses the preview grid only for rigs and blocks the former effect's activation", async () => {
        const { container } = await renderPreselection();
        const effect = switchById(container, "1")!;
        expect(effect).toBeEnabled();
        await preselectBank();
        expect(effect).toBeDisabled();
        await fireEvent.click(effect);
        expect(sentCommands("ACTIVATE_SWITCH")).toHaveLength(0);
        expect(sentCommands("SWITCH_PATCH")).toHaveLength(0);
      });

      it.each(["partial preset map", "no preset map"])("makes an existing unmapped rig selectable with %s", async mapping => {
        const inventory = [...rigs, patch(25, 10, "Finale")];
        await renderPreselection(inventory, mapping === "partial preset map" ? nav : null);
        await preselectBank(25, inventory);
        const rig = screen.getByRole("button", { name: "Rig 10: Finale", exact: true });
        expect(rig).toBeEnabled();
        expect(rig).toHaveAttribute("aria-pressed", "false");
        await fireEvent.click(rig);
        await replyTo(await navigationRequest("GET_DEVICE_INFO", 2), replyInfo());
        await replyTo(await navigationRequest("LIST_PATCHES", 2), replyInventory(inventory));
        const request = await navigationRequest("SWITCH_PATCH");
        expect(request).toMatchObject({ bank: 25, slot: 10 });
        await confirmPreselectedRig(request);
        expect(sentCommands("SWITCH_PATCH")).toHaveLength(1);
        expect(sentCommands("ACTIVATE_SWITCH")).toHaveLength(0);
      });

      it("commits the exact clicked Stage rig even when that mapped switch has a live effect binding", async () => {
        const { container, rerender } = await renderPreselection();
        await preselectBank();
        await chooseStageRig(container);
        const request = await navigationRequest("SWITCH_PATCH");
        expect(request).toMatchObject({ bank: 25, slot: 7 });
        expect(sentCommands("ACTIVATE_SWITCH")).toHaveLength(0);
        expect(container.querySelector(".stage__rig-name")).toHaveTextContent("Live current");
        await replyTo(request, { type: "ACK" });
        const confirmation = await navigationRequest("GET_DEVICE_INFO", 3);
        expect(screen.getByRole("button", { name: "Cancel bank preselection" })).toBeInTheDocument();
        await replyTo(confirmation, replyInfo(25, 7));
        await waitFor(() => expect(screen.queryByRole("button", { name: "Cancel bank preselection" })).not.toBeInTheDocument());
        await rerender({ deviceInfo: { ...nativeInfo, bank: 25, slot: 7 } });
        expect(container.querySelector(".stage__bank-number")).toHaveTextContent("BANK 25");
        expect(container.querySelector(".stage__rig-number")).toHaveTextContent("RIG 7");
        expect(sentCommands("SWITCH_PATCH")).toHaveLength(1);
      });

      it("does not fall back to another rig when the selected slot disappears before commit", async () => {
        const { container } = await renderPreselection();
        await preselectBank();
        await chooseStageRig(container, rigs.filter(rig => rig.bank !== 25 || rig.slot !== 7));
        expect(await screen.findByRole("alert")).toHaveTextContent("Rig is no longer available.");
        expect(sentCommands("SWITCH_PATCH")).toHaveLength(0);
        expect(sentCommands("ACTIVATE_SWITCH")).toHaveLength(0);
        expect(container.querySelector(".stage__rig-name")).toHaveTextContent("Live current");
      });

      it("does not reselect a rig if another controller already selected that exact bank and slot", async () => {
        const { container } = await renderPreselection();
        await preselectBank();
        await chooseStageRig(container, rigs, 25, 7);
        await waitFor(() => expect(screen.queryByRole("button", { name: "Cancel bank preselection" })).not.toBeInTheDocument());
        expect(sentCommands("GET_DEVICE_INFO")).toHaveLength(3);
        expect(sentCommands("LIST_PATCHES")).toHaveLength(3);
        expect(sentCommands("SWITCH_PATCH")).toHaveLength(0);
      });

      it("cancels a Stage preselection and restores the current bank's controls without loading anything", async () => {
        const { container } = await renderPreselection();
        await preselectBank();
        await fireEvent.click(screen.getByRole("button", { name: "Cancel bank preselection" }));
        expect(screen.queryByRole("button", { name: "Cancel bank preselection" })).not.toBeInTheDocument();
        expect(container.querySelector(".stage__bank-number")).toHaveTextContent("BANK 2");
        expect(switchById(container, "C")).toHaveTextContent("Live boost");
        expect(switchById(container, "1")).toBeEnabled();
        expect(sentCommands("SWITCH_PATCH")).toHaveLength(0);
        expect(sentCommands("ACTIVATE_SWITCH")).toHaveLength(0);
        expect(sentCommands("GET_DEVICE_INFO")).toHaveLength(2);
        expect(sentCommands("LIST_PATCHES")).toHaveLength(2);
      });

      it("choosing the current bank cancels an earlier preselection without reselecting the current rig", async () => {
        const { container } = await renderPreselection();
        await preselectBank();
        await openModePicker();
        await fireEvent.click(screen.getByRole("button", { name: "Bank 2", exact: true }));
        expect(screen.queryByRole("dialog", { name: "Choose bank" })).not.toBeInTheDocument();
        expect(screen.queryByRole("button", { name: "Cancel bank preselection" })).not.toBeInTheDocument();
        expect(container.querySelector(".stage__bank-number")).toHaveTextContent("BANK 2");
        expect(sentCommands("SWITCH_PATCH")).toHaveLength(0);
        expect(sentCommands("GET_DEVICE_INFO")).toHaveLength(3);
      });

      it.each([["Previous bank", 9], ["Next bank", 2]] as const)("%s browses from the preselected bank without loading a rig", async (direction, targetBank) => {
        const { container } = await renderPreselection();
        await preselectBank();
        await fireEvent.click(screen.getByRole("button", { name: direction }));
        await replyTo(await navigationRequest("GET_DEVICE_INFO", 2), replyInfo());
        await replyTo(await navigationRequest("LIST_PATCHES", 2), replyInventory(rigs));
        expect(container.querySelector(".stage__bank-number")).toHaveTextContent(`BANK ${targetBank}`);
        expect(container.querySelector(".stage__rig-name")).toHaveTextContent("Live current");
        if (targetBank === 2) {
          expect(screen.queryByRole("button", { name: "Cancel bank preselection" })).not.toBeInTheDocument();
        } else {
          expect(screen.getByRole("button", { name: "Cancel bank preselection" })).toBeEnabled();
          expect(screen.getByRole("button", { name: "Rig 3: Middle", exact: true })).toBeEnabled();
        }
        expect(sentCommands("SWITCH_PATCH")).toHaveLength(0);
        expect(sentCommands("ACTIVATE_SWITCH")).toHaveLength(0);
      });

      it("persists the mode across Stage instances while forgetting the uncommitted bank", async () => {
        const first = await renderPreselection();
        await preselectBank();
        expect(localStorage.getItem(modeKey)).toBe("preselect");
        first.unmount();
        invokeMock.mockClear();
        const { container } = await renderPreselection();
        expect(screen.queryByRole("button", { name: "Cancel bank preselection" })).not.toBeInTheDocument();
        expect(container.querySelector(".stage__bank-number")).toHaveTextContent("BANK 2");
        const dialog = await openModePicker();
        expect(within(dialog).getByRole("combobox", { name: "Bank selection" })).toHaveAttribute("data-mode", "preselect");
        await completeBankPreselection();
        expect(screen.queryByRole("dialog", { name: "Choose bank" })).not.toBeInTheDocument();
        expect(screen.getByRole("button", { name: "Cancel bank preselection" })).toBeInTheDocument();
        expect(sentCommands("SWITCH_PATCH")).toHaveLength(0);
      });

      it("changes mode without navigating and clears any uncommitted bank", async () => {
        const { container } = await renderPreselection();
        await preselectBank();
        const dialog = await openModePicker();
        const select = within(dialog).getByRole("combobox", { name: "Bank selection" });
        await changeSelectionMode("Load rig immediately");
        expect(localStorage.getItem(modeKey)).toBe("immediate");
        expect(select).toHaveAttribute("data-mode", "immediate");
        expect(select).toHaveAttribute("aria-expanded", "false");
        expect(screen.queryByRole("button", { name: "Cancel bank preselection" })).not.toBeInTheDocument();
        expect(container.querySelector(".stage__bank-number")).toHaveTextContent("BANK 2");
        await changeSelectionMode("Preselect bank");
        expect(localStorage.getItem(modeKey)).toBe("preselect");
        expect(select).toHaveAttribute("data-mode", "preselect");
        expect(select).toHaveAttribute("aria-expanded", "false");
        expect(screen.queryByRole("button", { name: "Cancel bank preselection" })).not.toBeInTheDocument();
        expect(sentCommands("SWITCH_PATCH")).toHaveLength(0);
        expect(sentCommands("ACTIVATE_SWITCH")).toHaveLength(0);
        expect(sentCommands("GET_DEVICE_INFO")).toHaveLength(3);
        expect(sentCommands("LIST_PATCHES")).toHaveLength(3);
      });

      it("blocks duplicate Stage rig taps and cancellation until exact position confirmation", async () => {
        const { container } = await renderPreselection();
        await preselectBank();
        const rig = screen.getByRole("button", { name: "Rig 7: Solo", exact: true });
        await Promise.all([fireEvent.click(rig), fireEvent.click(rig)]);
        const read = await navigationRequest("GET_DEVICE_INFO", 2);
        expect(sentCommands("GET_DEVICE_INFO")).toHaveLength(3);
        for (const tile of switchEls(container)) expect(tile).toBeDisabled();
        expect(screen.getByRole("button", { name: "Choose bank" })).toBeDisabled();
        const cancel = screen.getByRole("button", { name: "Cancel bank preselection" });
        expect(cancel).toBeDisabled();
        await fireEvent.click(cancel);
        await replyTo(read, replyInfo());
        await replyTo(await navigationRequest("LIST_PATCHES", 2), replyInventory(rigs));
        const request = await navigationRequest("SWITCH_PATCH");
        expect(request).toMatchObject({ bank: 25, slot: 7 });
        await confirmPreselectedRig(request);
        expect(sentCommands("SWITCH_PATCH")).toHaveLength(1);
        expect(sentCommands("ACTIVATE_SWITCH")).toHaveLength(0);
      });

      it.each(["disconnect", "profile change"])("forgets a Stage bank preselection after %s", async change => {
        const { container, rerender } = await renderPreselection();
        await preselectBank();
        if (change === "disconnect") {
          await rerender({ connected: false });
          await rerender({ connected: true });
        } else {
          await rerender({ deviceInfo: { ...nativeInfo, profile: "midi" } });
        }
        expect(screen.queryByRole("button", { name: "Cancel bank preselection" })).not.toBeInTheDocument();
        expect(container.querySelector(".stage__bank-number")).toHaveTextContent("BANK 2");
        expect(localStorage.getItem(modeKey)).toBe("preselect");
        expect(sentCommands("SWITCH_PATCH")).toHaveLength(0);
      });

      it("abandons an exact rig commit when disconnect and reconnect overtake its inventory reply", async () => {
        const { container, rerender } = await renderPreselection();
        await preselectBank();
        await fireEvent.click(screen.getByRole("button", { name: "Rig 7: Solo", exact: true }));
        await replyTo(await navigationRequest("GET_DEVICE_INFO", 2), replyInfo());
        const inventoryRead = await navigationRequest("LIST_PATCHES", 2);
        const disconnected = eventHandlers.get("firmware-disconnected");
        expect(disconnected).toBeTypeOf("function");
        disconnected!();
        await rerender({ connected: false });
        await rerender({ connected: true });
        await replyTo(inventoryRead, replyInventory(rigs));
        expect(screen.queryByRole("button", { name: "Cancel bank preselection" })).not.toBeInTheDocument();
        expect(sentCommands("SWITCH_PATCH")).toHaveLength(0);
        expect(sentCommands("ACTIVATE_SWITCH")).toHaveLength(0);
      });
    });
  });

  describe("expression pedal indicator", () => {
    it("shows only the confirmed mode text inside the title bar", async () => {
      const { container } = renderStage();
      await pushFirmwareMessage({ type: "CONTEXT", context: { expression_mode: "WAH" } });
      const badge = container.querySelector(".stage__header .stage__expression");
      expect(badge).toHaveTextContent("WAH");
      expect(badge?.querySelector("svg")).toBeNull();
      expect(badge).toHaveAttribute("aria-label", "Expression pedal: WAH");
      await pushFirmwareMessage({ type: "CONTEXT", partial: true, context: { expression_mode: "VOL" } });
      expect(badge).toHaveTextContent("VOL");
      expect(badge?.querySelector("svg")).toBeNull();
      expect(badge).toHaveAttribute("aria-label", "Expression pedal: VOL");
    });

    it("never guesses VOL when the state is missing, invalid, disconnected or changing rig", async () => {
      const { container, rerender } = renderStage();
      const badge = () => container.querySelector(".stage__expression");
      expect(badge()).toHaveTextContent("---");
      expect(badge()?.querySelector("svg")).toBeNull();
      await pushFirmwareMessage({ type: "CONTEXT", context: { expression_mode: "invalid" } });
      expect(badge()).toHaveTextContent("---");
      await pushFirmwareMessage({ type: "CONTEXT", context: { expression_mode: "WAH" } });
      await pushFirmwareMessage({ type: "CONTEXT", partial: true, context: { kemper_bpm: 120 } });
      expect(badge()).toHaveTextContent("WAH");
      await pushFirmwareMessage({ type: "EVENT", event: "patch_switched", bank: 1, slot: 3 });
      expect(badge()).toHaveTextContent("---");
      await pushFirmwareMessage({ type: "CONTEXT", context: { bank: 1, slot: 3, expression_mode: "WAH" } });
      expect(badge()).toHaveTextContent("WAH");
      await rerender({ connected: false });
      expect(badge()).toHaveTextContent("---");
      await rerender({ connected: true });
      expect(badge()).toHaveTextContent("---");
    });
  });

  describe("Screen layout title colors", () => {
    const colors = {
      patch_name: "#abcdef", bank: "#fedcba", kemper_rig_in_bank: "#12ab34", expression_mode: "#f0ab12",
    };
    const selectors: Record<string, string> = {
      patch_name: ".stage__rig-name", bank: ".stage__bank-number",
      kemper_rig_in_bank: ".stage__rig-number", expression_mode: ".stage__expression",
    };

    it("uses independent Screen colors for the title, bank, rig and mode while retaining Stage sizing", async () => {
      const { container, rerender } = renderStage({
        deviceInfo: { ...DEVICE, bank: 2, slot: 3 },
        device: { tft: { layout: Object.entries(colors).map(([field, color]) => ({
          field, color, x: 123, y: 170, size: 9, font: "custom.bdf",
        })) } },
      });
      for (const [field, color] of Object.entries(colors)) {
        const element = container.querySelector<HTMLElement>(selectors[field]);
        expect(element).toHaveStyle({ color });
        expect(element?.style.fontSize).toBe("");
        expect(element?.style.left).toBe("");
      }
      await pushFirmwareMessage({ type: "CONTEXT", context: { kemper_rig_in_bank: 3 } });
      // These entries have no prefix, so Screen displays their bare values.
      expect(container.querySelector(".stage__bank-readout .stage__bank-number")).toHaveTextContent("2");
      expect(container.querySelector(".stage__rig-readout .stage__rig-number")).toHaveTextContent("3");
      await rerender({ device: { tft: { layout: [{ field: "patch_name", color: "#123456" }] } } });
      expect(container.querySelector(".stage__rig-name")).toHaveStyle({ color: "#123456" });
      expect(container.querySelector<HTMLElement>(".stage__rig-number")?.style.color).toBe("");
    });

    it("accepts compact kiosk colors and core/live field aliases, preferring the desktop layout", () => {
      const { container } = renderStage({
        deviceInfo: { ...DEVICE, bank: 1, slot: 1 },
        device: {
          tft_colors: { kemper_rig_name: "#abcdef", kemper_bank: "#fedcba", slot: "#12ab34", expression_mode: "#f0ab12" },
          tft: { layout: [{ field: "kemper_rig_name", color: 0x123456 }] },
        },
      });
      expect(container.querySelector(".stage__rig-name")).toHaveStyle({ color: "#123456" });
      expect(container.querySelector(".stage__bank-number")).toHaveStyle({ color: "#fedcba" });
      expect(container.querySelector(".stage__rig-number")).toHaveStyle({ color: "#12ab34" });
      expect(container.querySelector(".stage__expression")).toHaveStyle({ color: "#f0ab12" });
    });

    it("uses the Kemper default Screen palette and ignores malformed colors so Stage theme can supply the fallback", async () => {
      const { container, rerender } = renderStage({
        deviceInfo: { ...DEVICE, bank: 1, slot: 1 },
        device: { tft: { layout: [
          { field: "patch_name", color: "#ffffff" }, { field: "bank", color: "#9aa1ad" },
          { field: "kemper_rig", color: "#6fd99b" }, { field: "expression_mode", color: "#ffffff" },
        ] } },
      });
      expect(container.querySelector(".stage__bank-number")).toHaveStyle({ color: "#9aa1ad" });
      expect(container.querySelector(".stage__rig-number")).toHaveStyle({ color: "#6fd99b" });
      await rerender({ device: { tft: { layout: [
        null, { field: "patch_name", color: "red; display:none" }, { field: "bank", color: "#oops" },
        { field: "kemper_rig", color: -1 }, { field: "expression_mode", color: 0x1000000 },
      ] }, tft_colors: { patch_name: [], expression_mode: "transparent" } } });
      for (const selector of Object.values(selectors)) {
        expect(container.querySelector<HTMLElement>(selector)?.style.color).toBe("");
      }
    });
  });

  describe("rendering", () => {
    it("renders without crashing", () => {
      const { container } = renderStage();
      expect(container.querySelector(".stage")).not.toBeNull();
      expect(screen.getByRole("button", { name: "Exit Stage" })).toBeInTheDocument();
    });

    it("renders the 2-row x 5-column pedal grid", () => {
      const { container } = renderStage();
      const rows = container.querySelectorAll(".stage__pedal-row");
      expect(rows).toHaveLength(2);
      rows.forEach((row) => {
        expect(row.querySelectorAll(".stage__switch")).toHaveLength(5);
      });
      expect(container.querySelectorAll(".stage__switch")).toHaveLength(10);
    });

    it("renders all 10 switches in layout order with their IDs", () => {
      const { container } = renderStage();
      expect(switchEls(container).map(idOf)).toEqual(DISPLAY_ORDER);
      switchEls(container).forEach((el, i) => {
        expect(idOf(el)).toBe(DISPLAY_ORDER[i]);
      });
    });
  });

  describe("layout", () => {
    it("DEFAULT_LAYOUT top row is [1, 2, 3, 4, up]", () => {
      expect(DEFAULT_LAYOUT[0]).toEqual(["1", "2", "3", "4", "up"]);
    });

    it("DEFAULT_LAYOUT bottom row is [A, B, C, D, down]", () => {
      expect(DEFAULT_LAYOUT[1]).toEqual(["A", "B", "C", "D", "down"]);
    });

    it("renders rows that match DEFAULT_LAYOUT", () => {
      const { container } = renderStage();
      const rows = Array.from(container.querySelectorAll(".stage__pedal-row"));
      const rendered = rows.map((row) =>
        Array.from(row.querySelectorAll(".stage__switch"))
          .map((sw) => sw.querySelector(".stage__switch-id")?.textContent ?? null),
      );
      // The component uppercases up/down on display (displaySwitch).
      expect(rendered).toEqual([
        ["1", "2", "3", "4", "UP"],
        ["A", "B", "C", "D", "DOWN"],
      ]);
    });
  });

  describe("header data", () => {
    it("uses the saved TFT prefixes, suffixes and exact live fields instead of abbreviated bank/slot labels", async () => {
      const { container } = renderStage({
        deviceInfo: { ...DEVICE, bank: 1, slot: 3 },
        device: { tft: { layout: [
          { field: "kemper_bank", prefix: "Bank ", suffix: " Tour" },
          { field: "kemper_rig", prefix: "Saved Rig ", suffix: "!" },
        ] } },
      });
      // An absolute Kemper rig must not be guessed from Captain slot 3.
      expect(container.querySelector(".stage__rig-number")?.textContent).toBe("");
      await pushFirmwareMessage({ type: "CONTEXT", context: {
        bank: 1, slot: 3, kemper_bank: 2, kemper_rig: 8, kemper_rig_in_bank: 3,
      } });
      expect(container.querySelector(".stage__bank-number")).toHaveTextContent("Bank 2 Tour");
      expect(container.querySelector(".stage__rig-number")).toHaveTextContent("Saved Rig 8!");
    });

    it("uses compact kiosk formatting and live values, preserving empty prefixes and unknown fields", async () => {
      const { container } = renderStage({
        deviceInfo: { ...DEVICE, bank: 1, slot: 3 },
        device: { tft_labels: {
          bank: { prefix: "Banco ", suffix: " live" },
          kemper_rig_in_bank: { prefix: "", suffix: " / 5" },
        } },
      });
      await pushFirmwareMessage({ type: "CONTEXT", context: {
        bank: 2, slot: 3, kemper_rig_in_bank: 4,
      } });
      expect(container.querySelector(".stage__bank-number")).toHaveTextContent("Banco 2 live");
      expect(container.querySelector(".stage__rig-number")).toHaveTextContent("4 / 5");
      await pushFirmwareMessage({ type: "CONTEXT", partial: true, context: { kemper_rig_in_bank: null } });
      expect(container.querySelector(".stage__rig-number")?.textContent).toBe("");
    });

    it("shows the rig name from context and lets it override the patch name", async () => {
      const { container } = renderStage({
        deviceInfo: { ...DEVICE, bank: 1, slot: 2 },
      });
      // Before any CONTEXT arrives the patch name (fetched via PATCH) is shown.
      await pushFirmwareMessage({
        type: "PATCH",
        bank: 1,
        slot: 2,
        patch: { name: "Crunch" },
      });
      expect(container.querySelector(".stage__rig-name")).toHaveTextContent("Crunch");

      await pushFirmwareMessage({
        type: "CONTEXT",
        context: { kemper_rig_name: "Lead 100" },
      });
      expect(container.querySelector(".stage__rig-name")).toHaveTextContent("Lead 100");
    });

    it("falls back to the patch name when no rig name is set", async () => {
      const { container } = renderStage({
        deviceInfo: { ...DEVICE, bank: 1, slot: 1 },
      });
      await pushFirmwareMessage({
        type: "PATCH",
        bank: 1,
        slot: 1,
        patch: { name: "Crunch" },
      });
      expect(container.querySelector(".stage__rig-name")).toHaveTextContent("Crunch");
    });

    it("falls back to bank/slot when no patch matches the device location", () => {
      const { container } = renderStage({
        deviceInfo: { ...DEVICE, bank: 3, slot: 2 },
        patches: [patch(1, 1, "Crunch")],
      });
      expect(container.querySelector(".stage__rig-name")).toHaveTextContent("3/2");
    });

    it("falls back to a dash with no device and no patches", () => {
      const { container } = renderStage({ deviceInfo: null, patches: [] });
      expect(container.querySelector(".stage__rig-name")).toHaveTextContent("-");
    });

    it("shows bank/slot info when deviceInfo is present", () => {
      renderStage({
        deviceInfo: { ...DEVICE, bank: 2, slot: 3 },
      });
      expect(screen.getByText("BANK 2")).toBeInTheDocument();
      expect(screen.getByText("RIG 3")).toBeInTheDocument();
    });

    it("shows no bank/slot info without deviceInfo", () => {
      const { container } = renderStage({ deviceInfo: null });
      expect(container.querySelector(".stage__bank")).toBeNull();
    });

    it("shows BPM when present in context", async () => {
      const { container } = renderStage();
      expect(container.querySelector(".stage__bpm")).toBeNull();

      await pushFirmwareMessage({
        type: "CONTEXT",
        context: { kemper_bpm: 120 },
      });
      expect(container.querySelector(".stage__bpm")).toHaveTextContent("120 BPM");
    });

    it("shows the tuner with note and pitch indicator when active", async () => {
      const { container } = renderStage();
      expect(container.querySelector(".tuner-screen")).toBeNull();

      await pushFirmwareMessage({
        type: "CONTEXT",
        context: {
          kemper_tuner: "on",
          kemper_tuner_note: "A",
          kemper_tuner_deviance: 8200,
        },
      });
      expect(container.querySelector(".tuner-screen__note")).toHaveTextContent("A");
      expect(container.querySelector(".tuner-screen__meter")).toHaveAttribute("aria-label", "A: In tune");

      // Flat feedback updates the fullscreen pitch indicator.
      await pushFirmwareMessage({
        type: "CONTEXT",
        context: {
          kemper_tuner: "on",
          kemper_tuner_note: "A",
          kemper_tuner_deviance: 7800,
        },
      });
      expect(container.querySelector(".tuner-screen__meter")).toHaveAttribute("aria-label", "A: Tune up");
    });

    it("hides the tuner when it turns off", async () => {
      const { container } = renderStage();
      await pushFirmwareMessage({
        type: "CONTEXT",
        context: { kemper_tuner: "on", kemper_tuner_note: "A", kemper_tuner_deviance: 8200 },
      });
      expect(container.querySelector(".tuner-screen")).not.toBeNull();

      await pushFirmwareMessage({
        type: "CONTEXT",
        context: { kemper_tuner: "off" },
      });
      expect(container.querySelector(".tuner-screen")).toBeNull();
    });
  });

  describe("switch bindings", () => {
    it("shows the binding label when a binding exists", async () => {
      const { container } = renderStage({
        deviceInfo: { ...DEVICE, bank: 1, slot: 1 },
      });
      await pushFirmwareMessage({
        type: "PATCH",
        bank: 1,
        slot: 1,
        patch: {
          name: "Crunch",
          bindings: [
            binding("1", { label: "Lead" }),
            binding("A", { label: "Delay" }),
          ],
        },
      });
      expect(labelOf(switchById(container, "1"))).toBe("Lead");
      expect(labelOf(switchById(container, "A"))).toBe("Delay");
      // The switch ID is still shown below the label.
      expect(idOf(switchById(container, "1"))).toBe("1");
    });

    it("derives an effect label from the core message schema", async () => {
      const { container } = renderStage({
        deviceInfo: { ...DEVICE, bank: 1, slot: 1 },
        manifest: fallbackManifest(),
      });
      await pushFirmwareMessage({
        type: "PATCH",
        bank: 1,
        slot: 1,
        patch: {
          bindings: [
            binding("1", {
              actions: {
                press: { messages: [{ type: "cc", channel: 1, cc: 80, value: 127 }] },
              },
            }),
          ],
        },
      });
      expect(labelOf(switchById(container, "1"))).toBe("CC 80=127 ch 1");
    });

    it("derives an effect label from a plugin message schema", async () => {
      const manifest: Manifest = {
        core_messages: {},
        plugins: {
          kemper: {
            label: "Kemper",
            version: "1.0.0",
            messages: {
              scene: { label: "Scene", params: {}, summary: "Scene {scene}" },
            },
          },
        },
      };
      const { container } = renderStage({
        deviceInfo: { ...DEVICE, bank: 1, slot: 1 },
        manifest,
      });
      await pushFirmwareMessage({
        type: "PATCH",
        bank: 1,
        slot: 1,
        patch: {
          bindings: [
            binding("A", {
              actions: { press: { messages: [{ type: "scene", plugin: "kemper", scene: 3 }] } },
            }),
          ],
        },
      });
      expect(labelOf(switchById(container, "A"))).toBe("Scene 3");
    });

    it("falls back to the message type when no schema is available", async () => {
      const { container } = renderStage({
        deviceInfo: { ...DEVICE, bank: 1, slot: 1 },
        manifest: null,
      });
      await pushFirmwareMessage({
        type: "PATCH",
        bank: 1,
        slot: 1,
        patch: {
          bindings: [
            binding("1", {
              actions: { press: { messages: [{ type: "cc", channel: 1, cc: 80 }] } },
            }),
          ],
        },
      });
      expect(labelOf(switchById(container, "1"))).toBe("cc");
    });

    it("shows a dash for unbound switches", () => {
      const { container } = renderStage();
      switchEls(container).forEach((el) => {
        expect(labelOf(el)).toBe("-");
      });
    });

    it("marks bound switches as bound and active", async () => {
      const { container } = renderStage({
        deviceInfo: { ...DEVICE, bank: 1, slot: 1 },
      });
      await pushFirmwareMessage({
        type: "PATCH",
        bank: 1,
        slot: 1,
        patch: { bindings: [binding("3")] },
      });
      const bound = switchById(container, "3");
      expect(bound).toHaveClass("stage__switch--bound");
      expect(bound).toHaveClass("stage__switch--active");

      const unbound = switchById(container, "4");
      expect(unbound).not.toHaveClass("stage__switch--bound");
      expect(unbound).not.toHaveClass("stage__switch--active");
    });
  });

  describe("preset navigation row", () => {
    // device.preset_navigation is a device-level overlay (mirrors firmware's
    // _paint_preset_nav_leds in captain/app.py): it is NOT a patch binding,
    // so a nav switch never appears in fullPatch.bindings. Regression for
    // "bottom row shows no rig names" (2026-08-14) - StageView used to only
    // ever read bindings, so these switches rendered "-" forever.
    function navDevice(switches: Record<string, number>, bankColors: Record<string, string> = {}) {
      return { preset_navigation: { switches, bank_colors: bankColors } };
    }

    it("labels a nav switch with the target slot's patch name", () => {
      const { container } = renderStage({
        deviceInfo: { ...DEVICE, bank: 1, slot: 1 },
        device: navDevice({ A: 2 }),
        patches: [patch(1, 2, "Lead 100")],
      });
      expect(labelOf(switchById(container, "A"))).toBe("Lead 100");
    });

    it("falls through to unbound when no patch exists at the target slot", () => {
      // Mirrors the firmware's available_slots gate (_paint_preset_nav_leds /
      // bindings.py): a switch mapped to an empty slot is fully inert on the
      // real pedal (LED off, no navigation), so Stage must not claim a name
      // for it either - "RIG 4" would describe a switch that does nothing
      // when pressed.
      const { container } = renderStage({
        deviceInfo: { ...DEVICE, bank: 1, slot: 1 },
        device: navDevice({ A: 4 }),
        patches: [],
      });
      const sw = switchById(container, "A");
      expect(labelOf(sw)).toBe("-");
      expect(sw).not.toHaveClass("stage__switch--bound");
      expect(sw).not.toHaveClass("stage__switch--active");
    });

    it("falls through to unbound in a bank where the mapped slot is empty, even if another bank has a patch there", () => {
      const { container } = renderStage({
        deviceInfo: { ...DEVICE, bank: 2, slot: 1 },
        device: navDevice({ A: 4 }),
        patches: [patch(1, 4, "Heavy")], // only bank 1 has a patch at slot 4
      });
      expect(labelOf(switchById(container, "A"))).toBe("-");
    });

    it("marks the nav switch bound, and active only when it targets the current slot", () => {
      const { container } = renderStage({
        deviceInfo: { ...DEVICE, bank: 1, slot: 2 },
        device: navDevice({ A: 2, B: 3 }),
        patches: [patch(1, 2, "Lead"), patch(1, 3, "Rhythm")],
      });
      const current = switchById(container, "A");
      expect(current).toHaveClass("stage__switch--bound");
      expect(current).toHaveClass("stage__switch--active");

      const other = switchById(container, "B");
      expect(other).toHaveClass("stage__switch--bound");
      expect(other).not.toHaveClass("stage__switch--active");
    });

    it("leaves switches with no preset_navigation entry unbound", () => {
      const { container } = renderStage({
        deviceInfo: { ...DEVICE, bank: 1, slot: 1 },
        device: navDevice({ A: 2 }),
        patches: [patch(1, 2, "Lead")],
      });
      const unmapped = switchById(container, "B");
      expect(unmapped).not.toHaveClass("stage__switch--bound");
      expect(labelOf(unmapped)).toBe("-");
    });

    it("a real patch binding on the same switch takes priority over the nav overlay", async () => {
      const { container } = renderStage({
        deviceInfo: { ...DEVICE, bank: 1, slot: 1 },
        device: navDevice({ A: 2 }),
        patches: [patch(1, 2, "Lead")],
      });
      await pushFirmwareMessage({
        type: "PATCH",
        bank: 1,
        slot: 1,
        patch: { bindings: [binding("A", { label: "Delay" })] },
      });
      expect(labelOf(switchById(container, "A"))).toBe("Delay");
    });
  });

  describe("bank/patch change while mounted (long-press bank-step)", () => {
    // App.svelte updates the `deviceInfo` prop on an inbound EVENT
    // "patch_switched" (fired by any switch_patch() call in app.py,
    // including bank_step() from a long-press captain_bank_step binding -
    // not just editor-initiated navigation). Regression for "long-press
    // DOWN to bank 2, Stage keeps showing the previous bank's patch"
    // (2026-08-14): reproduces the prop change in isolation, without
    // depending on the transport actually delivering the EVENT.
    it("refetches and redisplays the new patch when deviceInfo's bank/slot changes", async () => {
      const { container, rerender } = renderStage({
        deviceInfo: { ...DEVICE, bank: 1, slot: 1 },
      });
      await pushFirmwareMessage({
        type: "PATCH",
        bank: 1,
        slot: 1,
        patch: { name: "Acoustic", bindings: [binding("1", { label: "EQ" })] },
      });
      expect(container.querySelector(".stage__rig-name")).toHaveTextContent("Acoustic");
      expect(labelOf(switchById(container, "1"))).toBe("EQ");

      // Simulate the App.svelte EVENT handler: patch_switched updates
      // deviceInfo to the new bank/slot (here: a long-press bank-step to
      // bank 2, landing on slot 4 - the lowest slot bank_step falls back to
      // when the current slot doesn't exist there).
      await rerender({ deviceInfo: { ...DEVICE, bank: 2, slot: 4 } });

      // The old patch must disappear atomically with the location change.
      // Applying new CONTEXT block values to the old binding map is worse
      // than a short neutral state while GET_PATCH is in flight.
      expect(container.querySelector(".stage__rig-name")).toHaveTextContent("2/4");
      expect(container.querySelector(".stage__rig-name")).not.toHaveTextContent("Acoustic");
      expect(labelOf(switchById(container, "1"))).toBe("-");

      // GET_PATCH for the new location must have been requested...
      expect(invokeMock).toHaveBeenCalledWith(
        "send_command",
        expect.objectContaining({
          line: expect.stringMatching(/"type":"GET_PATCH".*"bank":2.*"slot":4/),
        }),
      );

      // ...and once the firmware answers, Stage must show the NEW patch,
      // not keep displaying bank 1's stale ACOUSTIC/EQ.
      await pushFirmwareMessage({
        type: "PATCH",
        bank: 2,
        slot: 4,
        patch: { name: "Heavy", bindings: [binding("4", { label: "Gate" })] },
      });
      expect(container.querySelector(".stage__rig-name")).toHaveTextContent("Heavy");
      expect(container.querySelector(".stage__rig-name")).not.toHaveTextContent("Acoustic");
      expect(labelOf(switchById(container, "4"))).toBe("Gate");
      expect(labelOf(switchById(container, "1"))).toBe("-");
    });

    it("ignores a stale PATCH response for the bank it just left", async () => {
      // If the old bank 1 GET_PATCH response arrives AFTER the bank-step
      // (reordered on a slow/self-healing link), Stage must not let it
      // clobber the already-displayed new patch.
      const { container, rerender } = renderStage({
        deviceInfo: { ...DEVICE, bank: 1, slot: 1 },
      });
      await rerender({ deviceInfo: { ...DEVICE, bank: 2, slot: 4 } });
      await pushFirmwareMessage({
        type: "PATCH",
        bank: 2,
        slot: 4,
        patch: { name: "Heavy" },
      });
      expect(container.querySelector(".stage__rig-name")).toHaveTextContent("Heavy");

      // Late-arriving response for the patch we already navigated away from.
      await pushFirmwareMessage({
        type: "PATCH",
        bank: 1,
        slot: 1,
        patch: { name: "Acoustic" },
      });
      expect(container.querySelector(".stage__rig-name")).toHaveTextContent("Heavy");
    });
  });

  describe("empty and disconnected states", () => {
    it("renders without deviceInfo", () => {
      const { container } = renderStage({ deviceInfo: null });
      expect(container.querySelector(".stage")).not.toBeNull();
      expect(container.querySelectorAll(".stage__switch")).toHaveLength(10);
      expect(container.querySelector(".stage__bank")).toBeNull();
      expect(container.querySelector(".stage__rig-name")).toHaveTextContent("-");
    });

    it("renders without a manifest", async () => {
      const { container } = renderStage({
        deviceInfo: { ...DEVICE, bank: 1, slot: 1 },
        manifest: null,
      });
      await pushFirmwareMessage({
        type: "PATCH",
        bank: 1,
        slot: 1,
        patch: {
          bindings: [
            binding("2", { actions: { press: { messages: [{ type: "cc" }] } } }),
          ],
        },
      });
      expect(container.querySelectorAll(".stage__switch")).toHaveLength(10);
      expect(labelOf(switchById(container, "2"))).toBe("cc");
    });

    it("polls GET_CONTEXT and fetches the current patch while connected", () => {
      renderStage({
        deviceInfo: { ...DEVICE, bank: 1, slot: 1 },
        connected: true,
      });
      expect(invokeMock).toHaveBeenCalledWith(
        "send_command",
        expect.objectContaining({ line: expect.stringContaining("GET_CONTEXT") }),
      );
      expect(invokeMock).toHaveBeenCalledWith(
        "send_command",
        expect.objectContaining({ line: expect.stringContaining("GET_PATCH") }),
      );
    });

    it("does not poll or subscribe while disconnected", () => {
      const { container } = renderStage({ connected: false });
      expect(container.querySelectorAll(".stage__switch")).toHaveLength(10);
      expect(container.querySelector(".stage__rig-name")).toHaveTextContent("-");
      expect(invokeMock).not.toHaveBeenCalledWith("send_command", expect.anything());
    });
  });

  describe("exit button", () => {
    it("calls onExit when clicked", () => {
      const onExit = vi.fn();
      renderStage({ onExit });
      fireEvent.click(screen.getByRole("button", { name: "Exit Stage" }));
      expect(onExit).toHaveBeenCalledTimes(1);
    });
  });

  describe("stage appearance panel", () => {
    async function chooseFont(label: string, value: string) {
      const trigger = screen.getByRole("combobox", { name: label });
      await fireEvent.click(trigger);
      const option = screen.getAllByRole("option").find(item => item.getAttribute("data-value") === value);
      expect(option).toBeDefined();
      await fireEvent.click(option!);
      expect(trigger).toHaveAttribute("aria-expanded", "false");
      expect(trigger).toHaveAttribute("data-value", value);
    }

    it("persists global and section fonts selected from the touch menus", async () => {
      const { container } = renderStage();
      await fireEvent.click(screen.getByRole("button", { name: "Stage appearance" }));
      expect(screen.getAllByRole("combobox")).toHaveLength(8);
      expect(container.querySelector(".theme-panel select")).toBeNull();

      await chooseFont("Default font", FONT_STACKS.Serif);
      await chooseFont("Bank / Rig font", FONT_STACKS.Monospace);
      const stage = container.querySelector(".stage") as HTMLElement;
      expect(stage.style.getPropertyValue("--stage-font")).toBe(FONT_STACKS.Serif);
      expect(stage.style.getPropertyValue("--stage-bank-font")).toBe(FONT_STACKS.Monospace);
      expect(JSON.parse(localStorage.getItem("BOSUN_STAGE_THEME")!)).toMatchObject({
        fontFamily: FONT_STACKS.Serif,
        sections: { bank: { fontFamily: FONT_STACKS.Monospace } },
      });

      await fireEvent.click(screen.getByRole("button", { name: "Close appearance panel" }));
      await fireEvent.click(screen.getByRole("button", { name: "Stage appearance" }));
      expect(screen.getByRole("combobox", { name: "Default font" })).toHaveAttribute("data-value", FONT_STACKS.Serif);
      expect(screen.getByRole("combobox", { name: "Bank / Rig font" })).toHaveAttribute("data-value", FONT_STACKS.Monospace);
    });

    it("restores inherited fonts without removing another section appearance setting", async () => {
      const { container } = renderStage();
      await fireEvent.click(screen.getByRole("button", { name: "Stage appearance" }));
      await fireEvent.input(container.querySelector('input[title="Tuner color"]')!, { target: { value: "#abcdef" } });
      await chooseFont("Default font", FONT_STACKS.Serif);
      await chooseFont("Tuner font", FONT_STACKS.Monospace);
      await chooseFont("Tuner font", "");
      await chooseFont("Default font", "");

      const stage = container.querySelector(".stage") as HTMLElement;
      expect(stage.style.getPropertyValue("--stage-font")).toBe("");
      expect(stage.style.getPropertyValue("--stage-tuner-font")).toBe("");
      expect(stage.style.getPropertyValue("--stage-tuner-color")).toBe("#abcdef");
      const saved = JSON.parse(localStorage.getItem("BOSUN_STAGE_THEME")!);
      expect(saved.fontFamily).toBeUndefined();
      expect(saved.sections.tuner).toEqual({ color: "#abcdef" });
    });

    it("is closed by default and opens on the gear button", () => {
      const { container } = renderStage();
      expect(container.querySelector(".theme-panel")).toBeNull();
      fireEvent.click(screen.getByRole("button", { name: "Stage appearance" }));
      expect(container.querySelector(".theme-panel")).not.toBeNull();
    });

    it("closes via its own close button, not onExit", () => {
      const onExit = vi.fn();
      const { container } = renderStage({ onExit });
      fireEvent.click(screen.getByRole("button", { name: "Stage appearance" }));
      fireEvent.click(screen.getByRole("button", { name: "Close appearance panel" }));
      expect(container.querySelector(".theme-panel")).toBeNull();
      expect(onExit).not.toHaveBeenCalled();
    });

    it("applies an edited section color as a CSS var on the stage root, and persists it", () => {
      const { container } = renderStage();
      fireEvent.click(screen.getByRole("button", { name: "Stage appearance" }));

      const colorInput = container.querySelector(
        'input[title="Rig name color"]',
      ) as HTMLInputElement;
      expect(colorInput).not.toBeNull();
      fireEvent.input(colorInput, { target: { value: "#123456" } });

      const stageEl = container.querySelector(".stage") as HTMLElement;
      expect(stageEl.style.getPropertyValue("--stage-rig-name-color").trim()).toBe("#123456");
      expect(JSON.parse(localStorage.getItem("BOSUN_STAGE_THEME")!)).toMatchObject({
        sections: { rigName: { color: "#123456" } },
      });
    });

    it("resetting a section removes its CSS var", () => {
      const { container } = renderStage();
      fireEvent.click(screen.getByRole("button", { name: "Stage appearance" }));

      const colorInput = container.querySelector(
        'input[title="Tuner color"]',
      ) as HTMLInputElement;
      fireEvent.input(colorInput, { target: { value: "#abcdef" } });
      const stageEl = container.querySelector(".stage") as HTMLElement;
      expect(stageEl.style.getPropertyValue("--stage-tuner-color").trim()).toBe("#abcdef");

      fireEvent.click(screen.getAllByRole("button", { name: "Reset" })[3]); // tuner row
      expect(stageEl.style.getPropertyValue("--stage-tuner-color").trim()).toBe("");
    });
  });

  // Every visible field must reflect a single firmware message within one
  // flush - no polling delay, no waiting for the next unsolicited push.
  // `pushFirmwareMessage` resolves after exactly one macrotask (the drain
  // -> subscriber -> Svelte flush chain); each assertion runs immediately
  // after, so a passing test means the update was synchronous with the
  // message, not deferred. Regression net for the Pi kiosk, where
  // StageView mounts before the link is confirmed.
  describe("live update speed", () => {
    const kemperToggle = (slot: string) => ({
      messages: [{ type: "kemper_effect_toggle", plugin: "kemper", slot }],
    });

    function blockBinding(sw: string, slot: string, label: string): Binding {
      return binding(sw, {
        mode: "latched",
        label,
        actions: { toggle_on: kemperToggle(slot), toggle_off: kemperToggle(slot) },
      });
    }

    async function loadPatch(rerender: (p: Partial<StageProps>) => Promise<void>) {
      await pushFirmwareMessage({
        type: "PATCH",
        bank: 1,
        slot: 1,
        patch: {
          name: "CLEAN",
          bindings: [blockBinding("3", "X", "FLANG"), blockBinding("A", "Mod", "CHORUS")],
        },
      });
    }

    it("uses the last duplicate binding for its label, LED color and authoritative effect state", async () => {
      const { container } = renderStage({
        deviceInfo: { ...DEVICE, bank: 1, slot: 1, profile: "kemper", stage_input: true },
      });
      await pushFirmwareMessage({
        type: "PATCH", bank: 1, slot: 1, profile: "", active_profile: "kemper",
        patch: { bindings: [
          { ...blockBinding("3", "A", "Old action"), led: { on: "#ff0000", off: "#330000" } },
          { ...blockBinding("3", "D", "Final action"), led: { on: "#00ff00", off: "#003300" } },
        ] },
      });
      await pushFirmwareMessage({ type: "CONTEXT", context: { kemper_block_A: "on", kemper_block_D: "off" } });
      const tile = switchById(container, "3")!;
      expect(tile).toBeEnabled();
      expect(labelOf(tile)).toBe("Final action");
      expect(tile).not.toHaveTextContent("Old action");
      expect(tile).toHaveStyle("border-color: #003300");
      expect(tile).not.toHaveClass("stage__switch--active");
      expect(tile).toHaveAttribute("aria-pressed", "false");
    });

    it("lights a bound switch the instant its kemper_block_* turns on", async () => {
      const { container, rerender } = renderStage({
        deviceInfo: { ...DEVICE, bank: 1, slot: 1 },
        manifest: fallbackManifest(),
      });
      await loadPatch(rerender);

      // Off by default (latched map reset on patch load, no CONTEXT yet).
      expect(switchById(container, "3")).not.toHaveClass("stage__switch--active");

      await pushFirmwareMessage({
        type: "CONTEXT",
        context: { kemper_block_X: "on", kemper_block_Mod: "off" },
      });
      expect(switchById(container, "3")).toHaveClass("stage__switch--active");

      expect(switchById(container, "A")).not.toHaveClass("stage__switch--active");

      await pushFirmwareMessage({
        type: "CONTEXT",
        context: { kemper_block_X: "off", kemper_block_Mod: "on" },
      });
      expect(switchById(container, "3")).not.toHaveClass("stage__switch--active");
      expect(switchById(container, "A")).toHaveClass("stage__switch--active");
    });

    it("merges a fast partial effect update without erasing the rig snapshot", async () => {
      const { container, rerender } = renderStage({
        deviceInfo: { ...DEVICE, bank: 1, slot: 1 },
        manifest: fallbackManifest(),
      });
      await loadPatch(rerender);
      await pushFirmwareMessage({
        type: "CONTEXT",
        context: { kemper_rig_name: "ACOUSTIC", kemper_block_X: "off", kemper_block_Mod: "on" },
      });
      await pushFirmwareMessage({
        type: "CONTEXT",
        partial: true,
        context: { kemper_block_X: "on" },
      });

      expect(screen.getByText("ACOUSTIC")).toBeInTheDocument();
      expect(switchById(container, "3")).toHaveClass("stage__switch--active");
      expect(switchById(container, "A")).toHaveClass("stage__switch--active");
    });

    it("shows a Captain effect toggle immediately despite stale Kemper context, then reconciles", async () => {
      const { container, rerender } = renderStage({
        deviceInfo: { ...DEVICE, bank: 1, slot: 1 },
      });
      await loadPatch(rerender);

      // This is the normal steady state just before a footswitch press. The
      // Kemper feedback that confirms the new value has not arrived yet.
      await pushFirmwareMessage({
        type: "CONTEXT",
        context: { kemper_block_X: "off" },
      });
      invokeMock.mockClear();
      await pushFirmwareMessage({
        type: "EVENT",
        event: "binding_fired",
        switch: "3",
        action: "toggle_on",
      });

      // binding_fired is prompt; stale CONTEXT must not hide it for ~200 ms.
      expect(switchById(container, "3")).toHaveClass("stage__switch--active");

      // A queued snapshot from before the press must not make the effect
      // visibly bounce off while the Kemper confirmation is in flight.
      await pushFirmwareMessage({
        type: "CONTEXT",
        context: { kemper_block_X: "off" },
      });
      expect(switchById(container, "3")).toHaveClass("stage__switch--active");

      // If no unsolicited Kemper feedback arrives, actively request a full
      // authoritative snapshot so an optimistic state cannot stick forever.
      // An unrelated snapshot must not cancel that safety read.
      await pushFirmwareMessage({
        type: "CONTEXT",
        context: { kemper_bpm: 123 },
      });
      await new Promise((resolve) => setTimeout(resolve, 925));
      expect(invokeMock).toHaveBeenCalledWith(
        "send_command",
        expect.objectContaining({ line: expect.stringContaining('"type":"GET_CONTEXT"') }),
      );

      // The next real Kemper snapshot remains authoritative and can correct
      // an optimistic update when the MIDI command was not applied.
      await pushFirmwareMessage({
        type: "CONTEXT",
        context: { kemper_block_X: "off" },
      });
      expect(switchById(container, "3")).not.toHaveClass("stage__switch--active");
    });

    it("protects two rapid effect toggles independently from a stale snapshot", async () => {
      const { container, rerender } = renderStage({
        deviceInfo: { ...DEVICE, bank: 1, slot: 1 },
      });
      await loadPatch(rerender);
      await pushFirmwareMessage({
        type: "CONTEXT",
        context: { kemper_block_X: "off", kemper_block_Mod: "off" },
      });

      await pushFirmwareMessage({
        type: "EVENT", event: "binding_fired", switch: "3", action: "toggle_on",
      });
      await pushFirmwareMessage({
        type: "EVENT", event: "binding_fired", switch: "A", action: "toggle_on",
      });
      expect(switchById(container, "3")).toHaveClass("stage__switch--active");
      expect(switchById(container, "A")).toHaveClass("stage__switch--active");

      // One old full snapshot can contain stale values for both switches.
      // Each optimistic transition needs its own confirmation fence: the
      // second press must not cancel protection for the first one.
      await pushFirmwareMessage({
        type: "CONTEXT",
        context: { kemper_block_X: "off", kemper_block_Mod: "off", kemper_bpm: 120 },
      });
      expect(switchById(container, "3")).toHaveClass("stage__switch--active");
      expect(switchById(container, "A")).toHaveClass("stage__switch--active");

      // Confirming only Mod must leave X protected until its own feedback.
      await pushFirmwareMessage({
        type: "CONTEXT",
        context: { kemper_block_X: "off", kemper_block_Mod: "on" },
      });
      expect(switchById(container, "3")).toHaveClass("stage__switch--active");
      expect(switchById(container, "A")).toHaveClass("stage__switch--active");
    });

    it("cancels effect reconciliation from the old rig on patch switch", async () => {
      const { rerender } = renderStage({
        deviceInfo: { ...DEVICE, bank: 1, slot: 1 },
      });
      await loadPatch(rerender);
      await pushFirmwareMessage({
        type: "EVENT", event: "binding_fired", switch: "3", action: "toggle_on",
      });

      await pushFirmwareMessage({
        type: "EVENT", event: "patch_switched", bank: 1, slot: 2, source: "captain",
      });
      // Ignore the authoritative GET_CONTEXT intentionally sent for the new
      // rig. No old-rig reconciliation timer may send a second one later.
      invokeMock.mockClear();
      await new Promise((resolve) => setTimeout(resolve, 925));
      const lateContextRequests = invokeMock.mock.calls.filter(
        ([command, args]) => command === "send_command"
          && String((args as { line?: string })?.line).includes('"type":"GET_CONTEXT"'),
      );
      expect(lateContextRequests).toHaveLength(0);
    });

    it("turns a momentary Captain switch off again on its release event", async () => {
      const { container } = renderStage({
        deviceInfo: { ...DEVICE, bank: 1, slot: 1 },
      });
      await pushFirmwareMessage({
        type: "PATCH",
        bank: 1,
        slot: 1,
        patch: {
          name: "CLEAN",
          bindings: [binding("3", {
            mode: "momentary",
            label: "HOLD",
            actions: {
              press: { messages: [{ type: "cc", channel: 1, cc: 80, value: 127 }] },
              release: { messages: [{ type: "cc", channel: 1, cc: 80, value: 0 }] },
            },
          })],
        },
      });

      await pushFirmwareMessage({ type: "EVENT", event: "binding_fired", switch: "3", action: "press" });
      expect(switchById(container, "3")).toHaveClass("stage__switch--active");

      await pushFirmwareMessage({ type: "EVENT", event: "binding_fired", switch: "3", action: "release" });
      expect(switchById(container, "3")).not.toHaveClass("stage__switch--active");
    });

    it("uses Kemper state when the effect toggle is not the binding's first message", async () => {
      const { container } = renderStage({
        deviceInfo: { ...DEVICE, bank: 1, slot: 1 },
      });
      await pushFirmwareMessage({
        type: "PATCH",
        bank: 1,
        slot: 1,
        patch: {
          name: "CLEAN",
          bindings: [binding("3", {
            mode: "latched",
            label: "COMPOSITE",
            actions: {
              toggle_on: { messages: [
                { type: "cc", channel: 1, cc: 7, value: 127 },
                { type: "kemper_effect_toggle", plugin: "kemper", slot: "X", value: "on" },
              ] },
              toggle_off: { messages: [
                { type: "cc", channel: 1, cc: 7, value: 0 },
                { type: "kemper_effect_toggle", plugin: "kemper", slot: "X", value: "off" },
              ] },
            },
          })],
        },
      });

      await pushFirmwareMessage({ type: "CONTEXT", context: { kemper_block_X: "on" } });
      expect(switchById(container, "3")).toHaveClass("stage__switch--active");
      await pushFirmwareMessage({ type: "CONTEXT", context: { kemper_block_X: "off" } });
      expect(switchById(container, "3")).not.toHaveClass("stage__switch--active");
    });

    it("subscribes and reflects state when mounted disconnected, then connected", async () => {
      // The Pi kiosk mounts StageView before the hub link is confirmed.
      const { container, rerender } = renderStage({
        connected: false,
        deviceInfo: { ...DEVICE, bank: 1, slot: 1 },
        manifest: fallbackManifest(),
      });
      expect(invokeMock).not.toHaveBeenCalledWith("send_command", expect.anything());

      await rerender({ connected: true });
      // On connect it pulls the current context + patch...
      expect(invokeMock).toHaveBeenCalledWith(
        "send_command",
        expect.objectContaining({ line: expect.stringContaining("GET_CONTEXT") }),
      );
      // ...and now reacts to live messages.
      await loadPatch(rerender);
      await pushFirmwareMessage({
        type: "CONTEXT",
        context: { kemper_block_X: "on" },
      });
      expect(switchById(container, "3")).toHaveClass("stage__switch--active");
    });

    it("re-pulls context and patch after a reconnect", async () => {
      const { rerender } = renderStage({
        connected: true,
        deviceInfo: { ...DEVICE, bank: 1, slot: 1 },
      });
      invokeMock.mockClear();

      await rerender({ connected: false });
      await rerender({ connected: true });

      const sent = invokeMock.mock.calls
        .filter(([cmd]) => cmd === "send_command")
        .map(([, arg]) => (arg as { line: string }).line);
      expect(sent.some((l) => l.includes("GET_CONTEXT"))).toBe(true);
      expect(sent.some((l) => l.includes("GET_PATCH"))).toBe(true);
    });

    it("requests the current patch only once per connection/location change", async () => {
      const { rerender } = renderStage({
        deviceInfo: { ...DEVICE, bank: 1, slot: 1 },
      });
      await waitFor(() => {
        const patchRequests = invokeMock.mock.calls.filter(
          ([command, args]) => command === "send_command"
            && String((args as { line?: string })?.line).includes('"type":"GET_PATCH"'),
        );
        expect(patchRequests).toHaveLength(1);
      });

      invokeMock.mockClear();
      await rerender({ deviceInfo: { ...DEVICE, bank: 2, slot: 4 } });
      await waitFor(() => {
        const patchRequests = invokeMock.mock.calls.filter(
          ([command, args]) => command === "send_command"
            && String((args as { line?: string })?.line).includes('"type":"GET_PATCH"'),
        );
        expect(patchRequests).toHaveLength(1);
      });
    });

    it("updates rig name, BPM and tuner together from one CONTEXT", async () => {
      const { container } = renderStage({
        deviceInfo: { ...DEVICE, bank: 1, slot: 2 },
      });
      await pushFirmwareMessage({
        type: "CONTEXT",
        context: {
          kemper_rig_name: "CRUNCH",
          kemper_bpm: 132,
          kemper_tuner: "on",
          kemper_tuner_note: "A",
          kemper_tuner_deviance: 8192,
        },
      });
      expect(container.querySelector(".stage__rig-name")).toHaveTextContent("CRUNCH");
      expect(container.querySelector(".stage__bpm")).toHaveTextContent("132");
      expect(container.querySelector(".tuner-screen__note")).toHaveTextContent("A");
    });

    it("follows a rig change: bank/rig header and block colours track together", async () => {
      const { container, rerender } = renderStage({
        deviceInfo: { ...DEVICE, bank: 1, slot: 1 },
        manifest: fallbackManifest(),
      });
      await loadPatch(rerender);
      await pushFirmwareMessage({ type: "CONTEXT", context: { kemper_block_X: "on" } });
      expect(switchById(container, "3")).toHaveClass("stage__switch--active");

      // Rig change: new deviceInfo, new PATCH, new CONTEXT with X now off.
      await rerender({ deviceInfo: { ...DEVICE, bank: 1, slot: 2 } });
      await pushFirmwareMessage({
        type: "PATCH",
        bank: 1,
        slot: 2,
        patch: { name: "LEAD", bindings: [blockBinding("3", "X", "FLANG")] },
      });
      await pushFirmwareMessage({ type: "CONTEXT", context: { kemper_block_X: "off" } });

      expect(container.querySelector(".stage__rig-number")).toHaveTextContent("RIG 2");
      expect(switchById(container, "3")).not.toHaveClass("stage__switch--active");
    });

    it("preserves block state when one CONTEXT message also changes the rig", async () => {
      const { container, rerender } = renderStage({
        deviceInfo: { ...DEVICE, bank: 1, slot: 1 },
      });
      await loadPatch(rerender);

      // KioskApp observes this same message and updates deviceInfo after the
      // Stage subscriber has already stored X=on. The following rerender used
      // to erase context, leaving Captain FLANG lit but Stage FLANG dark.
      await pushFirmwareMessage({
        type: "CONTEXT",
        partial: true,
        context: { bank: 1, slot: 2, kemper_block_X: "on" },
      });
      await rerender({ deviceInfo: { ...DEVICE, bank: 1, slot: 2 } });
      await pushFirmwareMessage({
        type: "PATCH",
        bank: 1,
        slot: 2,
        patch: { name: "CLEAN", bindings: [blockBinding("3", "X", "FLANG")] },
      });

      expect(switchById(container, "3")).toHaveClass("stage__switch--active");
    });

    it("does not flash a new binding from the previous rig's block state", async () => {
      const { container, rerender } = renderStage({
        deviceInfo: { ...DEVICE, bank: 1, slot: 1 },
      });
      await pushFirmwareMessage({
        type: "PATCH", bank: 1, slot: 1,
        patch: { name: "ACOUSTIC", bindings: [blockBinding("up", "Reverb", "SPACE")] },
      });
      await pushFirmwareMessage({
        type: "CONTEXT",
        context: { bank: 1, slot: 1, kemper_block_Reverb: "on" },
      });
      expect(switchById(container, "UP")).toHaveClass("stage__switch--active");

      // CLEAN also uses Reverb, but calls it BOOST and starts with it off. The
      // old ACOUSTIC value must not light the new binding while its context is
      // still in flight.
      await rerender({ deviceInfo: { ...DEVICE, bank: 1, slot: 2 } });
      await pushFirmwareMessage({
        type: "PATCH", bank: 1, slot: 2,
        patch: { name: "CLEAN", bindings: [blockBinding("up", "Reverb", "BOOST")] },
      });
      expect(switchById(container, "UP")).not.toHaveClass("stage__switch--active");
    });

    it("requests an authoritative context after every patch_switched event", async () => {
      const { container } = renderStage({
        deviceInfo: { ...DEVICE, bank: 1, slot: 1 },
      });
      await pushFirmwareMessage({
        type: "PATCH", bank: 1, slot: 1,
        patch: { name: "ACOUSTIC", bindings: [blockBinding("4", "X", "HARM")] },
      });
      await pushFirmwareMessage({
        type: "CONTEXT",
        context: { bank: 1, slot: 1, kemper_block_X: "on" },
      });
      expect(switchById(container, "4")).toHaveClass("stage__switch--active");
      invokeMock.mockClear();

      // Re-selecting the same rig is the important edge case: the Kemper may
      // emit no block delta because X never changed, while Stage invalidates
      // the old context to avoid displaying stale states.
      await pushFirmwareMessage({
        type: "EVENT", event: "patch_switched", bank: 1, slot: 1, source: "editor",
      });

      expect(switchById(container, "4")).not.toHaveClass("stage__switch--active");
      expect(invokeMock).toHaveBeenCalledWith(
        "send_command",
        expect.objectContaining({ line: expect.stringContaining('"type":"GET_CONTEXT"') }),
      );

      await pushFirmwareMessage({
        type: "PATCH", bank: 1, slot: 1,
        patch: { name: "ACOUSTIC", bindings: [blockBinding("4", "X", "HARM")] },
      });
      await pushFirmwareMessage({
        type: "CONTEXT", id: "post-switch-snapshot",
        context: { bank: 1, slot: 1, kemper_block_X: "on" },
      });
      expect(switchById(container, "4")).toHaveClass("stage__switch--active");
    });

    it("keeps fresh effects with the real event-PATCH-CONTEXT ordering", async () => {
      const { container, rerender } = renderStage({
        deviceInfo: { ...DEVICE, bank: 1, slot: 1 },
      });
      await pushFirmwareMessage({
        type: "PATCH", bank: 1, slot: 1,
        patch: { name: "ACOUSTIC", bindings: [blockBinding("up", "Reverb", "SPACE")] },
      });
      await pushFirmwareMessage({
        type: "CONTEXT",
        context: { bank: 1, slot: 1, kemper_block_Reverb: "on" },
      });

      // These may all arrive before KioskApp propagates its new deviceInfo
      // prop to StageView. PATCH must not be discarded and X must survive.
      await pushFirmwareMessage({
        type: "EVENT", event: "patch_switched", bank: 1, slot: 2, source: "editor",
      });
      await pushFirmwareMessage({
        type: "CONTEXT", id: "old-acoustic-poll",
        context: { bank: 1, slot: 1, kemper_block_Reverb: "on" },
      });
      await pushFirmwareMessage({
        type: "PATCH", bank: 1, slot: 2,
        patch: { name: "CLEAN", bindings: [
          blockBinding("3", "X", "FLANG"),
          blockBinding("up", "Reverb", "BOOST"),
        ] },
      });
      await pushFirmwareMessage({
        type: "CONTEXT", partial: true,
        context: { kemper_block_X: "on", kemper_block_Reverb: "off" },
      });
      await rerender({ deviceInfo: { ...DEVICE, bank: 1, slot: 2 } });

      expect(switchById(container, "3")).toHaveClass("stage__switch--active");
      expect(switchById(container, "UP")).not.toHaveClass("stage__switch--active");
    });

    it("treats an uncorrelated same-rig MIDI event as a real reselect", async () => {
      const { container } = renderStage({
        deviceInfo: { ...DEVICE, bank: 1, slot: 1 },
      });
      await pushFirmwareMessage({
        type: "EVENT", event: "patch_switched",
        bank: 1, slot: 1, source: "editor",
      });
      await pushFirmwareMessage({
        type: "PATCH", bank: 1, slot: 1,
        patch: {
          name: "ACOUSTIC",
          bindings: [blockBinding("4", "X", "HARM")],
        },
      });
      await pushFirmwareMessage({
        type: "CONTEXT", partial: true,
        context: {
          bank: 1, slot: 1,
          kemper_rig_name: "ACOUSTIC", kemper_block_X: "on",
        },
      });
      expect(switchById(container, "4")).toHaveClass("stage__switch--active");

      // Firmware may already have consumed the genuine PC echo. From
      // Stage's point of view the first same-location midi_in event, even
      // ~2.34 s later, can therefore be a real user reselect. Without a
      // causal token Stage must invalidate it and request fresh state.
      invokeMock.mockClear();
      await pushFirmwareMessage({
        type: "EVENT", event: "patch_switched",
        bank: 1, slot: 1, source: "midi_in",
      });
      expect(switchById(container, "4")).not.toHaveClass("stage__switch--active");
      expect(invokeMock).toHaveBeenCalledWith(
        "send_command",
        expect.objectContaining({ line: expect.stringContaining('"type":"GET_CONTEXT"') }),
      );

      await pushFirmwareMessage({
        type: "PATCH", bank: 1, slot: 1,
        patch: {
          name: "ACOUSTIC",
          bindings: [blockBinding("4", "X", "HARM")],
        },
      });
      await pushFirmwareMessage({
        type: "CONTEXT", id: "post-reselect-full",
        context: { bank: 1, slot: 1, kemper_rig_name: "ACOUSTIC" },
      });
      expect(switchById(container, "4")).not.toHaveClass("stage__switch--active");
    });
  });
});
