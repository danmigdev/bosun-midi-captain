import { invoke } from "@tauri-apps/api/core";
import { sendAndAwait } from "./protocol";
import { compareVersions } from "./firmware-update";
import { isNativeFirmware } from "./firmware-capabilities";

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
}

export interface PendingUpdate { job: string; endpoint: string }
export const PENDING_UPDATE_KEY = "BOSUN_PENDING_UPDATE";
const CHUNK_BYTES = 12 * 1024;
const MAX_PACKAGE_BYTES = 4 * 1024 * 1024;
const SHA256 = /^[a-f0-9]{64}$/i;
const RELEASE = /^\d+\.\d+\.\d+(?:-[a-z0-9.-]+)?$/i;
const TERMINAL_PHASES = new Set(["done", "error", "rolled-back", "recovery-required", "aborted"]);

export class UnifiedUpdateConnectionError extends Error {}

export function isTerminalUpdate(status: Pick<HubUpdateStatus, "phase">): boolean {
  return TERMINAL_PHASES.has(status.phase);
}

export function unifiedUpdateAvailable(installed: string | undefined, manifest: BundledUpdateManifest | null): boolean {
  if (!installed || !manifest || !RELEASE.test(installed)) return false;
  const comparison = compareVersions(manifest.release, installed);
  // A user may have installed this release's CP file bundle over direct USB.
  // Connecting through the Pi must still offer its native migration package.
  return comparison > 0 || (comparison === 0 && !isNativeFirmware({ fw: installed }));
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

export function readPendingUpdate(): PendingUpdate | null {
  try {
    const value = JSON.parse(localStorage.getItem(PENDING_UPDATE_KEY) || "null") as PendingUpdate | null;
    if (!value || typeof value.job !== "string" || !value.job || value.job.length > 160
        || typeof value.endpoint !== "string" || !value.endpoint.startsWith("tcp://")) return null;
    return { job: value.job, endpoint: value.endpoint };
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

async function command(message: { type: string; [key: string]: unknown }): Promise<HubUpdateStatus> {
  let reply;
  try { reply = await sendAndAwait(message, 12000); }
  catch (error) {
    // A correlated ERROR is a refusal from the hub, not an absent connection.
    // Keep it visible instead of repeatedly claiming that the Pi is offline.
    if (error instanceof Error && error.message.startsWith("error:")
        && !error.message.startsWith("error: disconnected")) throw error;
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

interface UpdatePackage { data: string; sha256: string; size: number }

async function readPackage(): Promise<UpdatePackage & { bytes: Uint8Array }> {
  const value = await invoke<UpdatePackage>("read_bundled_update");
  if (!value || typeof value.data !== "string" || !SHA256.test(value.sha256)
      || !Number.isSafeInteger(value.size) || value.size <= 0 || value.size > MAX_PACKAGE_BYTES
      || value.data.length > Math.ceil(MAX_PACKAGE_BYTES / 3) * 4) {
    throw new Error("The bundled update package is invalid.");
  }
  let decoded: string;
  try { decoded = atob(value.data); } catch { throw new Error("The bundled update package is invalid."); }
  if (decoded.length !== value.size) throw new Error("The bundled update size does not match its contents.");
  const bytes = Uint8Array.from(decoded, char => char.charCodeAt(0));
  const hash = Array.from(new Uint8Array(await crypto.subtle.digest("SHA-256", bytes)), byte => byte.toString(16).padStart(2, "0")).join("");
  if (hash !== value.sha256.toLowerCase()) throw new Error("The bundled update checksum does not match its contents.");
  return { ...value, sha256: hash, bytes };
}

export interface UnifiedUpdateOptions {
  endpoint: string;
  isCurrentConnection: () => boolean;
  onStatus: (status: HubUpdateStatus) => void;
  onJob?: (pending: PendingUpdate) => void;
}

async function uploadAndCommit(
  pending: PendingUpdate,
  bundle: UpdatePackage & { bytes: Uint8Array },
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

export async function startUnifiedUpdate(options: UnifiedUpdateOptions): Promise<HubUpdateStatus> {
  requireConnection(options.endpoint, options.isCurrentConnection);
  const previous = readPendingUpdate();
  if (previous) throw new Error(`Check the existing update on ${previous.endpoint.replace(/^tcp:\/\//, "")} before starting another one.`);
  if (!await hubSupportsUnifiedUpdate()) throw new Error("This Raspberry Pi is not ready to update the pedal.");
  requireConnection(options.endpoint, options.isCurrentConnection);
  const bundle = await readPackage();
  requireConnection(options.endpoint, options.isCurrentConnection);
  const status = await command({ type: "HUB_UPDATE_BEGIN", size: bundle.size, sha256: bundle.sha256 });
  if (status.phase !== "uploading" || !status.job) throw new Error(status.error || "The Raspberry Pi could not start the update.");
  const pending = { job: status.job, endpoint: options.endpoint };
  try { rememberUpdate(pending); }
  catch (error) {
    // BEGIN reserves an upload only. No flash has been requested yet.
    try { await command({ type: "HUB_UPDATE_ABORT", job: pending.job }); } catch { /* no device writes have started */ }
    throw error;
  }
  options.onJob?.(pending);
  options.onStatus({ ...status, size: bundle.size });
  return uploadAndCommit(pending, bundle, status, options);
}

export async function resumeUnifiedUpdate(options: UnifiedUpdateOptions): Promise<HubUpdateStatus> {
  const pending = readPendingUpdate();
  if (!pending || pending.endpoint !== options.endpoint) throw new Error("Connect to the Raspberry Pi where this update was started.");
  requireConnection(options.endpoint, options.isCurrentConnection);
  const status = await unifiedUpdateStatus(pending.job);
  options.onStatus(status);
  if (status.phase !== "uploading") return status;
  const bundle = await readPackage();
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
