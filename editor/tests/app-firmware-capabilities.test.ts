import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { cleanup, fireEvent, render, screen, waitFor, within } from "@testing-library/svelte";
import { tick } from "svelte";
import App from "../src/App.svelte";
import type { FirmwareMessage } from "../src/lib/protocol";

const mocks = vi.hoisted(() => ({
  invoke: vi.fn(),
  lifecycle: vi.fn(),
  onFirmwareMessage: vi.fn(),
  onDisconnected: vi.fn(),
  readNetworkBootstrap: vi.fn(),
  fetchBundledVersion: vi.fn(),
  fetchLatestRelease: vi.fn(),
  pickFirmwareSource: vi.fn(),
  prepareFirmwareSource: vi.fn(),
  pushFirmware: vi.fn(),
  cmd: {
    getDeviceInfo: vi.fn(), getManifest: vi.fn(), getManifestAwait: vi.fn(),
    listProfiles: vi.fn(), listPatches: vi.fn(), getDirty: vi.fn(),
    getMidiLearn: vi.fn(), getGlobal: vi.fn(), getStats: vi.fn(), getPatch: vi.fn(),
  },
}));

// Render the real App, MaintenancePanel and FirmwarePushOverlay. The upload
// boundary is stubbed so these regressions exercise every visible entry point
// without touching a pedal or starting a firmware transaction.
vi.mock("@tauri-apps/api/core", () => ({ invoke: mocks.invoke }));
vi.mock("@tauri-apps/api/event", () => ({ listen: vi.fn(async () => () => {}) }));
vi.mock("../src/lib/network-bootstrap", () => ({ readNetworkBootstrap: mocks.readNetworkBootstrap }));
vi.mock("../src/lib/protocol", async (importOriginal) => ({
  ...await importOriginal<typeof import("../src/lib/protocol")>(),
  cmd: mocks.cmd,
  onFirmwareMessage: mocks.onFirmwareMessage,
  onFirmwareRawLine: vi.fn(async () => () => {}),
  onDisconnected: mocks.onDisconnected,
  onReconnecting: vi.fn(async () => () => {}),
  onReconnected: vi.fn(async () => () => {}),
}));
vi.mock("../src/lib/installer", async (importOriginal) => ({
  ...await importOriginal<typeof import("../src/lib/installer")>(),
  detectPedal: vi.fn(async () => ({ kind: "none" })),
  pickFirmwareSource: mocks.pickFirmwareSource,
  prepareFirmwareSource: mocks.prepareFirmwareSource,
}));
vi.mock("../src/lib/firmware-update", async (importOriginal) => ({
  ...await importOriginal<typeof import("../src/lib/firmware-update")>(),
  fetchBundledVersion: mocks.fetchBundledVersion,
  fetchLatestRelease: mocks.fetchLatestRelease,
}));
vi.mock("../src/lib/firmware-push", () => ({ pushFirmware: mocks.pushFirmware }));
vi.mock("../src/lib/android-lifecycle", () => ({
  onLifecycleChange: mocks.lifecycle,
  onBackButton: vi.fn(() => () => {}),
  saveSessionState: vi.fn(),
  restoreSessionState: vi.fn(() => null),
}));

let backendConnected = false;

beforeEach(() => {
  backendConnected = false;
  localStorage.setItem("BOSUN_ONBOARDED", "1");
  localStorage.setItem("BOSUN_CONNECTION", JSON.stringify({
    mode: "network", host: "bosun-hub.local", port: "9876",
  }));
  mocks.invoke.mockReset().mockImplementation(async (command: string) => {
    if (command === "is_connected") return backendConnected;
    if (command === "tcp_connect") { backendConnected = true; return; }
    if (command === "disconnect") { backendConnected = false; return; }
    if (["discover_hubs", "list_ports", "tcp_list_ports"].includes(command)) return [];
    if (command === "auto_connect") throw new Error("No USB pedal connected");
    throw new Error(`Unexpected IPC: ${command}`);
  });
  mocks.lifecycle.mockReset().mockImplementation(() => () => {});
  mocks.onFirmwareMessage.mockReset().mockImplementation(async () => () => {});
  mocks.onDisconnected.mockReset().mockImplementation(async () => () => {});
  mocks.readNetworkBootstrap.mockReset().mockResolvedValue({ profiles: [], active: null });
  mocks.fetchBundledVersion.mockReset().mockResolvedValue("0.6.4");
  mocks.fetchLatestRelease.mockReset().mockResolvedValue(null);
  mocks.pickFirmwareSource.mockReset().mockResolvedValue(null);
  mocks.prepareFirmwareSource.mockReset().mockImplementation(async (source: string) => source);
  // Keep an accepted update open, allowing a subsequent device/session change
  // to exercise the live permission callback passed down to the uploader.
  mocks.pushFirmware.mockReset().mockImplementation(() => new Promise(() => {}));
  for (const command of Object.values(mocks.cmd)) command.mockReset().mockResolvedValue({});
  mocks.cmd.listProfiles.mockResolvedValue({ profiles: [] });
  mocks.cmd.getStats.mockResolvedValue({
    uptime_ms: 1000, mem_free: 19556, mem_alloc: 234396, loop_iters: 42,
    midi_rx_count: 0, midi_tx_count: 0, protocol_cmd_count: 1,
    last_patch_switch_ms: 0, current: { bank: 1, slot: 1 },
  });
});

