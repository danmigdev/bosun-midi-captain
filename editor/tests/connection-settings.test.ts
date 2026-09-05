import { describe, expect, it, vi } from "vitest";
import { networkAddress, readConnectionSettings, saveConnectionSettings } from "../src/lib/connection-settings";

describe("network connection settings", () => {
  it("remembers the selected connection and Raspberry address across launches", () => {
    expect(readConnectionSettings()).toEqual({ mode: "usb", host: "", port: "9876" });
    const settings = { mode: "network" as const, host: "192.168.1.91", port: "9876" };
    saveConnectionSettings(settings);
    expect(readConnectionSettings()).toEqual(settings);
    saveConnectionSettings({ ...settings, mode: "usb" });
    expect(readConnectionSettings()).toEqual({ ...settings, mode: "usb" });
  });

  it("recovers from corrupt or inaccessible storage", () => {
    localStorage.setItem("BOSUN_CONNECTION", "not JSON");
    expect(readConnectionSettings().mode).toBe("usb");
    vi.spyOn(Storage.prototype, "getItem").mockImplementation(() => { throw new Error("denied"); });
    vi.spyOn(Storage.prototype, "setItem").mockImplementation(() => { throw new Error("denied"); });
    expect(() => saveConnectionSettings({ mode: "network", host: "pi.local", port: "9876" })).not.toThrow();
    expect(readConnectionSettings().mode).toBe("usb");
  });

  it.each([
    [" 192.168.1.91 ", "9876", "192.168.1.91:9876"],
    ["bosun-hub.local", " 9877 ", "bosun-hub.local:9877"],
    ["::1", "9876", "[::1]:9876"],
    ["[2001:db8::1]", "9876", "[2001:db8::1]:9876"],
  ])("formats %s and port %s for the native socket", (host, port, expected) => {
    expect(networkAddress(host, port)).toBe(expected);
  });

  it.each([
    ["", "9876"], ["256.1.1.1", "9876"], ["192.168.1", "9876"],
    ["host with spaces", "9876"], ["tcp://pi.local:9876", "9876"],
    ["192.168.1.91:9876", "9876"], ["pi.local/path", "9876"],
    [":::broken", "9876"], ["pi.local", "0"], ["pi.local", "65536"],
    ["pi.local", "1.5"], ["pi.local", "1e3"], ["pi.local", ""],
  ])("rejects invalid endpoint %s / %s before connecting", (host, port) => {
    expect(() => networkAddress(host, port)).toThrow();
  });
});
