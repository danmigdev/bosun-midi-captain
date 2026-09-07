import { createHash, webcrypto } from "node:crypto";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { cleanup, fireEvent, render, screen, waitFor, within } from "@testing-library/svelte";
import { tick } from "svelte";
import App from "../src/App.svelte";
import UnifiedFirmwareUpdate from "../src/components/UnifiedFirmwareUpdate.svelte";
import {
  abortUnifiedUpload, bundledUpdateManifest, checkUnifiedRecovery, forgetUpdate, hubSupportsUnifiedUpdate,
  isTerminalUpdate, PENDING_UPDATE_KEY, readPendingUpdate, resumeUnifiedUpdate,
  startUnifiedUpdate, unifiedUpdateAvailable, unifiedUpdateStatus,
  type BundledUpdateManifest, type HubUpdateStatus,
} from "../src/lib/unified-update";

const mocks = vi.hoisted(() => ({
  invoke: vi.fn(), sendAndAwait: vi.fn(), lifecycle: vi.fn(),
  onFirmwareMessage: vi.fn(), onDisconnected: vi.fn(), readNetworkBootstrap: vi.fn(),
  cmd: {
    getDeviceInfo: vi.fn(), getManifest: vi.fn(), getManifestAwait: vi.fn(),
    listProfiles: vi.fn(), listPatches: vi.fn(), getDirty: vi.fn(),
    getMidiLearn: vi.fn(), getGlobal: vi.fn(), getStats: vi.fn(),
  },
}));

vi.mock("@tauri-apps/api/core", () => ({ invoke: mocks.invoke }));
vi.mock("@tauri-apps/api/event", () => ({ listen: vi.fn(async () => () => {}) }));
vi.mock("../src/lib/protocol", async (importOriginal) => ({
  ...await importOriginal<typeof import("../src/lib/protocol")>(),
  sendAndAwait: mocks.sendAndAwait, cmd: mocks.cmd,
  onFirmwareMessage: mocks.onFirmwareMessage,
  onFirmwareRawLine: vi.fn(async () => () => {}),
  onDisconnected: mocks.onDisconnected,
  onReconnecting: vi.fn(async () => () => {}), onReconnected: vi.fn(async () => () => {}),
}));
vi.mock("../src/lib/network-bootstrap", () => ({ readNetworkBootstrap: mocks.readNetworkBootstrap }));
vi.mock("../src/lib/installer", async (importOriginal) => ({
  ...await importOriginal<typeof import("../src/lib/installer")>(),
  detectPedal: vi.fn(async () => ({ kind: "none" })),
}));
vi.mock("../src/lib/firmware-update", async (importOriginal) => ({
  ...await importOriginal<typeof import("../src/lib/firmware-update")>(),
  fetchBundledVersion: vi.fn(async () => "0.6.4"), fetchLatestRelease: vi.fn(async () => null),
}));
vi.mock("../src/lib/android-lifecycle", () => ({
  onLifecycleChange: mocks.lifecycle, onBackButton: vi.fn(() => () => {}),
  saveSessionState: vi.fn(), restoreSessionState: vi.fn(() => null),
}));

const endpoint = "tcp://bosun-hub.local:9876";
const manifest: BundledUpdateManifest = {
  schema: 1, board: "midi-captain-rp2040", release: "0.6.5", family: "native",
  firmware_version: "0.6.5-native", flash_bytes: 8388608, firmware_sha256: "a".repeat(64),
};
function packageFixture(size = 31000) {
  const bytes = Buffer.from(Array.from({ length: size }, (_, i) => i % 256));
  return { data: bytes.toString("base64"), size, sha256: createHash("sha256").update(bytes).digest("hex") };
}
let bundle = packageFixture();
let backendConnected = false;
let bundledManifest: BundledUpdateManifest | null = null;
let currentStatus: HubUpdateStatus;

