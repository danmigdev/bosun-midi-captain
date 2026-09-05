import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

const { invoke, events, inbox } = vi.hoisted(() => ({
  invoke: vi.fn(),
  events: new Map<string, () => void>(),
  inbox: [] as string[],
}));
vi.mock("@tauri-apps/api/core", () => ({ invoke }));
vi.mock("@tauri-apps/api/event", () => ({
  listen: vi.fn(async (name: string, handler: () => void) => {
    events.set(name, handler);
    return () => events.delete(name);
  }),
}));

import { autoConnect, connect, disconnect, discoverHubs, reconnectLast, tcpConnect, waitForReboot } from "../src/lib/protocol";

beforeEach(async () => {
  invoke.mockReset();
  inbox.length = 0;
  invoke.mockImplementation(async (name: string, args?: Record<string, unknown>) => {
    if (name === "auto_connect") return "COM7";
    if (name === "drain_inbox") return inbox.splice(0);
    if (name === "send_command") {
      const message = JSON.parse(String(args?.line));
      inbox.push(JSON.stringify({ type: "ACK", id: message.id }));
      queueMicrotask(() => events.get("firmware-data-ready")?.());
    }
  });
  await autoConnect(); // Begin each test with the USB default.
  invoke.mockClear();
});
afterEach(() => vi.useRealTimers());

describe("network transport and recovery", () => {
  it("discovers hubs independently of the active Captain transport", async () => {
    const hubs = [{ host: "192.168.1.91", name: "bosun-hub", tcp_port: 9876 }];
    invoke.mockResolvedValueOnce(hubs);
    expect(await discoverHubs("192.168.1.91")).toEqual(hubs);
    expect(invoke).toHaveBeenCalledExactlyOnceWith("discover_hubs", { hint: "192.168.1.91" });
  });

  it("returns to the same Raspberry after disconnect, without trying USB", async () => {
    await tcpConnect("192.168.1.91:9876");
    await disconnect();
    invoke.mockClear();
    expect(await reconnectLast()).toBe("tcp://192.168.1.91:9876");
    expect(invoke).toHaveBeenCalledExactlyOnceWith("tcp_connect", { addr: "192.168.1.91:9876" });
  });

  it("keeps the last successful address if a different hub fails to connect", async () => {
    await tcpConnect("pi.local:9876");
    invoke.mockRejectedValueOnce(new Error("unreachable"));
    await expect(tcpConnect("192.168.1.99:9876")).rejects.toThrow("unreachable");
    invoke.mockClear();
    expect(await reconnectLast()).toBe("tcp://pi.local:9876");
  });

  it.each(["manual", "auto"])("switches recovery back to USB after a successful %s USB connection", async mode => {
    await tcpConnect("pi.local:9876");
    if (mode === "manual") await connect("COM7");
    else await autoConnect();
    await disconnect();
    invoke.mockClear();
    expect(await reconnectLast()).toBe("COM7");
    expect(invoke).toHaveBeenCalledExactlyOnceWith("auto_connect");
  });

  it("reconnects over TCP and confirms firmware liveness after a reboot", async () => {
    vi.useFakeTimers();
    await tcpConnect("pi.local:9876");
    invoke.mockClear();
    const result = waitForReboot(5000);
    await vi.advanceTimersByTimeAsync(1600);
    expect(await result).toBe(true);
    expect(invoke).toHaveBeenCalledWith("tcp_connect", { addr: "pi.local:9876" });
    expect(invoke).not.toHaveBeenCalledWith("auto_connect");
    expect(invoke.mock.calls.some(([name, args]) => name === "send_command" && JSON.parse(args.line).type === "PING")).toBe(true);
  });
});
