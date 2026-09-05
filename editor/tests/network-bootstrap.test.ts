import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

const { send } = vi.hoisted(() => ({ send: vi.fn() }));
vi.mock("../src/lib/protocol", () => ({
  sendAndAwait: send,
  FirmwareCommandTimeoutError: class extends Error {
    constructor(type: string, id: string) { super(`timeout: ${type}#${id}`); }
  },
}));

import { FirmwareCommandTimeoutError } from "../src/lib/protocol";
import { readNetworkBootstrap } from "../src/lib/network-bootstrap";

const profiles = [{ id: "live", name: "Live Kemper", kind: "kemper_player", active: true }];
const requests = [
  ["GET_DEVICE_INFO", 8000], ["LIST_PROFILES", 8000], ["GET_MANIFEST", 15000],
  ["LIST_PATCHES", 10000], ["GET_DIRTY", 8000], ["GET_MIDI_LEARN", 8000], ["GET_GLOBAL", 10000],
] as const;

function response(type: string) {
  if (type === "LIST_PROFILES") return { type: "PROFILE_LIST", profiles, active: "live" };
  return { type: ({ GET_DEVICE_INFO: "DEVICE_INFO", GET_MANIFEST: "MANIFEST",
    LIST_PATCHES: "PATCH_LIST", GET_DIRTY: "DIRTY", GET_MIDI_LEARN: "MIDI_LEARN", GET_GLOBAL: "GLOBAL" } as Record<string, string>)[type] };
}

