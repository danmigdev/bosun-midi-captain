import type { Binding } from "./protocol";

/** A reusable, switch-agnostic binding the user has saved to their local
 * "favorites" library. Persisted in localStorage so it survives reloads and
 * can be pasted onto any switch/patch. The stored `binding` keeps whatever
 * `.switch` it was captured with; use `bindingFromSnippet` to retarget it. */
export interface Snippet {
  id: string;
  name: string;
  binding: Binding;
  createdAt: number;
}

const STORAGE_KEY = "BOSUN_SNIPPETS_V1";

/** Generate a unique id, preferring crypto.randomUUID when available and
 * falling back to a timestamp+random string in environments (older
 * runtimes, non-secure contexts) that don't expose it. */
function newId(): string {
  try {
    const c = (globalThis as { crypto?: Crypto }).crypto;
    if (c && typeof c.randomUUID === "function") return c.randomUUID();
  } catch {
    /* fall through to the fallback below */
  }
  return `s-${Date.now().toString(36)}-${Math.random().toString(36).slice(2, 10)}`;
}

/** Read and parse the raw snippet array from localStorage. Returns [] for
 * any failure (missing, private-mode throw, malformed JSON, wrong shape) so
 * callers never have to guard. */
function readAll(): Snippet[] {
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
    // Keep only entries that look like a Snippet; skip anything malformed.
    return parsed.filter(
      (s): s is Snippet =>
        !!s &&
        typeof s === "object" &&
        typeof s.id === "string" &&
        typeof s.name === "string" &&
        typeof s.createdAt === "number" &&
        !!s.binding &&
        typeof s.binding === "object",
    );
  } catch {
    return [];
  }
}

/** Persist the given snippet array. Swallows storage errors (private mode,
 * quota) so a failed write never crashes the caller. */
function writeAll(snippets: Snippet[]): void {
  try {
    globalThis.localStorage?.setItem(STORAGE_KEY, JSON.stringify(snippets));
  } catch {
    /* storage unavailable - best effort, nothing to do */
  }
}

/** All saved snippets, newest first. Never throws. */
export function listSnippets(): Snippet[] {
  return readAll().sort((a, b) => b.createdAt - a.createdAt);
}

/** Deep-clone `binding` and store it under a new snippet with `name`. The
 * clone means later mutations of the caller's binding don't leak into the
 * saved copy. Returns the newly created snippet. */
export function saveSnippet(name: string, binding: Binding): Snippet {
  const snippet: Snippet = {
    id: newId(),
    name,
    binding: structuredClone(binding),
    createdAt: Date.now(),
  };
  const all = readAll();
  all.push(snippet);
  writeAll(all);
  return snippet;
}

/** Remove the snippet with the given id (no-op if it doesn't exist). */
export function deleteSnippet(id: string): void {
  const all = readAll();
  const next = all.filter((s) => s.id !== id);
  if (next.length !== all.length) writeAll(next);
}

/** Rename the snippet with the given id (no-op if it doesn't exist). */
export function renameSnippet(id: string, name: string): void {
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

/** Produce a fresh, deep-cloned Binding from a snippet, retargeted to
 * `switchName`. The clone keeps snippets switch-agnostic: the same snippet
 * can be applied to any switch without the results aliasing each other or
 * the stored snippet. */
export function bindingFromSnippet(snippet: Snippet, switchName: string): Binding {
  const binding = structuredClone(snippet.binding);
  binding.switch = switchName;
  return binding;
}
