// Shared firmware push pipeline. Reads the firmware tree bundled with the
// editor (via Tauri commands) and streams each file to the pedal over the
// already-open USB CDC connection using the PUT_FILE_* protocol.
//
// Works in performance mode - no bootloader, no MSC drive, no manual
// switch held at boot. This is the canonical "Update firmware" path; the
// MSC-drive Installer modal is reserved for the one-shot first install on
// a virgin pedal where CircuitPython itself has to be flashed first.

import { invoke } from "@tauri-apps/api/core";
import {
  cmd,
  waitForReboot,
  FirmwareCommandTimeoutError,
  type FirmwareFile,
  type FirmwareMessage,
} from "./protocol";
import { backupAllProfiles } from "./config-backup";

// 128 base64 characters decode to at most 96 bytes.  Larger chunks overlap
// with the Captain's 256-byte USB-MIDI read and can exceed the RP2040's
// contiguous free heap during an update, making the pedal stop ACKing chunks.
// Keep this aligned with tools/push_firmware.py.
export const FIRMWARE_CHUNK_B64 = 128;
export const FIRMWARE_FILE_RETRIES = 3;

type FirmwareAck = Extract<FirmwareMessage, { type: "ACK" }>;

/** Narrow command surface used by one OTA file transaction.  Keeping this
 * injectable makes the append/ACK ambiguity testable without a serial port. */
export interface FirmwareUploadCommands {
  putFileBegin(path: string, size: number): Promise<FirmwareMessage>;
  putFileChunk(path: string, dataB64: string, offset: number): Promise<FirmwareMessage>;
  putFileEnd(path: string): Promise<FirmwareMessage>;
}

export interface FirmwareFileUploadResult {
  size: number;
  attempts: number;
  /** Raw-byte offsets whose append completed without a correlated ACK. */
  uncertainOffsets: number[];
}

export interface FirmwareFileUploadOptions {
  commands?: FirmwareUploadCommands;
  retries?: number;
  retryDelayMs?: number;
  onWarning?: (message: string) => void;
}

function base64Value(code: number): number {
  if (code >= 65 && code <= 90) return code - 65;
  if (code >= 97 && code <= 122) return code - 97 + 26;
  if (code >= 48 && code <= 57) return code - 48 + 52;
  if (code === 43) return 62;
  if (code === 47) return 63;
  return -1;
}

/** Return the exact decoded byte length of canonical standard base64.
 * No decoded copy is allocated: large firmware modules remain a single
 * string until their bounded chunks are sent. */
export function decodedBase64Size(dataB64: string): number {
  const length = dataB64.length;
  if (length === 0) return 0;
  if (length % 4 !== 0) throw new Error("invalid firmware base64 length");

  let padding = 0;
  if (dataB64.charCodeAt(length - 1) === 61) padding += 1;
  if (dataB64.charCodeAt(length - 2) === 61) padding += 1;
  const dataEnd = length - padding;

  for (let i = 0; i < dataEnd; i += 1) {
    if (base64Value(dataB64.charCodeAt(i)) < 0) {
      throw new Error(`invalid firmware base64 character at ${i}`);
    }
  }
  for (let i = dataEnd; i < length; i += 1) {
    if (dataB64.charCodeAt(i) !== 61) {
      throw new Error(`invalid firmware base64 padding at ${i}`);
    }
  }

  // Reject non-canonical encodings whose unused low bits are non-zero.
  // Rust's STANDARD encoder always emits canonical data; accepting anything
  // else would make the advertised size weaker than the bytes being sent.
  if (padding === 2 && (base64Value(dataB64.charCodeAt(dataEnd - 1)) & 0x0f) !== 0) {
    throw new Error("non-canonical firmware base64 padding");
  }
  if (padding === 1 && (base64Value(dataB64.charCodeAt(dataEnd - 1)) & 0x03) !== 0) {
    throw new Error("non-canonical firmware base64 padding");
  }

  return (length / 4) * 3 - padding;
}

function requireAck(response: FirmwareMessage, operation: string): FirmwareAck {
  if (response.type !== "ACK") {
    throw new Error(`${operation}: expected ACK, received ${response.type}`);
  }
  return response;
}

