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
import {
  cmd, disconnect, onFirmwareMessage, sendAndAwait,
  isInternallyRetriedFirmwareError, FirmwareCommandTimeoutError,
  type FirmwareMessage,
} from "../src/lib/protocol";

let respond: (message: Record<string, any>) => void;
let unsubscribe: (() => void) | undefined;
const received: FirmwareMessage[] = [];
const visibleErrors: FirmwareMessage[] = [];
const toasts: any[] = [];
function toast(event: Event) { toasts.push((event as CustomEvent).detail); }
function reply(message: Record<string, unknown>) {
  inbox.push(JSON.stringify(message));
  events.get("firmware-data-ready")?.();
}
function busy(message: Record<string, any>) {
  reply({ type: "ERROR", id: message.id, error: "background_busy", of: "LIST_PATCHES" });
}
function listed(message: Record<string, any>, name = "Fresh") {
  reply({ type: "PATCH_LIST", id: message.id, patches: [{ bank: 1, slot: 1, name }] });
}
beforeEach(async () => {
  vi.useFakeTimers();
  inbox.length = commands.length = received.length = visibleErrors.length = toasts.length = 0;
  respond = () => {};
  invoke.mockReset().mockImplementation(async (name: string, args?: { line: string }) => {
    if (name === "drain_inbox") return inbox.splice(0);
    if (name === "send_command") {
      const message = JSON.parse(args!.line);
      commands.push(message);
      respond(message);
    }
  });
  unsubscribe = await onFirmwareMessage(message => {
    received.push(message);
    // This is App's error-to-toast boundary; raw messages remain observable.
    if (message.type === "ERROR" && !isInternallyRetriedFirmwareError(message)) visibleErrors.push(message);
  });
  window.addEventListener("bosun-toast", toast);
});
afterEach(async () => {
  await disconnect();
  await vi.runAllTimersAsync();
  unsubscribe?.();
  window.removeEventListener("bosun-toast", toast);
  vi.useRealTimers();
});

