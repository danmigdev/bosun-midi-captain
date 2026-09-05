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
  // Stage theme persists to localStorage (see stage-theme.ts) - clear it so
  // one test's edits can't bleed into the next.
  localStorage.clear();
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
  describe("expression pedal indicator", () => {
    it("shows the confirmed mode with a pedal icon inside the title bar", async () => {
      const { container } = renderStage();
      await pushFirmwareMessage({ type: "CONTEXT", context: { expression_mode: "WAH" } });
      const badge = container.querySelector(".stage__header .stage__expression");
      expect(badge).toHaveTextContent("WAH");
      expect(badge?.querySelector("svg")).not.toBeNull();
      expect(badge?.querySelector("svg")).toHaveAttribute("data-mode", "WAH");
      const wahPath = badge?.querySelector("path")?.getAttribute("d");
      expect(badge).toHaveAttribute("aria-label", "Expression pedal: WAH");
      await pushFirmwareMessage({ type: "CONTEXT", partial: true, context: { expression_mode: "VOL" } });
      expect(badge).toHaveTextContent("VOL");
      expect(badge?.querySelector("svg")).toHaveAttribute("data-mode", "VOL");
      expect(badge?.querySelector("path")?.getAttribute("d")).not.toBe(wahPath);
      expect(badge?.querySelector("path")).toHaveAttribute("fill", "currentColor");
    });

    it("never guesses VOL when the state is missing, invalid, disconnected or changing rig", async () => {
      const { container, rerender } = renderStage();
      const badge = () => container.querySelector(".stage__expression");
      expect(badge()).toHaveTextContent("---");
      expect(badge()?.querySelector("svg")).toHaveStyle("visibility: hidden");
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
      expect(container.querySelector(".stage__bank")).toHaveTextContent("· 2 · 3");
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
      expect(container.querySelector(".stage__bank-number")).toHaveTextContent("· Bank 2 Tour");
      expect(container.querySelector(".stage__rig-number")).toHaveTextContent("· Saved Rig 8!");
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
      expect(container.querySelector(".stage__bank-number")).toHaveTextContent("· Banco 2 live");
      expect(container.querySelector(".stage__rig-number")).toHaveTextContent("· 4 / 5");
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
      expect(screen.getByText("· BANK 2")).toBeInTheDocument();
      expect(screen.getByText("· RIG 3")).toBeInTheDocument();
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
      expect(container.querySelector(".stage__tuner")).toHaveTextContent("A");
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

      expect(container.querySelector(".stage__meta")).toHaveTextContent("RIG 2");
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
