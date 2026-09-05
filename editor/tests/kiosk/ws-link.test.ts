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

  it("honours a ?ws= override", async () => {
    (location as unknown as { search: string }).search = "?ws=ws://10.0.0.9:9999/";
    const link = await freshLink();
    link.start();
    expect(last().url).toBe("ws://10.0.0.9:9999/");
  });
});
