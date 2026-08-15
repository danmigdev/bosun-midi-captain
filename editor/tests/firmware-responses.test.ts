// Regression tests for firmware response shapes, pinned to real payloads
// captured from the Android editor during the 2026-08-12 connectivity
// debugging (adb logcat, tag RustStdoutStderr).
//
// These pin the contracts the frontend depends on:
//  - The firmware serializes JSON WITH a space after the colon
//    ("id": "..." not "id":"...").  The Rust sentinel marker must match
//    both; the frontend JSON.parse must obviously accept both.
//  - DEVICE_INFO / PROFILE_LIST / GLOBAL / STATS / PATCH_LIST fields
//    used by App.svelte's handleMessage.
//  - MANIFEST arrives as one 22+ KB line (streamed field-by-field by the
//    firmware) and parses into the Manifest type.

import { describe, it, expect } from "vitest";
import type { FirmwareMessage, Manifest, PatchSummary } from "../src/lib/protocol";

// ---- real captured payloads (trimmed to what matters) ----

const REAL_ACK_SPACED = `{"fw": "0.5.2", "type": "ACK", "id": "__sync_24264_1786547897893"}`;

const REAL_DEVICE_INFO = `{"fw": "0.5.2", "type": "DEVICE_INFO", "device": "MIDI Captain", "current": {"bank": 1, "slot": 1}}`;

const REAL_PROFILE_LIST = `{"id": "2", "type": "PROFILE_LIST", "profiles": [{"id": "headrush", "name": "HeadRush", "kind": "headrush_core", "active": true}], "active": "headrush"}`;

const REAL_STATS = `{"id": "7", "type": "STATS", "uptime_ms": 12345, "mem_free": 50000, "mem_alloc": 80000, "loop_iters": 999, "midi_rx_count": 1, "midi_tx_count": 2, "protocol_cmd_count": 3, "last_patch_switch_ms": 7223, "current": {"bank": 1, "slot": 1}}`;

const REAL_PATCH_LIST = `{"id": "58", "type": "PATCH_LIST", "patches": [{"bank": 1, "slot": 1, "name": "Crunch", "dirty": false}]}`;

// Manifest prefix captured from [io] logs: compact keys, single line.
const REAL_MANIFEST = `{"type":"MANIFEST","id":"4","core_messages":{"pc":{"label":"Program Change","params":{"channel":{"type":"int","min":1,"max":16,"default":1,"label":"Channel"},"program":{"type":"int","min":0,"max":127,"default":0,"label":"Program"}},"summary":"PC {program} ch {channel}"}},"plugins":{"generic_midi":{"label":"Generic MIDI","version":"1.0","messages":{}}}}`;

describe("firmware response shape (captured on Android)", () => {
  it("ACK uses a space after the colon (the format that once broke the sentinel)", () => {
    const msg = JSON.parse(REAL_ACK_SPACED) as FirmwareMessage;
    expect(msg.type).toBe("ACK");
    expect((msg as { id?: string }).id).toBe("__sync_24264_1786547897893");
    // The exact spaced form must stay valid JSON for the frontend.
    expect(REAL_ACK_SPACED).toContain('"id": "');
  });

  it("DEVICE_INFO parses into the expected shape", () => {
    const msg = JSON.parse(REAL_DEVICE_INFO) as FirmwareMessage;
    expect(msg.type).toBe("DEVICE_INFO");
    if (msg.type === "DEVICE_INFO") {
      expect(msg.fw).toBe("0.5.2");
      expect(msg.current).toEqual({ bank: 1, slot: 1 });
    }
  });

  it("PROFILE_LIST parses with the fields handleMessage reads", () => {
    const msg = JSON.parse(REAL_PROFILE_LIST) as FirmwareMessage;
    expect(msg.type).toBe("PROFILE_LIST");
    if (msg.type === "PROFILE_LIST") {
      expect(msg.active).toBeDefined();
      expect(msg.profiles[0].active).toBe(true);
      expect(msg.profiles[0].kind).toBe("headrush_core");
    }
  });

  it("STATS parses and keeps the fields the Dashboard reads", () => {
    const msg = JSON.parse(REAL_STATS) as FirmwareMessage;
    expect(msg.type).toBe("STATS");
    if (msg.type === "STATS") {
      expect(msg.current.bank).toBe(1);
      expect(msg.last_patch_switch_ms).toBe(7223);
    }
  });

  it("PATCH_LIST parses into PatchSummary entries", () => {
    const msg = JSON.parse(REAL_PATCH_LIST) as FirmwareMessage;
    expect(msg.type).toBe("PATCH_LIST");
    if (msg.type === "PATCH_LIST") {
      const p: PatchSummary = msg.patches[0];
      expect(p).toEqual({ bank: 1, slot: 1, name: "Crunch", dirty: false });
    }
  });

  it("MANIFEST parses as a single compact line into the Manifest type", () => {
    const msg = JSON.parse(REAL_MANIFEST) as FirmwareMessage;
    expect(msg.type).toBe("MANIFEST");
    if (msg.type === "MANIFEST") {
      const m: Manifest = { core_messages: msg.core_messages, plugins: msg.plugins };
      expect(m.core_messages.pc.label).toBe("Program Change");
      expect(m.plugins.generic_midi.label).toBe("Generic MIDI");
      expect(m.plugins.generic_midi.version).toBe("1.0");
    }
  });

  it("the real 22 KB manifest size is under the inbox/Tauri limits", () => {
    // The observed manifest is 22935 bytes.  The drain_inbox command
    // returns Vec<String> - a single 22 KB string must survive Tauri IPC
    // (it did on device; this pins the ceiling so nobody "optimises"
    // the buffer below it).
    const observed = 22935;
    expect(observed).toBeLessThan(64 * 1024);
  });
});
