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
import { cmd, isScreensaverEffect, onFirmwareMessage, type FirmwareMessage } from "../src/lib/protocol";

let respond: (message: Record<string, any>) => void;
function reply(message: Record<string, unknown>) {
  inbox.push(JSON.stringify(message));
  events.get("firmware-data-ready")?.();
}
beforeEach(() => {
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

describe("SCREENSAVER_START", () => {
  it("sends the chosen effect and resolves on ACK", async () => {
    respond = message => reply({ type: "ACK", id: message.id });
    await expect(cmd.startScreensaver("drift")).resolves.toBeUndefined();
    expect(commands).toEqual([{ type: "SCREENSAVER_START", id: expect.any(String), effect: "drift" }]);
  });

  it("leaves the effect to the pedal when none is given", async () => {
    respond = message => reply({ type: "ACK", id: message.id });
    await cmd.startScreensaver();
    expect(commands).toEqual([{ type: "SCREENSAVER_START", id: expect.any(String) }]);
  });

  it.each(["update_lock", "unknown_type"])("rejects with the firmware's %s refusal", async error => {
    respond = message => reply({ type: "ERROR", id: message.id, error, of: message.type });
    await expect(cmd.startScreensaver("mix")).rejects.toThrow(`error: ${error}`);
  });

  it("is not settled by an unsolicited SCREENSAVER event", async () => {
    vi.useFakeTimers();
    const started = cmd.startScreensaver("bounce");
    const outcome = vi.fn();
    started.then(() => outcome("ack"), () => outcome("error"));
    await vi.advanceTimersByTimeAsync(0);
    reply({ type: "SCREENSAVER", active: true, effect: "bounce" });
    await vi.advanceTimersByTimeAsync(0);
    expect(outcome).not.toHaveBeenCalled();
    reply({ type: "ACK", id: commands[0].id });
    await vi.advanceTimersByTimeAsync(0);
    expect(outcome).toHaveBeenCalledWith("ack");
  });
});

describe("GET_SCREENSAVER", () => {
  it("reads an active screensaver and the effect on screen", async () => {
    respond = message => reply({ type: "SCREENSAVER", id: message.id, active: true, effect: "breathe" });
    await expect(cmd.getScreensaver()).resolves.toEqual({ active: true, effect: "breathe" });
    expect(commands).toEqual([{ type: "GET_SCREENSAVER", id: expect.any(String) }]);
  });

  it.each([
    ["inactive", { active: false }],
    ["inactive with a stale effect", { active: false, effect: "drift" }],
    ["active with an unknown effect", { active: true, effect: "sparkle" }],
  ])("drops the effect when %s", async (_case, state) => {
    respond = message => reply({ type: "SCREENSAVER", id: message.id, ...state });
    await expect(cmd.getScreensaver()).resolves.toEqual({ active: state.active });
  });

  it("rejects firmware without a screensaver", async () => {
    respond = message => reply({ type: "ERROR", id: message.id, error: "unknown_type", of: message.type });
    await expect(cmd.getScreensaver()).rejects.toThrow("error: unknown_type");
  });
});

describe("unsolicited SCREENSAVER events", () => {
  it("reach subscribers without warnings", async () => {
    const warn = vi.spyOn(console, "warn");
    const error = vi.spyOn(console, "error");
    const received: FirmwareMessage[] = [];
    const unsubscribe = await onFirmwareMessage(message => received.push(message));
    reply({ type: "SCREENSAVER", active: true, effect: "drift" });
    reply({ type: "SCREENSAVER", active: false });
    await vi.waitFor(() => expect(received).toHaveLength(2));
    unsubscribe();
    expect(received).toEqual([
      { type: "SCREENSAVER", active: true, effect: "drift" },
      { type: "SCREENSAVER", active: false },
    ]);
    expect(warn).not.toHaveBeenCalled();
    expect(error).not.toHaveBeenCalled();
  });
});

describe("isScreensaverEffect", () => {
  it("accepts exactly the configurable effects", () => {
    for (const effect of ["bounce", "breathe", "drift", "mix"]) expect(isScreensaverEffect(effect)).toBe(true);
    for (const value of ["", "Mix", "sparkle", null, undefined, 1]) expect(isScreensaverEffect(value)).toBe(false);
  });
});
