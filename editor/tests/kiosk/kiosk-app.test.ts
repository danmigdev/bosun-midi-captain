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

function answerBootstrap(bank = 1, slot = 1) {
  reply({ type: "DEVICE_INFO", id: lastSent("GET_DEVICE_INFO")!.id, fw: "0.6.1", device: "midi_captain_10", current: { bank, slot } });
  reply({ type: "MANIFEST", id: lastSent("GET_MANIFEST")!.id, core_messages: {}, plugins: {} });
  reply({ type: "GLOBAL", id: lastSent("GET_GLOBAL")!.id, device: {} });
  reply({ type: "PATCH_LIST", id: lastSent("LIST_PATCHES")!.id, patches: [] });
}

const kToggle = (slot: string) => ({
  messages: [{ type: "kemper_effect_toggle", plugin: "kemper", slot }],
});
const sw = (container: HTMLElement, id: string) =>
  [...container.querySelectorAll(".stage__switch")].find(
    (el) => el.querySelector(".stage__switch-id")?.textContent === id,
  )!;

describe("kiosk integration", () => {
  it("shows 'Waiting for the pedal' until the hub link is up", async () => {
    const { container } = mountKiosk();
    expect(container.textContent).toContain("Waiting for the pedal");
    await bringLinkUp();
    await waitFor(() => expect(container.querySelector(".stage")).not.toBeNull());
    expect(container.textContent).not.toContain("Waiting for the pedal");
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
});
