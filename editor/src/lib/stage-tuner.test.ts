import { describe, expect, it } from "vitest";
import { tunerReading } from "./stage-tuner";

describe("Kemper tuner feedback", () => {
  it("centres the native neutral value and identifies flat/sharp", () => {
    expect(tunerReading("A", 8192)).toMatchObject({ direction: "center", position: 0 });
    expect(tunerReading("A", 7000).direction).toBe("flat");
    expect(tunerReading("A", 9000).direction).toBe("sharp");
    expect(tunerReading("A", 0).position).toBe(-1);
    expect(tunerReading("A", 16383).position).toBe(1);
  });

  it("uses a symmetric centre window", () => {
    for (const value of [7992, 8192, 8392]) expect(tunerReading("C", value).direction).toBe("center");
    expect(tunerReading("C", 7991).direction).toBe("flat");
    expect(tunerReading("C", 8393).direction).toBe("sharp");
  });

  it("does not claim a valid pitch when the feedback is missing or invalid", () => {
    for (const value of [undefined, null, "8192", NaN, Infinity, -1, 16384]) {
      expect(tunerReading("A", value)).toMatchObject({ valid: false, direction: "waiting" });
    }
    for (const note of [undefined, null, "", "--", "H", "A440"]) {
      expect(tunerReading(note, 8192)).toMatchObject({ valid: false, direction: "waiting" });
    }
  });

  it("separates the note and musical accidental", () => {
    expect(tunerReading("F#", 8192)).toMatchObject({ note: "F", accidental: "♯" });
    expect(tunerReading("Db", 8192)).toMatchObject({ note: "D", accidental: "♭" });
  });
});
