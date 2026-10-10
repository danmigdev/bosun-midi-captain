import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { cleanup, fireEvent, render, screen, waitFor, within } from "@testing-library/svelte";
import { tick } from "svelte";
import App from "../src/App.svelte";
import type { FirmwareMessage } from "../src/lib/protocol";
import type { DeviceState } from "../src/lib/installer";
import type { UsbUpdateJob } from "../src/lib/usb-update";

const mocks = vi.hoisted(() => ({
  invoke: vi.fn(),
  lifecycle: vi.fn(),
  onFirmwareMessage: vi.fn(),
  onDisconnected: vi.fn(),
  readNetworkBootstrap: vi.fn(),
  detectPedal: vi.fn(),
  enterBootloader: vi.fn(),
  cmd: {
    getDeviceInfo: vi.fn(), getManifest: vi.fn(), getManifestAwait: vi.fn(),
    listProfiles: vi.fn(), listPatches: vi.fn(), getDirty: vi.fn(),
    getMidiLearn: vi.fn(), getGlobal: vi.fn(), getStats: vi.fn(), getPatch: vi.fn(),
  },
}));

// Render the real App and MaintenancePanel. Firmware data, pedal detection and
// the bootloader request are stubbed so these regressions exercise every
// visible entry point without touching a pedal or starting a firmware job.
vi.mock("@tauri-apps/api/core", () => ({ invoke: mocks.invoke }));
vi.mock("../src/lib/bootloader", () => ({ enterBootloader: mocks.enterBootloader }));
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
  detectPedal: mocks.detectPedal,
}));
vi.mock("../src/lib/android-lifecycle", () => ({
  onLifecycleChange: mocks.lifecycle,
  onBackButton: vi.fn(() => () => {}),
  saveSessionState: vi.fn(),
  restoreSessionState: vi.fn(() => null),
}));

const NO_PEDAL: DeviceState = { bootloader_drive: null, circuitpy_drive: null, usb_pedal_present: false };
let backendConnected = false;

beforeEach(() => {
  backendConnected = false;
  mocks.enterBootloader.mockReset().mockResolvedValue(undefined);
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
  mocks.detectPedal.mockReset().mockResolvedValue(NO_PEDAL);
  for (const command of Object.values(mocks.cmd)) command.mockReset().mockResolvedValue({});
  mocks.cmd.listProfiles.mockResolvedValue({ profiles: [] });
  mocks.cmd.getStats.mockResolvedValue({
    uptime_ms: 1000, midi_rx_count: 0, midi_tx_count: 0, midi_tx_failed: 0,
    queue_overflows: 0, unsupported_messages: 0, invalid_messages: 0,
    protocol_errors: 0, storage_errors: 0, midi_events_dropped: 0, storage_ready: true,
  });
});

afterEach(() => { cleanup(); vi.useRealTimers(); });

type DeviceIdentity = {
  fw: string;
  reboot_modes?: string[];
};

