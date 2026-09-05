import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

const { invoke, inbox, events, commands } = vi.hoisted(() => ({
  invoke: vi.fn(), inbox: [] as string[], events: new Map<string, () => void>(),
  commands: [] as Record<string, any>[],
}));
vi.mock("@tauri-apps/api/core", () => ({ invoke }));
vi.mock("@tauri-apps/api/event", () => ({
  listen: vi.fn(async (name: string, handler: () => void) => {
    events.set(name, handler);
    return () => events.delete(name);
  }),
}));
import { cmd, FirmwareCommandTimeoutError } from "../src/lib/protocol";

const device = { tft: { layout: [{ field: "patch_name", color: "#ffffff", x: 0, y: 1 }] }, bank: 1 };
let respond: (message: Record<string, any>) => void;
function reply(message: Record<string, unknown>) {
  inbox.push(JSON.stringify(message));
  events.get("firmware-data-ready")?.();
}
beforeEach(() => {
  vi.useFakeTimers();
  inbox.length = commands.length = 0;
  respond = () => {};
  invoke.mockReset().mockImplementation(async (name: string, args?: { line: string }) => {
    if (name === "drain_inbox") return inbox.splice(0);
    if (name === "send_command") {
      const message = JSON.parse(args!.line);
      commands.push(message);
      respond(message);
    }
  });
});
afterEach(() => vi.useRealTimers());

describe("configuration save confirmation", () => {
  it("accepts a delayed ACK beyond the former four second timeout", async () => {
    respond = message => setTimeout(() => reply({ type: "ACK", id: message.id }), 6000);
    const saved = cmd.putGlobal(device);
    await vi.advanceTimersByTimeAsync(6500);
    expect(await saved).toMatchObject({ type: "ACK" });
    expect(commands.map(message => message.type)).toEqual(["PUT_GLOBAL"]);
  });

  it("sends type and request id before the large body, with a stable snapshot", async () => {
    const working = structuredClone(device);
    const saved = cmd.putGlobal(working);
    working.tft.layout[0].x = 99;
    await vi.advanceTimersByTimeAsync(0);
    const wire = invoke.mock.calls.find(([name]) => name === "send_command")![1].line;
    expect(Object.keys(JSON.parse(wire)).slice(0, 3)).toEqual(["type", "id", "device"]);
    expect(commands[0].device.tft.layout[0].x).toBe(0);
    reply({ type: "ACK", id: commands[0].id });
    await saved;
  });

  it("verifies the saved value after a lost ACK without writing it twice", async () => {
    respond = message => {
      if (message.type === "GET_GLOBAL") reply({ type: "GLOBAL", id: message.id,
        device: { bank: 1, tft: { layout: [{ y: 1, x: 0, color: "#ffffff", field: "patch_name" }] } } });
    };
    const saved = cmd.putGlobal(device);
    await vi.advanceTimersByTimeAsync(12001);
    expect(await saved).toMatchObject({ type: "ACK" });
    expect(commands.map(message => message.type)).toEqual(["PUT_GLOBAL", "GET_GLOBAL"]);
  });

  it.each(["different", "read-error", "read-timeout"])("keeps save unconfirmed when verification returns %s", async mode => {
    respond = message => {
      if (message.type !== "GET_GLOBAL") return;
      if (mode === "different") reply({ type: "GLOBAL", id: message.id, device: { tft: { layout: [] }, bank: 1 } });
      if (mode === "read-error") reply({ type: "ERROR", id: message.id, error: "background_busy" });
    };
    const saved = cmd.putGlobal(device);
    const result = saved.catch(error => error);
    await vi.advanceTimersByTimeAsync(22001);
    const failure = await result;
    expect(failure).toBeInstanceOf(FirmwareCommandTimeoutError);
    expect(failure.message).toContain("Save could not be confirmed");
    expect(commands.map(message => message.type)).toEqual(["PUT_GLOBAL", "GET_GLOBAL"]);
  });

  it("reports a rejected save immediately and does not retry or verify it", async () => {
    respond = message => reply({ type: "ERROR", id: message.id, error: "rx_oom" });
    await expect(cmd.putGlobal(device)).rejects.toThrow("rx_oom");
    expect(commands.map(message => message.type)).toEqual(["PUT_GLOBAL"]);
  });
});
