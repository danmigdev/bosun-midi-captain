// Free-form pedal switch layout for the PaintAudio MIDI Captain (10 switches).
//
// There is NO fixed physical layout baked in here. Every user is free to
// arrange the ten switches on the map however they like; the arrangement is a
// grid of rows persisted per user in localStorage. The helpers below load,
// validate/repair, and mutate that arrangement without ever throwing.

import type { Binding } from "./protocol";

/** A layout is a list of rows; each row is a list of switch names. Every one
 *  of the ten switch names appears exactly once across the whole grid. Rows may
 *  differ in length. An empty string "" is a spacer / empty cell. */
export type PedalLayout = string[][];

/** The complete set of switch names the firmware uses. */
export const ALL_SWITCHES: string[] = ["1", "2", "3", "4", "up", "A", "B", "C", "D", "down"];

/** A NEUTRAL starting arrangement only. This is NOT a fixed physical layout -
 *  it is just the default a brand-new user sees, and they can freely rearrange
 *  it (and persist their own) via the editable PedalMap. */
export const DEFAULT_LAYOUT: PedalLayout = [
  ["1", "2", "3", "4", "up"],
  ["A", "B", "C", "D", "down"],
];

const STORAGE_KEY = "BOSUN_PEDAL_LAYOUT_V1";

/** Repair an arbitrary layout so it is well-formed:
 *  - unknown names and duplicate occurrences are dropped,
 *  - spacer cells ("") are preserved in place,
 *  - any switch missing after that pass is appended as a new final row,
 *  - empty rows left behind are removed.
 *  Always returns a valid layout containing all ten switches exactly once.
 *  Never throws. */
export function normalizeLayout(layout: PedalLayout): PedalLayout {
  const known = new Set(ALL_SWITCHES);
  const seen = new Set<string>();
  const rows: string[][] = [];

  const src = Array.isArray(layout) ? layout : [];
  for (const row of src) {
    if (!Array.isArray(row)) continue;
    const outRow: string[] = [];
    for (const cell of row) {
      if (cell === "") { outRow.push(""); continue; }        // keep spacers
      if (typeof cell !== "string") continue;
      if (!known.has(cell)) continue;                        // drop unknown
      if (seen.has(cell)) continue;                          // drop duplicate
      seen.add(cell);
      outRow.push(cell);
    }
    // Keep the row if it has any real content (a switch or a spacer).
    if (outRow.length > 0) rows.push(outRow);
  }

  // Append any switches that never appeared as a new final row.
  const missing = ALL_SWITCHES.filter((s) => !seen.has(s));
  if (missing.length > 0) rows.push(missing);

  // Guarantee at least one row exists.
  if (rows.length === 0) rows.push([...ALL_SWITCHES]);

  return rows;
}

/** Load the user's saved layout, validated/repaired. Falls back to
 *  DEFAULT_LAYOUT when nothing is stored or the stored value is corrupt.
 *  Fully guarded - never throws. */
export function loadLayout(): PedalLayout {
  try {
    const raw = typeof localStorage !== "undefined" ? localStorage.getItem(STORAGE_KEY) : null;
    if (!raw) return normalizeLayout(DEFAULT_LAYOUT);
    const parsed = JSON.parse(raw) as unknown;
    if (!Array.isArray(parsed)) return normalizeLayout(DEFAULT_LAYOUT);
    return normalizeLayout(parsed as PedalLayout);
  } catch {
    return normalizeLayout(DEFAULT_LAYOUT);
  }
}

/** Persist the user's layout. Fully guarded - never throws. */
export function saveLayout(layout: PedalLayout): void {
  try {
    if (typeof localStorage === "undefined") return;
    localStorage.setItem(STORAGE_KEY, JSON.stringify(normalizeLayout(layout)));
  } catch {
    /* ignore quota / serialization / unavailable-storage errors */
  }
}

/** Return a NEW layout with `sw` removed from its current cell and inserted at
 *  (toRow, toCol). Indices are clamped into range. All ten switches stay
 *  present. Pure - does not mutate the input. Used by drag-and-drop. */
export function moveSwitch(
  layout: PedalLayout,
  sw: string,
  toRow: number,
  toCol: number,
): PedalLayout {
  // Deep copy so callers keep referential purity.
  const rows: string[][] = layout.map((r) => [...r]);

  // Remove every occurrence of `sw` (there should be exactly one).
  for (const row of rows) {
    for (let c = row.length - 1; c >= 0; c--) {
      if (row[c] === sw) row.splice(c, 1);
    }
  }

  // Clamp the target row. If there are no rows, create one.
  if (rows.length === 0) rows.push([]);
  const r = Math.max(0, Math.min(toRow, rows.length - 1));

  // Clamp the target column within the destination row (allow append at end).
  const c = Math.max(0, Math.min(toCol, rows[r].length));
  rows[r].splice(c, 0, sw);

  // Repair (drops rows emptied by the removal, restores anything odd).
  return normalizeLayout(rows);
}

/** The user label for a switch's binding, or "" when unbound / unlabeled.
 *  Never throws. */
export function labelForSwitch(bindings: Binding[], sw: string): string {
  return bindings.find((b) => b.switch === sw)?.label ?? "";
}

/** Whether a binding exists for the given switch. */
export function isBound(bindings: Binding[], sw: string): boolean {
  return bindings.some((b) => b.switch === sw);
}