async function ready(identity?: DeviceIdentity) {
  render(App);
  await waitFor(() => expect(mocks.lifecycle).toHaveBeenCalledOnce());
  await screen.findByTitle("Connected on tcp://bosun-hub.local:9876");
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

function showModalOpensDialogs() {
  Object.defineProperty(HTMLDialogElement.prototype, "showModal", { configurable: true, value() { this.setAttribute("open", ""); } });
}

describe("App direct USB firmware update", () => {
  it("enters the bootloader from Maintenance and clears the USB session", async () => {
    render(App);
    await screen.findByTitle("Connected on COM9");
    await deviceInfo({ fw: "0.6.10-native", reboot_modes: ["normal", "bootloader"] });
    await maintenance();
    await fireEvent.click(screen.getByRole("button", { name: "Enter bootloader", exact: true }));
    await waitFor(() => expect(mocks.enterBootloader).toHaveBeenCalledOnce());
    await screen.findByText(/Bootloader requested. Look for RPI-RP2/);
    expect(screen.queryByTitle("Connected on COM9")).not.toBeInTheDocument();
    expect(screen.queryByRole("heading", { name: /Install Bosun/ })).not.toBeInTheDocument();
  });
  let current: UsbUpdateJob | null;
  const updateJob = (phase: UsbUpdateJob["phase"]): UsbUpdateJob => ({
    id: "usb-job", phase, port: "COM9", previous_version: "0.6.5-native", version: "0.6.5-native",
    message: "USB update status", percent: 0, backup_path: "C:/backup/flash-before.bin",
    identity: { serial: "0123456789ABCDEF", bus: "1", ports: [2] },
  });
  beforeEach(() => {
    current = null;
    localStorage.setItem("BOSUN_CONNECTION", JSON.stringify({ mode: "usb", host: "", port: "9876" }));
    showModalOpensDialogs();
    const previous = mocks.invoke.getMockImplementation()!;
    mocks.invoke.mockImplementation(async (command: string, args: unknown) => {
      if (command === "usb_update_status") return current;
      if (command === "bundled_update_manifest") return { schema: 1, board: "midi-captain-rp2040", release: "0.6.5", family: "native", firmware_version: "0.6.5-native", flash_bytes: 8388608, firmware_sha256: "a".repeat(64) };
      if (command === "auto_connect" || command === "connect") { backendConnected = true; return "COM9"; }
      if (command === "usb_update_start") { backendConnected = false; current = updateJob("writing"); return current; }
      return previous(command, args);
    });
  });
  it.each([
    { installed: "0.6.4-native", available: true },
    { installed: "0.6.5-native", available: false },
    { installed: "0.6.6-native", available: false },
  ])("only advertises a USB update newer than $installed", async ({ installed, available }) => {
    render(App);
    await waitFor(() => expect(mocks.lifecycle).toHaveBeenCalledOnce());
    await screen.findByTitle("Connected on COM9");
    await deviceInfo({ fw: installed });
    const button = screen.queryByRole("button", { name: "Update firmware (USB)", exact: true });
    if (available) expect(button).toBeEnabled();
    else {
      expect(button).not.toBeInTheDocument();
      expect(screen.getByText(`Bosun firmware · v${installed}`)).toBeInTheDocument();
    }
    expect(mocks.invoke.mock.calls.filter(([c]) => c === "usb_update_start")).toHaveLength(0);
  });
  it("keeps same-version reinstall in Maintenance and suspends editor access while the USB worker runs", async () => {
    render(App);
    await waitFor(() => expect(mocks.lifecycle).toHaveBeenCalledOnce());
    await deviceInfo({ fw: "0.6.5-native" });
    expect(screen.queryByRole("button", { name: "Update firmware (USB)", exact: true })).not.toBeInTheDocument();
    await maintenance();
    await fireEvent.click(screen.getByRole("button", { name: /^Install firmware \(USB\)/ }));
    expect(screen.getByRole("dialog")).toHaveTextContent("COM9");
    expect(mocks.invoke.mock.calls.filter(([c]) => c === "usb_update_start")).toHaveLength(0);
    await fireEvent.click(screen.getByRole("button", { name: "Update", exact: true }));
    await screen.findByRole("heading", { name: "Installing firmware" });
    expect(screen.queryByRole("heading", { name: "Connect your pedal" })).not.toBeInTheDocument();
    const probes = mocks.invoke.mock.calls.filter(([c]) => c === "auto_connect").length;
    mocks.onDisconnected.mock.calls[0][0]();
    await tick();
    expect(mocks.invoke.mock.calls.filter(([c]) => c === "auto_connect")).toHaveLength(probes);
    current = updateJob("done");
    await screen.findByRole("heading", { name: "Bosun updated" }, { timeout: 2000 });
    await fireEvent.click(screen.getByRole("button", { name: "Close", exact: true }));
    await screen.findByTitle("Connected on COM9");
    expect(mocks.invoke).toHaveBeenCalledWith("connect", { port: "COM9" });
    expect(mocks.invoke.mock.calls.filter(([c]) => c === "usb_update_start")).toHaveLength(1);
  });
  it("reopens an interrupted USB update before any startup probe and retains offline recovery", async () => {
    current = updateJob("recovery-required");
    render(App);
    await screen.findByRole("button", { name: "Restore backup" });
    await waitFor(() => expect(mocks.lifecycle).toHaveBeenCalledOnce());
    expect(mocks.invoke.mock.calls.filter(([c]) => ["auto_connect", "connect", "usb_update_start", "usb_update_recover"].includes(c))).toHaveLength(0);
    await fireEvent.click(screen.getByRole("button", { name: "Close", exact: true }));
    expect(screen.queryByRole("dialog")).not.toBeInTheDocument();
    expect(screen.queryByRole("heading", { name: "Connect your pedal" })).not.toBeInTheDocument();
    await fireEvent.click(screen.getByRole("button", { name: "Check USB update" }));
    expect(screen.getByRole("button", { name: "Restore backup" })).toBeEnabled();
  });
});

describe("App firmware capabilities", () => {
  it("does not offer manual bootloader entry through a network hub", async () => {
    await ready({ fw: "0.6.10-native", reboot_modes: ["bootloader"] });
    await maintenance();
    expect(screen.queryByRole("button", { name: "Enter bootloader" })).not.toBeInTheDocument();
  });

  it("does not offer bootloader entry to firmware that does not report it", async () => {
    localStorage.setItem("BOSUN_CONNECTION", JSON.stringify({ mode: "usb", host: "", port: "9876" }));
    const previous = mocks.invoke.getMockImplementation()!;
    mocks.invoke.mockImplementation(async (command: string, args: unknown) => {
      if (command === "auto_connect") { backendConnected = true; return "COM9"; }
      if (command === "usb_update_status" || command === "bundled_update_manifest") return null;
      return previous(command, args);
    });
    render(App);
    await screen.findByTitle("Connected on COM9");
    await deviceInfo({ fw: "0.6.10-native" });
    await maintenance();
    expect(screen.queryByRole("button", { name: "Enter bootloader" })).not.toBeInTheDocument();
  });

  it("shows the firmware's live counters in Maintenance", async () => {
    mocks.cmd.getStats.mockResolvedValue({
      uptime_ms: 65_000, midi_rx_count: 12, midi_tx_count: 34, midi_tx_failed: 1,
      queue_overflows: 2, unsupported_messages: 0, invalid_messages: 0,
      protocol_errors: 3, storage_errors: 4, midi_events_dropped: 0, storage_ready: true,
    });
    await ready({ fw: "0.8.0-native" });
    await maintenance();
    const block = (await screen.findByText("protocol errors")).closest("section")!;
    expect(within(block).getByText("1m 5s")).toBeInTheDocument();
    expect(within(block).getByText("ready")).toBeInTheDocument();
    expect(within(block).getByText("34")).toBeInTheDocument();
    expect(within(block).getByText("4")).toBeInTheDocument();
  });
});

describe("App install prompt", () => {
  const CIRCUITPY: DeviceState = { bootloader_drive: null, circuitpy_drive: "E:\\", usb_pedal_present: true };
  const BOOTLOADER: DeviceState = { bootloader_drive: "F:\\", circuitpy_drive: null, usb_pedal_present: false };
  const STOCK_SERIAL: DeviceState = { bootloader_drive: null, circuitpy_drive: null, usb_pedal_present: true };
  let pedal: DeviceState;
  const guide = () => screen.queryByRole("dialog", { name: "Bosun setup guide" });

  beforeEach(() => {
    pedal = NO_PEDAL;
    localStorage.setItem("BOSUN_CONNECTION", JSON.stringify({ mode: "usb", host: "", port: "9876" }));
    showModalOpensDialogs();
    mocks.detectPedal.mockImplementation(async () => pedal);
    const previous = mocks.invoke.getMockImplementation()!;
    mocks.invoke.mockImplementation(async (command: string, args: unknown) => {
      if (command === "usb_update_status" || command === "bundled_update_manifest") return null;
      return previous(command, args);
    });
  });

  it.each([
    ["a CIRCUITPY drive", CIRCUITPY],
    ["an RPI-RP2 bootloader drive", BOOTLOADER],
  ])("offers the Bosun install for %s", async (_name, state) => {
    pedal = state;
    render(App);
    await waitFor(() => expect(guide()).toBeInTheDocument());
  });

  it("does not prompt while nothing install-worthy is plugged in", async () => {
    render(App);
    await waitFor(() => expect(mocks.detectPedal).toHaveBeenCalled());
    await tick();
    expect(guide()).not.toBeInTheDocument();
  });

  it("prompts for a pedal-class device only after repeated failed Bosun probes", async () => {
    vi.useFakeTimers();
    pedal = STOCK_SERIAL;
    render(App);
    await vi.waitFor(() => expect(mocks.detectPedal).toHaveBeenCalledOnce());
    await vi.advanceTimersByTimeAsync(2500);
    expect(guide()).not.toBeInTheDocument();
    await vi.advanceTimersByTimeAsync(2500);
    expect(guide()).toBeInTheDocument();
  });

  it("keeps a dismissed prompt closed until the pedal is unplugged", async () => {
    vi.useFakeTimers();
    pedal = CIRCUITPY;
    render(App);
    await vi.waitFor(() => expect(guide()).toBeInTheDocument());
    await fireEvent.click(within(guide()!).getByRole("button", { name: "Close guide" }));
    await vi.advanceTimersByTimeAsync(2500);
    expect(guide()).not.toBeInTheDocument();
    pedal = NO_PEDAL;
    await vi.advanceTimersByTimeAsync(2500);
    expect(guide()).not.toBeInTheDocument();
    pedal = CIRCUITPY;
    await vi.advanceTimersByTimeAsync(2500);
    expect(guide()).toBeInTheDocument();
  });
});