beforeEach(() => {
  vi.stubGlobal("crypto", webcrypto);
  backendConnected = false;
  bundledManifest = null;
  bundle = packageFixture();
  currentStatus = { type: "HUB_UPDATE", job: "job-123", phase: "flashing" };
  mocks.invoke.mockReset().mockImplementation(async (name: string) => {
    if (name === "read_bundled_update") return bundle;
    if (name === "bundled_update_manifest") return bundledManifest;
    if (name === "is_connected") return backendConnected;
    if (name === "tcp_connect") { backendConnected = true; return; }
    if (name === "disconnect") { backendConnected = false; return; }
    if (["discover_hubs", "list_ports", "tcp_list_ports"].includes(name)) return [];
    if (name === "auto_connect") throw new Error("No USB pedal connected");
    throw new Error(`Unexpected IPC: ${name}`);
  });
  mocks.sendAndAwait.mockReset().mockImplementation(async (request: Record<string, unknown>) => {
    const base = { type: "HUB_UPDATE", job: "job-123" };
    switch (request.type) {
      case "HUB_UPDATE_INFO": return { ...base, phase: "ready", supported: true };
      case "HUB_UPDATE_BEGIN": return { ...base, phase: "uploading", received: 0, size: request.size, sha256: request.sha256 };
      case "HUB_UPDATE_CHUNK": return { ...base, phase: "uploading", received: Number(request.offset) + atob(String(request.data)).length };
      case "HUB_UPDATE_COMMIT": return { ...base, phase: "queued" };
      case "HUB_UPDATE_STATUS": return currentStatus;
      case "HUB_UPDATE_RECOVER":
        currentStatus = { ...currentStatus, phase: "verifying", recovery_verified: false };
        return currentStatus;
      case "HUB_UPDATE_ABORT": return { ...base, phase: "aborted" };
      default: throw new Error(`Unexpected protocol command: ${request.type}`);
    }
  });
  mocks.lifecycle.mockReset().mockImplementation(() => () => {});
  mocks.onFirmwareMessage.mockReset().mockImplementation(async () => () => {});
  mocks.onDisconnected.mockReset().mockImplementation(async () => () => {});
  mocks.readNetworkBootstrap.mockReset().mockResolvedValue({ profiles: [], active: null });
  for (const command of Object.values(mocks.cmd)) command.mockReset().mockResolvedValue({});
  mocks.cmd.listProfiles.mockResolvedValue({ profiles: [] });
  mocks.cmd.getStats.mockResolvedValue({
    uptime_ms: 1000, mem_free: 19556, mem_alloc: 234396, loop_iters: 42,
    midi_rx_count: 0, midi_tx_count: 0, protocol_cmd_count: 1,
    last_patch_switch_ms: 0, current: { bank: 1, slot: 1 },
  });
});

afterEach(() => { cleanup(); vi.useRealTimers(); vi.unstubAllGlobals(); });

function options() {
  return { endpoint, isCurrentConnection: vi.fn(() => true), onStatus: vi.fn(), onJob: vi.fn() };
}
function saveJob(target = endpoint) {
  localStorage.setItem(PENDING_UPDATE_KEY, JSON.stringify({ job: "job-123", endpoint: target }));
}
function commands(type: string) {
  return mocks.sendAndAwait.mock.calls.filter(([request]) => request.type === type).map(([request]) => request);
}

