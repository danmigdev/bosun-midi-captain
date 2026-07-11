/** A single entry in a setlist: a reference to a patch by its grid position
 * (bank + slot). A setlist is an ordered list of these - the same patch may
 * appear more than once (a song can reuse a patch). */
export interface SetlistItem {
  bank: number;
  slot: number;
}

/** A named, ordered list of patches - a gig's songs in play order, independent
 * of where the patches live in the 99x10 grid. Persisted in localStorage so it
 * survives reloads and can be sent to the pedal. */
export interface Setlist {
  id: string;
  name: string;
  items: SetlistItem[];
}

const STORAGE_KEY = "BOSUN_SETLISTS_V1";

/** Generate a unique id, preferring crypto.randomUUID when available and
 * falling back to a timestamp+random string in environments (older runtimes,
 * non-secure contexts) that don't expose it. */
function newId(): string {
  try {
    const c = (globalThis as { crypto?: Crypto }).crypto;
    if (c && typeof c.randomUUID === "function") return c.randomUUID();
  } catch {
    /* fall through to the fallback below */
  }
  return `sl-${Date.now().toString(36)}-${Math.random().toString(36).slice(2, 10)}`;
}

/** Deep-copy an items array, keeping only well-formed { bank, slot } entries so
 * malformed stored data can never leak into callers. */
function cloneItems(items: SetlistItem[]): SetlistItem[] {
  return items
    .filter(
      (it): it is SetlistItem =>
        !!it &&
        typeof it === "object" &&
        typeof it.bank === "number" &&
        typeof it.slot === "number",
    )
    .map((it) => ({ bank: it.bank, slot: it.slot }));
}

/** Read and parse the raw setlist array from localStorage. Returns [] for any
 * failure (missing, private-mode throw, malformed JSON, wrong shape) so callers
 * never have to guard. */
function readAll(): Setlist[] {
  let raw: string | null = null;
  try {
    raw = globalThis.localStorage?.getItem(STORAGE_KEY) ?? null;
  } catch {
    return [];
  }
  if (!raw) return [];
  try {
    const parsed = JSON.parse(raw);
    if (!Array.isArray(parsed)) return [];
    // Keep only entries that look like a Setlist; skip anything malformed.
    return parsed
      .filter(
        (s): s is Setlist =>
          !!s &&
          typeof s === "object" &&
          typeof s.id === "string" &&
          typeof s.name === "string" &&
          Array.isArray(s.items),
      )
      .map((s) => ({ id: s.id, name: s.name, items: cloneItems(s.items) }));
  } catch {
    return [];
  }
}

/** Persist the given setlist array. Swallows storage errors (private mode,
 * quota) so a failed write never crashes the caller. */
function writeAll(setlists: Setlist[]): void {
  try {
    globalThis.localStorage?.setItem(STORAGE_KEY, JSON.stringify(setlists));
  } catch {
    /* storage unavailable - best effort, nothing to do */
  }
}

/** All saved setlists, in stored order. Never throws. */
export function listSetlists(): Setlist[] {
  return readAll();
}

/** The setlist with the given id, or undefined if none matches. */
export function getSetlist(id: string): Setlist | undefined {
  return readAll().find((s) => s.id === id);
}

/** Create a setlist with `name` and (deep-copied) `items` under a fresh id,
 * persist it, and return the newly created setlist. */
export function createSetlist(name: string, items: SetlistItem[] = []): Setlist {
  const setlist: Setlist = {
    id: newId(),
    name,
    items: cloneItems(items),
  };
  const all = readAll();
  all.push(setlist);
  writeAll(all);
  return setlist;
}

/** Replace the items of the setlist with the given id (deep-copied) and
 * persist. No-op if the setlist doesn't exist. */
export function updateSetlistItems(id: string, items: SetlistItem[]): void {
  const all = readAll();
  let changed = false;
  for (const s of all) {
    if (s.id === id) {
      s.items = cloneItems(items);
      changed = true;
    }
  }
  if (changed) writeAll(all);
}

/** Rename the setlist with the given id (no-op if it doesn't exist). */
export function renameSetlist(id: string, name: string): void {
  const all = readAll();
  let changed = false;
  for (const s of all) {
    if (s.id === id) {
      s.name = name;
      changed = true;
    }
  }
  if (changed) writeAll(all);
}

/** Remove the setlist with the given id (no-op if it doesn't exist). */
export function deleteSetlist(id: string): void {
  const all = readAll();
  const next = all.filter((s) => s.id !== id);
  if (next.length !== all.length) writeAll(next);
}
