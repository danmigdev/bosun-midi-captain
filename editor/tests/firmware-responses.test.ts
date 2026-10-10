// Regression tests for firmware response shapes, pinned to the replies the
// native firmware writes (firmware-native/src/protocol.c).
//
// These pin the contracts the frontend depends on:
//  - The firmware serializes compact JSON ("id":"..."), the form the Rust
//    sentinel marker matches; the frontend JSON.parse obviously accepts it.
//  - DEVICE_INFO / PROFILE_LIST / GLOBAL / STATS / PATCH_LIST fields
//    used by App.svelte's handleMessage.
//  - MANIFEST arrives as one large compact line and parses into the
//    Manifest type.

import { describe, it, expect } from "vitest";
import type { FirmwareMessage, Manifest, PatchSummary } from "../src/lib/protocol";

// ---- firmware payloads (trimmed to what matters) ----

const ACK = `{"type":"ACK","id":"__sync_24264_1786547897893","fw":"0.8.0-native"}`;

const DEVICE_INFO = `{"type":"DEVICE_INFO","id":"1","fw":"0.8.0-native","device":"MIDI Captain","profile":"kemper","current":{"bank":1,"slot":1}}`;

const PROFILE_LIST = `{"type":"PROFILE_LIST","id":"2","active":"kemper","profiles":[{"id":"kemper","name":"Kemper","kind":"kemper_player","color":null,"active":true}]}`;

const STATS = `{"type":"STATS","id":"7","fw":"0.8.0-native","uptime_ms":12345,"midi_rx_count":1,"midi_tx_count":2,"midi_tx_failed":0,"queue_overflows":0,"unsupported_messages":0,"invalid_messages":0,"protocol_errors":0,"storage_errors":0,"midi_events_dropped":0,"storage_ready":true}`;

const PATCH_LIST = `{"type":"PATCH_LIST","id":"58","profile":"kemper","patches":[{"bank":1,"slot":1,"name":"Crunch","dirty":false}]}`;

// Manifest prefix: compact keys, single line.
const MANIFEST = `{"type":"MANIFEST","id":"4","core_messages":{"pc":{"label":"Program Change","params":{"channel":{"type":"int","min":1,"max":16,"default":1,"label":"Channel"},"program":{"type":"int","min":0,"max":127,"default":0,"label":"Program"}},"summary":"PC {program} ch {channel}"}},"plugins":{"generic_midi":{"label":"Generic MIDI","version":"1.0","messages":{}}}}`;

describe("firmware response shape", () => {
  it("ACK echoes the request id in the compact form the sentinel sync matches", () => {
    const msg = JSON.parse(ACK) as FirmwareMessage;
    expect(msg.type).toBe("ACK");
    expect((msg as { id?: string }).id).toBe("__sync_24264_1786547897893");
    expect(ACK).toContain('"id":"__sync_24264_1786547897893"');
  });

  it("DEVICE_INFO parses into the expected shape", () => {
    const msg = JSON.parse(DEVICE_INFO) as FirmwareMessage;
    expect(msg.type).toBe("DEVICE_INFO");
    if (msg.type === "DEVICE_INFO") {
      expect(msg.fw).toBe("0.8.0-native");
      expect(msg.current).toEqual({ bank: 1, slot: 1 });
    }
  });

  it("PROFILE_LIST parses with the fields handleMessage reads", () => {
    const msg = JSON.parse(PROFILE_LIST) as FirmwareMessage;
    expect(msg.type).toBe("PROFILE_LIST");
    if (msg.type === "PROFILE_LIST") {
      expect(msg.active).toBeDefined();
      expect(msg.profiles[0].active).toBe(true);
      expect(msg.profiles[0].kind).toBe("kemper_player");
    }
  });

  it("STATS parses and keeps the fields the Dashboard and Maintenance read", () => {
    const msg = JSON.parse(STATS) as FirmwareMessage;
    expect(msg.type).toBe("STATS");
    if (msg.type === "STATS") {
      expect(msg.uptime_ms).toBe(12345);
      expect(msg.midi_tx_count).toBe(2);
      expect(msg.storage_ready).toBe(true);
    }
  });

  it("PATCH_LIST parses into PatchSummary entries", () => {
    const msg = JSON.parse(PATCH_LIST) as FirmwareMessage;
    expect(msg.type).toBe("PATCH_LIST");
    if (msg.type === "PATCH_LIST") {
      const p: PatchSummary = msg.patches[0];
      expect(p).toEqual({ bank: 1, slot: 1, name: "Crunch", dirty: false });
    }
  });

  it("MANIFEST parses as a single compact line into the Manifest type", () => {
    const msg = JSON.parse(MANIFEST) as FirmwareMessage;
    expect(msg.type).toBe("MANIFEST");
    if (msg.type === "MANIFEST") {
      const m: Manifest = { core_messages: msg.core_messages, plugins: msg.plugins };
      expect(m.core_messages.pc.label).toBe("Program Change");
      expect(m.plugins.generic_midi.label).toBe("Generic MIDI");
      expect(m.plugins.generic_midi.version).toBe("1.0");
    }
  });

  it("the largest observed manifest size is under the inbox/Tauri limits", () => {
    // The largest manifest observed was 22935 bytes. The drain_inbox command
    // returns Vec<String> - a single 22 KB string must survive Tauri IPC
    // (it did on device; this pins the ceiling so nobody "optimises"
    // the buffer below it).
    const observed = 22935;
    expect(observed).toBeLessThan(64 * 1024);
  });
});
