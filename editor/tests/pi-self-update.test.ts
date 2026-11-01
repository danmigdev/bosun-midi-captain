// "Update Bosun" through a Pi that can update itself: the signed Pi package
// first (across the hub restart), then the Captain firmware.
import { createHash, webcrypto } from "node:crypto";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { cleanup, fireEvent, render, screen, waitFor } from "@testing-library/svelte";
import { tick } from "svelte";
import App from "../src/App.svelte";
import UnifiedFirmwareUpdate from "../src/components/UnifiedFirmwareUpdate.svelte";
import {
  confirmPiRelease, hubInfo, PENDING_UPDATE_KEY, PiVersionMismatchError, planUnifiedUpdate,
  readPendingUpdate, resumeUnifiedUpdate, startPiUpdate,
  type BundledUpdateManifest, type HubInfo,
} from "../src/lib/unified-update";

const mocks = vi.hoisted(() => ({
  invoke: vi.fn(), sendAndAwait: vi.fn(), lifecycle: vi.fn(),
  onFirmwareMessage: vi.fn(), onDisconnected: vi.fn(), readNetworkBootstrap: vi.fn(),
  cmd: {
    getDeviceInfo: vi.fn(), getManifest: vi.fn(), getManifestAwait: vi.fn(),
    listProfiles: vi.fn(), listPatches: vi.fn(), getDirty: vi.fn(),
    getGlobal: vi.fn(), getStats: vi.fn(),
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
  detectPedal: vi.fn(async () => ({ bootloader_drive: null, circuitpy_drive: null, usb_pedal_present: false })),
}));
vi.mock("../src/lib/android-lifecycle", () => ({
  onLifecycleChange: mocks.lifecycle, onBackButton: vi.fn(() => () => {}),
  saveSessionState: vi.fn(), restoreSessionState: vi.fn(() => null),
}));

const endpoint = "tcp://bosun-hub.local:9876";
const SIGNATURE = "-----BEGIN SSH SIGNATURE-----\nU1NIU0lHAAAAAQ==\n-----END SSH SIGNATURE-----\n";
const manifest: BundledUpdateManifest = {
  schema: 1, board: "midi-captain-rp2040", release: "0.6.5", family: "native",
  firmware_version: "0.6.5", flash_bytes: 8388608, firmware_sha256: "a".repeat(64),
};
function fixture(size: number, seed: number) {
  const bytes = Buffer.from(Array.from({ length: size }, (_, i) => (i * seed + 0x1f) % 256));
  return { data: bytes.toString("base64"), size, sha256: createHash("sha256").update(bytes).digest("hex") };
}

interface Job { job: string; kind?: "pi"; phase: string; size: number; sha256: string; received: number;
  version?: string; error?: string; release?: string }
let captainBundle = fixture(20000, 3);
let piBundle: ReturnType<typeof fixture> & { signature: string } = { ...fixture(30000, 7), signature: SIGNATURE };
let signedPiBundled = true;
let bundledManifest: BundledUpdateManifest | null = manifest;
let backendConnected = false;
// A fake hub: HUB_INFO, the update jobs and the Captain behind it.
const hub = { online: true, version: "0.6.4" as string | undefined, piUpdate: true, fw: "0.6.4", seq: 0, jobs: new Map<string, Job>() };
let linkUp = true;

const piJob = () => [...hub.jobs.values()].find(job => job.kind === "pi")!;
const captainJob = () => [...hub.jobs.values()].find(job => !job.kind)!;
const commands = (type: string) => mocks.sendAndAwait.mock.calls.filter(([request]) => request.type === type).map(([request]) => request);
const commandOrder = () => mocks.sendAndAwait.mock.calls.map(([request]) => request.type as string);

function respond(request: Record<string, any>) {
  if (!hub.online) throw new Error("error: disconnected");
  const reply = (job: Job) => ({ type: "HUB_UPDATE", ...job });
  switch (request.type) {
    case "HUB_INFO":
      // An older hub forwards it to the Captain, which refuses it.
      if (hub.version === undefined) throw new Error("error: unknown_command");
      return { type: "HUB_INFO", version: hub.version, pi_update: hub.piUpdate };
    case "HUB_UPDATE_INFO": return { type: "HUB_UPDATE", phase: "ready", supported: true };
    case "GET_DEVICE_INFO": return { type: "DEVICE_INFO", fw: hub.fw, device: "MIDI Captain", current: { bank: 1, slot: 1 } };
    case "HUB_UPDATE_BEGIN": {
      const kind = request.kind === "pi" ? "pi" as const : undefined;
      const job: Job = { job: `${kind ?? "captain"}-job-${++hub.seq}`, ...(kind ? { kind } : {}),
        phase: "uploading", size: request.size, sha256: request.sha256, received: 0 };
      hub.jobs.set(job.job, job);
      return reply(job);
    }
    case "HUB_UPDATE_CHUNK": {
      const job = hub.jobs.get(request.job)!;
      job.received = request.offset + atob(request.data).length;
      return reply(job);
    }
    case "HUB_UPDATE_COMMIT": {
      const job = hub.jobs.get(request.job)!;
      job.phase = job.kind === "pi" ? "installing" : "queued";
      return reply(job);
    }
    case "HUB_UPDATE_STATUS": {
      const job = hub.jobs.get(request.job);
      if (!job) throw new Error("error: Unknown update job");
      return reply(job);
    }
    default: throw new Error(`Unexpected protocol command: ${request.type}`);
  }
}

beforeEach(() => {
  vi.stubGlobal("crypto", webcrypto);
  captainBundle = fixture(20000, 3);
  piBundle = { ...fixture(30000, 7), signature: SIGNATURE };
  signedPiBundled = true;
  bundledManifest = manifest;
  backendConnected = false;
  Object.assign(hub, { online: true, version: "0.6.4", piUpdate: true, fw: "0.6.4", seq: 0, jobs: new Map() });
  linkUp = true;
  mocks.invoke.mockReset().mockImplementation(async (name: string) => {
    if (name === "read_bundled_update") return captainBundle;
    if (name === "bundled_update_manifest") return bundledManifest;
    if (name === "read_bundled_pi_update") return piBundle;
    if (name === "bundled_pi_update") return signedPiBundled ? { size: piBundle.size, sha256: piBundle.sha256 } : null;
    if (name === "export_pi_setup") return "C:\\Users\\A\\bosun-pi-setup.tar.gz";
    if (name === "is_connected") return backendConnected;
    if (name === "tcp_connect") { backendConnected = true; return; }
    if (name === "disconnect") { backendConnected = false; return; }
    if (["discover_hubs", "list_ports", "tcp_list_ports"].includes(name)) return [];
    if (name === "auto_connect") throw new Error("No USB pedal connected");
    throw new Error(`Unexpected IPC: ${name}`);
  });
  mocks.sendAndAwait.mockReset().mockImplementation(async (request: Record<string, any>) => respond(request));
  mocks.lifecycle.mockReset().mockImplementation(() => () => {});
  mocks.onFirmwareMessage.mockReset().mockImplementation(async () => () => {});
  mocks.onDisconnected.mockReset().mockImplementation(async () => () => {});
  mocks.readNetworkBootstrap.mockReset().mockResolvedValue({ profiles: [], active: null });
  for (const command of Object.values(mocks.cmd)) command.mockReset().mockResolvedValue({});
  mocks.cmd.listProfiles.mockResolvedValue({ profiles: [] });
  mocks.cmd.getStats.mockResolvedValue({
    uptime_ms: 1000, midi_rx_count: 0, midi_tx_count: 0, midi_tx_failed: 0,
    queue_overflows: 0, unsupported_messages: 0, invalid_messages: 0,
    protocol_errors: 0, storage_errors: 0, midi_events_dropped: 0, storage_ready: true,
  });
});

afterEach(() => { cleanup(); vi.useRealTimers(); vi.unstubAllGlobals(); });

const options = () => ({ endpoint, isCurrentConnection: vi.fn(() => true), onStatus: vi.fn(), onJob: vi.fn() });
const piHub: HubInfo = { version: "0.6.4", pi_update: true };
const piPackage = () => ({ size: piBundle.size, sha256: piBundle.sha256 });

describe("Pi self-update plan and transport", () => {
  it("updates the Pi only when it is older, can update itself and a signed package is bundled", () => {
    expect(planUnifiedUpdate("0.6.4", manifest, piHub, piPackage())).toEqual({ pi: true, piManual: false, captain: true });
    expect(planUnifiedUpdate("0.6.5", manifest, piHub, piPackage())).toEqual({ pi: true, piManual: false, captain: false });
    expect(planUnifiedUpdate("0.6.4", manifest, { version: "0.6.5", pi_update: true }, piPackage()))
      .toEqual({ pi: false, piManual: false, captain: true });
    // Unsigned local build, or an older hub without self-update: manual once.
    expect(planUnifiedUpdate("0.6.4", manifest, piHub, null)).toEqual({ pi: false, piManual: true, captain: true });
    expect(planUnifiedUpdate("0.6.4", manifest, { pi_update: false }, piPackage())).toEqual({ pi: false, piManual: true, captain: true });
    expect(planUnifiedUpdate("0.6.4", manifest, null, piPackage())).toEqual({ pi: false, piManual: false, captain: true });
  });

  it("treats a HUB_INFO refusal, an odd reply or an invalid version as an older hub", async () => {
    expect(await hubInfo()).toEqual({ version: "0.6.4", pi_update: true });
    hub.version = undefined;
    expect(await hubInfo()).toEqual({ pi_update: false });
    mocks.sendAndAwait.mockResolvedValueOnce({ type: "ACK" });
    expect(await hubInfo()).toEqual({ pi_update: false });
    mocks.sendAndAwait.mockResolvedValueOnce({ type: "HUB_INFO", version: "dev", pi_update: true });
    expect(await hubInfo()).toEqual({ pi_update: false });
    mocks.sendAndAwait.mockRejectedValueOnce(new Error("timeout: HUB_INFO#1"));
    expect(await hubInfo()).toEqual({ pi_update: false });
  });

  it("uploads the signed Pi package as a pi job and saves its kind", async () => {
    const result = await startPiUpdate(options());
    expect(result.phase).toBe("installing");
    expect(commands("HUB_UPDATE_BEGIN")).toEqual([{ type: "HUB_UPDATE_BEGIN", kind: "pi", signature: SIGNATURE, size: piBundle.size, sha256: piBundle.sha256 }]);
    const chunks = commands("HUB_UPDATE_CHUNK");
    expect(Buffer.concat(chunks.map(chunk => Buffer.from(chunk.data, "base64"))).toString("base64")).toBe(piBundle.data);
    expect(commands("HUB_UPDATE_COMMIT")).toHaveLength(1);
    expect(JSON.parse(localStorage.getItem(PENDING_UPDATE_KEY)!)).toEqual({ job: "pi-job-1", endpoint, kind: "pi" });
    expect(readPendingUpdate()).toEqual({ job: "pi-job-1", endpoint, kind: "pi" });
  });

  it.each([
    ["an older hub", () => { hub.version = undefined; }],
    ["a hub without the root installer", () => { hub.piUpdate = false; }],
  ])("never uploads the Pi package to %s", async (_, setup) => {
    setup();
    await expect(startPiUpdate(options())).rejects.toThrow(/Setup guide command/);
    expect(commands("HUB_UPDATE_BEGIN")).toHaveLength(0);
    expect(mocks.invoke).not.toHaveBeenCalledWith("read_bundled_pi_update");
  });

  it.each(["", "not a signature", "-----BEGIN SSH SIGNATURE-----\n" + "A".repeat(5000) + "\n-----END SSH SIGNATURE-----\n"])(
    "refuses an invalid bundled signature before BEGIN", async signature => {
      piBundle.signature = signature;
      await expect(startPiUpdate(options())).rejects.toThrow(/signature is invalid/);
      expect(commands("HUB_UPDATE_BEGIN")).toHaveLength(0);
    });

  it("resumes an interrupted Pi upload from the hub offset with the Pi package", async () => {
    localStorage.setItem(PENDING_UPDATE_KEY, JSON.stringify({ job: "pi-job-1", endpoint, kind: "pi" }));
    hub.jobs.set("pi-job-1", { job: "pi-job-1", kind: "pi", phase: "uploading", size: piBundle.size, sha256: piBundle.sha256, received: 12288 });
    expect((await resumeUnifiedUpdate(options())).phase).toBe("installing");
    expect(commands("HUB_UPDATE_CHUNK").map(chunk => chunk.offset)).toEqual([12288, 24576]);
    expect(mocks.invoke).not.toHaveBeenCalledWith("read_bundled_update");
  });

  it("drops a saved job of an unknown kind", () => {
    localStorage.setItem(PENDING_UPDATE_KEY, JSON.stringify({ job: "x", endpoint, kind: "other" }));
    expect(readPendingUpdate()).toBeNull();
  });

  it("requires the exact release after the Pi update", async () => {
    hub.version = "0.6.5";
    expect(await confirmPiRelease("0.6.5")).toEqual({ version: "0.6.5", pi_update: true });
    hub.version = "0.6.4";
    await expect(confirmPiRelease("0.6.5")).rejects.toBeInstanceOf(PiVersionMismatchError);
    hub.version = undefined;
    await expect(confirmPiRelease("0.6.5")).rejects.toThrow(/unknown version/);
  });
});

function renderDialog(props: Record<string, unknown> = {}) {
  const callbacks = { onClose: vi.fn(), onJob: vi.fn(), onComplete: vi.fn() };
  render(UnifiedFirmwareUpdate, {
    manifest, installed: "0.6.4", endpoint, hub: piHub, piPackage: piPackage(),
    isCurrentConnection: () => linkUp, ...callbacks, ...props,
  });
  return callbacks;
}
const dialog = () => screen.getByRole("dialog");
const heading = () => screen.getByRole("heading").textContent;

async function finishPi(version = "0.6.5", phase = "done", extra: Partial<Job> = {}) {
  Object.assign(piJob(), { phase, version, ...extra });
  if (phase === "done") hub.version = version;
}

describe("Update Bosun through a self-updating Pi", () => {
  it("updates the Pi, follows it across the hub restart, checks its release, then updates the pedal", async () => {
    vi.useFakeTimers();
    const { onJob, onComplete } = renderDialog();
    expect(dialog()).toHaveTextContent("Update Bosun on the Raspberry Pi and the pedal to 0.6.5");
    expect(dialog()).toHaveTextContent("The Raspberry Pi is updated first and restarts, then the pedal.");
    await fireEvent.click(screen.getByRole("button", { name: "Update", exact: true }));
    await vi.waitFor(() => expect(commands("HUB_UPDATE_COMMIT")).toHaveLength(1));
    expect(heading()).toBe("Updating the Raspberry Pi");
    expect(readPendingUpdate()).toEqual({ job: "pi-job-1", endpoint, kind: "pi" });
    expect(onJob).toHaveBeenLastCalledWith({ job: "pi-job-1", endpoint, kind: "pi" });

    // The install restarts the hub: the link drops and comes back.
    hub.online = false; linkUp = false;
    await vi.advanceTimersByTimeAsync(1000);
    expect(heading()).toBe("Waiting for Raspberry Pi");
    expect(dialog()).toHaveTextContent("restarting to finish its update");
    await vi.advanceTimersByTimeAsync(10_000);
    expect(readPendingUpdate()?.job).toBe("pi-job-1");
    hub.online = true; linkUp = true;
    await finishPi();
    hub.fw = "0.6.4";
    await vi.advanceTimersByTimeAsync(1000);
    await vi.advanceTimersByTimeAsync(1000);
    await vi.waitFor(() => expect(commands("HUB_UPDATE_COMMIT")).toHaveLength(2));

    const begins = commands("HUB_UPDATE_BEGIN");
    expect(begins).toHaveLength(2);
    expect(begins[0].kind).toBe("pi");
    expect(begins[1]).toEqual({ type: "HUB_UPDATE_BEGIN", size: captainBundle.size, sha256: captainBundle.sha256 });
    // Pi first, its release confirmed, the pedal read, and only then the pedal job.
    const order = commandOrder();
    const piCommit = order.indexOf("HUB_UPDATE_COMMIT");
    const captainBegin = order.lastIndexOf("HUB_UPDATE_BEGIN");
    expect(order.slice(piCommit, captainBegin)).toEqual(expect.arrayContaining(["HUB_INFO", "GET_DEVICE_INFO"]));
    expect(order.slice(piCommit, captainBegin).lastIndexOf("HUB_INFO")).toBeLessThan(order.slice(piCommit, captainBegin).lastIndexOf("GET_DEVICE_INFO"));
    expect(readPendingUpdate()).toEqual({ job: "captain-job-2", endpoint });
    expect(onJob).toHaveBeenLastCalledWith({ job: "captain-job-2", endpoint });
    expect(onJob).not.toHaveBeenCalledWith(null);
    expect(onComplete).not.toHaveBeenCalled();

    Object.assign(captainJob(), { phase: "done", release: "0.6.5" });
    await vi.advanceTimersByTimeAsync(1000);
    expect(heading()).toBe("Bosun updated");
    expect(onComplete).toHaveBeenCalledOnce();
    await fireEvent.click(screen.getByRole("button", { name: "Done", exact: true }));
    expect(readPendingUpdate()).toBeNull();
  });

  it("finishes after the Pi when the pedal already runs the release", async () => {
    vi.useFakeTimers();
    hub.fw = "0.6.5";
    const { onComplete } = renderDialog({ installed: "0.6.5" });
    expect(dialog()).toHaveTextContent("Update Bosun on the Raspberry Pi from 0.6.4 to 0.6.5. The pedal is already up to date.");
    await fireEvent.click(screen.getByRole("button", { name: "Update", exact: true }));
    await vi.waitFor(() => expect(commands("HUB_UPDATE_COMMIT")).toHaveLength(1));
    await finishPi();
    await vi.advanceTimersByTimeAsync(2000);
    expect(heading()).toBe("Bosun updated");
    expect(dialog()).toHaveTextContent("The Raspberry Pi runs Bosun 0.6.5. The pedal is already up to date");
    expect(commands("HUB_UPDATE_BEGIN")).toHaveLength(1);
    expect(onComplete).toHaveBeenCalledOnce();
    await fireEvent.click(screen.getByRole("button", { name: "Done", exact: true }));
    expect(readPendingUpdate()).toBeNull();
  });

  it("explains a rollback plainly and offers the pedal update on its own", async () => {
    vi.useFakeTimers();
    renderDialog();
    await fireEvent.click(screen.getByRole("button", { name: "Update", exact: true }));
    await vi.waitFor(() => expect(commands("HUB_UPDATE_COMMIT")).toHaveLength(1));
    await finishPi("0.6.4", "rolled-back", { error: "Stage did not start after the update" });
    await vi.advanceTimersByTimeAsync(1000);
    expect(heading()).toBe("Raspberry Pi update undone");
    expect(dialog()).toHaveTextContent("could not install Bosun 0.6.5 and went back to its previous version (0.6.4). The pedal was not changed.");
    expect(dialog()).toHaveTextContent("Stage did not start after the update");
    await vi.advanceTimersByTimeAsync(5000);
    expect(commands("HUB_UPDATE_BEGIN")).toHaveLength(1);
    expect(commands("HUB_INFO")).toHaveLength(1);

    await fireEvent.click(screen.getByRole("button", { name: "Update pedal only" }));
    await vi.waitFor(() => expect(commands("HUB_UPDATE_COMMIT")).toHaveLength(2));
    expect(commands("HUB_UPDATE_BEGIN")[1]).not.toHaveProperty("kind");
    expect(readPendingUpdate()).toEqual({ job: "captain-job-2", endpoint });
  });

  it("reports a stopped Pi update without touching the pedal", async () => {
    vi.useFakeTimers();
    renderDialog();
    await fireEvent.click(screen.getByRole("button", { name: "Update", exact: true }));
    await vi.waitFor(() => expect(commands("HUB_UPDATE_COMMIT")).toHaveLength(1));
    await finishPi("0.6.4", "error", { error: "The package signature is not valid" });
    await vi.advanceTimersByTimeAsync(1000);
    expect(heading()).toBe("Raspberry Pi update stopped");
    expect(dialog()).toHaveTextContent("The Raspberry Pi did not install the update. The pedal was not changed.");
    expect(dialog()).toHaveTextContent("The package signature is not valid");
    expect(commands("HUB_UPDATE_BEGIN")).toHaveLength(1);
    await fireEvent.click(screen.getByRole("button", { name: "Done", exact: true }));
    expect(readPendingUpdate()).toBeNull();
  });

  it("stops before the pedal when the Pi reports another release after its update", async () => {
    vi.useFakeTimers();
    renderDialog();
    await fireEvent.click(screen.getByRole("button", { name: "Update", exact: true }));
    await vi.waitFor(() => expect(commands("HUB_UPDATE_COMMIT")).toHaveLength(1));
    Object.assign(piJob(), { phase: "done", version: "0.6.5" });
    hub.version = "0.6.4";
    await vi.advanceTimersByTimeAsync(2000);
    expect(heading()).toBe("Raspberry Pi version mismatch");
    expect(dialog()).toHaveTextContent("After its update the Raspberry Pi reports Bosun 0.6.4 instead of 0.6.5. The pedal was not updated.");
    await vi.advanceTimersByTimeAsync(5000);
    expect(commands("HUB_UPDATE_BEGIN")).toHaveLength(1);
    expect(commands("GET_DEVICE_INFO")).toHaveLength(0);
    expect(screen.getByRole("button", { name: "Update pedal only" })).toBeEnabled();
  });

  it("asks again before the pedal update when the Pi job is resumed after an app restart", async () => {
    vi.useFakeTimers();
    localStorage.setItem(PENDING_UPDATE_KEY, JSON.stringify({ job: "pi-job-1", endpoint, kind: "pi" }));
    hub.jobs.set("pi-job-1", { job: "pi-job-1", kind: "pi", phase: "installing", size: piBundle.size, sha256: piBundle.sha256, received: piBundle.size });
    renderDialog({ installed: undefined });
    await vi.waitFor(() => expect(heading()).toBe("Updating the Raspberry Pi"));
    await finishPi();
    await vi.advanceTimersByTimeAsync(2000);
    expect(heading()).toBe("Update the pedal");
    expect(dialog()).toHaveTextContent("Update the pedal from 0.6.4 to 0.6.5 now.");
    await vi.advanceTimersByTimeAsync(5000);
    expect(commands("HUB_UPDATE_BEGIN")).toHaveLength(0);
    await fireEvent.click(screen.getByRole("button", { name: "Update pedal" }));
    await vi.waitFor(() => expect(commands("HUB_UPDATE_COMMIT")).toHaveLength(1));
    expect(commands("HUB_UPDATE_BEGIN")[0]).not.toHaveProperty("kind");
  });

  it("shows the Setup guide command for an older hub and still updates the pedal", async () => {
    hub.version = undefined;
    renderDialog({ hub: { pi_update: false } });
    expect(dialog()).toHaveTextContent("This Raspberry Pi cannot update itself yet.");
    expect(dialog()).toHaveTextContent("It runs an older Bosun version.");
    expect(dialog()).toHaveTextContent("You can still update the pedal now.");
    await fireEvent.click(screen.getByRole("button", { name: "Save Raspberry Pi package" }));
    await fireEvent.input(screen.getByLabelText("Username on the Pi"), { target: { value: "musician" } });
    const command = await screen.findByText(/^scp /);
    expect(command.textContent).toContain("'C:\\Users\\A\\bosun-pi-setup.tar.gz'");
    expect(command.textContent).toContain("'musician@bosun-hub.local:bosun-pi-setup.tar.gz'");
    expect(command.textContent).toContain("quick-install.sh");
    await fireEvent.click(screen.getByRole("button", { name: "Update", exact: true }));
    await waitFor(() => expect(commands("HUB_UPDATE_COMMIT")).toHaveLength(1));
    expect(commands("HUB_UPDATE_BEGIN")).toEqual([{ type: "HUB_UPDATE_BEGIN", size: captainBundle.size, sha256: captainBundle.sha256 }]);
    expect(mocks.invoke).not.toHaveBeenCalledWith("read_bundled_pi_update");
  });

  it("names an unsigned build instead of blaming the Pi", () => {
    renderDialog({ piPackage: null });
    expect(dialog()).toHaveTextContent("This Bosun build cannot update the Raspberry Pi.");
    expect(dialog()).toHaveTextContent("It runs Bosun 0.6.4.");
  });
});

async function renderConnectedApp(fw: string) {
  localStorage.setItem("BOSUN_ONBOARDED", "1");
  localStorage.setItem("BOSUN_CONNECTION", JSON.stringify({ mode: "network", host: "bosun-hub.local", port: "9876" }));
  render(App);
  await vi.waitFor(() => expect(mocks.lifecycle).toHaveBeenCalledOnce());
  await vi.waitFor(() => expect(screen.getByTitle(`Connected on ${endpoint}`)).toBeInTheDocument());
  mocks.onFirmwareMessage.mock.calls[0][0]({
    type: "DEVICE_INFO", fw, device: "MIDI Captain", profile: "kemper", current: { bank: 1, slot: 1 },
  });
  await tick();
}

describe("App Pi self-update entry points", () => {
  it("offers the Pi update when only the Pi is older", async () => {
    hub.fw = "0.6.5";
    await renderConnectedApp("0.6.5");
    await fireEvent.click(await screen.findByRole("button", { name: "Update Raspberry Pi (0.6.4 -> 0.6.5)" }));
    expect(dialog()).toHaveTextContent("Update Bosun on the Raspberry Pi from 0.6.4 to 0.6.5.");
  });

  it("keeps the usual pedal update label when both are older", async () => {
    await renderConnectedApp("0.6.4");
    expect(await screen.findByRole("button", { name: "Update Bosun (0.6.4 -> 0.6.5)" })).toBeInTheDocument();
  });

  it("does not offer a Pi-only update from a build without a signed Pi package", async () => {
    signedPiBundled = false;
    hub.fw = "0.6.5";
    await renderConnectedApp("0.6.5");
    await tick();
    expect(screen.queryByRole("button", { name: /^Update Raspberry Pi/ })).not.toBeInTheDocument();
    expect(screen.queryByRole("button", { name: /^Update Bosun/ })).not.toBeInTheDocument();
  });

  it("keeps reconnecting to the same Pi while its update restarts the hub", async () => {
    vi.useFakeTimers();
    localStorage.setItem(PENDING_UPDATE_KEY, JSON.stringify({ job: "pi-job-1", endpoint, kind: "pi" }));
    hub.jobs.set("pi-job-1", { job: "pi-job-1", kind: "pi", phase: "installing", size: piBundle.size, sha256: piBundle.sha256, received: piBundle.size });
    await renderConnectedApp("0.6.4");
    await vi.waitFor(() => expect(screen.getByRole("heading", { name: "Updating the Raspberry Pi" })).toBeInTheDocument());
    const implementation = mocks.invoke.getMockImplementation()!;
    let hubDown = true;
    mocks.invoke.mockImplementation(async (name: string, args?: unknown) => {
      if (name === "tcp_connect" && hubDown) throw new Error("Connection refused");
      return implementation(name, args);
    });
    hub.online = false;
    backendConnected = false;
    mocks.onDisconnected.mock.calls[0][0]();
    // Longer than the 20 s budget of an ordinary network drop.
    await vi.advanceTimersByTimeAsync(45_000);
    expect(screen.queryByRole("alert")).not.toBeInTheDocument();
    hubDown = false; hub.online = true;
    await finishPi();
    await vi.advanceTimersByTimeAsync(5000);
    expect(screen.getByTitle(`Connected on ${endpoint}`)).toBeInTheDocument();
    const connects = mocks.invoke.mock.calls.filter(([name, args]) => name === "tcp_connect" && (args as { addr: string }).addr === "bosun-hub.local:9876");
    expect(connects.length).toBeGreaterThan(10);
    await vi.advanceTimersByTimeAsync(3000);
    expect(screen.getByRole("heading", { name: "Update the pedal" })).toBeInTheDocument();
  });
});
