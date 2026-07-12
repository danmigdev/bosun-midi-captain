import { describe, it, expect } from "vitest";

import type { Binding } from "./protocol";
import { parseHex, rgbToHex, ledColorFor } from "./led-color";

// These tests pin the editor's LED preview to the firmware's _color_for
// (firmware/lib/captain/leds.py). The load-bearing behaviour: a latched-off
// switch must NEVER go black - it scales the on colour by dim/255 (integer
// floor) whenever led.off is absent or explicitly black. The default dim is 64,
// which is ~/4 (64/255 == 0.251), matching the historical 25%.

function makeBinding(over: Partial<Binding>): Binding {
  return {
    switch: "1",
    mode: "tap",
    actions: {},
    ...over,
  };
}

describe("parseHex", () => {
  it("parses a valid '#rrggbb'", () => {
    expect(parseHex("#1f2e3d")).toEqual([0x1f, 0x2e, 0x3d]);
  });

  it("returns [0,0,0] for an empty string", () => {
    expect(parseHex("")).toEqual([0, 0, 0]);
  });

  it("returns [0,0,0] when the leading '#' is missing", () => {
    expect(parseHex("1f2e3d")).toEqual([0, 0, 0]);
  });

  it("returns [0,0,0] for the wrong length", () => {
    expect(parseHex("#abc")).toEqual([0, 0, 0]);
  });

  it("returns [0,0,0] for non-hex digits", () => {
    expect(parseHex("#gggggg")).toEqual([0, 0, 0]);
  });
});

describe("rgbToHex", () => {
  it("round-trips and zero-pads to lowercase", () => {
    expect(rgbToHex([0x1f, 0x2e, 0x3d])).toBe("#1f2e3d");
    expect(rgbToHex([0, 0, 0])).toBe("#000000");
    expect(rgbToHex([31, 31, 31])).toBe("#1f1f1f");
  });
});

describe("ledColorFor", () => {
  it("(a) tap mode returns led.on", () => {
    const b = makeBinding({ mode: "tap", led: { on: "#123456" } });
    expect(ledColorFor(b, false)).toBe("#123456");
  });

  it("(b) latched + latchedOn=true returns led.on", () => {
    const b = makeBinding({ mode: "latched", led: { on: "#123456" } });
    expect(ledColorFor(b, true)).toBe("#123456");
  });

  it("(c) latched + off + explicit non-black led.off returns that off colour", () => {
    const b = makeBinding({ mode: "latched", led: { on: "#123456", off: "#654321" } });
    expect(ledColorFor(b, false)).toBe("#654321");
  });

  it("(d) latched + off + led.off missing dims led.on by /4", () => {
    const b = makeBinding({ mode: "latched", led: { on: "#404040" } });
    // 0x40 = 64 -> floor(64/4) = 16 = 0x10
    expect(ledColorFor(b, false)).toBe("#101010");
  });

  it("(e) latched + off + led.off='#000000' also dims led.on by /4 (not black)", () => {
    const b = makeBinding({ mode: "latched", led: { on: "#404040", off: "#000000" } });
    const result = ledColorFor(b, false);
    expect(result).not.toBe("#000000");
    expect(result).toBe("#101010");
  });

  it("(f) integer-floor proof: on '#7d7d7d' (125) dims to '#1f1f1f' (31)", () => {
    const b = makeBinding({ mode: "latched", led: { on: "#7d7d7d" } });
    // 125 / 4 = 31.25 -> floor -> 31 = 0x1f (default 25% == /4)
    expect(ledColorFor(b, false)).toBe("#1f1f1f");
  });

  it("(g) configurable dim on the 0-255 scale: lower value gives a fainter off state", () => {
    const b = makeBinding({ mode: "latched", led: { on: "#808080" } }); // 128
    // dim 32 -> floor(128*32/255) = 16 = 0x10
    expect(ledColorFor(b, false, 32)).toBe("#101010");
    // dim 128 -> floor(128*128/255) = 64 = 0x40
    expect(ledColorFor(b, false, 128)).toBe("#404040");
    // dim 255 -> full on colour (no dimming)
    expect(ledColorFor(b, false, 255)).toBe("#808080");
    // dim 0 -> black (fully off)
    expect(ledColorFor(b, false, 0)).toBe("#000000");
    // default (omitted) == 64 -> floor(128*64/255) = 32 = 0x20
    expect(ledColorFor(b, false)).toBe("#202020");
  });
});
