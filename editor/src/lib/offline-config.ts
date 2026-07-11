// Offline editing scaffold (item 7 - stretch, follow-up).
//
// This module is the in-memory foundation for editing a config imported from a
// backup file/folder without a pedal connected. It is intentionally decoupled
// from the live serial protocol: it never touches cmd.* and holds no Tauri
// dependency, so bringing it in cannot regress the connected-to-pedal path.
//
// STATUS: scaffold. The data model + push planner below are complete and
// unit-tested. What is NOT yet wired (documented as follow-up in the report):
//   - App.svelte offline state + "Offline" banner
//   - routing PatchEditor/Settings/PatchesGrid reads at an OfflineSession
//     instead of `currentPatch`/`globalDevice`/`patches`
//   - a "Push to device" button that runs planPush() against a freshly
//     connected pedal (importConfig in config-backup.ts already does the
//     firmware-write half - planPush() produces the ordered work list).
//
// The connected path is unchanged; this is additive and dormant until wired.

import { validateBackup, type ConfigBackup } from "./config-backup";
import { patchIdOf, type Patch, type PatchSummary, type MidiLearnTable } from "./protocol";

/** An in-memory, editable configuration loaded from a backup. Mirrors the
 * shape App.svelte keeps for a connected pedal (device, patches, midi_learn)
 * so the same editor components can later be pointed at it. */
export interface OfflineSession {
  /** Human label for the source (profile name / filename). */
  label: string;
  /** Plugin kind, if known (e.g. "kemper_player"); "" when unknown. */
  kind: string;
  device: Record<string, unknown>;
  /** Keyed by "BB/SS" (zero-padded) so lookups match patchIdOf(). */
  patches: Map<string, Patch>;
  midiLearn: MidiLearnTable;
}

/** Build an editable OfflineSession from an unknown parsed JSON blob (e.g. the
 * contents of a backup file). Throws via validateBackup on a malformed file. */
export function sessionFromBackup(parsed: unknown): OfflineSession {
  const backup: ConfigBackup = validateBackup(parsed);
  const patches = new Map<string, Patch>();
  for (const entry of backup.patches) {
    patches.set(patchIdOf(entry.bank, entry.slot), entry.patch);
  }
  return {
    label: backup.profile_label ?? "Imported config",
    kind: backup.kind ?? "",
    device: backup.device ?? {},
    patches,
    midiLearn: backup.midi_learn ?? { pc_to_patch: [] },
  };
}

/** Derive the PatchSummary list the Patches grid renders, from the in-memory
 * session. Marks every patch dirty=true: nothing has been pushed to a device
 * yet, so the whole session is "unsaved" relative to any pedal. */
export function summariesOf(session: OfflineSession): PatchSummary[] {
  const out: PatchSummary[] = [];
  for (const [id, patch] of session.patches) {
    const m = id.match(/^(\d+)\/(\d+)$/);
    if (!m) continue;
    out.push({
      bank: parseInt(m[1], 10),
      slot: parseInt(m[2], 10),
      name: patch.name ?? "",
      dirty: true,
      linked_to: patch.linked_to,
    });
  }
  return out.sort((a, b) => a.bank - b.bank || a.slot - b.slot);
}

/** One unit of work to reconcile an offline session onto a connected pedal. */
export type PushStep =
  | { kind: "device" }
  | { kind: "patch"; bank: number; slot: number }
  | { kind: "midi_learn" };

/** Produce the ordered list of writes needed to push an offline session to a
 * device: device.json first (so plugin blocks exist), then every patch, then
 * the MIDI learn table. The caller executes each step via cmd.* against the
 * live pedal (see importConfig in config-backup.ts for the write primitives). */
export function planPush(session: OfflineSession): PushStep[] {
  const steps: PushStep[] = [{ kind: "device" }];
  for (const summary of summariesOf(session)) {
    steps.push({ kind: "patch", bank: summary.bank, slot: summary.slot });
  }
  if (session.midiLearn.pc_to_patch.length > 0) steps.push({ kind: "midi_learn" });
  return steps;
}
