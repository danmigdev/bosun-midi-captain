import { describe, it, expect, beforeEach } from "vitest";

import {
  listSetlists,
  getSetlist,
  createSetlist,
  updateSetlistItems,
  renameSetlist,
  deleteSetlist,
} from "./setlists";
import type { SetlistItem } from "./setlists";

/** Minimal in-memory localStorage backed by a Map, matching the subset of the
 * Storage API our module touches (getItem/setItem/removeItem). */
function makeMemoryStorage() {
  const map = new Map<string, string>();
  return {
    getItem: (k: string): string | null => (map.has(k) ? map.get(k)! : null),
    setItem: (k: string, v: string): void => {
      map.set(k, String(v));
    },
    removeItem: (k: string): void => {
      map.delete(k);
    },
    _map: map,
  };
}

function sampleItems(): SetlistItem[] {
  return [
    { bank: 1, slot: 1 },
    { bank: 2, slot: 3 },
  ];
}

beforeEach(() => {
  // Fresh storage per test so state never leaks across cases.
  (globalThis as { localStorage: unknown }).localStorage = makeMemoryStorage();
  // Ensure crypto.randomUUID exists (some test runtimes don't expose it).
  const c = (globalThis as { crypto?: Partial<Crypto> }).crypto;
  if (!c || typeof c.randomUUID !== "function") {
    let n = 0;
    (globalThis as { crypto: Partial<Crypto> }).crypto = {
      ...(c ?? {}),
      randomUUID: (() => `uuid-${++n}` as `${string}-${string}-${string}-${string}-${string}`),
    } as Crypto;
  }
});

describe("createSetlist + listSetlists", () => {
  it("round-trips name, items and a generated id", () => {
    const created = createSetlist("Friday gig", sampleItems());
    expect(created.id).toBeTruthy();
    expect(typeof created.id).toBe("string");
    expect(created.name).toBe("Friday gig");
    expect(created.items).toEqual(sampleItems());

    const list = listSetlists();
    expect(list).toHaveLength(1);
    expect(list[0].id).toBe(created.id);
    expect(list[0].name).toBe("Friday gig");
    expect(list[0].items).toEqual(sampleItems());
  });

  it("defaults items to an empty array", () => {
    const created = createSetlist("Empty");
    expect(created.items).toEqual([]);
    expect(listSetlists()[0].items).toEqual([]);
  });

  it("deep-copies items - mutating the input afterward doesn't change the stored copy", () => {
    const input = sampleItems();
    createSetlist("Immutable", input);

    input[0].bank = 99;
    input.push({ bank: 5, slot: 5 });

    const list = listSetlists();
    expect(list[0].items).toEqual(sampleItems());
  });
});

describe("getSetlist", () => {
  it("returns the setlist matching the id", () => {
    const a = createSetlist("a", [{ bank: 1, slot: 1 }]);
    const b = createSetlist("b", [{ bank: 2, slot: 2 }]);

    expect(getSetlist(a.id)?.name).toBe("a");
    expect(getSetlist(b.id)?.items).toEqual([{ bank: 2, slot: 2 }]);
  });

  it("returns undefined for an unknown id", () => {
    createSetlist("a");
    expect(getSetlist("nope")).toBeUndefined();
  });
});

describe("updateSetlistItems", () => {
  it("replaces the items of the given setlist", () => {
    const s = createSetlist("gig", sampleItems());
    const next: SetlistItem[] = [{ bank: 9, slot: 9 }];
    updateSetlistItems(s.id, next);

    expect(getSetlist(s.id)?.items).toEqual([{ bank: 9, slot: 9 }]);
  });

  it("deep-copies - mutating the input afterward doesn't change the stored copy", () => {
    const s = createSetlist("gig");
    const next: SetlistItem[] = [{ bank: 1, slot: 1 }];
    updateSetlistItems(s.id, next);

    next[0].bank = 42;
    next.push({ bank: 7, slot: 7 });

    expect(getSetlist(s.id)?.items).toEqual([{ bank: 1, slot: 1 }]);
  });

  it("is a no-op for an unknown id", () => {
    const s = createSetlist("gig", sampleItems());
    updateSetlistItems("nope", [{ bank: 8, slot: 8 }]);
    expect(getSetlist(s.id)?.items).toEqual(sampleItems());
  });
});

describe("renameSetlist", () => {
  it("updates the name in place", () => {
    const s = createSetlist("old");
    renameSetlist(s.id, "new");

    const list = listSetlists();
    expect(list).toHaveLength(1);
    expect(list[0].id).toBe(s.id);
    expect(list[0].name).toBe("new");
  });

  it("is a no-op for an unknown id", () => {
    const s = createSetlist("keep");
    renameSetlist("nope", "changed");
    expect(listSetlists()[0].name).toBe("keep");
    expect(listSetlists()[0].id).toBe(s.id);
  });
});

describe("deleteSetlist", () => {
  it("removes the setlist with the given id", () => {
    const a = createSetlist("a");
    const b = createSetlist("b");
    deleteSetlist(a.id);

    const list = listSetlists();
    expect(list).toHaveLength(1);
    expect(list[0].id).toBe(b.id);
  });

  it("is a no-op for an unknown id", () => {
    createSetlist("a");
    deleteSetlist("does-not-exist");
    expect(listSetlists()).toHaveLength(1);
  });
});

describe("listSetlists resilience", () => {
  it("returns [] when the stored JSON is corrupt", () => {
    globalThis.localStorage.setItem("BOSUN_SETLISTS_V1", "not json");
    expect(listSetlists()).toEqual([]);
  });

  it("returns [] when nothing is stored", () => {
    expect(listSetlists()).toEqual([]);
  });
});
