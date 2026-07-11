import { describe, it, expect, beforeEach } from "vitest";

import {
  listSnippets,
  saveSnippet,
  deleteSnippet,
  renameSnippet,
  bindingFromSnippet,
} from "./snippets";
import type { Binding } from "./protocol";

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

function sampleBinding(overrides: Partial<Binding> = {}): Binding {
  return {
    switch: "1",
    mode: "tap",
    label: "Boost",
    led: { on: "#00ff00", off: "#000000" },
    actions: {
      press: { messages: [{ type: "cc", channel: 1, cc: 20, value: 127 }] },
    },
    ...overrides,
  };
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

describe("saveSnippet + listSnippets", () => {
  it("round-trips name, binding and a generated id", () => {
    const saved = saveSnippet("My Boost", sampleBinding());
    expect(saved.id).toBeTruthy();
    expect(typeof saved.id).toBe("string");
    expect(saved.name).toBe("My Boost");
    expect(saved.createdAt).toBeTypeOf("number");

    const list = listSnippets();
    expect(list).toHaveLength(1);
    expect(list[0].id).toBe(saved.id);
    expect(list[0].name).toBe("My Boost");
    expect(list[0].binding).toEqual(sampleBinding());
  });

  it("returns snippets sorted by createdAt descending", () => {
    const a = saveSnippet("first", sampleBinding());
    const b = saveSnippet("second", sampleBinding());
    // Force a distinct, higher createdAt on the second entry regardless of
    // clock resolution.
    const raw = JSON.parse(globalThis.localStorage.getItem("BOSUN_SNIPPETS_V1")!);
    for (const s of raw) {
      if (s.id === a.id) s.createdAt = 1000;
      if (s.id === b.id) s.createdAt = 2000;
    }
    globalThis.localStorage.setItem("BOSUN_SNIPPETS_V1", JSON.stringify(raw));

    const list = listSnippets();
    expect(list.map((s) => s.name)).toEqual(["second", "first"]);
  });

  it("stores a deep clone - mutating the input afterward doesn't change the stored copy", () => {
    const input = sampleBinding();
    saveSnippet("Immutable", input);

    input.label = "Mutated";
    input.actions.press.messages[0].value = 0;

    const list = listSnippets();
    expect(list[0].binding.label).toBe("Boost");
    expect(list[0].binding.actions.press.messages[0].value).toBe(127);
  });
});

describe("deleteSnippet", () => {
  it("removes the snippet with the given id", () => {
    const a = saveSnippet("a", sampleBinding());
    const b = saveSnippet("b", sampleBinding());
    deleteSnippet(a.id);

    const list = listSnippets();
    expect(list).toHaveLength(1);
    expect(list[0].id).toBe(b.id);
  });

  it("is a no-op for an unknown id", () => {
    saveSnippet("a", sampleBinding());
    deleteSnippet("does-not-exist");
    expect(listSnippets()).toHaveLength(1);
  });
});

describe("renameSnippet", () => {
  it("updates the name in place", () => {
    const s = saveSnippet("old", sampleBinding());
    renameSnippet(s.id, "new");

    const list = listSnippets();
    expect(list).toHaveLength(1);
    expect(list[0].id).toBe(s.id);
    expect(list[0].name).toBe("new");
  });

  it("is a no-op for an unknown id", () => {
    const s = saveSnippet("keep", sampleBinding());
    renameSnippet("nope", "changed");
    expect(listSnippets()[0].name).toBe("keep");
    expect(listSnippets()[0].id).toBe(s.id);
  });
});

describe("bindingFromSnippet", () => {
  it("sets .switch to the requested switch name", () => {
    const s = saveSnippet("boost", sampleBinding({ switch: "1" }));
    const out = bindingFromSnippet(s, "D");
    expect(out.switch).toBe("D");
    expect(out.label).toBe("Boost");
  });

  it("deep-clones - mutating the result doesn't affect the snippet", () => {
    const s = saveSnippet("boost", sampleBinding());
    const out = bindingFromSnippet(s, "A");
    out.label = "Changed";
    out.actions.press.messages[0].value = 0;

    expect(s.binding.label).toBe("Boost");
    expect(s.binding.actions.press.messages[0].value).toBe(127);
    expect(s.binding.switch).toBe("1");
  });
});

describe("listSnippets resilience", () => {
  it("returns [] when the stored JSON is corrupt", () => {
    globalThis.localStorage.setItem("BOSUN_SNIPPETS_V1", "not json");
    expect(listSnippets()).toEqual([]);
  });

  it("returns [] when nothing is stored", () => {
    expect(listSnippets()).toEqual([]);
  });
});
