// Integration test for the Pi kiosk: the real transport shims
// (src/kiosk/tauri-core.ts + tauri-event.ts) over a fake WebSocket,
// driving the real protocol.ts + KioskApp + StageView. Proves that a
// firmware message from the hub reaches the rendered grid, and does so
// promptly - the exact path that was silently not subscribing on the
// device.

import { describe, it, expect, vi, beforeEach, afterEach } from "vitest";
import { render, waitFor } from "@testing-library/svelte";
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
      expect(view.container.querySelector(".stage__bank-number")).toHaveTextContent("· Bank 1 Tour");
      expect(view.container.querySelector(".stage__rig-number")).toHaveTextContent("· Preset 3!");
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
    await waitFor(() => expect(container.querySelector(".stage__meta")).toHaveTextContent("RIG 1"));

    // CONTEXT is a full authoritative snapshot and carries the Captain's
    // current location. No EVENT:patch_switched is sent in this scenario.
    reply({
      type: "CONTEXT",
      context: { bank: 2, slot: 4, patch_name: "HEAVY", kemper_rig_name: "HEAVY" },
    });

    await waitFor(() => {
      expect(container.querySelector(".stage__meta")).toHaveTextContent("BANK 2 · RIG 4");
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
    await waitFor(() => expect(container.querySelector(".stage__meta")).toHaveTextContent("RIG 1"));

    reply({ type: "EVENT", event: "patch_switched", bank: 1, slot: 2, source: "editor" });
    await waitFor(() => expect(container.querySelector(".stage__meta")).toHaveTextContent("RIG 2"));
    reply({
      type: "CONTEXT", id: "old-poll",
      context: { bank: 1, slot: 1, kemper_block_Reverb: "on" },
    });

    await new Promise((resolve) => setTimeout(resolve, 20));
    expect(container.querySelector(".stage__meta")).toHaveTextContent("RIG 2");
  });

  it("never requests GLOBAL or re-requests PATCH_LIST after fast bootstrap", async () => {
    // The manifest streams for seconds on the firmware and starves the
    // data channel of CONTEXT pushes (the effect-block state the Stage
    // view lives on). A reconnect storm re-fetching it was why effect
    // toggles stopped updating on the Pi. A reconnect must cost only a
    // tiny GET_DEVICE_INFO.
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
    expect(heavy).toHaveLength(1); // still just the first-boot LIST_PATCHES
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
