import { describe, it, expect } from "vitest";
import {
  DEFAULT_LAYOUT,
  ALL_SWITCHES,
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

describe("DEFAULT_LAYOUT", () => {
  it("flattened (minus spacers) holds all ten switches exactly once", () => {
    const flat = switchesOf(DEFAULT_LAYOUT);
    expect(flat).toHaveLength(10);
    expect(new Set(flat).size).toBe(10);
    expect([...flat].sort()).toEqual([...ALL_SWITCHES].sort());
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