function delay(ms: number): Promise<void> {
  return ms > 0 ? new Promise(resolve => setTimeout(resolve, ms)) : Promise.resolve();
}

/** Upload one file as a transaction.
 *
 * PUT_FILE_CHUNK is append-only on every deployed Captain and has no request
 * id deduplication.  A timeout therefore makes the append ambiguous: sending
 * that same chunk again could duplicate bytes.  We send each chunk at most
 * once per transaction.  When BEGIN explicitly negotiated exact-size
 * verification, we continue after an ACK timeout and let PUT_FILE_END decide;
 * if the chunk truly never arrived, END rejects the short temporary file and
 * the next attempt starts from a truncating BEGIN.  Legacy firmware cannot
 * make that decision, so an uncertain chunk aborts its attempt immediately. */
export async function pushFirmwareFile(
  path: string,
  dataB64: string,
  options: FirmwareFileUploadOptions = {},
): Promise<FirmwareFileUploadResult> {
  const commands = options.commands ?? cmd;
  const retries = options.retries ?? FIRMWARE_FILE_RETRIES;
  const retryDelayMs = options.retryDelayMs ?? 500;
  if (!Number.isSafeInteger(retries) || retries < 1) {
    throw new Error("firmware file retries must be a positive integer");
  }
  if (!Number.isFinite(retryDelayMs) || retryDelayMs < 0) {
    throw new Error("firmware retry delay must be non-negative");
  }

  // This is the post-normalisation payload returned by Tauri (not the file
  // metadata size, which can still include a stripped UTF-8 BOM).
  const expectedSize = decodedBase64Size(dataB64);

  for (let attempt = 1; attempt <= retries; attempt += 1) {
    const uncertainOffsets: number[] = [];
    try {
      const beginAck = requireAck(
        await commands.putFileBegin(path, expectedSize),
        `PUT_FILE_BEGIN ${path}`,
      );
      // Older firmware accepts unknown JSON fields, so merely sending `size`
      // does not prove END will enforce it.  Trust the ambiguity-resolving
      // path only when the firmware explicitly echoes this transaction's
      // exact expected size.  On legacy firmware a chunk timeout aborts the
      // attempt and a fresh BEGIN truncates the temporary file before retry.
      if (beginAck.size_check === true && beginAck.size !== expectedSize) {
        throw new Error(
          `${path}: PUT_FILE_BEGIN size capability mismatch ` +
          `(expected ${expectedSize}, echoed ${String(beginAck.size)})`,
        );
      }
      const endVerifiesSize = beginAck.size_check === true &&
        beginAck.size === expectedSize;

      let offset = 0;
      for (let i = 0; i < dataB64.length; i += FIRMWARE_CHUNK_B64) {
        const chunk = dataB64.slice(i, i + FIRMWARE_CHUNK_B64);
        const chunkSize = decodedBase64Size(chunk);
        const chunkOffset = offset;
        offset += chunkSize;
        try {
          requireAck(
            await commands.putFileChunk(path, chunk, chunkOffset),
            `PUT_FILE_CHUNK ${path}@${chunkOffset}`,
          );
        } catch (error) {
          if (error instanceof FirmwareCommandTimeoutError &&
              error.commandType === "PUT_FILE_CHUNK") {
            if (!endVerifiesSize) {
              options.onWarning?.(
                `${path}: firmware did not confirm end-to-end size checking; ` +
                `restarting after uncertain chunk at offset ${chunkOffset}`,
              );
              throw error;
            }
            uncertainOffsets.push(chunkOffset);
            options.onWarning?.(
              `${path}: no ACK for chunk at offset ${chunkOffset}; ` +
              "continuing without unsafe resend",
            );
            continue;
          }
          throw error;
        }
      }

      if (offset !== expectedSize) {
        throw new Error(
          `${path}: local size mismatch (expected ${expectedSize}, chunked ${offset})`,
        );
      }

      requireAck(await commands.putFileEnd(path), `PUT_FILE_END ${path}`);
      return { size: expectedSize, attempts: attempt, uncertainOffsets };
    } catch (error) {
      if (attempt === retries) throw error;
      options.onWarning?.(
        `${path}: ${String(error)}; retrying complete file ` +
        `(${attempt + 1}/${retries})`,
      );
      await delay(retryDelayMs);
    }
  }

  // The validated retries invariant and loop bounds make this unreachable.
  throw new Error(`${path}: firmware upload exhausted without a result`);
}

