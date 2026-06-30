// Config backup / restore. Saves and restores a profile's full state
// through the existing JSON protocol - no firmware changes needed.
//
// A backup is a single JSON file containing:
//   { format, version, generated_at, profile_label, kind?,
//     device, patches, midi_learn }
//
// `patches` is an array of {bank, slot, patch} - the raw patch JSON the
// firmware would store under /config/profiles/<id>/patches/BB/SS.json.
//
// Restore writes everything via PUT_GLOBAL, PUT_PATCH, PUT_MIDI_LEARN.
// By default it targets the active profile; pass `asNewProfile` to
// create a fresh profile from the backup instead of overwriting.
//
// Versioning: v1 had no `kind`; v2 stores it so an import-as-new-profile
// flow can pick the right plugin kind without asking the user (CREATE_PROFILE
// needs it). validateBackup transparently accepts both.

import { cmd, sendAndAwait, waitForReboot } from "./protocol";
import type { Patch, MidiLearnTable, PatchSummary } from "./protocol";

export interface ConfigBackup {
  format: "bosun-config-backup";
  version: 1 | 2;
  generated_at: string;
  /** Human description picked at backup time (active profile name). */
  profile_label?: string;
  /** Plugin kind of the source profile (e.g. "kemper_player"). v2+. */
  kind?: string;
  device: Record<string, unknown>;
  patches: Array<{ bank: number; slot: number; patch: Patch }>;
  midi_learn?: MidiLearnTable;
}

export interface BackupProgress {
  phase: "device" | "patches" | "midi_learn" | "done";
  total: number;
  done: number;
  current: string;
}

export async function exportConfig(
  onProgress?: (p: BackupProgress) => void,
  profileLabel?: string,
  kind?: string,
  profileId?: string,
): Promise<ConfigBackup> {
  const report = (p: BackupProgress) => onProgress?.(p);

  // When profileId is set we ask the firmware to read straight from
  // disk for that specific profile - no SWITCH_PROFILE reboot needed.
  // Requires firmware 0.3.2+; older firmware will ignore the profile
  // field and return the active profile's data (which is correct
  // when profileId === active anyway, so the fallback is safe).
  const profileArg = profileId ? { profile: profileId } : {};

  report({ phase: "device", total: 0, done: 0, current: "device.json" });
  const gResp = await sendAndAwait<{ type: "GLOBAL"; id?: string; device: Record<string, unknown> }>(
    { type: "GET_GLOBAL", ...profileArg }, 5000);
  const device = gResp.device ?? {};

  const lpResp = await sendAndAwait<{ type: "PATCH_LIST"; id?: string; patches: PatchSummary[] }>(
    { type: "LIST_PATCHES", ...profileArg }, 5000);
  const summaries = lpResp.patches ?? [];

  report({ phase: "patches", total: summaries.length, done: 0, current: "" });
  const patches: ConfigBackup["patches"] = [];
  for (const s of summaries) {
    report({ phase: "patches", total: summaries.length, done: patches.length,
             current: `${String(s.bank).padStart(2, "0")}/${String(s.slot).padStart(2, "0")}` });
    const pResp = await sendAndAwait<{ type: "PATCH"; id?: string; bank: number; slot: number; patch: Patch }>(
      { type: "GET_PATCH", bank: s.bank, slot: s.slot, ...profileArg }, 5000);
    patches.push({ bank: s.bank, slot: s.slot, patch: pResp.patch });
  }

  report({ phase: "midi_learn", total: 0, done: 0, current: "midi_learn.json" });
  let midi_learn: MidiLearnTable | undefined;
  try {
    const mlResp = await sendAndAwait<{ type: "MIDI_LEARN"; id?: string; table: MidiLearnTable }>(
      { type: "GET_MIDI_LEARN", ...profileArg }, 5000);
    midi_learn = mlResp.table;
  } catch { /* midi_learn is optional */ }

  report({ phase: "done", total: summaries.length, done: summaries.length, current: "" });
  return {
    format: "bosun-config-backup",
    version: 2,
    generated_at: new Date().toISOString(),
    profile_label: profileLabel,
    kind,
    device,
    patches,
    midi_learn,
  };
}

export function backupFilename(backup: ConfigBackup): string {
  const safeProfile = (backup.profile_label || "profile").replace(/[^\w-]+/g, "_");
  return `${safeProfile}.json`;
}

export function timestampedFolderName(prefix = "bosun-export"): string {
  // YYYY-MM-DD_HH-MM-SS - filesystem-safe in every OS we care about.
  const d = new Date();
  const pad = (n: number) => String(n).padStart(2, "0");
  const stamp = `${d.getFullYear()}-${pad(d.getMonth() + 1)}-${pad(d.getDate())}` +
                `_${pad(d.getHours())}-${pad(d.getMinutes())}-${pad(d.getSeconds())}`;
  return `${prefix}_${stamp}`;
}

export function validateBackup(parsed: unknown): ConfigBackup {
  if (!parsed || typeof parsed !== "object") throw new Error("Not a JSON object");
  const b = parsed as Partial<ConfigBackup>;
  if (b.format !== "bosun-config-backup") throw new Error("Not a Bosun backup file");
  if (b.version !== 1 && b.version !== 2) throw new Error(`Unsupported backup version ${b.version}`);
  if (!b.device || typeof b.device !== "object") throw new Error("Missing 'device' section");
  if (!Array.isArray(b.patches)) throw new Error("Missing or invalid 'patches' array");
  return b as ConfigBackup;
}

