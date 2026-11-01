// Unit tests for the Pi-kiosk WebSocket transport (src/kiosk/ws-link.ts).
// A fake WebSocket lets us drive open / message / close deterministically
// and assert the invoke()/listen() surface the rest of the app sees.

import { describe, it, expect, vi, beforeEach, afterEach } from "vitest";

class FakeWebSocket {
  static instances: FakeWebSocket[] = [];
  static OPEN = 1;
  url: string;
  readyState = 0;
  sent: string[] = [];
  onopen: (() => void) | null = null;
  onclose: (() => void) | null = null;
  onmessage: ((e: { data: string }) => void) | null = null;
  onerror: (() => void) | null = null;

  constructor(url: string) {
    this.url = url;
    FakeWebSocket.instances.push(this);
  }
  send(data: string) {
    this.sent.push(data);
  }
  close() {
    this.readyState = 3;
    this.onclose?.();
  }
  // helpers
  _open() {
    this.readyState = 1;
    this.onopen?.();
  }
  _msg(data: string) {
    this.onmessage?.({ data });
  }
}

async function freshLink() {
  vi.resetModules();
  const mod = await import("../../src/kiosk/ws-link");
  return mod.wsLink;
}

const last = () => FakeWebSocket.instances[FakeWebSocket.instances.length - 1];

beforeEach(() => {
  FakeWebSocket.instances = [];
  vi.stubGlobal("WebSocket", FakeWebSocket as unknown as typeof WebSocket);
  vi.stubGlobal("location", {
    protocol: "http:",
    hostname: "pi.local",
    search: "",
  } as unknown as Location);
  vi.useFakeTimers();
});

afterEach(() => {
  vi.useRealTimers();
  vi.unstubAllGlobals();
});

