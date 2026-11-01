import { invoke } from "@tauri-apps/api/core";
import { sendAndAwait } from "./protocol";
import { compareVersions } from "./firmware-update";

export interface BundledUpdateManifest {
  schema: 1;
  board: "midi-captain-rp2040";
  release: string;
  family: "native";
  firmware_version: string;
  flash_bytes: 8388608;
  firmware_sha256: string;
}

export interface HubUpdateStatus {
  type: "HUB_UPDATE";
  job?: string;
  phase: string;
  received?: number;
  size?: number;
  error?: string;
  detail?: string;
  backup?: string;
  release?: string;
  firmware_version?: string;
  sha256?: string;
  supported?: boolean;
  recovery_verified?: boolean;
  recovery_error?: string;
  /** "pi" for a Raspberry Pi software job; absent for the Captain firmware. */
  kind?: string;
  /** Pi jobs: the hub release installed (or restored) by the root installer. */
  version?: string | null;
}

/** HUB_INFO of a 0.8.6+ hub. Older hubs leave the version unknown. */
export interface HubInfo { version?: string; pi_update: boolean }
/** The signed Raspberry Pi package bundled with this Desktop build. */
export interface BundledPiUpdate { size: number; sha256: string }
export interface UpdatePlan {
  /** Update the Pi software first, from the bundled signed package. */
  pi: boolean;
  /** The Pi is older but cannot update itself: show the Setup guide command. */
  piManual: boolean;
  captain: boolean;
}

export interface PendingUpdate { job: string; endpoint: string; kind?: "pi" }
export const PENDING_UPDATE_KEY = "BOSUN_PENDING_UPDATE";
const CHUNK_BYTES = 12 * 1024;
const MAX_PACKAGE_BYTES = 4 * 1024 * 1024;
const MAX_SIGNATURE_BYTES = 4 * 1024;
const SHA256 = /^[a-f0-9]{64}$/i;
const RELEASE = /^\d+\.\d+\.\d+(?:-[a-z0-9.-]+)?$/i;
const SIGNATURE = /^-----BEGIN SSH SIGNATURE-----\r?\n[A-Za-z0-9+/=\r\n]+-----END SSH SIGNATURE-----\r?\n?$/;
const TERMINAL_PHASES = new Set(["done", "error", "rolled-back", "recovery-required", "aborted"]);

export class UnifiedUpdateConnectionError extends Error {}
/** The hub runs another release than the one just installed on it. */
export class PiVersionMismatchError extends Error {}

export function isTerminalUpdate(status: Pick<HubUpdateStatus, "phase">): boolean {
  return TERMINAL_PHASES.has(status.phase);
}

export function unifiedUpdateAvailable(installed: string | undefined, manifest: BundledUpdateManifest | null): boolean {
  if (!installed || !manifest || !RELEASE.test(installed)) return false;
  return compareVersions(manifest.release, installed) > 0;
}

/** True when the hub is older than the release, or too old to say. */
export function piUpdateNeeded(hub: HubInfo | null, manifest: BundledUpdateManifest | null): boolean {
  if (!hub || !manifest) return false;
  return !hub.version || compareVersions(manifest.release, hub.version) > 0;
}

export function planUnifiedUpdate(
  installed: string | undefined, manifest: BundledUpdateManifest | null,
  hub: HubInfo | null, piPackage: BundledPiUpdate | null,
): UpdatePlan {
  const outdated = piUpdateNeeded(hub, manifest);
  const pi = outdated && !!hub?.pi_update && !!piPackage;
  return { pi, piManual: outdated && !pi, captain: unifiedUpdateAvailable(installed, manifest) };
}

export async function bundledUpdateManifest(): Promise<BundledUpdateManifest | null> {
  try {
    const value = await invoke<BundledUpdateManifest | null>("bundled_update_manifest");
    if (!value || value.schema !== 1 || value.board !== "midi-captain-rp2040"
        || value.family !== "native" || value.flash_bytes !== 8388608
        || !RELEASE.test(value.release) || !RELEASE.test(value.firmware_version)
        || !SHA256.test(value.firmware_sha256)) return null;
    return value;
  } catch { return null; }
}

