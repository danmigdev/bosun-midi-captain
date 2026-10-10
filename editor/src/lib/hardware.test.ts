import { describe, it, expect } from "vitest";
import {
  CAPTAIN_10,
  MINI_6,
  KNOWN_HARDWARE,
  expressionJacks,
  hardwareLabel,
  hasSwitch,
  knownHardware,
  missingSwitches,
  parseHardware,
} from "./hardware";

/** DEVICE_INFO.hardware exactly as firmware 0.8 sends it. */
const MINI_6_WIRE = {
  model: "mini6", name: "MIDI Captain Mini 6", configured: true,
  switches: ["1", "2", "3", "A", "B", "C"], rows: [["1", "2", "3"], ["A", "B", "C"]],
  led_count: 18, expression_jacks: 0, models: ["captain10", "mini6"],
};

describe("known models", () => {
  it("describe every switch once, in row-major chain order", () => {
    for (const hardware of KNOWN_HARDWARE) {
      expect(hardware.rows.flat()).toEqual(hardware.switches);
      expect(new Set(hardware.switches).size).toBe(hardware.switches.length);
      expect(hardware.led_count).toBe(hardware.switches.length * 3);
    }
    expect(CAPTAIN_10.switches).toEqual(["1", "2", "3", "4", "up", "A", "B", "C", "D", "down"]);
    expect(MINI_6.switches).toEqual(["1", "2", "3", "A", "B", "C"]);
    expect(MINI_6.expression_jacks).toBe(0);
  });

  it("cannot be edited through a shared reference", () => {
    expect(Object.isFrozen(CAPTAIN_10)).toBe(true);
    expect(Object.isFrozen(CAPTAIN_10.switches)).toBe(true);
    expect(Object.isFrozen(MINI_6.rows[0])).toBe(true);
  });

  it("are found by model id and labelled with their switch count", () => {
    expect(knownHardware("mini6")).toBe(MINI_6);
    expect(knownHardware("nano4")).toBeNull();
    expect(knownHardware(undefined)).toBeNull();
    expect(hardwareLabel(MINI_6)).toBe("MIDI Captain Mini 6 (6 switches)");
    expect(hardwareLabel(CAPTAIN_10)).toBe("MIDI Captain (10 switches)");
  });
});

describe("parseHardware", () => {
  it("treats firmware without a descriptor as the 10-switch Captain", () => {
    for (const raw of [undefined, null, "mini6", 6, [], true]) {
      expect(parseHardware(raw)).toBe(CAPTAIN_10);
    }
  });

  it("adopts a firmware Mini 6 descriptor", () => {
    const parsed = parseHardware(MINI_6_WIRE);
    expect(parsed).toMatchObject({
      model: "mini6", name: "MIDI Captain Mini 6", configured: true,
      switches: MINI_6.switches, rows: MINI_6.rows, led_count: 18, expression_jacks: 0,
      models: ["captain10", "mini6"],
    });
  });

  it("reports whether the model came from a persisted record", () => {
    expect(parseHardware({ ...MINI_6_WIRE, configured: false }).configured).toBe(false);
    expect(parseHardware({ ...MINI_6_WIRE, configured: "yes" }).configured).toBe(false);
  });

  it("falls back to the editor's table when a known model sends unusable switches", () => {
    for (const switches of [undefined, [], ["1", "1"], ["1", ""], "1,2,3", Array(11).fill("x").map((x, i) => x + i)]) {
      const parsed = parseHardware({ ...MINI_6_WIRE, switches });
      expect(parsed.switches).toEqual(MINI_6.switches);
      expect(parsed.rows).toEqual(MINI_6.rows);
      expect(parsed.configured).toBe(true);
    }
    expect(parseHardware({ model: "nano4", switches: [] })).toBe(CAPTAIN_10);
  });

  it("repairs rows that do not match the switch list", () => {
    expect(parseHardware({ ...MINI_6_WIRE, rows: [["1", "2", "3"]] }).rows).toEqual(MINI_6.rows);
    expect(parseHardware({ ...MINI_6_WIRE, rows: "x" }).rows).toEqual(MINI_6.rows);
    const unknown = parseHardware({ model: "nano4", switches: ["1", "2", "A", "B"], rows: [["1", "2", "A"]] });
    expect(unknown.rows).toEqual([["1", "2", "A", "B"]]);
  });

  it("accepts a future model described by the firmware", () => {
    const parsed = parseHardware({
      model: "nano4", name: "MIDI Captain Nano 4", switches: ["1", "2", "A", "B"],
      rows: [["1", "2"], ["A", "B"]], led_count: 12, expression_jacks: 1,
    });
    expect(parsed).toMatchObject({ model: "nano4", switches: ["1", "2", "A", "B"], rows: [["1", "2"], ["A", "B"]], expression_jacks: 1, models: [] });
  });

  it("bounds jack counts and keeps unique model ids", () => {
    expect(parseHardware({ ...MINI_6_WIRE, expression_jacks: 9 }).expression_jacks).toBe(2);
    expect(parseHardware({ ...MINI_6_WIRE, expression_jacks: -1 }).expression_jacks).toBe(0);
    expect(parseHardware({ ...MINI_6_WIRE, expression_jacks: 1.5 }).expression_jacks).toBe(0);
    expect(parseHardware({ ...MINI_6_WIRE, models: ["mini6", "mini6", 3, "", "captain10"] }).models)
      .toEqual(["mini6", "captain10"]);
    expect(parseHardware({ ...MINI_6_WIRE, models: "mini6" }).models).toEqual([]);
  });

  it("never shares mutable arrays with the caller's message", () => {
    const wire = structuredClone(MINI_6_WIRE);
    const parsed = parseHardware(wire);
    wire.switches.push("D");
    wire.rows[0].push("D");
    expect(parsed.switches).toEqual(MINI_6.switches);
    expect(parsed.rows).toEqual(MINI_6.rows);
  });
});

describe("switch helpers", () => {
  it("find switches a configuration names but this pedal lacks", () => {
    expect(hasSwitch(MINI_6, "A")).toBe(true);
    expect(hasSwitch(MINI_6, "up")).toBe(false);
    expect(missingSwitches(MINI_6, ["1", "up", "A", "down", "up", "4"])).toEqual(["up", "down", "4"]);
    expect(missingSwitches(CAPTAIN_10, ["1", "up", "A", "down"])).toEqual([]);
  });

  it("list the pedal's expression jacks", () => {
    expect(expressionJacks(CAPTAIN_10)).toEqual([1, 2]);
    expect(expressionJacks(MINI_6)).toEqual([]);
  });
});
