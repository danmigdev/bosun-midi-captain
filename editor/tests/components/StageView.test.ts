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

import { describe, it, expect, vi, beforeEach } from "vitest";
import { render, screen, fireEvent, waitFor } from "@testing-library/svelte";
import StageView from "../../src/components/StageView.svelte";
import { DEFAULT_LAYOUT } from "../../src/lib/pedal-layout";
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
});

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

// --- tests ---------------------------------------------------------------

describe("StageView", () => {
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
      expect(screen.getByText("BANK 2 · RIG 3")).toBeInTheDocument();
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
      expect(container.querySelector(".stage__tuner")).toBeNull();

      await pushFirmwareMessage({
        type: "CONTEXT",
        context: {
          kemper_tuner: "on",
          kemper_tuner_note: "A",
          kemper_tuner_deviance: 8200,
        },
      });
      expect(container.querySelector(".stage__tuner")).toHaveTextContent(/A ●/);

      // Deviance below 8000 renders a flat sign.
      await pushFirmwareMessage({
        type: "CONTEXT",
        context: {
          kemper_tuner: "on",
          kemper_tuner_note: "A",
          kemper_tuner_deviance: 7800,
        },
      });
      expect(container.querySelector(".stage__tuner")).toHaveTextContent(/A ♭/);
    });

    it("hides the tuner when it turns off", async () => {
      const { container } = renderStage();
      await pushFirmwareMessage({
        type: "CONTEXT",
        context: { kemper_tuner: "on", kemper_tuner_note: "A", kemper_tuner_deviance: 8200 },
      });
      expect(container.querySelector(".stage__tuner")).not.toBeNull();

      await pushFirmwareMessage({
        type: "CONTEXT",
        context: { kemper_tuner: "off" },
      });
      expect(container.querySelector(".stage__tuner")).toBeNull();
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
});