describe("network bootstrap", () => {
  beforeEach(() => { send.mockReset(); });
  afterEach(() => { vi.useRealTimers(); });

  it("waits for each correlated response before sending the next request", async () => {
    const pending: Array<(value: unknown) => void> = [];
    send.mockImplementation(() => new Promise(resolve => pending.push(resolve)));
    const bootstrap = readNetworkBootstrap();
    for (let index = 0; index < requests.length; index++) {
      await vi.waitFor(() => expect(send).toHaveBeenCalledTimes(index + 1));
      const [type, timeout] = requests[index];
      expect(send).toHaveBeenNthCalledWith(index + 1, { type }, timeout);
      // Waiting another turn with this response unresolved must not admit
      // another read, even though writing its request already succeeded.
      await Promise.resolve();
      expect(send).toHaveBeenCalledTimes(index + 1);
      pending[index](response(type));
    }
    await expect(bootstrap).resolves.toEqual({ profiles, active: "live" });
  });

  it("still loads the manifest but skips profile state when no profile is active", async () => {
    const inactive = [{ ...profiles[0], active: false }];
    send.mockImplementation(async ({ type }) => type === "LIST_PROFILES"
      ? { type: "PROFILE_LIST", profiles: inactive, active: "" } : response(type));
    await expect(readNetworkBootstrap()).resolves.toEqual({ profiles: inactive, active: "" });
    expect(send.mock.calls.map(([message]) => message.type)).toEqual([
      "GET_DEVICE_INFO", "LIST_PROFILES", "GET_MANIFEST",
    ]);
  });

  it.each([
    ["background_busy", () => new Error("error: background_busy")],
    ["request_timeout", () => new Error("error: request_timeout")],
    ["local timeout", () => new FirmwareCommandTimeoutError("LIST_PROFILES", "timed-out")],
  ])("retries an initial %s after 250 ms without treating it as a missing profile", async (_name, failure) => {
    vi.useFakeTimers();
    let attempts = 0;
    send.mockImplementation(async ({ type }) => {
      if (type === "LIST_PROFILES" && attempts++ === 0) throw failure();
      return response(type);
    });
    const bootstrap = readNetworkBootstrap();
    await vi.advanceTimersByTimeAsync(0);
    expect(send.mock.calls.map(([message]) => message.type)).toEqual(["GET_DEVICE_INFO", "LIST_PROFILES"]);
    await vi.advanceTimersByTimeAsync(249);
    expect(send).toHaveBeenCalledTimes(2);
    await vi.advanceTimersByTimeAsync(1);
    await expect(bootstrap).resolves.toEqual({ profiles, active: "live" });
    expect(send.mock.calls.map(([message]) => message.type)).toEqual([
      "GET_DEVICE_INFO", "LIST_PROFILES", "LIST_PROFILES", "GET_MANIFEST",
      "LIST_PATCHES", "GET_DIRTY", "GET_MIDI_LEARN", "GET_GLOBAL",
    ]);
  });

  it("waits for another client's five-second stream to drain before bootstrapping", async () => {
    vi.useFakeTimers();
    const started = Date.now();
    send.mockImplementation(async ({ type }) => {
      if (type === "LIST_PROFILES" && Date.now() - started < 5000) {
        throw new Error("error: background_busy");
      }
      return response(type);
    });
    const bootstrap = readNetworkBootstrap();
    await vi.advanceTimersByTimeAsync(4999);
    expect(send.mock.calls.some(([message]) => message.type === "GET_MANIFEST")).toBe(false);
    await vi.advanceTimersByTimeAsync(1001);
    await expect(bootstrap).resolves.toEqual({ profiles, active: "live" });
    expect(send.mock.calls.filter(([message]) => message.type === "LIST_PROFILES").length).toBeGreaterThan(2);
    expect(send.mock.calls.some(([message]) => message.type === "GET_GLOBAL")).toBe(true);
  });

  it("bounds endless busy responses and each attempt by the original two-read budget", async () => {
    vi.useFakeTimers();
    const started = Date.now();
    const attempts: Array<{ elapsed: number; timeout: number }> = [];
    const failure = new Error("error: background_busy");
    send.mockImplementation(async (_message, timeout) => {
      attempts.push({ elapsed: Date.now() - started, timeout });
      throw failure;
    });
    const outcome = expect(readNetworkBootstrap()).rejects.toBe(failure);
    await vi.advanceTimersByTimeAsync(16000);
    await outcome;
    expect(attempts.length).toBeGreaterThan(2);
    for (const attempt of attempts) {
      expect(attempt.timeout).toBeGreaterThan(0);
      expect(attempt.timeout).toBeLessThanOrEqual(8000);
      expect(attempt.elapsed + attempt.timeout).toBeLessThanOrEqual(16000);
    }
    const count = send.mock.calls.length;
    await vi.advanceTimersByTimeAsync(60000);
    expect(send).toHaveBeenCalledTimes(count);
    expect(send.mock.calls.every(([message]) => message.type === "GET_DEVICE_INFO")).toBe(true);
  });

  it.each([
    ["request_timeout", () => new Error("error: request_timeout")],
    ["local timeout", () => new FirmwareCommandTimeoutError("LIST_PROFILES", "timed-out")],
  ])("propagates a second %s and never sends later requests", async (_name, makeFailure) => {
    vi.useFakeTimers();
    const failure = makeFailure();
    send.mockImplementation(async ({ type }) => {
      if (type === "LIST_PROFILES") throw failure;
      return response(type);
    });
    const outcome = expect(readNetworkBootstrap()).rejects.toBe(failure);
    await vi.runAllTimersAsync();
    await outcome;
    expect(send.mock.calls.map(([message]) => message.type)).toEqual([
      "GET_DEVICE_INFO", "LIST_PROFILES", "LIST_PROFILES",
    ]);
  });

  it("does not reset the one-time timeout retry when busy responses intervene", async () => {
    vi.useFakeTimers();
    const failure = new FirmwareCommandTimeoutError("GET_DEVICE_INFO", "timed-out");
    send.mockRejectedValueOnce(failure)
      .mockRejectedValueOnce(new Error("error: background_busy"))
      .mockRejectedValueOnce(failure);
    const outcome = expect(readNetworkBootstrap()).rejects.toBe(failure);
    await vi.runAllTimersAsync();
    await outcome;
    expect(send).toHaveBeenCalledTimes(3);
  });

  it.each(["error: not_found", "error: disconnected", "permission denied", "unexpected background_busy detail"])(
    "does not retry the permanent failure %s", async message => {
      const failure = new Error(message);
      send.mockRejectedValue(failure);
      await expect(readNetworkBootstrap()).rejects.toBe(failure);
      expect(send).toHaveBeenCalledTimes(1);
    },
  );
});
