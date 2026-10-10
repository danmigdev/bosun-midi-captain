import { describe, it, expect } from "vitest";
import {
  labelForSwitch,
  isBound,
  layoutFor,
  type PedalLayout,
} from "./pedal-layout";
import { CAPTAIN_10, MINI_6 } from "./hardware";
import type { Binding } from "./protocol";

function binding(sw: string, label?: string): Binding {
  return { switch: sw, mode: "tap", ...(label !== undefined ? { label } : {}), actions: {} };
}

/** All switch names present in a layout (spacers excluded). */
function switchesOf(layout: PedalLayout): string[] {
  return layout.flat().filter((c) => c !== "");
}

describe("the 10-switch Captain layout", () => {
  it("flattened (minus spacers) holds all ten switches exactly once", () => {
    const flat = switchesOf(CAPTAIN_10.rows);
    expect(flat).toHaveLength(10);
    expect(new Set(flat).size).toBe(10);
    expect([...flat].sort()).toEqual([...CAPTAIN_10.switches].sort());
  });
});

describe("layoutFor", () => {
  it("draws the 10-switch Captain as two rows of five, also by default", () => {
    expect(layoutFor(CAPTAIN_10)).toEqual([["1", "2", "3", "4", "up"], ["A", "B", "C", "D", "down"]]);
    expect(layoutFor(undefined)).toEqual(CAPTAIN_10.rows);
    expect(layoutFor(null)).toEqual(CAPTAIN_10.rows);
  });

  it("draws the Mini 6 as two rows of three", () => {
    const layout = layoutFor(MINI_6);
    expect(layout).toEqual([["1", "2", "3"], ["A", "B", "C"]]);
    expect(switchesOf(layout)).not.toContain("up");
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