describe("unified update package and transport", () => {
  it.each(["0.6.4", "0.6.5", "0.1.0-native", "0.1.0-native-experimental"])("offers one Bosun release to installed %s", installed => {
    expect(unifiedUpdateAvailable(installed, manifest)).toBe(true);
  });
  it.each([undefined, "", "not-a-version", "0.6.5-native", "0.6.6"])("does not offer an upgrade from %s", installed => {
    expect(unifiedUpdateAvailable(installed, manifest)).toBe(false);
  });
  it("accepts only the supported bundled manifest schema and board", async () => {
    bundledManifest = manifest;
    expect(await bundledUpdateManifest()).toEqual(manifest);
    for (const change of [{ schema: 2 }, { board: "other" }, { family: "circuitpython" }, { flash_bytes: 1024 }, { firmware_sha256: "invalid" }]) {
      bundledManifest = { ...manifest, ...change } as BundledUpdateManifest;
      expect(await bundledUpdateManifest()).toBeNull();
    }
  });
  it("treats a missing bundle or unsupported hub as unavailable", async () => {
    expect(await bundledUpdateManifest()).toBeNull();
    mocks.sendAndAwait.mockResolvedValueOnce({ type: "ACK" });
    expect(await hubSupportsUnifiedUpdate()).toBe(false);
    mocks.sendAndAwait.mockResolvedValueOnce({ type: "HUB_UPDATE", phase: "ready", supported: false });
    expect(await hubSupportsUnifiedUpdate()).toBe(false);
  });
  it("uploads exact bounded chunks, persists only the job and endpoint, then commits once", async () => {
    const result = await startUnifiedUpdate(options());
    expect(result.phase).toBe("queued");
    expect(commands("HUB_UPDATE_BEGIN")).toEqual([{ type: "HUB_UPDATE_BEGIN", size: bundle.size, sha256: bundle.sha256 }]);
    const chunks = commands("HUB_UPDATE_CHUNK");
    expect(chunks.map(chunk => chunk.offset)).toEqual([0, 12288, 24576]);
    expect(chunks.every(chunk => atob(chunk.data).length <= 16384)).toBe(true);
    expect(Buffer.concat(chunks.map(chunk => Buffer.from(chunk.data, "base64"))).toString("base64")).toBe(bundle.data);
    expect(commands("HUB_UPDATE_COMMIT")).toHaveLength(1);
    expect(JSON.parse(localStorage.getItem(PENDING_UPDATE_KEY)!)).toEqual({ job: "job-123", endpoint });
  });
  it.each(["checksum", "size", "base64"])("rejects package %s corruption before creating an upload", async corruption => {
    if (corruption === "checksum") bundle.sha256 = "0".repeat(64);
    if (corruption === "size") bundle.size++;
    if (corruption === "base64") bundle.data = "%%%";
    await expect(startUnifiedUpdate(options())).rejects.toThrow(/bundled update/i);
    expect(commands("HUB_UPDATE_BEGIN")).toHaveLength(0);
    expect(readPendingUpdate()).toBeNull();
  });
  it("stops sending chunks when the selected connection changes", async () => {
    const opts = options();
    const implementation = mocks.sendAndAwait.getMockImplementation()!;
    mocks.sendAndAwait.mockImplementation(async request => {
      const result = await implementation(request);
      if (request.type === "HUB_UPDATE_CHUNK") opts.isCurrentConnection.mockReturnValue(false);
      return result;
    });
    await expect(startUnifiedUpdate(opts)).rejects.toThrow(/Reconnect to the same/);
    expect(commands("HUB_UPDATE_CHUNK")).toHaveLength(1);
    expect(commands("HUB_UPDATE_COMMIT")).toHaveLength(0);
    expect(readPendingUpdate()?.job).toBe("job-123");
  });
  it("does not upload when the fresh hub check refuses support", async () => {
    mocks.sendAndAwait.mockResolvedValueOnce({ type: "HUB_UPDATE", phase: "ready", supported: false });
    await expect(startUnifiedUpdate(options())).rejects.toThrow(/not ready/);
    expect(commands("HUB_UPDATE_BEGIN")).toHaveLength(0);
    expect(mocks.invoke).not.toHaveBeenCalledWith("read_bundled_update");
  });
  it("cancels an uncommitted upload if its recovery job cannot be persisted", async () => {
    vi.spyOn(Storage.prototype, "setItem").mockImplementation(() => { throw new Error("Storage unavailable"); });
    await expect(startUnifiedUpdate(options())).rejects.toThrow(/Storage unavailable/);
    expect(commands("HUB_UPDATE_ABORT")).toHaveLength(1);
    expect(commands("HUB_UPDATE_CHUNK")).toHaveLength(0);
    expect(commands("HUB_UPDATE_COMMIT")).toHaveLength(0);
  });
  it("rejects an incomplete chunk acknowledgement without committing", async () => {
    const implementation = mocks.sendAndAwait.getMockImplementation()!;
    mocks.sendAndAwait.mockImplementation(request => request.type === "HUB_UPDATE_CHUNK"
      ? { type: "HUB_UPDATE", job: "job-123", phase: "uploading", received: 1 }
      : implementation(request));
    await expect(startUnifiedUpdate(options())).rejects.toThrow(/acknowledge/);
    expect(commands("HUB_UPDATE_COMMIT")).toHaveLength(0);
  });
  it("recovers a lost COMMIT response through status without a second flash or abort", async () => {
    const implementation = mocks.sendAndAwait.getMockImplementation()!;
    mocks.sendAndAwait.mockImplementation(request => {
      if (request.type === "HUB_UPDATE_COMMIT") throw new Error("connection lost");
      return implementation(request);
    });
    await expect(startUnifiedUpdate(options())).rejects.toThrow(/connection lost/);
    expect(readPendingUpdate()?.job).toBe("job-123");
    expect((await resumeUnifiedUpdate(options())).phase).toBe("flashing");
    expect(commands("HUB_UPDATE_COMMIT")).toHaveLength(1);
    expect(commands("HUB_UPDATE_ABORT")).toHaveLength(0);
  });
  it("resumes only the remainder of an upload after restarting the app", async () => {
    saveJob();
    currentStatus = { type: "HUB_UPDATE", phase: "uploading", job: "job-123", received: 12288, size: bundle.size, sha256: bundle.sha256 };
    expect((await resumeUnifiedUpdate(options())).phase).toBe("queued");
    expect(commands("HUB_UPDATE_BEGIN")).toHaveLength(0);
    expect(commands("HUB_UPDATE_CHUNK").map(chunk => chunk.offset)).toEqual([12288, 24576]);
    expect(commands("HUB_UPDATE_COMMIT")).toHaveLength(1);
  });
  it("does not resume an upload using a different bundle", async () => {
    saveJob();
    currentStatus = { type: "HUB_UPDATE", phase: "uploading", job: "job-123", received: 1, size: bundle.size, sha256: "f".repeat(64) };
    await expect(resumeUnifiedUpdate(options())).rejects.toThrow(/different update package/);
    expect(commands("HUB_UPDATE_CHUNK")).toHaveLength(0);
    expect(commands("HUB_UPDATE_COMMIT")).toHaveLength(0);
  });
  it("never queries a saved job through a different Raspberry Pi", async () => {
    saveJob("tcp://other-hub.local:9876");
    await expect(resumeUnifiedUpdate(options())).rejects.toThrow(/where this update was started/);
    expect(mocks.sendAndAwait).not.toHaveBeenCalled();
  });
  it("requires the status response to identify the requested job", async () => {
    currentStatus = { type: "HUB_UPDATE", job: "another-job", phase: "done" };
    await expect(unifiedUpdateStatus("job-123")).rejects.toThrow(/different update job/);
  });
  it("allows cancellation only while uploading", async () => {
    saveJob();
    await expect(abortUnifiedUpload(endpoint, () => true)).rejects.toThrow(/already started/);
    expect(commands("HUB_UPDATE_ABORT")).toHaveLength(0);
    currentStatus.phase = "uploading";
    expect((await abortUnifiedUpload(endpoint, () => true)).phase).toBe("aborted");
    expect(readPendingUpdate()).toBeNull();
  });
  it.each(["different endpoint", "disconnected"])("blocks recovery checking through a %s", async connection => {
    saveJob(connection === "different endpoint" ? "tcp://other-hub.local:9876" : endpoint);
    const opts = options();
    if (connection === "disconnected") opts.isCurrentConnection.mockReturnValue(false);
    await expect(checkUnifiedRecovery(opts)).rejects.toThrow(/Raspberry Pi/);
    expect(mocks.sendAndAwait).not.toHaveBeenCalled();
    expect(readPendingUpdate()?.job).toBe("job-123");
  });
  it("rejects a recovery acknowledgement for another job without discarding the saved job", async () => {
    saveJob();
    const opts = options();
    mocks.sendAndAwait.mockResolvedValue({ type: "HUB_UPDATE", job: "other-job", phase: "verifying" });
    await expect(checkUnifiedRecovery(opts)).rejects.toThrow(/different update job/);
    expect(opts.onStatus).not.toHaveBeenCalled();
    expect(readPendingUpdate()?.job).toBe("job-123");
  });
  it("keeps unrelated persisted jobs when dismissing an old result", () => {
    saveJob();
    forgetUpdate("previous-job");
    expect(readPendingUpdate()?.job).toBe("job-123");
    forgetUpdate("job-123");
    expect(readPendingUpdate()).toBeNull();
  });
  it.each(["done", "error", "rolled-back", "recovery-required", "aborted"])("recognizes %s as a terminal state", phase => {
    expect(isTerminalUpdate({ phase })).toBe(true);
  });
});

