// Shared firmware push pipeline. Reads the firmware tree bundled with the
// editor (via Tauri commands) and streams each file to the pedal over the
// already-open USB CDC connection using the PUT_FILE_* protocol.
//
// Works in performance mode - no bootloader, no MSC drive, no manual
// switch held at boot. This is the canonical "Update firmware" path; the
// MSC-drive Installer modal is reserved for the one-shot first install on
// a virgin pedal where CircuitPython itself has to be flashed first.

import { invoke } from "@tauri-apps/api/core";
import { cmd, waitForReboot, type FirmwareFile } from "./protocol";
import { backupAllProfiles } from "./config-backup";

const CHUNK_B64 = 512;

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
      await cmd.putFileBegin(file.dst);
      for (let i = 0; i < b64.length; i += CHUNK_B64) {
        await cmd.putFileChunk(file.dst, b64.slice(i, i + CHUNK_B64));
      }
      await cmd.putFileEnd(file.dst);
      progress.done += 1;
      log(`OK  ${file.dst}  (${humanBytes(file.size)})`);
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
