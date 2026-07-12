import { describe, it, expect } from "vitest";
import { decodeMidi, noteName, toHex } from "./midi-decode";

describe("noteName", () => {
  it("uses the C4 = 60 convention", () => {
    expect(noteName(60)).toBe("C4");
    expect(noteName(69)).toBe("A4"); // A440
    expect(noteName(0)).toBe("C-1");
  });
});

describe("toHex", () => {
  it("renders upper-case zero-padded bytes", () => {
    expect(toHex([0xb0, 0x07, 0x64])).toBe("B0 07 64");
    expect(toHex([0x00, 0x0f])).toBe("00 0F");
  });
});

describe("decodeMidi channel-voice", () => {
  it("decodes CC with a known name and channel", () => {
    const d = decodeMidi([0xb0, 7, 100]); // CC7 vol on ch1
    expect(d.kind).toBe("cc");
    expect(d.channel).toBe(1);
    expect(d.label).toBe("CC 7 (Volume) = 100");
  });

  it("shows the channel from the status low nibble", () => {
    const d = decodeMidi([0xb3, 11, 64]); // ch4
    expect(d.channel).toBe(4);
    expect(d.label).toContain("Expression");
  });

  it("decodes Program Change", () => {
    const d = decodeMidi([0xc2, 12]);
    expect(d.kind).toBe("program_change");
    expect(d.channel).toBe(3);
    expect(d.label).toBe("Program Change 12");
  });

  it("decodes Note On and Note Off", () => {
    expect(decodeMidi([0x90, 60, 96]).label).toBe("Note On C4 vel 96");
    expect(decodeMidi([0x80, 60, 0]).kind).toBe("note_off");
  });

  it("treats Note On velocity 0 as Note Off", () => {
    const d = decodeMidi([0x90, 60, 0]);
    expect(d.kind).toBe("note_off");
  });

  it("decodes 14-bit pitch bend relative to center", () => {
    expect(decodeMidi([0xe0, 0x00, 0x40]).label).toBe("Pitch Bend 0"); // center
    expect(decodeMidi([0xe0, 0x00, 0x00]).label).toBe("Pitch Bend -8192");
  });
});

describe("decodeMidi sysex", () => {
  it("counts inner bytes without the F0/F7 framing", () => {
    const d = decodeMidi([0xf0, 0x00, 0x20, 0x33, 0x02, 0xf7]);
    expect(d.kind).toBe("sysex");
    expect(d.channel).toBe(null);
    expect(d.label).toBe("SysEx (4 bytes)");
  });

  it("handles an unterminated sysex", () => {
    const d = decodeMidi([0xf0, 0x01, 0x02]);
    expect(d.kind).toBe("sysex");
    expect(d.label).toBe("SysEx (2 bytes)");
  });
});

describe("decodeMidi robustness", () => {
  it("does not throw on empty input", () => {
    expect(decodeMidi([]).kind).toBe("unknown");
  });

  it("flags a non-status leading byte as unknown", () => {
    expect(decodeMidi([0x40, 0x01]).kind).toBe("unknown");
  });
});
