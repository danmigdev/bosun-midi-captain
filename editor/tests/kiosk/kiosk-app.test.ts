// Integration test for the Pi kiosk: the real transport shims
// (src/kiosk/tauri-core.ts + tauri-event.ts) over a fake WebSocket,
// driving the real protocol.ts + KioskApp + StageView. Proves that a
// firmware message from the hub reaches the rendered grid, and does so
// promptly - the exact path that was silently not subscribing on the
// device.

import { describe, it, expect, vi, beforeEach, afterEach } from "vitest";
import { fireEvent, render, screen, waitFor } from "@testing-library/svelte";
import KioskApp from "../../src/kiosk/KioskApp.svelte";
import { wsLink } from "../../src/kiosk/ws-link";

class FakeWebSocket {
  static instances: FakeWebSocket[] = [];
  static OPEN = 1;
  readyState = 0;
  sent: string[] = [];
  onopen: (() => void) | null = null;
  onclose: (() => void) | null = null;
  onmessage: ((e: { data: string }) => void) | null = null;
  onerror: (() => void) | null = null;
  constructor(public url: string) {
    FakeWebSocket.instances.push(this);
  }
  send(d: string) {
    this.sent.push(d);
  }
  close() {
    this.readyState = 3;
    this.onclose?.();
  }
  _open() {
    this.readyState = 1;
    this.onopen?.();
  }
  _msg(d: string) {
    this.onmessage?.({ data: d });
  }
}

vi.mock("@tauri-apps/api/core", async () => await import("../../src/kiosk/tauri-core"));
vi.mock("@tauri-apps/api/event", async () => await import("../../src/kiosk/tauri-event"));

const sock = () => FakeWebSocket.instances[FakeWebSocket.instances.length - 1];
function reply(obj: unknown) {
  sock()._msg(JSON.stringify(obj));
}
function lastSent(match: string): Record<string, unknown> | undefined {
  const line = [...sock().sent].reverse().find((l) => l.includes(match));
  return line ? JSON.parse(line) : undefined;
}
function sentCount(match: string): number {
  return sock().sent.filter((line) => line.includes(match)).length;
}

beforeEach(() => {
  FakeWebSocket.instances = [];
  vi.stubGlobal("WebSocket", FakeWebSocket as unknown as typeof WebSocket);
  vi.stubGlobal("location", {
    protocol: "http:",
    hostname: "pi",
    search: "",
    reload: vi.fn(),
  } as unknown as Location);
  localStorage.clear();
  wsLink.__cycleSocketForTest();
  wsLink.start(); // opens a fresh fake socket for this test
});

afterEach(() => {
  wsLink.__cycleSocketForTest();
  vi.unstubAllGlobals();
});

function mountKiosk() {
  return render(KioskApp);
}

async function bringLinkUp() {
  sock()._open();
  sock()._msg(JSON.stringify({ type: "HUB", link: "up" }));
  await waitFor(() => expect(lastSent("GET_DEVICE_INFO")).toBeTruthy());
}

/** Complete the current-firmware fast bootstrap without GET_GLOBAL. */
function answerBootstrap(bank = 1, slot = 1) {
  reply({
    type: "DEVICE_INFO", id: lastSent("GET_DEVICE_INFO")!.id,
    fw: "0.6.4", device: "midi_captain_10", current: { bank, slot },
    preset_navigation: {},
  });
  const p = lastSent("LIST_PATCHES");
  if (p) reply({ type: "PATCH_LIST", id: p.id, patches: [] });
}

const kToggle = (slot: string) => ({
  messages: [{ type: "kemper_effect_toggle", plugin: "kemper", slot }],
});
const sw = (container: HTMLElement, id: string) =>
  [...container.querySelectorAll(".stage__switch")].find(
    (el) => el.querySelector(".stage__switch-id")?.textContent === id,
  )!;