/** Null when this build carries no signed Pi package (local unsigned builds). */
export async function bundledPiUpdate(): Promise<BundledPiUpdate | null> {
  try {
    const value = await invoke<BundledPiUpdate | null>("bundled_pi_update");
    if (!value || !SHA256.test(value.sha256) || !Number.isSafeInteger(value.size)
        || value.size <= 0 || value.size > MAX_PACKAGE_BYTES) return null;
    return { size: value.size, sha256: value.sha256.toLowerCase() };
  } catch { return null; }
}

export function readPendingUpdate(): PendingUpdate | null {
  try {
    const value = JSON.parse(localStorage.getItem(PENDING_UPDATE_KEY) || "null") as PendingUpdate | null;
    if (!value || typeof value.job !== "string" || !value.job || value.job.length > 160
        || typeof value.endpoint !== "string" || !value.endpoint.startsWith("tcp://")
        || (value.kind !== undefined && value.kind !== "pi")) return null;
    return value.kind === "pi" ? { job: value.job, endpoint: value.endpoint, kind: "pi" } : { job: value.job, endpoint: value.endpoint };
  } catch { return null; }
}

function rememberUpdate(pending: PendingUpdate): void {
  // Saving before any flash command makes interrupted updates discoverable on
  // app restart. Fail here if storage is unavailable instead of losing the job.
  localStorage.setItem(PENDING_UPDATE_KEY, JSON.stringify(pending));
}

export function forgetUpdate(job: string): void {
  if (readPendingUpdate()?.job === job) localStorage.removeItem(PENDING_UPDATE_KEY);
}

function isRefusal(error: unknown): boolean {
  // A correlated ERROR is a refusal from the hub, not an absent connection.
  return error instanceof Error && error.message.startsWith("error:")
    && !error.message.startsWith("error: disconnected");
}

async function command(message: { type: string; [key: string]: unknown }): Promise<HubUpdateStatus> {
  let reply;
  try { reply = await sendAndAwait(message, 12000); }
  catch (error) {
    // Keep a refusal visible instead of repeatedly claiming that the Pi is offline.
    if (isRefusal(error)) throw error;
    throw new UnifiedUpdateConnectionError(String(error));
  }
  if (reply.type !== "HUB_UPDATE" || typeof reply.phase !== "string") {
    throw new Error("The Raspberry Pi returned an unexpected update response.");
  }
  return reply;
}

export async function hubSupportsUnifiedUpdate(): Promise<boolean> {
  try {
    const reply = await command({ type: "HUB_UPDATE_INFO" });
    return reply.phase === "ready" && reply.supported === true;
  } catch { return false; }
}

/** HUB_INFO; a lost connection throws, any refusal or odd reply means an older hub. */
async function queryHubInfo(): Promise<HubInfo> {
  let reply;
  try { reply = await sendAndAwait({ type: "HUB_INFO" }, 5000); }
  catch (error) {
    // Older hubs forward HUB_INFO to the Captain, which answers ERROR.
    if (isRefusal(error)) return { pi_update: false };
    throw new UnifiedUpdateConnectionError(String(error));
  }
  if (reply.type !== "HUB_INFO") return { pi_update: false };
  const version = typeof reply.version === "string" && RELEASE.test(reply.version) ? reply.version : undefined;
  return version ? { version, pi_update: reply.pi_update === true } : { pi_update: false };
}

export async function hubInfo(): Promise<HubInfo> {
  try { return await queryHubInfo(); } catch { return { pi_update: false }; }
}

/** After a Pi job: the hub must now report exactly the bundled release. */
export async function confirmPiRelease(release: string): Promise<HubInfo> {
  const info = await queryHubInfo();
  if (info.version !== release) {
    throw new PiVersionMismatchError(`After its update the Raspberry Pi reports ${info.version ? `Bosun ${info.version}` : "an unknown version"} instead of ${release}.`);
  }
  return info;
}

export async function unifiedUpdateStatus(job: string): Promise<HubUpdateStatus> {
  const status = await command({ type: "HUB_UPDATE_STATUS", job });
  if (status.job !== job) throw new Error("The Raspberry Pi returned a different update job.");
  return status;
}

function requireConnection(endpoint: string, isCurrentConnection: () => boolean): void {
  if (!endpoint.startsWith("tcp://") || !isCurrentConnection()) {
    throw new UnifiedUpdateConnectionError("Reconnect to the same Raspberry Pi to continue checking this update.");
  }
}

interface UpdatePackage { data: string; sha256: string; size: number; signature?: string }
type LoadedPackage = UpdatePackage & { bytes: Uint8Array };