afterEach(() => { cleanup(); });

type DeviceIdentity = {
  fw: string;
  native_experimental?: boolean;
  firmware_ota?: boolean;
};

async function ready(identity?: DeviceIdentity) {
  render(App);
  await waitFor(() => expect(mocks.lifecycle).toHaveBeenCalledOnce());
  await screen.findByTitle("Connected on tcp://bosun-hub.local:9876");
  await waitFor(() => expect(mocks.fetchLatestRelease).toHaveBeenCalled());
  await tick();
  if (identity) await deviceInfo(identity);
}

async function message(msg: FirmwareMessage) {
  mocks.onFirmwareMessage.mock.calls[0][0](msg);
  await tick();
}

async function deviceInfo(identity: DeviceIdentity) {
  await message({
    type: "DEVICE_INFO", ...identity, device: "MIDI Captain",
    profile: "kemper", current: { bank: 1, slot: 1 },
  } as FirmwareMessage);
}

async function maintenance() {
  await fireEvent.click(screen.getByRole("button", { name: "Menu", exact: true }));
  await fireEvent.click(within(screen.getByRole("navigation")).getByRole("button", { name: /Maintenance$/ }));
  expect(await screen.findByRole("button", { name: "Reboot pedal" })).toBeEnabled();
}

async function requestPush(source?: string) {
  window.dispatchEvent(new CustomEvent("bosun-open-firmware-push", { detail: { source } }));
  await tick();
}