function renderDialog() {
  return render(UnifiedFirmwareUpdate, {
    manifest, installed: "0.6.4", endpoint, isCurrentConnection: () => true,
    onClose: vi.fn(), onJob: vi.fn(), onComplete: vi.fn(),
  });
}

describe("unified update review and recovery UI", () => {
  it("shows the target release and preserves explicit user confirmation before any upload", async () => {
    renderDialog();
    expect(screen.getByRole("dialog")).toHaveTextContent("to 0.6.5");
    expect(screen.getByRole("dialog")).toHaveTextContent("profiles and settings are backed up");
    expect(mocks.sendAndAwait).not.toHaveBeenCalled();
    await fireEvent.click(screen.getByRole("button", { name: "Update", exact: true }));
    await waitFor(() => expect(commands("HUB_UPDATE_COMMIT")).toHaveLength(1));
  });
  it.each([
    ["rolled-back", "Previous firmware restored", "restored the previous firmware and settings"],
    ["recovery-required", "Recovery required", "needs manual recovery"],
    ["error", "Update stopped", "Check the error below"],
  ])("shows a truthful %s outcome and the server backup location", async (phase, title, detail) => {
    saveJob();
    currentStatus = { type: "HUB_UPDATE", job: "job-123", phase, error: "Verification failed", backup: "/var/lib/bosun/backups/job-123" };
    renderDialog();
    expect(await screen.findByRole("heading", { name: title })).toBeInTheDocument();
    expect(screen.getByRole("dialog")).toHaveTextContent(detail);
    expect(screen.getByText("/var/lib/bosun/backups/job-123")).toBeInTheDocument();
    expect(commands("HUB_UPDATE_COMMIT")).toHaveLength(0);
  });
  it("resumes status polling for a saved running update without requiring a new confirmation", async () => {
    vi.useFakeTimers();
    saveJob();
    renderDialog();
    await vi.waitFor(() => expect(commands("HUB_UPDATE_STATUS")).toHaveLength(1));
    currentStatus = { type: "HUB_UPDATE", job: "job-123", phase: "done", release: "0.6.5" };
    await vi.advanceTimersByTimeAsync(1000);
    expect(screen.getByRole("heading", { name: "Bosun updated" })).toBeInTheDocument();
    expect(commands("HUB_UPDATE_COMMIT")).toHaveLength(0);
    await vi.advanceTimersByTimeAsync(5000);
    expect(commands("HUB_UPDATE_STATUS")).toHaveLength(2);
  });
  it("shows an explicit hub refusal without pretending the connection was interrupted", async () => {
    saveJob();
    mocks.sendAndAwait.mockRejectedValue(new Error("error: Unknown update job"));
    renderDialog();
    expect(await screen.findByRole("alert")).toHaveTextContent("Unknown update job");
    expect(screen.queryByRole("heading", { name: "Waiting for Raspberry Pi" })).not.toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Check again" })).toBeEnabled();
  });
  it("retains an unresolved recovery job when its result window is closed", async () => {
    saveJob();
    currentStatus = { type: "HUB_UPDATE", job: "job-123", phase: "recovery-required", backup: "/var/lib/bosun/backups/job-123" };
    renderDialog();
    await screen.findByRole("heading", { name: "Recovery required" });
    await fireEvent.click(screen.getByRole("button", { name: "Close", exact: true }));
    expect(readPendingUpdate()).toEqual({ job: "job-123", endpoint });
  });
  it("checks recovery once, polls verification, and clears the failed update only after acknowledgement", async () => {
    vi.useFakeTimers();
    saveJob();
    currentStatus = { type: "HUB_UPDATE", job: "job-123", phase: "recovery-required", error: "Pedal did not reconnect" };
    const onComplete = vi.fn();
    render(UnifiedFirmwareUpdate, {
      manifest, installed: "0.1.0-native", endpoint, isCurrentConnection: () => true,
      onClose: vi.fn(), onJob: vi.fn(), onComplete,
    });
    await vi.waitFor(() => expect(screen.getByRole("button", { name: "Check recovery" })).toBeEnabled());
    expect(screen.getByRole("dialog")).toHaveTextContent("This check does not write to the pedal.");
    const button = screen.getByRole("button", { name: "Check recovery" });
    await Promise.all([fireEvent.click(button), fireEvent.click(button)]);
    await tick();
    expect(commands("HUB_UPDATE_RECOVER")).toEqual([{ type: "HUB_UPDATE_RECOVER", job: "job-123" }]);
    expect(screen.getByRole("heading", { name: "Checking firmware and settings" })).toBeInTheDocument();
    expect(readPendingUpdate()?.job).toBe("job-123");
    currentStatus = { ...currentStatus, phase: "error", recovery_verified: true };
    await vi.advanceTimersByTimeAsync(1000);
    expect(screen.getByRole("heading", { name: "Pedal recovery verified" })).toBeInTheDocument();
    expect(screen.getByRole("dialog")).toHaveTextContent("The update was not installed");
    expect(screen.getByRole("dialog")).toHaveTextContent("Original update error: Pedal did not reconnect");
    expect(onComplete).not.toHaveBeenCalled();
    expect(readPendingUpdate()?.job).toBe("job-123");
    await vi.advanceTimersByTimeAsync(5000);
    expect(commands("HUB_UPDATE_STATUS")).toHaveLength(2);
    await fireEvent.click(screen.getByRole("button", { name: "Done", exact: true }));
    expect(readPendingUpdate()).toBeNull();
    expect(commands("HUB_UPDATE_BEGIN")).toHaveLength(0);
    expect(commands("HUB_UPDATE_COMMIT")).toHaveLength(0);
    expect(commands("HUB_UPDATE_ABORT")).toHaveLength(0);
  });
  it("keeps a failed recovery check and its details available for another physical reconnect", async () => {
    vi.useFakeTimers();
    saveJob();
    currentStatus = { type: "HUB_UPDATE", job: "job-123", phase: "recovery-required" };
    renderDialog();
    await vi.waitFor(() => expect(screen.getByRole("button", { name: "Check recovery" })).toBeEnabled());
    await fireEvent.click(screen.getByRole("button", { name: "Check recovery" }));
    currentStatus = { ...currentStatus, phase: "recovery-required", recovery_verified: false, detail: "The saved configuration does not match the pedal" };
    await vi.advanceTimersByTimeAsync(1000);
    expect(screen.getByRole("dialog")).toHaveTextContent("The saved configuration does not match the pedal");
    expect(screen.getByRole("button", { name: "Check recovery" })).toBeEnabled();
    await fireEvent.click(screen.getByRole("button", { name: "Close", exact: true }));
    expect(readPendingUpdate()).toEqual({ job: "job-123", endpoint });
    expect(commands("HUB_UPDATE_RECOVER")).toHaveLength(1);
  });
  it("retains recovery-required after a refused check without claiming a lost connection", async () => {
    saveJob();
    currentStatus = { type: "HUB_UPDATE", job: "job-123", phase: "recovery-required" };
    const implementation = mocks.sendAndAwait.getMockImplementation()!;
    mocks.sendAndAwait.mockImplementation(request => {
      if (request.type === "HUB_UPDATE_RECOVER") throw new Error("error: The original pedal is not connected");
      return implementation(request);
    });
    renderDialog();
    await fireEvent.click(await screen.findByRole("button", { name: "Check recovery" }));
    expect(await screen.findByRole("alert")).toHaveTextContent("The original pedal is not connected");
    expect(screen.getByRole("heading", { name: "Recovery required" })).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Check recovery" })).toBeEnabled();
    await fireEvent.click(screen.getByRole("button", { name: "Close", exact: true }));
    expect(readPendingUpdate()?.job).toBe("job-123");
  });
  it("recovers a lost check acknowledgement by polling without submitting recovery again", async () => {
    vi.useFakeTimers();
    saveJob();
    currentStatus = { type: "HUB_UPDATE", job: "job-123", phase: "recovery-required" };
    const implementation = mocks.sendAndAwait.getMockImplementation()!;
    mocks.sendAndAwait.mockImplementation(async request => {
      const response = await implementation(request);
      if (request.type === "HUB_UPDATE_RECOVER") throw new Error("connection lost");
      return response;
    });
    renderDialog();
    await vi.waitFor(() => expect(screen.getByRole("button", { name: "Check recovery" })).toBeEnabled());
    await fireEvent.click(screen.getByRole("button", { name: "Check recovery" }));
    await tick();
    expect(screen.getByRole("heading", { name: "Waiting for Raspberry Pi" })).toBeInTheDocument();
    currentStatus = { ...currentStatus, phase: "error", recovery_verified: true };
    await vi.advanceTimersByTimeAsync(1000);
    expect(screen.getByRole("heading", { name: "Pedal recovery verified" })).toBeInTheDocument();
    expect(commands("HUB_UPDATE_RECOVER")).toHaveLength(1);
    expect(commands("HUB_UPDATE_STATUS")).toHaveLength(2);
    expect(readPendingUpdate()?.job).toBe("job-123");
  });
  it("disables recovery checking when the active connection no longer matches", async () => {
    saveJob();
    currentStatus = { type: "HUB_UPDATE", job: "job-123", phase: "recovery-required" };
    const isCurrentConnection = vi.fn(() => true);
    const view = render(UnifiedFirmwareUpdate, {
      manifest, endpoint, isCurrentConnection, onClose: vi.fn(), onJob: vi.fn(), onComplete: vi.fn(),
    });
    await screen.findByRole("button", { name: "Check recovery" });
    await view.rerender({ isCurrentConnection: () => false });
    const button = screen.getByRole("button", { name: "Check recovery" });
    expect(button).toBeDisabled();
    await fireEvent.click(button);
    expect(commands("HUB_UPDATE_RECOVER")).toHaveLength(0);
    expect(readPendingUpdate()?.job).toBe("job-123");
  });
  it.each(["done", "error", "rolled-back", "aborted"])("clears a finished %s job when its result is acknowledged", async phase => {
    saveJob();
    currentStatus = { type: "HUB_UPDATE", job: "job-123", phase };
    renderDialog();
    await fireEvent.click(await screen.findByRole("button", { name: "Done", exact: true }));
    expect(readPendingUpdate()).toBeNull();
  });
});