/** Best-effort plugin kind inference for v1 backups (no `kind` field).
 * Looks at which plugin config block is present in `device`. Returns ""
 * when nothing matches - caller should ask the user to pick a kind in
 * that case. */
export function inferKindFromDevice(device: Record<string, unknown>): string {
  // Heuristic: a plugin's CONFIG_SCHEMA.key is the dict key it lives
  // under in device.json. We don't have the live plugin list here, so
  // we lean on the two we ship: kemper -> kemper_player, ampero -> ampero_ii_stage.
  if (device.kemper && typeof device.kemper === "object") return "kemper_player";
  if (device.ampero && typeof device.ampero === "object") return "ampero_ii_stage";
  return "";
}

export interface RestoreProgress {
  phase: "create_profile" | "switch_profile" | "device" | "patches" | "midi_learn" | "done";
  total: number;
  done: number;
  current: string;
}

export interface ImportOptions {
  /** Create a fresh profile from the backup instead of overwriting the
   * active one. profile_id is a slug; kind matches a plugin (e.g.
   * "kemper_player"); name is the human label. */
  asNewProfile?: { profile_id: string; name: string; kind: string };
}

/** Run an export over every profile on the pedal and write each one to
 *  disk under `<documents>/bosun-backups/<sub>/`. Used by the firmware
 *  install / push flow to make sure the user's customizations are
 *  preserved before we overwrite anything on the device.
 *
 *  Returns the absolute folder path the backups landed in, or `null` if
 *  the firmware reported no profiles (nothing to back up). Throws if
 *  the export itself failed - caller decides whether to proceed. */
export async function backupAllProfiles(
  sub: string,
  onProgress?: (msg: string) => void,
): Promise<string | null> {
  const { invoke: _invoke } = await import("@tauri-apps/api/core");
  const folder = await _invoke<string>("default_backup_folder", { sub });
  const profilesResp = await cmd.listProfiles();
  const profiles = profilesResp.profiles ?? [];
  if (profiles.length === 0) return null;
  for (const p of profiles) {
    onProgress?.(`Backing up "${p.name}"…`);
    // Cross-profile read (firmware 0.3.3+): we pass the profile id so
    // the firmware reads straight from disk for that profile - no
    // SWITCH_PROFILE reboot needed. Older firmware will return the
    // active profile's data; that's still useful as a fallback.
    const backup = await exportConfig(undefined, p.name || p.id, p.kind, p.id);
    const filename = backupFilename(backup);
    await _invoke<string>("write_export_file", {
      folder, relative: filename, content: JSON.stringify(backup, null, 2),
    });
  }
  return folder;
}


export async function importConfig(
  backup: ConfigBackup,
  onProgress?: (p: RestoreProgress) => void,
  options: ImportOptions = {},
): Promise<void> {
  const report = (p: RestoreProgress) => onProgress?.(p);

  // Writing a patch/device.json to the RP2040 flash can occasionally stall
  // past a tight deadline (FAT write + tmp-rename, with the main loop and
  // MIDI competing). Give each write a generous window and retry once on a
  // timeout - PUT_* is idempotent, so a retry after a slow-but-successful
  // write just rewrites the same bytes.
  async function putRetry(msg: { type: string; [k: string]: unknown }): Promise<void> {
    try {
      await sendAndAwait(msg, 8000);
    } catch (e) {
      if (String(e).toLowerCase().includes("timeout")) {
        await sendAndAwait(msg, 12000);
      } else {
        throw e;
      }
    }
  }

  // When importing as a new profile we add `profile: <new_id>` to each
  // PUT_* so the firmware (0.3.3+) writes straight to that profile's
  // files on disk - no SWITCH_PROFILE, no reboot, no waitForReboot.
  // The active profile stays untouched for the whole flow.
  let targetProfile: string | undefined;
  if (options.asNewProfile) {
    const { profile_id, name, kind } = options.asNewProfile;
    report({ phase: "create_profile", total: 0, done: 0, current: name });
    await cmd.createProfile(profile_id, name, kind);
    targetProfile = profile_id;
  }

  const profileArg = targetProfile ? { profile: targetProfile } : {};

  report({ phase: "device", total: 0, done: 0, current: "device.json" });
  await putRetry({ type: "PUT_GLOBAL", device: backup.device, ...profileArg });

  report({ phase: "patches", total: backup.patches.length, done: 0, current: "" });
  let done = 0;
  for (const { bank, slot, patch } of backup.patches) {
    report({ phase: "patches", total: backup.patches.length, done,
             current: `${String(bank).padStart(2, "0")}/${String(slot).padStart(2, "0")}` });
    await putRetry({ type: "PUT_PATCH", bank, slot, patch, ...profileArg });
    done += 1;
  }

  if (backup.midi_learn) {
    report({ phase: "midi_learn", total: 0, done: 0, current: "midi_learn.json" });
    await putRetry({ type: "PUT_MIDI_LEARN", table: backup.midi_learn, ...profileArg });
  }

  report({ phase: "done", total: backup.patches.length, done: backup.patches.length, current: "" });
}
