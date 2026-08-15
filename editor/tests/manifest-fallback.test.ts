// Tests for the editor's manifest fallback path (src/lib/protocol.ts).
//
// When the firmware's GET_MANIFEST never lands (e.g. an older build
// truncating the large plugin manifest response), the editor falls back to
// the hardcoded core message types so Patches/Editor stay usable with core
// MIDI only. These tests pin that contract: the fallback is plugin-free,
// covers the essential core message types, and deep-clones the shared
// constant so callers can't corrupt it.

import { describe, it, expect } from "vitest";
import {
  fallbackManifest,
  CORE_MESSAGE_TYPES,
} from "../src/lib/protocol";

describe("fallbackManifest", () => {
  it("returns a manifest with core_messages and no plugins", () => {
    const m = fallbackManifest();
    expect(m.plugins).toEqual({});
    expect(m.core_messages).toBeDefined();
    expect(typeof m.core_messages).toBe("object");
    expect(Object.keys(m.core_messages).length).toBeGreaterThan(0);
  });

  it("covers the essential core message types", () => {
    const types = Object.keys(CORE_MESSAGE_TYPES);
    for (const required of ["cc", "pc", "note_on", "note_off", "captain_patch"]) {
      expect(types, `missing core type ${required}`).toContain(required);
    }
    // The same set is what the fallback ships.
    expect(Object.keys(fallbackManifest().core_messages)).toEqual(types);
  });

  it("deep-clones the constant (structuredClone) - no shared references", () => {
    const a = fallbackManifest();
    const b = fallbackManifest();

    // Fresh top-level objects and fresh nested schema objects.
    expect(a).not.toBe(b);
    expect(a.core_messages).not.toBe(b.core_messages);
    expect(a.core_messages.cc).not.toBe(CORE_MESSAGE_TYPES.cc);
    expect(a.core_messages.cc.params).not.toBe(CORE_MESSAGE_TYPES.cc.params);

    // Mutating one build must not leak into the constant or a later build.
    (a.core_messages.cc.params.value as { max: number }).max = 999;
    expect(CORE_MESSAGE_TYPES.cc.params.value.max).toBe(127);
    expect(b.core_messages.cc.params.value.max).toBe(127);
    expect(fallbackManifest().core_messages.cc.params.value.max).toBe(127);
  });
});