async function decodePackage(value: UpdatePackage, label: string): Promise<LoadedPackage> {
  if (!value || typeof value.data !== "string" || !SHA256.test(value.sha256)
      || !Number.isSafeInteger(value.size) || value.size <= 0 || value.size > MAX_PACKAGE_BYTES
      || value.data.length > Math.ceil(MAX_PACKAGE_BYTES / 3) * 4) {
    throw new Error(`The bundled ${label} is invalid.`);
  }
  let decoded: string;
  try { decoded = atob(value.data); } catch { throw new Error(`The bundled ${label} is invalid.`); }
  if (decoded.length !== value.size) throw new Error(`The bundled ${label} size does not match its contents.`);
  const bytes = Uint8Array.from(decoded, char => char.charCodeAt(0));
  const hash = Array.from(new Uint8Array(await crypto.subtle.digest("SHA-256", bytes)), byte => byte.toString(16).padStart(2, "0")).join("");
  if (hash !== value.sha256.toLowerCase()) throw new Error(`The bundled ${label} checksum does not match its contents.`);
  return { ...value, sha256: hash, bytes };
}

async function readPackage(): Promise<LoadedPackage> {
  return decodePackage(await invoke<UpdatePackage>("read_bundled_update"), "update package");
}

async function readPiPackage(): Promise<LoadedPackage & { signature: string }> {
  const value = await invoke<UpdatePackage>("read_bundled_pi_update");
  const signature = value?.signature;
  if (typeof signature !== "string" || signature.length > MAX_SIGNATURE_BYTES || !SIGNATURE.test(signature)) {
    throw new Error("The bundled Raspberry Pi package signature is invalid.");
  }
  return { ...await decodePackage(value, "Raspberry Pi package"), signature };
}

export interface UnifiedUpdateOptions {
  endpoint: string;
  isCurrentConnection: () => boolean;
  onStatus: (status: HubUpdateStatus) => void;
  onJob?: (pending: PendingUpdate) => void;
}

async function uploadAndCommit(
  pending: PendingUpdate,
  bundle: LoadedPackage,
  initial: HubUpdateStatus,
  options: UnifiedUpdateOptions,
): Promise<HubUpdateStatus> {
  let offset = initial.received ?? 0;
  if (!Number.isSafeInteger(offset) || offset < 0 || offset > bundle.size) {
    throw new Error("The Raspberry Pi reported an invalid upload offset.");
  }
  while (offset < bundle.size) {
    requireConnection(pending.endpoint, options.isCurrentConnection);
    const end = Math.min(offset + CHUNK_BYTES, bundle.size);
    const data = btoa(String.fromCharCode(...bundle.bytes.subarray(offset, end)));
    const status = await command({ type: "HUB_UPDATE_CHUNK", job: pending.job, offset, data });
    if (status.job !== pending.job || status.phase !== "uploading" || status.received !== end) {
      throw new Error(status.error || "The Raspberry Pi did not acknowledge the complete update chunk.");
    }
    offset = end;
    options.onStatus({ ...status, size: bundle.size });
  }
  requireConnection(pending.endpoint, options.isCurrentConnection);
  // The hub owns the job after COMMIT. If this response is lost, keep the job
  // and recover with STATUS; never resend a flash or abort a committed job.
  const status = await command({ type: "HUB_UPDATE_COMMIT", job: pending.job });
  if (status.job !== pending.job) throw new Error("The Raspberry Pi returned a different update job.");
  options.onStatus(status);
  return status;
}

async function beginUpload(
  options: UnifiedUpdateOptions, bundle: LoadedPackage, begin: Record<string, unknown>, kind?: "pi",
): Promise<HubUpdateStatus> {
  requireConnection(options.endpoint, options.isCurrentConnection);
  const status = await command({ type: "HUB_UPDATE_BEGIN", ...begin, size: bundle.size, sha256: bundle.sha256 });
  if (status.phase !== "uploading" || !status.job) throw new Error(status.error || "The Raspberry Pi could not start the update.");
  const pending: PendingUpdate = kind ? { job: status.job, endpoint: options.endpoint, kind } : { job: status.job, endpoint: options.endpoint };
  try { rememberUpdate(pending); }
  catch (error) {
    // BEGIN reserves an upload only. No installation has been requested yet.
    try { await command({ type: "HUB_UPDATE_ABORT", job: pending.job }); } catch { /* nothing was installed */ }
    throw error;
  }
  options.onJob?.(pending);
  options.onStatus({ ...status, size: bundle.size });
  return uploadAndCommit(pending, bundle, status, options);
}

