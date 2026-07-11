import { describe, it, expect } from "vitest";
import {
  fallbackManifest,
  flattenManifest,
  defaultMessageFromSchema,
  continuousControlTypes,
  CORE_MESSAGE_TYPES,
  type Manifest,
} from "./protocol";

describe("fallbackManifest", () => {
  it("contains all core message types and no plugins", () => {
    const m = fallbackManifest();
    expect(Object.keys(m.plugins)).toEqual([]);
    expect(Object.keys(m.core_messages).sort()).toEqual(
      ["captain_bank_step", "captain_patch", "captain_preview_cancel",
       "captain_preview_commit", "captain_preview_step", "captain_setlist_step",
       "cc", "delay", "note_off", "note_on", "pc"],
    );
  });

  it("each core message carries a label and params", () => {
    const m = fallbackManifest();
    for (const [type, schema] of Object.entries(m.core_messages)) {
      expect(schema.label, `${type} label`).toBeTruthy();
      expect(schema.params, `${type} params`).toBeTypeOf("object");
    }
  });

  it("cc has the standard channel/cc/value int params (0-127 value)", () => {
    const cc = fallbackManifest().core_messages.cc;
    expect(cc.params.value).toMatchObject({ type: "int", min: 0, max: 127 });
    expect(cc.params.channel).toMatchObject({ type: "int", min: 1, max: 16 });
  });

  it("is flattenable into the editor's picker list, all sourced from core", () => {
    const flat = flattenManifest(fallbackManifest());
    expect(flat).toHaveLength(11);
    expect(flat.every(f => f.source === "core")).toBe(true);
    expect(flat.map(f => f.type)).toContain("cc");
  });

  it("produces usable default messages from the fallback schemas", () => {
    const cc = fallbackManifest().core_messages.cc;
    expect(defaultMessageFromSchema("cc", cc)).toEqual({
      type: "cc", channel: 1, cc: 0, value: 0,
    });
  });

  it("returns an independent deep clone (mutating one does not affect the constant)", () => {
    const a = fallbackManifest();
    (a.core_messages.cc.params.value as { max: number }).max = 999;
    // The shared constant and a fresh build must be untouched.
    expect(CORE_MESSAGE_TYPES.cc.params.value.max).toBe(127);
    expect(fallbackManifest().core_messages.cc.params.value.max).toBe(127);
  });
});

describe("continuousControlTypes", () => {
  const M: Manifest = {
    core_messages: CORE_MESSAGE_TYPES,
    plugins: {
      kemper_player: {
        label: "Kemper Player",
        version: "1",
        messages: {
          kemper_wah:    { label: "Wah",    params: { value: { type: "int", min: 0, max: 127 } } },
          kemper_volume: { label: "Volume", params: { channel: { type: "int", min: 1, max: 16 }, value: { type: "int", min: 0, max: 127 } } },
          // Not a continuous control: no value param.
          kemper_scene:  { label: "Scene",  params: { scene: { type: "int", min: 0, max: 4 } } },
          // value out of the 0-127 range -> excluded.
          kemper_narrow: { label: "Narrow", params: { value: { type: "int", min: 0, max: 63 } } },
        },
      },
    },
  };

  it("always lists CC first even with no manifest", () => {
    const out = continuousControlTypes(null);
    expect(out[0].type).toBe("cc");
  });

  it("includes plugin messages with a value int 0-127 and excludes others", () => {
    const types = continuousControlTypes(M).map(t => t.type);
    expect(types[0]).toBe("cc"); // core cc first
    expect(types).toContain("kemper_wah");
    expect(types).toContain("kemper_volume");
    expect(types).not.toContain("kemper_scene");
    expect(types).not.toContain("kemper_narrow");
  });

  it("labels plugin entries with their source", () => {
    const wah = continuousControlTypes(M).find(t => t.type === "kemper_wah");
    expect(wah?.label).toBe("Kemper Player · Wah");
  });

  it("does not duplicate cc and keeps it first when core provides it", () => {
    const out = continuousControlTypes(M);
    expect(out.filter(t => t.type === "cc")).toHaveLength(1);
    expect(out[0].type).toBe("cc");
  });
});
