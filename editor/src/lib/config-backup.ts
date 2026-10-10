// Config backup / restore. Saves and restores a profile's full state
// through the existing JSON protocol - no firmware changes needed.
//
// A backup is a single JSON file containing:
//   { format, version, generated_at, profile_label, kind?,
//     device, patches, hardware? }
//
// `patches` is an array of {bank, slot, patch} - the raw patch JSON the
// firmware would store under /config/profiles/<id>/patches/BB/SS.json.
//
// Restore writes everything via PUT_GLOBAL and PUT_PATCH. Backups from
// older releases may also carry a `midi_learn` section from the retired
// MIDI Learn page; restore ignores it.
// By default it targets the active profile; pass `asNewProfile` to
// create a fresh profile from the backup instead of overwriting.
//
// Versioning: version 2 stores the source profile's plugin `kind`, so an
// import-as-new-profile flow can pick the right plugin without asking the
// user (CREATE_PROFILE needs it).

import { cmd, sendAndAwait } from "./protocol";
import type { Patch, PatchSummary } from "./protocol";
import { CAPTAIN_10, type HardwareLayout } from "./hardware";

export interface ConfigBackup {
  format: "bosun-config-backup";
  version: 2;
  generated_at: string;
  /** Human description picked at backup time (active profile name). */
  profile_label?: string;
  /** Plugin kind of the source profile (e.g. "kemper_player"). */
  kind?: string;
  device: Record<string, unknown>;
  patches: Array<{ bank: number; slot: number; patch: Patch }>;
  /** Pedal model the backup was taken from (editor 0.8+). Older backups
   * omit it: they all come from the 10-switch Captain. Informational only. */
  hardware?: { model: string; name: string };
}

export interface BackupProgress {
  phase: "device" | "patches" | "done";
  total: number;
  done: number;
  current: string;
}

export async function exportConfig(
  onProgress?: (p: BackupProgress) => void,
  profileLabel?: string,
  kind?: string,
  profileId?: string,
  hardware?: Pick<HardwareLayout, "model" | "name">,
): Promise<ConfigBackup> {
  const report = (p: BackupProgress) => onProgress?.(p);

  // When profileId is set we ask the firmware to read straight from
  // disk for that specific profile - no SWITCH_PROFILE reboot needed.
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

  report({ phase: "done", total: summaries.length, done: summaries.length, current: "" });
  return {
    format: "bosun-config-backup",
    version: 2,
    generated_at: new Date().toISOString(),
    profile_label: profileLabel,
    kind,
    device,
    patches,
    ...(hardware ? { hardware: { model: hardware.model, name: hardware.name } } : {}),
  };
}

/** A notice for restoring a backup taken on another pedal model, or "". The
 * restore itself is unchanged: entries for switches or jacks the pedal lacks
 * are kept, never fire, and are listed for removal in the editor. */
export function backupHardwareNotice(backup: ConfigBackup, hardware: HardwareLayout): string {
  const source = backup.hardware?.model || CAPTAIN_10.model;
  if (source === hardware.model) return "";
  const name = backup.hardware?.name || CAPTAIN_10.name;
  return `This backup comes from a ${name}. Its bindings and settings for switches or expression jacks the ${hardware.name} does not have are kept but stay inactive: review them in the patch editor and Settings.`;
}

/** Why the active profile cannot be overwritten with this backup, or "".
 * Another kind's settings would not fit: a Kemper block on a Generic MIDI
 * profile does nothing. Backups without a recorded kind are not checked. */
export function overwriteKindError(backup: ConfigBackup, activeKind: string | undefined): string {
  if (!backup.kind || !activeKind || backup.kind === activeKind) return "";
  return `This backup is from a ${backup.kind} profile and the active profile is ${activeKind}. Import it as a new profile instead.`;
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
  if (b.version !== 2) throw new Error(`Unsupported backup version ${b.version}`);
  if (!b.device || typeof b.device !== "object") throw new Error("Missing 'device' section");
  if (!Array.isArray(b.patches)) throw new Error("Missing or invalid 'patches' array");
  return b as ConfigBackup;
}

export interface RestoreProgress {
  phase: "create_profile" | "switch_profile" | "device" | "patches" | "done";
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

export async function importConfig(
  backup: ConfigBackup,
  onProgress?: (p: RestoreProgress) => void,
  options: ImportOptions = {},
): Promise<void> {
  const report = (p: RestoreProgress) => onProgress?.(p);

  // Writing a patch/device.json to the RP2040 flash can occasionally stall
  // past a tight deadline (flash erase/program, with the main loop and
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
  // PUT_* so the firmware writes straight to that profile's files on
  // disk - no SWITCH_PROFILE, no reboot, no waitForReboot.
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

  // Clear every existing patch before writing the backup's patches.
  // Without this step old patches at bank/slot positions not covered by
  // the incoming backup survive the import, which looks like a "merge"
  // when the user expects a full replacement.
  if (!options.asNewProfile) {
    let _id = 1;
    const delRetry = async (msg: { type: string; [k: string]: unknown }) => {
      try { await sendAndAwait({ id: String(_id++), ...msg }, 4000); } catch { /* skip */ }
    };
    try {
      const existing = await sendAndAwait({ type: "LIST_PATCHES", id: String(_id++) }, 4000) as { patches?: Array<{ bank: number; slot: number }> };
      for (const p of (existing.patches || [])) {
        report({ phase: "patches", total: 0, done: 0, current: `clearing ${String(p.bank).padStart(2, "0")}/${String(p.slot).padStart(2, "0")}` });
        await delRetry({ type: "DELETE_PATCH", bank: p.bank, slot: p.slot });
      }
    } catch { /* best effort: the backup's patches are still written below */ }
  }

  report({ phase: "patches", total: backup.patches.length, done: 0, current: "" });
  let done = 0;
  for (const { bank, slot, patch } of backup.patches) {
    report({ phase: "patches", total: backup.patches.length, done,
             current: `${String(bank).padStart(2, "0")}/${String(slot).padStart(2, "0")}` });
    await putRetry({ type: "PUT_PATCH", bank, slot, patch, ...profileArg });
    done += 1;
  }

  report({ phase: "done", total: backup.patches.length, done: backup.patches.length, current: "" });
}