function refuseSecondJob(): void {
  const previous = readPendingUpdate();
  if (previous) throw new Error(`Check the existing update on ${previous.endpoint.replace(/^tcp:\/\//, "")} before starting another one.`);
}

export async function startUnifiedUpdate(options: UnifiedUpdateOptions): Promise<HubUpdateStatus> {
  requireConnection(options.endpoint, options.isCurrentConnection);
  refuseSecondJob();
  if (!await hubSupportsUnifiedUpdate()) throw new Error("This Raspberry Pi is not ready to update the pedal.");
  requireConnection(options.endpoint, options.isCurrentConnection);
  return beginUpload(options, await readPackage(), {});
}

/** Upload the signed Pi package; the hub's root installer verifies and installs it. */
export async function startPiUpdate(options: UnifiedUpdateOptions): Promise<HubUpdateStatus> {
  requireConnection(options.endpoint, options.isCurrentConnection);
  refuseSecondJob();
  if (!(await hubInfo()).pi_update) {
    throw new Error("This Raspberry Pi cannot update itself. Update it once with the Setup guide command.");
  }
  requireConnection(options.endpoint, options.isCurrentConnection);
  const bundle = await readPiPackage();
  return beginUpload(options, bundle, { kind: "pi", signature: bundle.signature }, "pi");
}

export async function resumeUnifiedUpdate(options: UnifiedUpdateOptions): Promise<HubUpdateStatus> {
  const pending = readPendingUpdate();
  if (!pending || pending.endpoint !== options.endpoint) throw new Error("Connect to the Raspberry Pi where this update was started.");
  requireConnection(options.endpoint, options.isCurrentConnection);
  const status = await unifiedUpdateStatus(pending.job);
  options.onStatus(status);
  if (status.phase !== "uploading") return status;
  const bundle = pending.kind === "pi" ? await readPiPackage() : await readPackage();
  if (status.size !== bundle.size || status.sha256?.toLowerCase() !== bundle.sha256) {
    throw new Error("This upload belongs to a different update package. Cancel the unfinished upload before starting again.");
  }
  return uploadAndCommit(pending, bundle, status, options);
}

export async function abortUnifiedUpload(endpoint: string, isCurrentConnection: () => boolean): Promise<HubUpdateStatus> {
  const pending = readPendingUpdate();
  if (!pending || pending.endpoint !== endpoint) throw new Error("Connect to the Raspberry Pi where this update was started.");
  requireConnection(endpoint, isCurrentConnection);
  const current = await unifiedUpdateStatus(pending.job);
  if (current.phase !== "uploading") throw new Error("The update has already started. Check its status instead of cancelling it.");
  requireConnection(endpoint, isCurrentConnection);
  const status = await command({ type: "HUB_UPDATE_ABORT", job: pending.job });
  if (status.job !== pending.job || status.phase !== "aborted") throw new Error(status.error || "The Raspberry Pi did not cancel the upload.");
  forgetUpdate(pending.job);
  return status;
}

export async function checkUnifiedRecovery(options: UnifiedUpdateOptions): Promise<HubUpdateStatus> {
  const pending = readPendingUpdate();
  if (!pending || pending.endpoint !== options.endpoint) throw new Error("Connect to the Raspberry Pi where this update was started.");
  requireConnection(options.endpoint, options.isCurrentConnection);
  // This starts a read-only check, never a restore or another update. If the
  // acknowledgement is lost, resume STATUS instead of submitting it again.
  const status = await command({ type: "HUB_UPDATE_RECOVER", job: pending.job });
  if (status.job !== pending.job) throw new Error("The Raspberry Pi returned a different update job.");
  options.onStatus(status);
  return status;
}

/** The Captain firmware version read through the hub, which also lets a
 *  freshly restarted hub learn the Captain identity it needs for an update. */
export async function readCaptainFirmware(): Promise<string | undefined> {
  try {
    const reply = await sendAndAwait({ type: "GET_DEVICE_INFO" }, 8000);
    return reply.type === "DEVICE_INFO" && typeof reply.fw === "string" ? reply.fw : undefined;
  } catch (error) {
    if (isRefusal(error)) return undefined;
    throw new UnifiedUpdateConnectionError(String(error));
  }
}