export interface FirmwarePushProgress {
  total: number;
  done: number;
  current: string;
  /** Free-form human log lines, newest last. Capped to ~60 entries. */
  log: string[];
}

export type FirmwarePushPhase =
  | "idle"
  | "backing-up"
  | "listing"
  | "pushing"
  | "rebooting"
  | "done"
  | "error";

export interface FirmwarePushState {
  phase: FirmwarePushPhase;
  progress: FirmwarePushProgress;
  error: string;
}

/** Push the bundled firmware tree to the pedal. `onState` is invoked
 * synchronously on every progress change so the caller can drive a
 * Svelte $state-backed overlay without manual diffing. */
export async function pushFirmware(
  onState: (s: FirmwarePushState) => void,
  opts: { reboot?: boolean; source?: string } = {},
): Promise<void> {
  const progress: FirmwarePushProgress = { total: 0, done: 0, current: "", log: [] };
  const state: FirmwarePushState = { phase: "listing", progress, error: "" };
  const push = () => onState({ ...state, progress: { ...progress, log: [...progress.log] } });
  const log = (s: string) => {
    progress.log.push(s);
    if (progress.log.length > 60) progress.log.splice(0, progress.log.length - 60);
    push();
  };

  push();
  try {
    // Always back up the user's current pedal state before we start
    // overwriting files on it. The bundled firmware tree includes
    // /config/profiles/... which would wipe customizations otherwise.
    state.phase = "backing-up";
    log("Backing up current pedal state");
    push();
    try {
      const ts = new Date().toISOString().replace(/[:.]/g, "-").replace("T", "_").slice(0, 19);
      const folder = await backupAllProfiles(`pre-firmware-update_${ts}`, msg => log(msg));
      if (folder) log(`Backup saved to ${folder}`);
      else        log("No profiles on the pedal - nothing to back up");
    } catch (e) {
      // Don't block the update on a backup failure - log loudly and
      // proceed. The user can still abort by closing the modal.
      log("Backup failed: " + String(e) + " (continuing anyway)");
    }

    log(opts.source ? "Listing firmware files from " + opts.source : "Listing firmware files");
    const files = opts.source
      ? await invoke<FirmwareFile[]>("list_firmware_files_at", { root: opts.source })
      : await invoke<FirmwareFile[]>("list_firmware_files");
    log(`${files.length} files, ${humanBytes(files.reduce((a, f) => a + f.size, 0))} total`);
    progress.total = files.length;
    state.phase = "pushing";
    push();

    for (const file of files) {
      progress.current = file.dst;
      push();
      const b64 = opts.source
        ? await invoke<string>("read_firmware_file_at_b64", { root: opts.source, rel: file.rel })
        : await invoke<string>("read_firmware_file_b64", { rel: file.rel });
      const uploaded = await pushFirmwareFile(file.dst, b64, { onWarning: log });
      progress.done += 1;
      log(`OK  ${file.dst}  (${humanBytes(uploaded.size)})`);
    }

    if (opts.reboot !== false) {
      state.phase = "rebooting";
      push();
      try { await cmd.reboot(); log("REBOOT sent"); }
      catch (e) { log("Reboot request failed: " + String(e)); }
      // Wait for the pedal to come back so the "done" screen reflects
      // reality (you can close the dialog and immediately use the
      // editor). Without this the user sees "all done" while the
      // connection pill turns red and the patches list goes empty.
      const back = await waitForReboot(20000);
      log(back ? "Firmware back online" : "Firmware did not respond within 20s - reconnect manually");
    }

    state.phase = "done";
    push();
  } catch (e) {
    state.error = String(e);
    state.phase = "error";
    log("ERROR: " + state.error);
    push();
    throw e;
  }
}

export function humanBytes(n: number): string {
  if (n < 1024) return `${n} B`;
  if (n < 1024 * 1024) return `${(n / 1024).toFixed(1)} KB`;
  return `${(n / 1024 / 1024).toFixed(2)} MB`;
}
