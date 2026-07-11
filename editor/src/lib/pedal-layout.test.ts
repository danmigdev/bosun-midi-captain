import { describe, it, expect, beforeEach } from "vitest";
import {
  DEFAULT_LAYOUT,
  ALL_SWITCHES,
  normalizeLayout,
  loadLayout,
  saveLayout,
  moveSwitch,
  labelForSwitch,
  isBound,
  type PedalLayout,
} from "./pedal-layout";
import type { Binding } from "./protocol";

function binding(sw: string, label?: string): Binding {
  return { switch: sw, mode: "tap", ...(label !== undefined ? { label } : {}), actions: {} };
}

/** All switch names present in a layout (spacers excluded). */
function switchesOf(layout: PedalLayout): string[] {
  return layout.flat().filter((c) => c !== "");
}

// A Map-backed localStorage stub so loadLayout/saveLayout can be exercised
// without a DOM. Reset before each test.
beforeEach(() => {
  const store = new Map<string, string>();
  const stub = {
    getItem: (k: string) => (store.has(k) ? store.get(k)! : null),
    setItem: (k: string, v: string) => { store.set(k, String(v)); },
    removeItem: (k: string) => { store.delete(k); },
    clear: () => { store.clear(); },
    key: (i: number) => Array.from(store.keys())[i] ?? null,
    get length() { return store.size; },
  };
  (globalThis as { localStorage: unknown }).localStorage = stub;
});

describe("DEFAULT_LAYOUT", () => {
  it("flattened (minus spacers) holds all ten switches exactly once", () => {
    const flat = switchesOf(DEFAULT_LAYOUT);
    expect(flat).toHaveLength(10);
    expect(new Set(flat).size).toBe(10);
    expect([...flat].sort()).toEqual([...ALL_SWITCHES].sort());
  });
});

describe("normalizeLayout", () => {
  it("appends a switch missing from the layout", () => {
    const partial: PedalLayout = [["1", "2", "3", "4", "up"], ["A", "B", "C", "D"]]; // "down" missing
    const fixed = normalizeLayout(partial);
    const flat = switchesOf(fixed);
    expect(new Set(flat)).toEqual(new Set(ALL_SWITCHES));
    expect(flat).toHaveLength(10);
    // The missing one is appended as a new final row.
    expect(fixed[fixed.length - 1]).toContain("down");
  });

  it("dedupes a duplicated name and keeps ten unique switches", () => {
    const dup: PedalLayout = [["A", "A", "B", "C", "D"], ["1", "2", "3", "4", "up", "down"]];
    const fixed = normalizeLayout(dup);
    const flat = switchesOf(fixed);
    expect(flat).toHaveLength(10);
    expect(new Set(flat)).toEqual(new Set(ALL_SWITCHES));
  });

  it("drops unknown names", () => {
    const bogus: PedalLayout = [["A", "B", "C", "D", "X", "zzz"], ["1", "2", "3", "4", "up", "down"]];
    const fixed = normalizeLayout(bogus);
    const flat = switchesOf(fixed);
    expect(flat).not.toContain("X");
    expect(flat).not.toContain("zzz");
    expect(new Set(flat)).toEqual(new Set(ALL_SWITCHES));
  });

  it("preserves spacer cells", () => {
    const spaced: PedalLayout = [["A", "", "B", "C", "D"], ["1", "2", "3", "4", "up", "down"]];
    const fixed = normalizeLayout(spaced);
    expect(fixed[0]).toContain("");
    expect(switchesOf(fixed)).toHaveLength(10);
  });
});

describe("loadLayout", () => {
  it("returns a valid ten-switch layout when storage is empty", () => {
    const layout = loadLayout();
    expect(new Set(switchesOf(layout))).toEqual(new Set(ALL_SWITCHES));
  });

  it("returns a valid ten-switch layout when storage holds corrupt JSON", () => {
    localStorage.setItem("BOSUN_PEDAL_LAYOUT_V1", "{ not json ]");
    const layout = loadLayout();
    expect(new Set(switchesOf(layout))).toEqual(new Set(ALL_SWITCHES));
  });

  it("round-trips a saved layout through saveLayout", () => {
    const custom: PedalLayout = [["down", "up"], ["A", "B", "C", "D", "1", "2", "3", "4"]];
    saveLayout(custom);
    const layout = loadLayout();
    expect(new Set(switchesOf(layout))).toEqual(new Set(ALL_SWITCHES));
    expect(layout[0]).toEqual(["down", "up"]);
  });
});

describe("moveSwitch", () => {
  it("relocates a switch and keeps all ten present", () => {
    const start: PedalLayout = [["1", "2", "3", "4", "up"], ["A", "B", "C", "D", "down"]];
    const moved = moveSwitch(start, "down", 0, 0);
    expect(moved[0][0]).toBe("down");
    expect(new Set(switchesOf(moved))).toEqual(new Set(ALL_SWITCHES));
    expect(switchesOf(moved)).toHaveLength(10);
  });

  it("does not mutate the input layout", () => {
    const start: PedalLayout = [["1", "2"], ["A", "B", "C", "D", "3", "4", "up", "down"]];
    const snapshot = JSON.stringify(start);
    moveSwitch(start, "A", 0, 0);
    expect(JSON.stringify(start)).toBe(snapshot);
  });

  it("clamps out-of-range indices", () => {
    const start: PedalLayout = [["1", "2", "3", "4", "up"], ["A", "B", "C", "D", "down"]];
    const moved = moveSwitch(start, "up", 99, 99);
    expect(new Set(switchesOf(moved))).toEqual(new Set(ALL_SWITCHES));
  });
});

describe("labelForSwitch", () => {
  const bindings: Binding[] = [binding("A", "Drive"), binding("1")];

  it("returns the label of a bound, labeled switch", () => {
    expect(labelForSwitch(bindings, "A")).toBe("Drive");
  });

  it('returns "" for a bound switch with no label', () => {
    expect(labelForSwitch(bindings, "1")).toBe("");
  });

  it('returns "" for an unbound switch', () => {
    expect(labelForSwitch(bindings, "D")).toBe("");
  });

  it('returns "" without throwing on empty bindings', () => {
    expect(labelForSwitch([], "A")).toBe("");
  });
});

describe("isBound", () => {
  const bindings: Binding[] = [binding("A", "Drive")];

  it("is true for a bound switch", () => {
    expect(isBound(bindings, "A")).toBe(true);
  });

  it("is false for an unbound switch", () => {
    expect(isBound(bindings, "B")).toBe(false);
  });

  it("is false against empty bindings", () => {
    expect(isBound([], "A")).toBe(false);
  });
});