function expectNoOtaControls() {
  expect(screen.queryByRole("button", { name: /^Update firmware \(/ })).not.toBeInTheDocument();
  expect(screen.queryByRole("button", { name: "Re-flash firmware" })).not.toBeInTheDocument();
  expect(screen.queryByRole("button", { name: "Update from bundled" })).not.toBeInTheDocument();
  expect(screen.queryByRole("button", { name: /^From folder/ })).not.toBeInTheDocument();
  expect(screen.queryByRole("button", { name: /^From \.zip/ })).not.toBeInTheDocument();
}

describe("App firmware update capabilities", () => {
  it.each([
    { fw: "0.1.0-native", native_experimental: true, firmware_ota: false },
    { fw: "0.1.0-native" },
    { fw: "0.6.3", firmware_ota: false },
  ])("blocks all CircuitPython update sources for $fw with its reported capabilities", async identity => {
    await ready(identity);
    expectNoOtaControls();
    expect(screen.queryByTitle("Firmware up to date")).not.toBeInTheDocument();
    if (identity.fw.includes("native")) {
      expect(screen.getByText("Native firmware · v0.1.0-native")).toBeInTheDocument();
    }
    await maintenance();
    expectNoOtaControls();
    for (const source of [undefined, "C:/firmware/custom", "C:/firmware/extracted-zip"]) {
      await requestPush(source);
      expect(screen.queryByRole("dialog")).not.toBeInTheDocument();
      expect(mocks.pushFirmware).not.toHaveBeenCalled();
    }
  });

  it("waits for device information before allowing any firmware update source", async () => {
    await ready();
    expectNoOtaControls();
    await maintenance();
    expectNoOtaControls();
    await requestPush("C:/firmware/custom");
    expect(mocks.pushFirmware).not.toHaveBeenCalled();

    await deviceInfo({ fw: "0.6.3" });
    expect(screen.getByRole("button", { name: "Update from bundled" })).toBeEnabled();
    expect(screen.getByRole("button", { name: /^Update firmware \(0\.6\.3 -> 0\.6\.4\)$/ })).toBeEnabled();
  });

  it.each(["top bar", "Maintenance"])("still opens the bundled legacy CircuitPython update from %s", async entry => {
    await ready({ fw: "0.6.3" });
    await maintenance();
    expect(screen.getByRole("button", { name: "Update from bundled" })).toBeEnabled();
    expect(screen.getByRole("button", { name: /^From folder/ })).toBeEnabled();
    expect(screen.getByRole("button", { name: /^From \.zip/ })).toBeEnabled();
    await fireEvent.click(screen.getByRole("button", {
      name: entry === "top bar" ? /^Update firmware \(0\.6\.3 -> 0\.6\.4\)$/ : "Update from bundled",
    }));
    await waitFor(() => expect(mocks.pushFirmware).toHaveBeenCalledOnce());
    const options = mocks.pushFirmware.mock.calls[0][1];
    expect(options.source).toBeUndefined();
    expect(options.isUpdateAllowed).toEqual(expect.any(Function));
    expect(options.isUpdateAllowed()).toBe(true);
  });

  it("forwards an allowed custom source to the guarded upload", async () => {
    await ready({ fw: "0.6.3", firmware_ota: true });
    await requestPush("C:/firmware/custom");
    await waitFor(() => expect(mocks.pushFirmware).toHaveBeenCalledOnce());
    expect(mocks.pushFirmware.mock.calls[0][1].source).toBe("C:/firmware/custom");
    expect(mocks.pushFirmware.mock.calls[0][1].isUpdateAllowed()).toBe(true);
  });

  it("revokes an open update on a native capability change and does not revive it on later device info", async () => {
    await ready({ fw: "0.6.3" });
    await requestPush();
    await waitFor(() => expect(mocks.pushFirmware).toHaveBeenCalledOnce());
    const isUpdateAllowed = mocks.pushFirmware.mock.calls[0][1].isUpdateAllowed;
    expect(isUpdateAllowed()).toBe(true);

    await deviceInfo({ fw: "0.1.0-native", native_experimental: true, firmware_ota: false });
    expect(isUpdateAllowed()).toBe(false);
    expectNoOtaControls();
    await deviceInfo({ fw: "0.6.3" });
    expect(isUpdateAllowed()).toBe(false);
    expect(mocks.pushFirmware).toHaveBeenCalledOnce();
  });

  it("keeps an interrupted update invalid after reconnecting to a supported pedal", async () => {
    await ready({ fw: "0.6.3" });
    await requestPush();
    await waitFor(() => expect(mocks.pushFirmware).toHaveBeenCalledOnce());
    const isUpdateAllowed = mocks.pushFirmware.mock.calls[0][1].isUpdateAllowed;

    backendConnected = false;
    window.dispatchEvent(new CustomEvent("rust-disconnected", { detail: "manual" }));
    await tick();
    expect(isUpdateAllowed()).toBe(false);
    const connectButton = await screen.findByRole("button", { name: "Connect", exact: true });
    await fireEvent.click(connectButton);
    await screen.findByTitle("Connected on tcp://bosun-hub.local:9876");
    await deviceInfo({ fw: "0.6.3" });
    expect(isUpdateAllowed()).toBe(false);
    expect(mocks.pushFirmware).toHaveBeenCalledOnce();
  });

  it("preserves an accepted update through ordinary patch changes on the same pedal", async () => {
    await ready({ fw: "0.6.3" });
    await requestPush();
    await waitFor(() => expect(mocks.pushFirmware).toHaveBeenCalledOnce());
    const isUpdateAllowed = mocks.pushFirmware.mock.calls[0][1].isUpdateAllowed;
    await message({ type: "EVENT", event: "patch_switched", bank: 2, slot: 3 });
    expect(isUpdateAllowed()).toBe(true);
  });

  it("does not open a picked firmware source if the pedal becomes native while the picker is open", async () => {
    let finishPicker!: (source: string) => void;
    mocks.pickFirmwareSource.mockImplementationOnce(() => new Promise<string>(resolve => { finishPicker = resolve; }));
    await ready({ fw: "0.6.3" });
    await maintenance();
    await fireEvent.click(screen.getByRole("button", { name: /^From folder/ }));
    expect(mocks.pickFirmwareSource).toHaveBeenCalledWith(false);
    await deviceInfo({ fw: "0.1.0-native", firmware_ota: false });
    finishPicker("C:/firmware/custom");
    await tick();
    await tick();
    expect(mocks.pushFirmware).not.toHaveBeenCalled();
    expectNoOtaControls();
  });

  it("does not open an extracted zip if the pedal becomes native while the source is being prepared", async () => {
    let finishPreparation!: (source: string) => void;
    mocks.pickFirmwareSource.mockResolvedValueOnce("C:/firmware/custom.zip");
    mocks.prepareFirmwareSource.mockImplementationOnce(() => new Promise<string>(resolve => { finishPreparation = resolve; }));
    await ready({ fw: "0.6.3" });
    await maintenance();
    await fireEvent.click(screen.getByRole("button", { name: /^From \.zip/ }));
    await waitFor(() => expect(mocks.prepareFirmwareSource).toHaveBeenCalledWith("C:/firmware/custom.zip"));
    expect(mocks.pickFirmwareSource).toHaveBeenCalledWith(true);
    await deviceInfo({ fw: "0.1.0-native", firmware_ota: false });
    finishPreparation("C:/firmware/extracted-zip");
    await tick();
    await tick();
    expect(mocks.pushFirmware).not.toHaveBeenCalled();
    expectNoOtaControls();
  });
});