describe("patch list refresh after save", () => {
  it("waits for the actual list response, with no repeated write or read", async () => {
    let settled = false;
    const refresh = cmd.listPatches().then(() => { settled = true; });
    await vi.advanceTimersByTimeAsync(0);
    expect(settled).toBe(false);
    expect(commands.map(message => message.type)).toEqual(["LIST_PATCHES"]);
    listed(commands[0]);
    await refresh;
    expect(settled).toBe(true);
  });

  it("retries only the rejected read after a successful save, without a false error toast", async () => {
    let attempts = 0;
    respond = message => {
      if (message.type === "SAVE_NOW") reply({ type: "SAVED", id: message.id, patches: [{ bank: 1, slot: 1 }] });
      if (message.type === "LIST_PATCHES") attempts++ === 0 ? busy(message) : listed(message);
    };
    await cmd.saveNow(1, 1);
    const refresh = cmd.listPatches();
    await vi.advanceTimersByTimeAsync(249);
    expect(commands.map(message => message.type)).toEqual(["SAVE_NOW", "LIST_PATCHES"]);
    expect(visibleErrors).toEqual([]);
    expect(received.some(message => message.type === "ERROR")).toBe(true);
    await vi.advanceTimersByTimeAsync(1);
    await refresh;
    expect(commands.map(message => message.type)).toEqual(["SAVE_NOW", "LIST_PATCHES", "LIST_PATCHES"]);
    expect(commands[1].id).not.toBe(commands[2].id);
    expect(toasts).toEqual([]);
  });

  it("coalesces concurrent refreshes but reads again after an in-flight snapshot predating a save", async () => {
    const first = cmd.listPatches();
    await vi.advanceTimersByTimeAsync(0);
    const second = cmd.listPatches();
    const third = cmd.listPatches();
    expect(second).toBe(first);
    expect(third).toBe(first);
    expect(commands).toHaveLength(1);
    listed(commands[0], "Before save");
    await vi.advanceTimersByTimeAsync(0);
    expect(commands).toHaveLength(2);
    listed(commands[1], "After save");
    await Promise.all([first, second, third]);
    expect(commands).toHaveLength(2);
    expect(received.at(-1)).toMatchObject({ type: "PATCH_LIST", patches: [{ name: "After save" }] });
  });

  it("waits with bounded backoff while another client's stream is busy for five seconds", async () => {
    const start = Date.now();
    respond = message => Date.now() - start < 5000 ? busy(message) : listed(message);
    const refresh = cmd.listPatches();
    await vi.advanceTimersByTimeAsync(6000);
    await refresh;
    expect(commands.length).toBeGreaterThan(2);
    expect(commands.length).toBeLessThan(10);
    expect(visibleErrors).toEqual([]);
  });

  it("keeps the final busy failure visible and stops before the twenty-second deadline", async () => {
    respond = busy;
    const outcome = cmd.listPatches().catch(error => error);
    await vi.advanceTimersByTimeAsync(20000);
    expect(await outcome).toMatchObject({ message: "error: background_busy" });
    expect(visibleErrors).toHaveLength(1);
    expect(visibleErrors[0]).toMatchObject({ type: "ERROR", error: "background_busy" });
    const count = commands.length;
    await vi.advanceTimersByTimeAsync(60000);
    expect(commands).toHaveLength(count);
    expect(toasts).toEqual([]); // App displays the final ERROR once.
  });

  it.each(["not_found", "request_timeout", "rx_oom", "disconnected"])(
    "does not suppress or retry the different failure %s", async error => {
      respond = message => reply({ type: "ERROR", id: message.id, of: "LIST_PATCHES", error });
      await expect(cmd.listPatches()).rejects.toThrow(error);
      await vi.advanceTimersByTimeAsync(20000);
      expect(commands).toHaveLength(1);
      expect(visibleErrors).toHaveLength(1);
    },
  );

  it("does not suppress another request's identical busy error", async () => {
    respond = message => {
      if (message.type === "LIST_PATCHES") busy(message);
      else reply({ type: "ERROR", id: message.id, of: message.type, error: "background_busy" });
    };
    const refresh = cmd.listPatches().catch(error => error);
    await vi.advanceTimersByTimeAsync(0);
    await expect(sendAndAwait({ type: "GET_GLOBAL" })).rejects.toThrow("background_busy");
    reply({ type: "ERROR", id: "unowned", of: "LIST_PATCHES", error: "background_busy" });
    await vi.advanceTimersByTimeAsync(0);
    expect(visibleErrors.map(message => (message as { of: string }).of)).toEqual(["GET_GLOBAL", "LIST_PATCHES"]);
    await disconnect();
    await vi.advanceTimersByTimeAsync(1000);
    expect(await refresh).toMatchObject({ message: "error: disconnected" });
  });

  it("stops stale retries across disconnect and lets a new connection request a fresh list", async () => {
    respond = busy;
    const previous = cmd.listPatches().catch(error => error);
    await vi.advanceTimersByTimeAsync(0);
    await disconnect();
    respond = message => listed(message, "New profile");
    const current = cmd.listPatches();
    await current;
    await vi.advanceTimersByTimeAsync(20000);
    expect(await previous).toMatchObject({ message: "error: disconnected" });
    expect(commands).toHaveLength(2);
    expect(toasts).toEqual([]);
  });

  it("does not send if disconnected while awaiting the protocol listener", async () => {
    const refresh = cmd.listPatches().catch(error => error);
    await disconnect();
    await vi.advanceTimersByTimeAsync(0);
    expect(await refresh).toMatchObject({ message: "error: disconnected" });
    expect(commands).toEqual([]);
    expect(toasts).toEqual([]);
  });

  it("honours a new refresh queued while the preceding response promise is settling", async () => {
    respond = listed;
    let next: Promise<void> | undefined;
    let first = true;
    const off = await onFirmwareMessage(message => {
      if (message.type === "PATCH_LIST" && first) {
        first = false;
        // The protocol resolver resumes its read first; this callback runs
        // before the public catch/finally promise has finished settling.
        queueMicrotask(() => { next = cmd.listPatches(); });
      }
    });
    try {
      await cmd.listPatches();
      await vi.advanceTimersByTimeAsync(0);
      await next;
      expect(commands).toHaveLength(2);
    } finally { off(); }
  });

  it("reports a lost reply once without blindly retrying the read", async () => {
    const outcome = cmd.listPatches().catch(error => error);
    await vi.advanceTimersByTimeAsync(20000);
    expect(await outcome).toBeInstanceOf(FirmwareCommandTimeoutError);
    expect(commands).toHaveLength(1);
    expect(toasts).toHaveLength(1);
    expect(toasts[0]).toMatchObject({ level: "error", message: expect.stringContaining("patch list could not be refreshed") });
  });

  it("keeps legacy fire-and-forget refresh failures observable without unhandled rejections", async () => {
    respond = message => reply({ type: "ERROR", id: message.id, of: "LIST_PATCHES", error: "rx_oom" });
    void cmd.listPatches();
    await vi.advanceTimersByTimeAsync(0);
    expect(visibleErrors).toHaveLength(1);
    expect(commands).toHaveLength(1);
  });
});
