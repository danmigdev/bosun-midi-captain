// Scenarios mirror tools/fsm_test.py - keep the two in sync.
//
// The Python harness drives a debounced hardware pin: a press/release only
// "settles" on the second poll (~6ms after the raw edge), so press_start is
// registered at that settle time. This simulator drives CLEAN edges, so the
// press()/release()/tick() timestamps below correspond to the Python settle
// times (e.g. Python polls at t=100 then t=106 -> here we press at 106).

import { describe, it, expect } from "vitest";
import { SwitchFsm } from "./switch-fsm";

function fresh(opts = {}) {
  return new SwitchFsm({
    longPressMs: 600,
    doubleTapWindowMs: 250,
    autoMomentaryOnHold: true,
    autoMomentaryMs: 500,
    ...opts,
  });
}

describe("tap", () => {
  it("press fires once, release silent", () => {
    const fsm = fresh();
    expect(fsm.press(106, "tap")).toEqual(["press"]);
    expect(fsm.release(206, "tap")).toEqual([]);
  });
});

describe("latched", () => {
  it("alternates toggle_on / toggle_off across presses", () => {
    const fsm = fresh({ autoMomentaryOnHold: false });
    const expected = ["toggle_on", "toggle_off", "toggle_on"];
    expected.forEach((exp, i) => {
      const t = 1000 + i * 200 + 6;
      expect(fsm.press(t, "latched")).toEqual([exp]);
      fsm.release(t + 50, "latched");
    });
    expect(fsm.latchedOn).toBe(true);
  });
});

describe("momentary", () => {
  it("press fires press, release fires release", () => {
    const fsm = fresh();
    expect(fsm.press(106, "momentary")).toEqual(["press"]);
    expect(fsm.release(206, "momentary")).toEqual(["release"]);
  });
});

describe("long_press_alt", () => {
  it("quick press fires 'press' on release", () => {
    const fsm = fresh({ longPressMs: 600 });
    expect(fsm.press(106, "long_press_alt")).toEqual([]);
    // release at 306ms - under threshold, fires 'press'
    expect(fsm.release(306, "long_press_alt")).toEqual(["press"]);
  });

  it("long hold fires 'long_press' on tick, release is silent", () => {
    const fsm = fresh({ longPressMs: 600 });
    expect(fsm.press(1006, "long_press_alt")).toEqual([]);
    // tick at 1700ms (>600ms after press) - long-press fires
    expect(fsm.tick(1700, "long_press_alt")).toEqual(["long_press"]);
    // release after long_press fired - should NOT also fire 'press'
    expect(fsm.release(1806, "long_press_alt")).toEqual([]);
  });

  it("boundary: fires exactly at the threshold, not 1ms under", () => {
    const fsm = fresh({ longPressMs: 600 });
    // press settles at t=1006; threshold = 1606.
    fsm.press(1006, "long_press_alt");
    expect(fsm.tick(1605, "long_press_alt")).toEqual([]);
    expect(fsm.tick(1606, "long_press_alt")).toEqual(["long_press"]);
  });
});

describe("double_tap", () => {
  it("two taps in the window fire 'double_tap'", () => {
    const fsm = fresh({ doubleTapWindowMs: 250 });
    // First press arms the window
    expect(fsm.press(1006, "double_tap")).toEqual([]);
    fsm.release(1066, "double_tap");
    // Second press at 1156ms - well inside window
    expect(fsm.press(1156, "double_tap")).toEqual(["double_tap"]);
  });

  it("a lone tap resolves to 'press' after the window expires", () => {
    const fsm = fresh({ doubleTapWindowMs: 250 });
    expect(fsm.press(2006, "double_tap")).toEqual([]);
    fsm.release(2066, "double_tap");
    // Tick past the window (2006 + 250 = 2256)
    expect(fsm.tick(2300, "double_tap")).toEqual(["press"]);
  });
});

describe("auto-momentary", () => {
  it("hold past threshold then release reverts latched", () => {
    const fsm = fresh({ autoMomentaryOnHold: true, autoMomentaryMs: 500 });
    // First press (quick tap) - latches on
    expect(fsm.press(1006, "latched")).toEqual(["toggle_on"]);
    fsm.release(1106, "latched");
    expect(fsm.latchedOn).toBe(true);

    // Second press, hold 700ms, release - should revert (toggle_off then back)
    expect(fsm.press(2006, "latched")).toEqual(["toggle_off"]);
    expect(fsm.latchedOn).toBe(false);
    // Held > 500ms -> release reverts back to True (another toggle_on fires)
    expect(fsm.release(2712, "latched")).toEqual(["toggle_on"]);
    expect(fsm.latchedOn).toBe(true);
  });

  it("short tap does NOT revert", () => {
    const fsm = fresh({ autoMomentaryOnHold: true, autoMomentaryMs: 500 });
    expect(fsm.press(1006, "latched")).toEqual(["toggle_on"]);
    // only ~200ms held - well under 500ms
    expect(fsm.release(1206, "latched")).toEqual([]);
    expect(fsm.latchedOn).toBe(true);
  });

  it("disabled auto-momentary: long hold does NOT revert", () => {
    const fsm = fresh({ autoMomentaryOnHold: false, autoMomentaryMs: 500 });
    expect(fsm.press(1006, "latched")).toEqual(["toggle_on"]);
    // ~1000ms held - would normally revert, but the feature is off
    expect(fsm.release(2006, "latched")).toEqual([]);
    expect(fsm.latchedOn).toBe(true);
  });
});

describe("reset", () => {
  it("clears latched state and timers", () => {
    const fsm = fresh();
    fsm.press(1006, "latched");
    expect(fsm.latchedOn).toBe(true);
    fsm.reset();
    expect(fsm.latchedOn).toBe(false);
  });
});