describe("ws-link", () => {
  it("connects to ws://<host>:8081 and reports connected only after a HUB up frame", async () => {
    const link = await freshLink();
    const reconnected = vi.fn();
    link.on("firmware-reconnected", reconnected);
    link.start();
    expect(last().url).toBe("ws://pi.local:8081/");

    last()._open();
    expect(link.isConnected()).toBe(false); // socket open, hub link unknown
    expect(reconnected).not.toHaveBeenCalled();

    last()._msg(JSON.stringify({ type: "HUB", link: "up" }));
    expect(link.isConnected()).toBe(true);
    expect(reconnected).toHaveBeenCalledTimes(1);

    last()._msg(JSON.stringify({ type: "HUB", link: "down" }));
    expect(link.isConnected()).toBe(false);
  });

  it("delivers protocol lines to drain() and rings the doorbell; HUB frames are not in the inbox", async () => {
    const link = await freshLink();
    const doorbell = vi.fn();
    link.on("firmware-data-ready", doorbell);
    link.start();
    last()._open();

    last()._msg(JSON.stringify({ type: "HUB", link: "up" }));
    last()._msg('{"type":"CONTEXT","context":{"kemper_bpm":120}}');
    last()._msg('{"type":"EVENT","event":"binding_fired","switch":"3"}');

    expect(doorbell).toHaveBeenCalled();
    const lines = link.drain();
    expect(lines).toEqual([
      '{"type":"CONTEXT","context":{"kemper_bpm":120}}',
      '{"type":"EVENT","event":"binding_fired","switch":"3"}',
    ]);
    expect(link.drain()).toEqual([]); // drained
  });

  it("splits a multi-line WebSocket message into separate protocol lines", async () => {
    const link = await freshLink();
    link.start();
    last()._open();
    last()._msg('{"type":"ACK","id":"a"}\n{"type":"CONTEXT","context":{}}');
    expect(link.drain()).toEqual([
      '{"type":"ACK","id":"a"}',
      '{"type":"CONTEXT","context":{}}',
    ]);
  });

  it("send() forwards to the socket only while open", async () => {
    const link = await freshLink();
    link.start();
    link.send('{"type":"PING"}'); // socket not open yet
    last()._open();
    link.send('{"type":"GET_CONTEXT","id":"1"}\n');
    expect(last().sent).toEqual(['{"type":"GET_CONTEXT","id":"1"}']); // trailing \n trimmed
  });

  it("reconnects with backoff after a close, and fires the reconnect events", async () => {
    const link = await freshLink();
    const reconnecting = vi.fn();
    const reconnected = vi.fn();
    link.on("firmware-reconnecting", reconnecting);
    link.on("firmware-reconnected", reconnected);
    link.start();
    last()._open();
    last()._msg(JSON.stringify({ type: "HUB", link: "up" }));
    expect(FakeWebSocket.instances).toHaveLength(1);

    last().close();
    expect(link.isConnected()).toBe(false);
    expect(reconnecting).toHaveBeenCalled();

    vi.advanceTimersByTime(600); // first backoff step
    expect(FakeWebSocket.instances).toHaveLength(2);
    last()._open();
    expect(reconnected).toHaveBeenCalledTimes(1);
    last()._msg(JSON.stringify({ type: "HUB", link: "up" }));
    expect(reconnected).toHaveBeenCalledTimes(2);
  });

  it("ignores callbacks and reconnect timers left by an obsolete socket generation", async () => {
    const link = await freshLink();
    link.start();
    const obsolete = last();
    const lateClose = obsolete.onclose!;

    link.__cycleSocketForTest();
    link.start();
    const current = last();
    current._open();
    current._msg(JSON.stringify({ type: "HUB", link: "up" }));
    expect(link.isConnected()).toBe(true);

    // Model an OS/browser close callback arriving after a replacement socket
    // is already live. It must neither mark the new link down nor spawn a
    // third socket later.
    lateClose();
    expect(link.isConnected()).toBe(true);
    await vi.advanceTimersByTimeAsync(5_000);
    expect(FakeWebSocket.instances).toHaveLength(2);
  });

  it("reports the hub's update steps and stays disconnected until the frame without one", async () => {
    const link = await freshLink();
    const updates = vi.fn();
    const disconnected = vi.fn();
    const reconnected = vi.fn();
    link.on("hub-update", updates);
    link.on("firmware-disconnected", disconnected);
    link.on("firmware-reconnected", reconnected);
    link.start();
    last()._open();
    last()._msg(JSON.stringify({ type: "HUB", link: "up" }));
    expect(reconnected).toHaveBeenCalledTimes(1);
    expect(updates).not.toHaveBeenCalled();

    // The job starts while the hub still owns a live Captain link.
    const validating = { phase: "validating", detail: "" };
    last()._msg(JSON.stringify({ type: "HUB", link: "up", update: validating }));
    expect(updates).toHaveBeenLastCalledWith({ payload: validating });
    expect(link.update).toEqual(validating);
    expect(link.isConnected()).toBe(false);
    expect(disconnected).toHaveBeenCalledTimes(1);

    // Same step again: no event. A new detail: one event.
    last()._msg(JSON.stringify({ type: "HUB", link: "down", update: validating }));
    expect(updates).toHaveBeenCalledTimes(1);
    const flashing = { phase: "flashing", detail: "flashing firmware" };
    last()._msg(JSON.stringify({ type: "HUB", link: "down", update: flashing }));
    expect(updates).toHaveBeenCalledTimes(2);
    expect(updates).toHaveBeenLastCalledWith({ payload: flashing });

    // The new firmware's link comes back before the hub ends the job.
    last()._msg(JSON.stringify({ type: "HUB", link: "up", update: { phase: "verifying", detail: "complete" } }));
    expect(link.isConnected()).toBe(false);
    expect(reconnected).toHaveBeenCalledTimes(1);

    last()._msg(JSON.stringify({ type: "HUB", link: "up" }));
    expect(updates).toHaveBeenLastCalledWith({ payload: null });
    expect(link.update).toBeNull();
    expect(link.isConnected()).toBe(true);
    expect(reconnected).toHaveBeenCalledTimes(2);
    expect(link.drain()).toEqual([]); // HUB frames never reach the firmware bus
  });

  it("ignores a malformed update field and keeps a known update across a socket drop", async () => {
    const link = await freshLink();
    const updates = vi.fn();
    link.on("hub-update", updates);
    link.start();
    last()._open();
    for (const update of [null, "flashing", ["flashing"], { detail: "x" }, { phase: "" }]) {
      last()._msg(JSON.stringify({ type: "HUB", link: "up", update }));
    }
    expect(updates).not.toHaveBeenCalled();
    expect(link.isConnected()).toBe(true);

    last()._msg(JSON.stringify({ type: "HUB", link: "down", update: { phase: "rebooting", detail: 7 } }));
    expect(link.update).toEqual({ phase: "rebooting", detail: "" });
    last().close();
    expect(link.update).toEqual({ phase: "rebooting", detail: "" });
    vi.advanceTimersByTime(600);
    last()._open();
    // The hub's first frame on the new socket is authoritative.
    last()._msg(JSON.stringify({ type: "HUB", link: "up" }));
    expect(link.update).toBeNull();
    expect(updates).toHaveBeenLastCalledWith({ payload: null });
    expect(link.isConnected()).toBe(true);
  });

  it("reports the Captain's screensaver from HUB up frames without counting a reconnect", async () => {
    const link = await freshLink();
    const screensavers = vi.fn();
    const reconnected = vi.fn();
    link.on("hub-screensaver", ({ payload }) => screensavers(payload));
    link.on("firmware-reconnected", reconnected);
    link.start();
    last()._open();
    last()._msg(JSON.stringify({ type: "HUB", link: "up" }));
    expect(screensavers).not.toHaveBeenCalled();

    last()._msg(JSON.stringify({ type: "HUB", link: "up", screensaver: { effect: "bounce" } }));
    expect(link.screensaver).toEqual({ effect: "bounce" });
    last()._msg(JSON.stringify({ type: "HUB", link: "up", screensaver: { effect: "bounce" } }));
    last()._msg(JSON.stringify({ type: "HUB", link: "up", screensaver: { effect: "drift" } }));
    // An effect this Stage does not know (or none) still covers it, calmly.
    last()._msg(JSON.stringify({ type: "HUB", link: "up", screensaver: { effect: "starfield" } }));
    last()._msg(JSON.stringify({ type: "HUB", link: "up", screensaver: {} }));
    last()._msg(JSON.stringify({ type: "HUB", link: "up" }));
    expect(screensavers.mock.calls).toEqual([
      [{ effect: "bounce" }], [{ effect: "drift" }], [{ effect: "breathe" }], [null],
    ]);
    expect(link.screensaver).toBeNull();
    for (const screensaver of [null, "bounce", ["bounce"]]) {
      last()._msg(JSON.stringify({ type: "HUB", link: "up", screensaver }));
    }
    expect(screensavers).toHaveBeenCalledTimes(4);

    expect(link.isConnected()).toBe(true);
    expect(reconnected).toHaveBeenCalledTimes(1);
    expect(link.drain()).toEqual([]); // HUB frames never reach the firmware bus
  });

  it("turns the screensaver off when the Captain link or the hub socket is lost", async () => {
    const link = await freshLink();
    const screensavers = vi.fn();
    link.on("hub-screensaver", ({ payload }) => screensavers(payload));
    link.start();
    last()._open();
    last()._msg(JSON.stringify({ type: "HUB", link: "up", screensaver: { effect: "breathe" } }));
    expect(link.screensaver).toEqual({ effect: "breathe" });

    // Only an up frame can carry it: a lost Captain cannot show one.
    last()._msg(JSON.stringify({ type: "HUB", link: "down", screensaver: { effect: "breathe" } }));
    expect(link.screensaver).toBeNull();
    expect(link.isConnected()).toBe(false);

    last()._msg(JSON.stringify({ type: "HUB", link: "up", screensaver: { effect: "drift" } }));
    expect(link.screensaver).toEqual({ effect: "drift" });
    last().close();
    expect(link.screensaver).toBeNull();
    expect(screensavers.mock.calls).toEqual([
      [{ effect: "breathe" }], [null], [{ effect: "drift" }], [null],
    ]);

    // A Stage reopened while the screensaver shows learns it from the first frame.
    vi.advanceTimersByTime(600);
    last()._open();
    last()._msg(JSON.stringify({ type: "HUB", link: "up", screensaver: { effect: "drift" } }));
    expect(link.screensaver).toEqual({ effect: "drift" });
    expect(link.isConnected()).toBe(true);
  });

  it("honours a ?ws= override", async () => {
    (location as unknown as { search: string }).search = "?ws=ws://10.0.0.9:9999/";
    const link = await freshLink();
    link.start();
    expect(last().url).toBe("ws://10.0.0.9:9999/");
  });
});