describe("kiosk integration", () => {
  it("applies and refreshes the bank limit from the compact firmware projection without GET_GLOBAL", async () => {
    mountKiosk();
    await bringLinkUp();
    const deviceInfo = (bank_count?: number) => ({
      type: "DEVICE_INFO", id: lastSent("GET_DEVICE_INFO")!.id,
      fw: "0.6.5-native", device: "MIDI Captain", current: { bank: 1, slot: 1 },
      profile: "generic", preset_navigation: {}, bank_count,
    });
    reply(deviceInfo(1));
    reply({ type: "PATCH_LIST", id: lastSent("LIST_PATCHES")!.id, profile: "generic",
      patches: [1, 2].map(bank => ({ bank, slot: 1, name: `Bank ${bank}`, dirty: false })) });
    await waitFor(() => expect(screen.getByRole("button", { name: "Next bank" })).toBeDisabled());
    const infos = sentCount("GET_DEVICE_INFO");
    reply({ type: "EVENT", event: "global_changed" });
    await waitFor(() => expect(sentCount("GET_DEVICE_INFO")).toBe(infos + 1));
    reply(deviceInfo(2));
    await waitFor(() => expect(screen.getByRole("button", { name: "Next bank" })).toBeEnabled());
    reply({ type: "EVENT", event: "global_changed" });
    await waitFor(() => expect(sentCount("GET_DEVICE_INFO")).toBe(infos + 2));
    reply(deviceInfo(1));
    await waitFor(() => expect(screen.getByRole("button", { name: "Next bank" })).toBeDisabled());
    reply({ type: "EVENT", event: "global_changed" });
    await waitFor(() => expect(sentCount("GET_DEVICE_INFO")).toBe(infos + 3));
    reply(deviceInfo()); // missing setting restores the default, rather than leaking the previous limit
    await waitFor(() => expect(screen.getByRole("button", { name: "Next bank" })).toBeEnabled());
    expect(sentCount("GET_GLOBAL")).toBe(0);
    expect(sentCount("SWITCH_PATCH")).toBe(0);
  });

  it("passes Stage input capability and guarded taps through the real WebSocket path, retaining confirmed latch state after refresh", async () => {
    const { container } = mountKiosk();
    await bringLinkUp();
    const deviceInfo = () => ({
      type: "DEVICE_INFO", id: lastSent("GET_DEVICE_INFO")!.id,
      fw: "0.6.5-native", device: "MIDI Captain", current: { bank: 2, slot: 3 },
      profile: "generic", stage_input: true, preset_navigation: {},
    });
    reply(deviceInfo());
    reply({ type: "PATCH_LIST", id: lastSent("LIST_PATCHES")!.id, profile: "generic", patches: [] });
    await waitFor(() => expect(lastSent("GET_PATCH")).toBeTruthy());
    const patch = () => ({
      type: "PATCH", id: lastSent("GET_PATCH")!.id, bank: 2, slot: 3, profile: "", active_profile: "generic",
      patch: { name: "Generic", bindings: [{ switch: "up", mode: "latched", label: "Delay", actions: {
        toggle_on: { messages: [{ type: "cc", channel: 1, cc: 22, value: 127 }] },
        toggle_off: { messages: [{ type: "cc", channel: 1, cc: 22, value: 0 }] },
      } }] },
    });
    expect(lastSent("GET_PATCH")).toMatchObject({ bank: 2, slot: 3 });
    expect(lastSent("GET_PATCH")).not.toHaveProperty("profile");
    reply(patch());
    await waitFor(() => expect(sw(container, "UP")).toBeEnabled());
    const tile = sw(container, "UP");
    const infos = sentCount("GET_DEVICE_INFO");
    await fireEvent.click(tile.querySelector(".stage__switch-label")!);
    await waitFor(() => expect(lastSent("ACTIVATE_SWITCH")).toBeTruthy());
    const command = lastSent("ACTIVATE_SWITCH")!;
    expect(command).toMatchObject({ switch: "up", bank: 2, slot: 3, profile: "generic" });
    expect(sentCount("GET_DEVICE_INFO")).toBe(infos);
    expect(tile).toBeDisabled();
    expect(tile).toHaveAttribute("aria-pressed", "false");
    await fireEvent.click(tile);
    expect(sentCount("ACTIVATE_SWITCH")).toBe(1);
    reply({ type: "EVENT", event: "binding_fired", switch: "up", action: "toggle_on" });
    await waitFor(() => expect(tile).toHaveAttribute("aria-pressed", "true"));
    reply({ type: "ACK", id: command.id });
    await waitFor(() => expect(sentCount("GET_DEVICE_INFO")).toBe(infos + 1));
    const patches = sentCount("GET_PATCH");
    reply(deviceInfo());
    await waitFor(() => expect(sentCount("GET_PATCH")).toBeGreaterThan(patches));
    reply(patch());
    await waitFor(() => expect(tile).toBeEnabled());
    expect(tile).toHaveAttribute("aria-pressed", "true");
    expect(sentCount("ACTIVATE_SWITCH")).toBe(1);
  });

  it.each([undefined, false, "true"])("does not enable remote switches when DEVICE_INFO stage_input is %s", async stage_input => {
    const { container } = mountKiosk();
    await bringLinkUp();
    reply({ type: "DEVICE_INFO", id: lastSent("GET_DEVICE_INFO")!.id,
      fw: "0.6.5-native", device: "MIDI Captain", current: { bank: 1, slot: 1 },
      profile: "generic", stage_input, preset_navigation: {} });
    reply({ type: "PATCH_LIST", id: lastSent("LIST_PATCHES")!.id, profile: "generic", patches: [] });
    await waitFor(() => expect(lastSent("GET_PATCH")).toBeTruthy());
    reply({ type: "PATCH", id: lastSent("GET_PATCH")!.id, bank: 1, slot: 1, profile: "generic",
      patch: { name: "Generic", bindings: [{ switch: "1", mode: "tap", label: "Action", actions: {} }] } });
    await waitFor(() => expect(sw(container, "1")).toHaveTextContent("Action"));
    expect(sw(container, "1")).toBeDisabled();
    await fireEvent.click(sw(container, "1"));
    expect(sentCount("ACTIVATE_SWITCH")).toBe(0);
    expect(container).toHaveTextContent("Update Captain firmware to control switches from Stage.");
  });

  it("keeps old switch bindings disabled across reconnect until the new device and patch replies arrive", async () => {
    const { container } = mountKiosk();
    await bringLinkUp();
    const deviceInfo = () => ({ type: "DEVICE_INFO", id: lastSent("GET_DEVICE_INFO")!.id,
      fw: "0.6.5-native", device: "MIDI Captain", current: { bank: 1, slot: 1 },
      profile: "generic", stage_input: true, preset_navigation: {} });
    const patch = (label: string) => ({ type: "PATCH", id: lastSent("GET_PATCH")!.id,
      bank: 1, slot: 1, profile: "", active_profile: "generic", patch: { name: "Generic", bindings: [
        { switch: "1", mode: "tap", label, actions: {} },
      ] } });
    reply(deviceInfo());
    reply({ type: "PATCH_LIST", id: lastSent("LIST_PATCHES")!.id, profile: "generic", patches: [] });
    await waitFor(() => expect(lastSent("GET_PATCH")).toBeTruthy());
    reply(patch("Old action"));
    await waitFor(() => expect(sw(container, "1")).toBeEnabled());
    const infos = sentCount("GET_DEVICE_INFO");
    reply({ type: "HUB", link: "down" });
    await waitFor(() => expect(sw(container, "1")).toBeDisabled());
    reply({ type: "HUB", link: "up" });
    await waitFor(() => expect(sentCount("GET_DEVICE_INFO")).toBeGreaterThan(infos));
    expect(sw(container, "1")).toBeDisabled();
    reply(deviceInfo());
    reply({ type: "PATCH_LIST", id: lastSent("LIST_PATCHES")!.id, profile: "generic", patches: [] });
    await waitFor(() => expect(lastSent("GET_PATCH")).toMatchObject({ bank: 1, slot: 1 }));
    expect(lastSent("GET_PATCH")).not.toHaveProperty("profile");
    expect(sw(container, "1")).toBeDisabled();
    reply(patch("New action"));
    await waitFor(() => expect(sw(container, "1")).toBeEnabled());
    expect(sw(container, "1")).toHaveTextContent("New action");
    expect(sentCount("ACTIVATE_SWITCH")).toBe(0);
  });

  it("reloads bindings when firmware replaces the patch at the current coordinates", async () => {
    const { container } = mountKiosk();
    await bringLinkUp();
    answerBootstrap(1, 1);
    await waitFor(() => expect(lastSent("GET_PATCH")).toBeTruthy());
    const patch = (label: string) => ({
      type: "PATCH", id: lastSent("GET_PATCH")!.id, bank: 1, slot: 1,
      patch: { name: "CLEAN", bindings: [{
        switch: "1", mode: "latched", label,
        actions: { toggle_on: kToggle("X"), toggle_off: kToggle("X") },
      }] },
    });
    reply(patch("OLD LABEL"));
    await waitFor(() => expect(sw(container, "1")).toHaveTextContent("OLD LABEL"));
    const before = sentCount("GET_PATCH");
    reply({ type: "EVENT", event: "patch_switched", bank: 1, slot: 1, source: "editor" });
    await waitFor(() => expect(sentCount("GET_PATCH")).toBeGreaterThan(before));
    reply(patch("NEW LABEL"));
    await waitFor(() => expect(sw(container, "1")).toHaveTextContent("NEW LABEL"));
    expect(sw(container, "1")).not.toHaveTextContent("OLD LABEL");
  });

  it("uses compact Screen colors and refreshes them after a save without fetching GLOBAL or polling", async () => {
    vi.useFakeTimers();
    const view = mountKiosk();
    try {
      await vi.advanceTimersByTimeAsync(0);
      sock()._open();
      reply({ type: "HUB", link: "up" });
      await vi.advanceTimersByTimeAsync(0);
      const deviceInfo = (tft_colors: Record<string, string>) => ({
        type: "DEVICE_INFO", id: lastSent("GET_DEVICE_INFO")!.id,
        fw: "0.6.4", device: "midi_captain_10", current: { bank: 1, slot: 3 },
        preset_navigation: {}, tft_colors,
        tft_labels: { bank: { prefix: "Bank ", suffix: " Tour" }, slot: { prefix: "Preset ", suffix: "!" } },
      });
      reply(deviceInfo({ patch_name: "#abcdef", bank: "#fedcba", kemper_rig: "#12ab34", expression_mode: "#f0ab12" }));
      reply({ type: "PATCH_LIST", id: lastSent("LIST_PATCHES")!.id, patches: [] });
      await vi.advanceTimersByTimeAsync(0);
      expect(view.container.querySelector(".stage__rig-name")).toHaveStyle({ color: "#abcdef" });
      expect(view.container.querySelector(".stage__bank-number")).toHaveStyle({ color: "#fedcba" });
      expect(view.container.querySelector(".stage__rig-number")).toHaveStyle({ color: "#12ab34" });
      expect(view.container.querySelector(".stage__expression")).toHaveStyle({ color: "#f0ab12" });
      expect(view.container.querySelector(".stage__bank-number")).toHaveTextContent("Bank 1 Tour");
      expect(view.container.querySelector(".stage__rig-number")).toHaveTextContent("Preset 3!");
      const before = sentCount("GET_DEVICE_INFO");
      reply({ type: "EVENT", event: "global_changed" });
      await vi.advanceTimersByTimeAsync(0);
      expect(sentCount("GET_DEVICE_INFO")).toBe(before + 1);
      // A second save overtaking that snapshot is coalesced into one fresh
      // read after its reply, so an old in-flight color cannot remain stuck.
      reply({ type: "EVENT", event: "global_changed" });
      await vi.advanceTimersByTimeAsync(0);
      expect(sentCount("GET_DEVICE_INFO")).toBe(before + 1);
      reply(deviceInfo({ patch_name: "#abcdef" }));
      await vi.advanceTimersByTimeAsync(0);
      expect(sentCount("GET_DEVICE_INFO")).toBe(before + 2);
      reply(deviceInfo({ patch_name: "#123456" }));
      await vi.advanceTimersByTimeAsync(0);
      expect(view.container.querySelector(".stage__rig-name")).toHaveStyle({ color: "#123456" });
      expect(view.container.querySelector<HTMLElement>(".stage__bank-number")?.style.color).toBe("");
      await vi.advanceTimersByTimeAsync(5_000);
      expect(sentCount("GET_DEVICE_INFO")).toBe(before + 2);
      expect(sentCount("GET_GLOBAL")).toBe(0);
    } finally {
      view.unmount();
      vi.useRealTimers();
    }
  });

  it("shows 'Waiting for the pedal' until the hub link is up", async () => {
    const { container } = mountKiosk();
    expect(container.textContent).toContain("Waiting for the pedal");
    await bringLinkUp();
    await waitFor(() => expect(container.querySelector(".stage")).not.toBeNull());
    expect(container.textContent).not.toContain("Waiting for the pedal");
  });

  it("does not bootstrap on WebSocket open before the Captain link is up", async () => {
    vi.useFakeTimers();
    const view = mountKiosk();
    try {
      // Let KioskApp finish registering all transport listeners first.
      await vi.advanceTimersByTimeAsync(0);
      sock()._open();
      await vi.advanceTimersByTimeAsync(2_100);

      expect(sentCount("GET_DEVICE_INFO")).toBe(0);
      expect(sentCount("LIST_PATCHES")).toBe(0);
      expect(sentCount("GET_GLOBAL")).toBe(0);

      sock()._msg(JSON.stringify({ type: "HUB", link: "up" }));
      await vi.advanceTimersByTimeAsync(0);
      expect(sentCount("GET_DEVICE_INFO")).toBe(1);
      expect(sentCount("LIST_PATCHES")).toBe(1);
      expect(sentCount("GET_GLOBAL")).toBe(0);
    } finally {
      view.unmount();
      vi.useRealTimers();
    }
  });

  it("renders the Stage grid and tracks a live effect toggle", async () => {
    const { container } = mountKiosk();
    await bringLinkUp();
    answerBootstrap(1, 1);
    await waitFor(() => expect(container.querySelector(".stage")).not.toBeNull());

    await waitFor(() => expect(lastSent("GET_PATCH")).toBeTruthy());
    reply({
      type: "PATCH",
      id: lastSent("GET_PATCH")!.id,
      bank: 1,
      slot: 1,
      patch: {
        name: "CLEAN",
        bindings: [
          { switch: "3", mode: "latched", label: "FLANG", actions: { toggle_on: kToggle("X"), toggle_off: kToggle("X") } },
        ],
      },
    });

    await waitFor(() => expect(sw(container, "3")).toHaveClass("stage__switch--bound"));
    expect(sw(container, "3")).not.toHaveClass("stage__switch--active");

    reply({ type: "CONTEXT", context: { kemper_block_X: "on" } });
    await waitFor(() => expect(sw(container, "3")).toHaveClass("stage__switch--active"));

    reply({ type: "CONTEXT", context: { kemper_block_X: "off" } });
    await waitFor(() => expect(sw(container, "3")).not.toHaveClass("stage__switch--active"));
  });

  it("renders the lower rig row from DEVICE_INFO + PATCH_LIST without GET_GLOBAL", async () => {
    vi.useFakeTimers();
    const view = mountKiosk();
    try {
      await vi.advanceTimersByTimeAsync(0);
      sock()._open();
      sock()._msg(JSON.stringify({ type: "HUB", link: "up" }));
      await vi.advanceTimersByTimeAsync(0);
      expect(sentCount("GET_GLOBAL")).toBe(0);

      reply({
        type: "DEVICE_INFO", id: lastSent("GET_DEVICE_INFO")!.id,
        fw: "0.6.4", device: "midi_captain_10", current: { bank: 1, slot: 1 },
        preset_navigation: { switches: { A: 1, B: 2, C: 3 } },
      });
      reply({
        type: "PATCH_LIST", id: lastSent("LIST_PATCHES")!.id,
        patches: [
          { bank: 1, slot: 1, name: "ACOUSTIC" },
          { bank: 1, slot: 2, name: "CLEAN" },
          { bank: 1, slot: 3, name: "CRUNCH" },
        ],
      });
      await vi.advanceTimersByTimeAsync(0);

      expect(sw(view.container, "A")).toHaveTextContent("ACOUSTIC");
      expect(sw(view.container, "B")).toHaveTextContent("CLEAN");
      expect(sw(view.container, "C")).toHaveTextContent("CRUNCH");
      await vi.advanceTimersByTimeAsync(30_000);
      expect(sentCount("GET_GLOBAL")).toBe(0);
    } finally {
      view.unmount();
      vi.useRealTimers();
    }
  });

  it("retries a lost DEVICE_INFO on a stable link without falling back to GLOBAL", async () => {
    vi.useFakeTimers();
    const view = mountKiosk();
    try {
      await vi.advanceTimersByTimeAsync(0);
      sock()._open();
      sock()._msg(JSON.stringify({ type: "HUB", link: "up" }));
      await vi.advanceTimersByTimeAsync(0);
      reply({
        type: "PATCH_LIST", id: lastSent("LIST_PATCHES")!.id,
        patches: [{ bank: 1, slot: 1, name: "ACOUSTIC" }],
      });
      await vi.advanceTimersByTimeAsync(0);

      expect(sentCount("GET_DEVICE_INFO")).toBe(1);
      await vi.advanceTimersByTimeAsync(1_499);
      expect(sentCount("GET_DEVICE_INFO")).toBe(1);
      await vi.advanceTimersByTimeAsync(1);
      expect(sentCount("GET_DEVICE_INFO")).toBe(2);
      expect(sentCount("GET_GLOBAL")).toBe(0);

      reply({
        type: "DEVICE_INFO", id: lastSent("GET_DEVICE_INFO")!.id,
        fw: "0.6.4", device: "midi_captain_10", current: { bank: 1, slot: 1 },
        preset_navigation: { switches: { A: 1 } },
      });
      await vi.advanceTimersByTimeAsync(0);
      expect(sw(view.container, "A")).toHaveTextContent("ACOUSTIC");

      await vi.advanceTimersByTimeAsync(15_000);
      expect(sentCount("GET_DEVICE_INFO")).toBe(2);
      expect(sentCount("GET_GLOBAL")).toBe(0);
    } finally {
      view.unmount();
      vi.useRealTimers();
    }
  });

  it("falls back to GET_GLOBAL only after a legacy DEVICE_INFO response", async () => {
    vi.useFakeTimers();
    const view = mountKiosk();
    try {
      await vi.advanceTimersByTimeAsync(0);
      sock()._open();
      sock()._msg(JSON.stringify({ type: "HUB", link: "up" }));
      await vi.advanceTimersByTimeAsync(0);
      expect(sentCount("GET_GLOBAL")).toBe(0);

      reply({
        type: "DEVICE_INFO", id: lastSent("GET_DEVICE_INFO")!.id,
        fw: "0.6.3", device: "midi_captain_10", current: { bank: 1, slot: 1 },
      });
      await vi.advanceTimersByTimeAsync(0);
      expect(sentCount("GET_GLOBAL")).toBe(1);
      const listIndex = sock().sent.findIndex((line) => line.includes('"type":"LIST_PATCHES"'));
      const globalIndex = sock().sent.findIndex((line) => line.includes('"type":"GET_GLOBAL"'));
      expect(listIndex).toBeGreaterThanOrEqual(0);
      expect(globalIndex).toBeGreaterThan(listIndex);

      reply({
        type: "PATCH_LIST", id: lastSent("LIST_PATCHES")!.id,
        patches: [{ bank: 1, slot: 1, name: "ACOUSTIC" }],
      });
      reply({
        type: "GLOBAL", id: lastSent("GET_GLOBAL")!.id,
        device: { preset_navigation: { switches: { A: 1 } } },
      });
      await vi.advanceTimersByTimeAsync(0);
      expect(sw(view.container, "A")).toHaveTextContent("ACOUSTIC");
    } finally {
      view.unmount();
      vi.useRealTimers();
    }
  });

  it("retries repeated PATCH_LIST losses once per window, then stops on success", async () => {
    vi.useFakeTimers();
    const view = mountKiosk();
    try {
      await vi.advanceTimersByTimeAsync(0);
      sock()._open();
      sock()._msg(JSON.stringify({ type: "HUB", link: "up" }));
      await vi.advanceTimersByTimeAsync(0);

      reply({
        type: "DEVICE_INFO", id: lastSent("GET_DEVICE_INFO")!.id,
        fw: "0.6.4", device: "midi_captain_10", current: { bank: 1, slot: 1 },
        preset_navigation: { switches: { A: 1, B: 2 } },
      });
      await vi.advanceTimersByTimeAsync(0);
      expect(sentCount("LIST_PATCHES")).toBe(1);

      // The 2 s connectivity poll must not add a duplicate while the 2.5 s
      // watchdog for the current request is still armed.
      await vi.advanceTimersByTimeAsync(2_499);
      expect(sentCount("LIST_PATCHES")).toBe(1);
      await vi.advanceTimersByTimeAsync(1);
      expect(sentCount("LIST_PATCHES")).toBe(2);

      await vi.advanceTimersByTimeAsync(2_499);
      expect(sentCount("LIST_PATCHES")).toBe(2);
      await vi.advanceTimersByTimeAsync(1);
      expect(sentCount("LIST_PATCHES")).toBe(3);

      reply({
        type: "PATCH_LIST", id: lastSent("LIST_PATCHES")!.id,
        patches: [
          { bank: 1, slot: 1, name: "ACOUSTIC" },
          { bank: 1, slot: 2, name: "CLEAN" },
        ],
      });
      await vi.advanceTimersByTimeAsync(0);
      expect(sw(view.container, "A")).toHaveTextContent("ACOUSTIC");
      expect(sw(view.container, "B")).toHaveTextContent("CLEAN");

      await vi.advanceTimersByTimeAsync(10_000);
      expect(sentCount("LIST_PATCHES")).toBe(3);
      expect(sentCount("GET_GLOBAL")).toBe(0);
    } finally {
      view.unmount();
      vi.useRealTimers();
    }
  });

  it("keeps retrying when a PATCH_LIST response is malformed", async () => {
    vi.useFakeTimers();
    const view = mountKiosk();
    try {
      await vi.advanceTimersByTimeAsync(0);
      sock()._open();
      sock()._msg(JSON.stringify({ type: "HUB", link: "up" }));
      await vi.advanceTimersByTimeAsync(0);

      reply({
        type: "DEVICE_INFO", id: lastSent("GET_DEVICE_INFO")!.id,
        fw: "0.6.4", device: "midi_captain_10", current: { bank: 1, slot: 1 },
        preset_navigation: { switches: { A: 1 } },
      });
      reply({
        type: "PATCH_LIST", id: lastSent("LIST_PATCHES")!.id,
        patches: { truncated: true },
      });
      await vi.advanceTimersByTimeAsync(0);

      await vi.advanceTimersByTimeAsync(2_499);
      expect(sentCount("LIST_PATCHES")).toBe(1);
      await vi.advanceTimersByTimeAsync(1);
      expect(sentCount("LIST_PATCHES")).toBe(2);

      reply({
        type: "PATCH_LIST", id: lastSent("LIST_PATCHES")!.id,
        patches: [{ bank: 1, slot: 1, name: "ACOUSTIC" }],
      });
      await vi.advanceTimersByTimeAsync(0);
      expect(sw(view.container, "A")).toHaveTextContent("ACOUSTIC");

      await vi.advanceTimersByTimeAsync(5_000);
      expect(sentCount("LIST_PATCHES")).toBe(2);
    } finally {
      view.unmount();
      vi.useRealTimers();
    }
  });

  it("keeps retrying when a GLOBAL response is malformed", async () => {
    vi.useFakeTimers();
    const view = mountKiosk();
    try {
      await vi.advanceTimersByTimeAsync(0);
      sock()._open();
      sock()._msg(JSON.stringify({ type: "HUB", link: "up" }));
      await vi.advanceTimersByTimeAsync(0);

      reply({
        type: "DEVICE_INFO", id: lastSent("GET_DEVICE_INFO")!.id,
        fw: "0.6.3", device: "midi_captain_10", current: { bank: 1, slot: 1 },
      });
      await vi.advanceTimersByTimeAsync(0);
      expect(sentCount("GET_GLOBAL")).toBe(1);
      reply({
        type: "PATCH_LIST", id: lastSent("LIST_PATCHES")!.id,
        patches: [{ bank: 1, slot: 1, name: "ACOUSTIC" }],
      });
      reply({
        type: "GLOBAL", id: lastSent("GET_GLOBAL")!.id,
        device: ["truncated"],
      });
      await vi.advanceTimersByTimeAsync(0);

      await vi.advanceTimersByTimeAsync(9_999);
      expect(sentCount("GET_GLOBAL")).toBe(1);
      await vi.advanceTimersByTimeAsync(1);
      expect(sentCount("GET_GLOBAL")).toBe(2);

      reply({
        type: "GLOBAL", id: lastSent("GET_GLOBAL")!.id,
        device: { preset_navigation: { switches: { A: 1 } } },
      });
      await vi.advanceTimersByTimeAsync(0);
      expect(sw(view.container, "A")).toHaveTextContent("ACOUSTIC");

      await vi.advanceTimersByTimeAsync(20_000);
      expect(sentCount("GET_GLOBAL")).toBe(2);
    } finally {
      view.unmount();
      vi.useRealTimers();
    }
  });

  it("does not restart bootstrap after an in-flight connectivity poll is unmounted", async () => {
    vi.useFakeTimers();
    const view = mountKiosk();
    let unmounted = false;
    try {
      await vi.advanceTimersByTimeAsync(0);
      sock()._open();
      sock()._msg(JSON.stringify({ type: "HUB", link: "up" }));
      await vi.advanceTimersByTimeAsync(0);
      reply({
        type: "DEVICE_INFO", id: lastSent("GET_DEVICE_INFO")!.id,
        fw: "0.6.4", device: "midi_captain_10", current: { bank: 1, slot: 1 },
        preset_navigation: {},
      });
      await vi.advanceTimersByTimeAsync(0);

      const listBefore = sentCount("LIST_PATCHES");
      const globalBefore = sentCount("GET_GLOBAL");
      const deviceInfoBefore = sentCount("GET_DEVICE_INFO");

      // Fire the 2 s poll synchronously. sync() reaches `await isConnected()`;
      // unmount before its already-resolved Promise continuation can run.
      vi.advanceTimersByTime(2_000);
      view.unmount();
      unmounted = true;
      await vi.advanceTimersByTimeAsync(30_000);

      expect(sentCount("LIST_PATCHES")).toBe(listBefore);
      expect(sentCount("GET_GLOBAL")).toBe(globalBefore);
      expect(sentCount("GET_DEVICE_INFO")).toBe(deviceInfoBefore);
    } finally {
      if (!unmounted) view.unmount();
      vi.useRealTimers();
    }
  });

  it("re-subscribes and recovers after the hub link drops and returns", async () => {
    const { container } = mountKiosk();
    await bringLinkUp();
    answerBootstrap(1, 1);
    await waitFor(() => expect(container.querySelector(".stage")).not.toBeNull());

    // Link drops (hub restart during a firmware push): last Stage view
    // stays on screen with a reconnecting badge, not a blank panel.
    sock().close();
    await waitFor(() => expect(container.textContent).toContain("reconnecting"));
    expect(container.querySelector(".stage")).not.toBeNull();

    // wsLink's backoff timer opens a new fake socket; drive it up.
    await waitFor(() => expect(FakeWebSocket.instances.length).toBeGreaterThan(1));
    sock()._open();
    sock()._msg(JSON.stringify({ type: "HUB", link: "up" }));
    await waitFor(() => expect(lastSent("GET_DEVICE_INFO")).toBeTruthy());
    answerBootstrap(1, 2);

    await waitFor(() => expect(lastSent("GET_CONTEXT")).toBeTruthy());
    reply({ type: "CONTEXT", context: { kemper_rig_name: "LEAD" } });
    await waitFor(() =>
      expect(container.querySelector(".stage__rig-name")).toHaveTextContent("LEAD"),
    );
  });

  it("reacts to a pedal-link drop on an open WebSocket and resyncs within 500 ms", async () => {
    const { container } = mountKiosk();
    await bringLinkUp();
    answerBootstrap(1, 1);
    await waitFor(() => expect(container.querySelector(".stage")).not.toBeNull());

    const deviceInfoBefore = sentCount("GET_DEVICE_INFO");
    const contextBefore = sentCount("GET_CONTEXT");
    const patchBefore = sentCount("GET_PATCH");

    // Only the upstream Captain link drops. The browser<->hub WebSocket
    // deliberately remains open, so its onclose handler cannot mask the bug.
    sock()._msg(JSON.stringify({ type: "HUB", link: "down" }));
    await waitFor(
      () => expect(container.textContent).toContain("reconnecting"),
      { timeout: 450 },
    );

    sock()._msg(JSON.stringify({ type: "HUB", link: "up" }));
    await waitFor(() => {
      expect(sentCount("GET_DEVICE_INFO")).toBeGreaterThan(deviceInfoBefore);
      expect(sentCount("GET_CONTEXT")).toBeGreaterThan(contextBefore);
      expect(sentCount("GET_PATCH")).toBeGreaterThan(patchBefore);
    }, { timeout: 450 });
  });

  it("recovers Captain bank/slot from CONTEXT within 500 ms when patch_switched is lost", async () => {
    const { container } = mountKiosk();
    await bringLinkUp();
    answerBootstrap(1, 1);
    await waitFor(() => expect(container.querySelector(".stage__rig-number")).toHaveTextContent("RIG 1"));

    // CONTEXT is a full authoritative snapshot and carries the Captain's
    // current location. No EVENT:patch_switched is sent in this scenario.
    reply({
      type: "CONTEXT",
      context: { bank: 2, slot: 4, patch_name: "HEAVY", kemper_rig_name: "HEAVY" },
    });

    await waitFor(() => {
      expect(container.querySelector(".stage__bank-readout .stage__bank-number")).toHaveTextContent("BANK 2");
      expect(container.querySelector(".stage__rig-readout .stage__rig-number")).toHaveTextContent("RIG 4");
      const requested = [...sock().sent].reverse().find((line) =>
        line.includes('"type":"GET_PATCH"') && line.includes('"bank":2') && line.includes('"slot":4'),
      );
      expect(requested).toBeTruthy();
    }, { timeout: 450 });
  });

  it("does not roll back a patch_switched event with an old in-flight CONTEXT", async () => {
    const { container } = mountKiosk();
    await bringLinkUp();
    answerBootstrap(1, 1);
    await waitFor(() => expect(container.querySelector(".stage__rig-number")).toHaveTextContent("RIG 1"));

    reply({ type: "EVENT", event: "patch_switched", bank: 1, slot: 2, source: "editor" });
    await waitFor(() => expect(container.querySelector(".stage__rig-number")).toHaveTextContent("RIG 2"));
    reply({
      type: "CONTEXT", id: "old-poll",
      context: { bank: 1, slot: 1, kemper_block_Reverb: "on" },
    });

    await new Promise((resolve) => setTimeout(resolve, 20));
    expect(container.querySelector(".stage__rig-number")).toHaveTextContent("RIG 2");
  });

  it("refreshes PATCH_LIST once per reconnect without requesting GLOBAL or MANIFEST", async () => {
    // The manifest streams for seconds on the firmware and starves the
    // data channel of CONTEXT pushes (the effect-block state the Stage
    // view lives on). A reconnect storm re-fetching it was why effect
    // toggles stopped updating on the Pi. A reconnect needs only the compact
    // DEVICE_INFO plus PATCH_LIST, which may have changed while disconnected.
    const { container } = mountKiosk();
    await bringLinkUp();
    answerBootstrap(1, 1);
    await waitFor(() => expect(container.querySelector(".stage")).not.toBeNull());

    const heavyOnFirstBoot = sock().sent.filter(
      (l) => l.includes("GET_GLOBAL") || l.includes("LIST_PATCHES"),
    ).length;
    expect(heavyOnFirstBoot).toBe(1); // only LIST_PATCHES on first connect

    for (let i = 0; i < 3; i++) {
      sock().close();
      await waitFor(() => expect(FakeWebSocket.instances.length).toBeGreaterThan(i + 1));
      sock()._open();
      sock()._msg(JSON.stringify({ type: "HUB", link: "up" }));
      await waitFor(() => expect(lastSent("GET_DEVICE_INFO")).toBeTruthy());
      answerBootstrap(1, 1);
    }

    const heavy = FakeWebSocket.instances
      .flatMap((s) => s.sent)
      .filter(
        (l) =>
          l.includes("GET_MANIFEST") ||
          l.includes("GET_GLOBAL") ||
          l.includes("LIST_PATCHES"),
      );
    expect(heavy).toHaveLength(4); // bootstrap plus one list for each reconnect
    expect(heavy.every((line) => line.includes("LIST_PATCHES"))).toBe(true);
  });

  it("replaces the old inventory after reconnect and ignores a delayed old response", async () => {
    vi.useFakeTimers();
    const view = mountKiosk();
    try {
      await vi.advanceTimersByTimeAsync(0);
      sock()._open();
      reply({ type: "HUB", link: "up" });
      await vi.advanceTimersByTimeAsync(0);
      const info = () => ({
        type: "DEVICE_INFO", id: lastSent("GET_DEVICE_INFO")!.id,
        fw: "0.6.5-native", device: "Captain", profile: "live", current: { bank: 1, slot: 1 },
        preset_navigation: { switches: { A: 1 } },
      });
      reply(info());
      const oldId = lastSent("LIST_PATCHES")!.id;
      reply({ type: "PATCH_LIST", id: oldId, profile: "live", patches: [{ bank: 1, slot: 1, name: "OLD" }] });
      await vi.advanceTimersByTimeAsync(0);
      expect(sw(view.container, "A")).toHaveTextContent("OLD");

      reply({ type: "HUB", link: "down" });
      reply({ type: "HUB", link: "up" });
      await vi.advanceTimersByTimeAsync(0);
      expect(sentCount("LIST_PATCHES")).toBe(2);
      expect(sw(view.container, "A")).not.toHaveTextContent("OLD");
      reply(info());
      reply({ type: "PATCH_LIST", id: lastSent("LIST_PATCHES")!.id, profile: "live",
        patches: [{ bank: 1, slot: 1, name: "NEW" }] });
      await vi.advanceTimersByTimeAsync(0);
      reply({ type: "PATCH_LIST", id: oldId, profile: "live", patches: [{ bank: 1, slot: 1, name: "OLD" }] });
      await vi.advanceTimersByTimeAsync(0);
      expect(sw(view.container, "A")).toHaveTextContent("NEW");
      await vi.advanceTimersByTimeAsync(10_000);
      expect(sentCount("LIST_PATCHES")).toBe(2);
      expect(sentCount("GET_GLOBAL")).toBe(0);
    } finally { view.unmount(); vi.useRealTimers(); }
  });

  it("keeps the DEVICE_INFO profile and rejects a list overtaken by a profile switch", async () => {
    vi.useFakeTimers();
    const view = mountKiosk();
    try {
      await vi.advanceTimersByTimeAsync(0);
      sock()._open();
      reply({ type: "HUB", link: "up" });
      await vi.advanceTimersByTimeAsync(0);
      const info = (profile: string) => ({
        type: "DEVICE_INFO", id: lastSent("GET_DEVICE_INFO")!.id,
        fw: "0.6.5-native", device: "Captain", profile, current: { bank: 1, slot: 1 },
        preset_navigation: { switches: { A: 1 } },
      });
      reply(info("live"));
      reply({ type: "PATCH_LIST", id: lastSent("LIST_PATCHES")!.id, profile: "live",
        patches: [{ bank: 1, slot: 1, name: "LIVE" }] });
      await vi.advanceTimersByTimeAsync(0);
      expect(sw(view.container, "A")).toHaveTextContent("LIVE");

      reply({ type: "EVENT", event: "dirty_state_changed", patches: [{ bank: 1, slot: 1 }] });
      await vi.advanceTimersByTimeAsync(0);
      const outdated = lastSent("LIST_PATCHES")!.id;
      reply({ type: "EVENT", event: "global_changed" });
      await vi.advanceTimersByTimeAsync(0);
      reply(info("spare"));
      await vi.advanceTimersByTimeAsync(0);
      expect(sw(view.container, "A")).not.toHaveTextContent("LIVE");
      reply({ type: "PATCH_LIST", id: outdated, profile: "live",
        patches: [{ bank: 1, slot: 1, name: "STALE LIVE" }] });
      await vi.advanceTimersByTimeAsync(0);
      expect(sentCount("LIST_PATCHES")).toBe(3);
      expect(sw(view.container, "A")).not.toHaveTextContent("STALE LIVE");
      reply({ type: "PATCH_LIST", id: lastSent("LIST_PATCHES")!.id, profile: "spare",
        patches: [{ bank: 1, slot: 1, name: "SPARE" }] });
      await vi.advanceTimersByTimeAsync(0);
      expect(sw(view.container, "A")).toHaveTextContent("SPARE");
      await vi.advanceTimersByTimeAsync(10_000);
      expect(sentCount("LIST_PATCHES")).toBe(3);
      expect(sentCount("GET_GLOBAL")).toBe(0);
    } finally { view.unmount(); vi.useRealTimers(); }
  });

  it("coalesces patch mutation events and refreshes the created and removed slots", async () => {
    vi.useFakeTimers();
    const view = mountKiosk();
    try {
      await vi.advanceTimersByTimeAsync(0);
      sock()._open();
      reply({ type: "HUB", link: "up" });
      await vi.advanceTimersByTimeAsync(0);
      reply({ type: "DEVICE_INFO", id: lastSent("GET_DEVICE_INFO")!.id,
        fw: "0.6.5-native", device: "Captain", current: { bank: 1, slot: 1 },
        preset_navigation: { switches: { A: 1, B: 2 } } });
      reply({ type: "PATCH_LIST", id: lastSent("LIST_PATCHES")!.id,
        patches: [{ bank: 1, slot: 1, name: "ORIGINAL" }] });
      await vi.advanceTimersByTimeAsync(0);

      reply({ type: "EVENT", event: "dirty_state_changed", patches: [{ bank: 1, slot: 2 }] });
      await vi.advanceTimersByTimeAsync(0);
      const older = lastSent("LIST_PATCHES")!.id;
      reply({ type: "EVENT", event: "saved", patches: [{ bank: 1, slot: 2 }] });
      reply({ type: "EVENT", event: "dirty_state_changed", patches: [] });
      await vi.advanceTimersByTimeAsync(0);
      expect(sentCount("LIST_PATCHES")).toBe(2);
      reply({ type: "PATCH_LIST", id: older, patches: [{ bank: 1, slot: 1, name: "STALE" }] });
      await vi.advanceTimersByTimeAsync(0);
      expect(sw(view.container, "A")).toHaveTextContent("ORIGINAL");
      expect(sentCount("LIST_PATCHES")).toBe(3);
      reply({ type: "PATCH_LIST", id: lastSent("LIST_PATCHES")!.id,
        patches: [{ bank: 1, slot: 1, name: "ORIGINAL" }, { bank: 1, slot: 2, name: "CREATED" }] });
      await vi.advanceTimersByTimeAsync(0);
      expect(sw(view.container, "B")).toHaveTextContent("CREATED");

      // DISCARD may remove a newly created, still-unsaved patch. Both firmware
      // families announce the resulting change with this existing event.
      reply({ type: "EVENT", event: "discarded", patches: [{ bank: 1, slot: 2 }] });
      await vi.advanceTimersByTimeAsync(0);
      reply({ type: "PATCH_LIST", id: lastSent("LIST_PATCHES")!.id,
        patches: [{ bank: 1, slot: 1, name: "ORIGINAL" }] });
      await vi.advanceTimersByTimeAsync(0);
      expect(sw(view.container, "B")).not.toHaveTextContent("CREATED");
      expect(sw(view.container, "B")).not.toHaveClass("stage__switch--bound");
      await vi.advanceTimersByTimeAsync(15_000);
      expect(sentCount("LIST_PATCHES")).toBe(4);
      expect(sentCount("GET_DEVICE_INFO")).toBe(1);
      expect(sentCount("GET_GLOBAL")).toBe(0);
    } finally { view.unmount(); vi.useRealTimers(); }
  });

  it("does not refresh the inventory for a physical switch to an existing patch", async () => {
    vi.useFakeTimers();
    const view = mountKiosk();
    try {
      await vi.advanceTimersByTimeAsync(0);
      sock()._open();
      reply({ type: "HUB", link: "up" });
      await vi.advanceTimersByTimeAsync(0);
      reply({ type: "DEVICE_INFO", id: lastSent("GET_DEVICE_INFO")!.id,
        fw: "0.6.5-native", device: "Captain", current: { bank: 1, slot: 1 },
        preset_navigation: { switches: { A: 1, B: 2 } } });
      reply({ type: "PATCH_LIST", id: lastSent("LIST_PATCHES")!.id,
        patches: [{ bank: 1, slot: 1, name: "ONE" }, { bank: 1, slot: 2, name: "TWO" }] });
      await vi.advanceTimersByTimeAsync(0);
      reply({ type: "EVENT", event: "patch_switched", bank: 1, slot: 2, source: "binding" });
      await vi.advanceTimersByTimeAsync(0);
      expect(sentCount("LIST_PATCHES")).toBe(1);
      await vi.advanceTimersByTimeAsync(10_000);
      expect(sentCount("LIST_PATCHES")).toBe(1);
    } finally { view.unmount(); vi.useRealTimers(); }
  });

  it("reconciles a profile mismatch with DEVICE_INFO without accepting the wrong inventory", async () => {
    vi.useFakeTimers();
    const view = mountKiosk();
    try {
      await vi.advanceTimersByTimeAsync(0);
      sock()._open();
      reply({ type: "HUB", link: "up" });
      await vi.advanceTimersByTimeAsync(0);
      const info = (profile: string) => ({
        type: "DEVICE_INFO", id: lastSent("GET_DEVICE_INFO")!.id,
        fw: "0.6.5-native", device: "Captain", profile, current: { bank: 1, slot: 1 },
        preset_navigation: { switches: { A: 1 } },
      });
      reply(info("live"));
      reply({ type: "PATCH_LIST", id: lastSent("LIST_PATCHES")!.id, profile: "spare",
        patches: [{ bank: 1, slot: 1, name: "WRONG PROFILE" }] });
      await vi.advanceTimersByTimeAsync(0);
      expect(sw(view.container, "A")).not.toHaveTextContent("WRONG PROFILE");
      expect(sentCount("GET_DEVICE_INFO")).toBe(2);
      reply(info("spare"));
      await vi.advanceTimersByTimeAsync(0);
      expect(sentCount("LIST_PATCHES")).toBe(2);
      reply({ type: "PATCH_LIST", id: lastSent("LIST_PATCHES")!.id, profile: "spare",
        patches: [{ bank: 1, slot: 1, name: "FRESH SPARE" }] });
      await vi.advanceTimersByTimeAsync(0);
      expect(sw(view.container, "A")).toHaveTextContent("FRESH SPARE");
      expect(sentCount("GET_GLOBAL")).toBe(0);
    } finally { view.unmount(); vi.useRealTimers(); }
  });

  it("confirms BANK+ over the kiosk transport while its inventory refresh is in flight", async () => {
    vi.useFakeTimers();
    const view = mountKiosk();
    try {
      await vi.advanceTimersByTimeAsync(0);
      sock()._open();
      reply({ type: "HUB", link: "up" });
      await vi.advanceTimersByTimeAsync(0);
      const info = (id: unknown, bank: number, slot: number) => ({
        type: "DEVICE_INFO", id, fw: "0.6.5-native", device: "midi_captain_10",
        profile: "kemper_player_buk4", current: { bank, slot },
        preset_navigation: { switches: { A: 1, B: 2, C: 3, D: 4, E: 5 } },
      });
      const initialPatches = [
        { bank: 1, slot: 1, name: "FIRST BANK" },
        { bank: 2, slot: 4, name: "BEFORE EDIT" },
      ];
      const freshPatches = [initialPatches[0], { bank: 2, slot: 4, name: "NEW BANK RIG" }];
      reply(info(lastSent("GET_DEVICE_INFO")!.id, 1, 1));
      reply({ type: "PATCH_LIST", id: lastSent("LIST_PATCHES")!.id,
        profile: "kemper_player_buk4", patches: initialPatches });
      await vi.advanceTimersByTimeAsync(0);
      expect(sw(view.container, "A")).toHaveTextContent("FIRST BANK");

      // Another editor has changed a patch; the kiosk's own list remains in
      // flight while Stage asks for a separate, fresh navigation snapshot.
      reply({ type: "EVENT", event: "dirty_state_changed", patches: [{ bank: 2, slot: 4 }] });
      await vi.advanceTimersByTimeAsync(0);
      const kioskListId = lastSent("LIST_PATCHES")!.id;
      const bankPlus = view.getByRole("button", { name: "Next bank", exact: true });
      expect(bankPlus).toBeEnabled();
      await fireEvent.click(bankPlus);
      await vi.advanceTimersByTimeAsync(0);
      expect(bankPlus).toBeDisabled();
      expect(sentCount("GET_DEVICE_INFO")).toBe(2);
      reply(info(lastSent("GET_DEVICE_INFO")!.id, 1, 1));
      await vi.advanceTimersByTimeAsync(0);
      const stageListId = lastSent("LIST_PATCHES")!.id;
      expect(stageListId).not.toBe(kioskListId);
      reply({ type: "PATCH_LIST", id: stageListId,
        profile: "kemper_player_buk4", patches: freshPatches });
      await vi.advanceTimersByTimeAsync(0);
      const switchRequest = lastSent("SWITCH_PATCH")!;
      expect(switchRequest).toMatchObject({ type: "SWITCH_PATCH", bank: 2, slot: 4 });
      expect(sentCount("SWITCH_PATCH")).toBe(1);
      expect(view.container.querySelector(".stage__bank-number")).toHaveTextContent("BANK 1");
      expect(view.container.querySelector(".stage__rig-number")).toHaveTextContent("RIG 1");

      reply({ type: "ACK", id: switchRequest.id });
      await vi.advanceTimersByTimeAsync(0);
      expect(sentCount("GET_DEVICE_INFO")).toBe(3);
      // The ACK alone does not paint the target; confirmation also repairs
      // the parent shell when the unsolicited patch_switched event was lost.
      expect(view.container.querySelector(".stage__bank-number")).toHaveTextContent("BANK 1");
      expect(bankPlus).toBeDisabled();
      reply(info(lastSent("GET_DEVICE_INFO")!.id, 2, 4));
      await vi.advanceTimersByTimeAsync(0);
      expect(view.container.querySelector(".stage__bank-number")).toHaveTextContent("BANK 2");
      expect(view.container.querySelector(".stage__rig-number")).toHaveTextContent("RIG 4");
      expect(bankPlus).toBeEnabled();

      reply({ type: "PATCH_LIST", id: kioskListId,
        profile: "kemper_player_buk4", patches: freshPatches });
      await vi.advanceTimersByTimeAsync(0);
      expect(sw(view.container, "D")).toHaveTextContent("NEW BANK RIG");
      expect(sw(view.container, "D")).toHaveClass("stage__switch--active");
      expect(sw(view.container, "A")).not.toHaveTextContent("FIRST BANK");
      expect(sentCount("LIST_PATCHES")).toBe(3);
      expect(sentCount("GET_GLOBAL")).toBe(0);
      expect(view.queryByText("Bank change not confirmed.")).not.toBeInTheDocument();
    } finally { view.unmount(); vi.useRealTimers(); }
  });

  it("retries DEVICE_INFO and PATCH_LIST after reconnect when responses were lost", async () => {
    mountKiosk();
    await bringLinkUp();
    // Deliberately do not answer either fast-bootstrap request.
    const deviceInfoBefore = sentCount("GET_DEVICE_INFO");
    const patchesBefore = sentCount("LIST_PATCHES");

    sock()._msg(JSON.stringify({ type: "HUB", link: "down" }));
    sock()._msg(JSON.stringify({ type: "HUB", link: "up" }));

    await waitFor(() => {
      expect(sentCount("GET_DEVICE_INFO")).toBeGreaterThan(deviceInfoBefore);
      expect(sentCount("LIST_PATCHES")).toBeGreaterThan(patchesBefore);
    }, { timeout: 2000 });
    expect(sentCount("GET_GLOBAL")).toBe(0);
  });

  it("never requests the heavy manifest at all", async () => {
    // On the RP2040 the manifest streams as a background generator for
    // ~7 s and a GET_PATCH mid-stream (every rig change) can wedge it,
    // permanently queueing every CONTEXT push behind it. The Stage kiosk
    // does without it (label fallbacks only).
    vi.useFakeTimers();
    try {
      mountKiosk();
      sock()._open();
      sock()._msg(JSON.stringify({ type: "HUB", link: "up" }));
      await vi.advanceTimersByTimeAsync(30000);
      expect(sock().sent.some((l) => l.includes("GET_MANIFEST"))).toBe(false);
    } finally {
      vi.useRealTimers();
    }
  });
});