async function renderConnectedApp(fw: string) {
  bundledManifest = manifest;
  localStorage.setItem("BOSUN_ONBOARDED", "1");
  localStorage.setItem("BOSUN_CONNECTION", JSON.stringify({ mode: "network", host: "bosun-hub.local", port: "9876" }));
  render(App);
  await waitFor(() => expect(mocks.lifecycle).toHaveBeenCalledOnce());
  await screen.findByTitle(`Connected on ${endpoint}`);
  mocks.onFirmwareMessage.mock.calls[0][0]({
    type: "DEVICE_INFO", fw, device: "MIDI Captain", profile: "kemper",
    current: { bank: 1, slot: 1 },
    ...(fw.includes("native") ? { native_experimental: true, firmware_ota: false } : {}),
  });
  await tick();
}

describe("App unified update entry points", () => {
  it.each(["0.6.4", "0.1.0-native-experimental"])("offers the same Bosun update flow to %s", async fw => {
    await renderConnectedApp(fw);
    const button = await screen.findByRole("button", { name: `Update Bosun (${fw} -> 0.6.5)` });
    expect(screen.queryByRole("button", { name: /^Update firmware \(/ })).not.toBeInTheDocument();
    await fireEvent.click(button);
    expect(screen.getByRole("dialog")).toHaveTextContent("to 0.6.5");
    expect(commands("HUB_UPDATE_BEGIN")).toHaveLength(0);
  });
  it("offers the unified update from Maintenance on native firmware", async () => {
    await renderConnectedApp("0.1.0-native-experimental");
    await fireEvent.click(screen.getByRole("button", { name: "Menu", exact: true }));
    await fireEvent.click(within(screen.getByRole("navigation")).getByRole("button", { name: /Maintenance$/ }));
    await fireEvent.click(await screen.findByRole("button", { name: "Update Bosun to 0.6.5" }));
    expect(screen.getByRole("dialog")).toHaveTextContent("to 0.6.5");
    expect(screen.queryByRole("button", { name: "Update from bundled" })).not.toBeInTheDocument();
    expect(screen.queryByRole("button", { name: /^From folder/ })).not.toBeInTheDocument();
  });
  it("does not advertise the new bundle when the hub cannot perform the update", async () => {
    mocks.sendAndAwait.mockResolvedValue({ type: "HUB_UPDATE", phase: "ready", supported: false });
    await renderConnectedApp("0.1.0-native-experimental");
    expect(screen.queryByRole("button", { name: /^Update Bosun/ })).not.toBeInTheDocument();
    expect(screen.queryByRole("button", { name: /^Update firmware \(/ })).not.toBeInTheDocument();
  });
  it("automatically reopens a saved update only on its original endpoint", async () => {
    saveJob();
    await renderConnectedApp("0.1.0-native-experimental");
    expect(await screen.findByRole("heading", { name: "Installing Bosun" })).toBeInTheDocument();
    expect(commands("HUB_UPDATE_STATUS")).toHaveLength(2);
    expect(commands("HUB_UPDATE_BEGIN")).toHaveLength(0);
  });
  it("resumes a maintenance job without asking the unavailable Captain for its normal bootstrap", async () => {
    saveJob();
    mocks.readNetworkBootstrap.mockRejectedValue(new Error("error: maintenance_busy"));
    await renderConnectedApp("0.1.0-native-experimental");
    await screen.findByRole("heading", { name: "Installing Bosun" });
    expect(mocks.readNetworkBootstrap).not.toHaveBeenCalled();
    expect(mocks.cmd.getManifest).not.toHaveBeenCalled();
    expect(mocks.cmd.getStats).not.toHaveBeenCalled();
    expect(backendConnected).toBe(true);
    await fireEvent.click(screen.getByRole("button", { name: "Close", exact: true }));
    expect(screen.getByRole("heading", { name: "Bosun update on Raspberry Pi" })).toBeInTheDocument();
    expect(screen.queryByRole("heading", { name: "Welcome back" })).not.toBeInTheDocument();

    // A later successful status unlocks the ordinary editor and performs its
    // normal configuration reads exactly once.
    currentStatus = { type: "HUB_UPDATE", job: "job-123", phase: "done", release: "0.6.5" };
    mocks.readNetworkBootstrap.mockResolvedValue({ profiles: [], active: null });
    await fireEvent.click(screen.getAllByRole("button", { name: "Check Bosun update" })[0]);
    await screen.findByRole("heading", { name: "Bosun updated" });
    await waitFor(() => expect(mocks.readNetworkBootstrap).toHaveBeenCalledOnce());
    expect(await screen.findByRole("heading", { name: "Welcome back" })).toBeInTheDocument();
    expect(backendConnected).toBe(true);
    expect(commands("HUB_UPDATE_BEGIN")).toHaveLength(0);
  });
  it("keeps a recovery result and backup reachable while the Captain cannot bootstrap", async () => {
    saveJob();
    currentStatus = { type: "HUB_UPDATE", job: "job-123", phase: "recovery-required", backup: "/var/lib/bosun/backups/job-123" };
    mocks.readNetworkBootstrap.mockRejectedValue(new Error("error: link_down"));
    await renderConnectedApp("0.1.0-native-experimental");
    await screen.findByRole("heading", { name: "Recovery required" });
    expect(mocks.readNetworkBootstrap).not.toHaveBeenCalled();
    await fireEvent.click(screen.getByRole("button", { name: "Close", exact: true }));
    expect(screen.queryByRole("dialog")).not.toBeInTheDocument();
    expect(readPendingUpdate()).toEqual({ job: "job-123", endpoint });
    await fireEvent.click(screen.getAllByRole("button", { name: "Check Bosun update" })[0]);
    expect(await screen.findByRole("heading", { name: "Recovery required" })).toBeInTheDocument();
    expect(screen.getByText("/var/lib/bosun/backups/job-123")).toBeInTheDocument();
    expect(backendConnected).toBe(true);
    expect(mocks.readNetworkBootstrap).not.toHaveBeenCalled();
  });
  it("returns to the editor after the original pedal passes recovery verification and the result is acknowledged", async () => {
    saveJob();
    currentStatus = { type: "HUB_UPDATE", job: "job-123", phase: "recovery-required" };
    await renderConnectedApp("0.1.0-native-experimental");
    await fireEvent.click(await screen.findByRole("button", { name: "Check recovery" }));
    expect(mocks.readNetworkBootstrap).not.toHaveBeenCalled();
    currentStatus = { ...currentStatus, phase: "error", recovery_verified: true };
    await screen.findByRole("heading", { name: "Pedal recovery verified" }, { timeout: 2000 });
    expect(mocks.readNetworkBootstrap).not.toHaveBeenCalled();
    expect(readPendingUpdate()?.job).toBe("job-123");
    await fireEvent.click(screen.getByRole("button", { name: "Done", exact: true }));
    await waitFor(() => expect(mocks.readNetworkBootstrap).toHaveBeenCalledOnce());
    expect(await screen.findByRole("heading", { name: "Welcome back" })).toBeInTheDocument();
    expect(readPendingUpdate()).toBeNull();
    expect(backendConnected).toBe(true);
    expect(commands("HUB_UPDATE_RECOVER")).toHaveLength(1);
    expect(commands("HUB_UPDATE_COMMIT")).toHaveLength(0);
  });
  it("does not reopen another Raspberry Pi's saved update", async () => {
    saveJob("tcp://other-hub.local:9876");
    await renderConnectedApp("0.1.0-native-experimental");
    expect(screen.queryByRole("dialog")).not.toBeInTheDocument();
    expect(commands("HUB_UPDATE_STATUS")).toHaveLength(0);
  });
});
