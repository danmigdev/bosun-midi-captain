/**
 * Integration tests for the Android MIDI bridge (Kemper <-> pedal) contract.
 *
 * The bridge lives in two places:
 *  - BosunMidiBridge.kt (android.media.midi singleton, drives the USB-MIDI
 *    hardware on the phone)
 *  - src-tauri/src/midi_android.rs (the Rust JNI wrapper, registered under
 *    cfg(target_os = "android") in lib.rs with the same four commands the
 *    desktop midi.rs exposes: midi_list_ports, midi_bridge_start,
 *    midi_bridge_stop, midi_bridge_status)
 *
 * protocol.ts talks to whichever backend is compiled, so the TS contract is
 * identical on both platforms. These tests exercise that contract end to end
 * against a fake backend that mirrors the Android semantics in
 * BosunMidiBridge.kt (idempotent start/stop, status snapshot, hint-overridable
 * device matching, clock/active-sensing filtering byte by byte), with one
 * deliberate deviation: a non-blank device hint REPLACES the default name
 * patterns instead of merely adding a match opportunity, so a hint can
 * actually disambiguate two Kemper-class devices. That matches the desktop
 * midi.rs reference and the intent of the Kotlin KDoc. The fake is the
 * reference behavior the upcoming midi_android.rs must reproduce, and the
 * invoke-call log locks the exact command names + argument shapes the Rust
 * side must register.
 *
 * IS_ANDROID is false under jsdom (no Android user agent), so the tests run
 * down the real invoke() path in protocol.ts - the same path the Android
 * build takes once the stubbed branch is removed.
 *
 * Mocking follows tests/protocol-stress.test.ts: vi.mock factories are
 * hoisted, so the fake bridge state lives in vi.hoisted() to avoid TDZ.
 */
import { describe, it, expect, beforeEach, expectTypeOf, vi } from "vitest";

// ---------------------------------------------------------------------------
// Fake backend: a faithful JS model of BosunMidiBridge.kt. All behavior here
// mirrors the Kotlin singleton (state machine, matching, filtering), so the
// tests double as a specification for midi_android.rs.
// ---------------------------------------------------------------------------

const fakeBridge = vi.hoisted(() => {
  const MIDI_CLOCK = 0xf8;
  const MIDI_ACTIVE_SENSING = 0xfe;

  const KEMPER_PATTERNS = ["profiler", "kemper"];
  const CAPTAIN_PATTERNS = ["circuitpython", "captain"];

  const DEFAULT_DEVICES = [
    "Kemper Profiler Player",
    "PaintAudio MIDI Captain (CircuitPython)",
  ];

  let devices: string[] = [...DEFAULT_DEVICES];
  let portLists: { inputs: string[]; outputs: string[] } | null = null;

  // Bridge state, mirroring the @Volatile fields of the Kotlin object.
  let active = false;
  let kemperLabel: string | null = null;
  let captainLabel: string | null = null;
  let kemperInfoId = -1;
  let captainInfoId = -1;

  // Devices + ports currently open, for the leak assertions.
  let openCount = 0;

  const calls: Array<{ cmd: string; args?: Record<string, unknown> }> = [];

  /** A non-blank hint REPLACES the default patterns: the device must contain
   *  the hint to match. This mirrors the desktop midi.rs (the hint becomes the
   *  only needle) and the intent of the Kotlin KDoc ("hints narrow the device
   *  search"). Note the Kotlin implementation as written falls back to the
   *  patterns when the hint does not match, which makes a hint unable to
   *  disambiguate two Kemper-class devices; the spec for midi_android.rs is
   *  the replace semantics used here. A blank hint is treated as absent
   *  (isNullOrBlank). */
  function matchesLabel(label: string, hint: string | null, patterns: string[]): boolean {
    const low = label.toLowerCase();
    if (hint !== null && hint.trim() !== "") return low.includes(hint.toLowerCase());
    return patterns.some((p) => low.includes(p));
  }

  /** findDevices: first device matching each role, in enumeration order. */
  function findDevices(
    kemperHint: string | null,
    captainHint: string | null,
  ): { kemper: string | null; captain: string | null } {
    let kemper: string | null = null;
    let captain: string | null = null;
    for (const d of devices) {
      if (kemper === null && matchesLabel(d, kemperHint, KEMPER_PATTERNS)) kemper = d;
      if (captain === null && matchesLabel(d, captainHint, CAPTAIN_PATTERNS)) captain = d;
      if (kemper !== null && captain !== null) break;
    }
    return { kemper, captain };
  }

  /** FilteringForwarder: scan the whole buffer, strip 0xF8/0xFE anywhere. */
  function filterNoise(data: number[]): number[] {
    if (!data.some((b) => b === MIDI_CLOCK || b === MIDI_ACTIVE_SENSING)) return [...data];
    return data.filter((b) => b !== MIDI_CLOCK && b !== MIDI_ACTIVE_SENSING);
  }

  return {
    reset: (deviceLabels: string[] = DEFAULT_DEVICES) => {
      devices = [...deviceLabels];
      portLists = null;
      active = false;
      kemperLabel = null;
      captainLabel = null;
      kemperInfoId = -1;
      captainInfoId = -1;
      openCount = 0;
      calls.length = 0;
    },

    setDevices: (deviceLabels: string[]) => {
      devices = [...deviceLabels];
    },

    /** Independent in/out port lists for midi_list_ports; null = derive from devices. */
    setPorts: (inputs: string[], outputs: string[]) => {
      portLists = { inputs: [...inputs], outputs: [...outputs] };
    },

    listPorts: () => ({
      inputs: portLists ? [...portLists.inputs] : [...devices],
      outputs: portLists ? [...portLists.outputs] : [...devices],
    }),

    start: (kemperHint: string | null, captainHint: string | null) => {
      // Idempotent: an active bridge returns its current status unchanged.
      if (active) return fakeBridge.status();

      const { kemper, captain } = findDevices(kemperHint, captainHint);
      if (kemper === null || captain === null) {
        // Kotlin reports the label of whichever half was found, even on failure.
        return { active: false, kemper_port: kemper, pedal_port: captain };
      }

      openCount += 2;
      kemperInfoId = devices.indexOf(kemper);
      captainInfoId = devices.indexOf(captain);
      active = true;
      kemperLabel = kemper;
      captainLabel = captain;
      return fakeBridge.status();
    },

    stop: () => {
      if (!active && openCount === 0) return; // idempotent no-op
      // Clear the ids first so a queued removal callback cannot re-trigger stop.
      kemperInfoId = -1;
      captainInfoId = -1;
      openCount = 0;
      active = false;
      kemperLabel = null;
      captainLabel = null;
    },

    status: () => ({
      active,
      kemper_port: kemperLabel,
      pedal_port: captainLabel,
    }),

    /** Kemper IN -> Captain OUT link (clock/active-sensing stripped). */
    forwardKemperToCaptain: (data: number[]): number[] => filterNoise(data),

    /** Captain IN -> Kemper OUT link (same filtering, mirrored links). */
    forwardCaptainToKemper: (data: number[]): number[] => filterNoise(data),

    /** Simulate onDeviceRemoved: a bridged device unplugging stops the bridge. */
    unplug: (label: string) => {
      devices = devices.filter((d) => d !== label);
      if (active && (label === kemperLabel || label === captainLabel)) fakeBridge.stop();
    },

    openCount: () => openCount,
    activeDeviceIds: () => ({ kemper: kemperInfoId, captain: captainInfoId }),
    calls,
  };
});

vi.mock("@tauri-apps/api/core", () => ({
  invoke: vi.fn(async (cmd: string, args?: Record<string, unknown>) => {
    fakeBridge.calls.push({ cmd, args });
    switch (cmd) {
      case "midi_list_ports":
        return fakeBridge.listPorts();
      case "midi_bridge_start":
        return fakeBridge.start(
          (args?.kemper as string | null | undefined) ?? null,
          (args?.pedal as string | null | undefined) ?? null,
        );
      case "midi_bridge_stop":
        fakeBridge.stop();
        return undefined;
      case "midi_bridge_status":
        return fakeBridge.status();
      default:
        return undefined;
    }
  }),
}));

import {
  midiListPorts,
  midiBridgeStart,
  midiBridgeStop,
  midiBridgeStatus,
  type BridgeStatus,
  type MidiPorts,
} from "../src/lib/protocol";

beforeEach(() => {
  fakeBridge.reset();
});

// ---------------------------------------------------------------------------
// 1. BridgeStatus type
// ---------------------------------------------------------------------------

describe("BridgeStatus type", () => {
  it("accepts a valid active state with both ports named", () => {
    const active: BridgeStatus = {
      active: true,
      kemper_port: "Kemper Profiler Player",
      pedal_port: "PaintAudio MIDI Captain (CircuitPython)",
    };
    expect(active).toEqual({
      active: true,
      kemper_port: "Kemper Profiler Player",
      pedal_port: "PaintAudio MIDI Captain (CircuitPython)",
    });
  });

  it("accepts a valid inactive state with null ports", () => {
    const inactive: BridgeStatus = { active: false, kemper_port: null, pedal_port: null };
    expect(inactive.active).toBe(false);
    expect(inactive.kemper_port).toBeNull();
    expect(inactive.pedal_port).toBeNull();
  });

  it("types port names as optional strings (string or null)", () => {
    expectTypeOf<BridgeStatus["kemper_port"]>().toEqualTypeOf<string | null>();
    expectTypeOf<BridgeStatus["pedal_port"]>().toEqualTypeOf<string | null>();
    expectTypeOf<BridgeStatus["active"]>().toEqualTypeOf<boolean>();
  });

  it("allows a port label while inactive (one half found, other missing)", () => {
    // BosunMidiBridge.start reports the label of whichever device was found
    // even when the other half is missing.
    const partial: BridgeStatus = {
      active: false,
      kemper_port: "Kemper Profiler Player",
      pedal_port: null,
    };
    expect(partial).toEqual({
      active: false,
      kemper_port: "Kemper Profiler Player",
      pedal_port: null,
    });
  });
});

// ---------------------------------------------------------------------------
// 2. MidiPorts type
// ---------------------------------------------------------------------------

describe("MidiPorts type", () => {
  it("accepts an empty port list", () => {
    const empty: MidiPorts = { inputs: [], outputs: [] };
    expect(empty).toEqual({ inputs: [], outputs: [] });
  });

  it("accepts a populated port list", () => {
    const populated: MidiPorts = {
      inputs: ["Kemper Profiler Player", "PaintAudio MIDI Captain (CircuitPython)"],
      outputs: ["Kemper Profiler Player", "PaintAudio MIDI Captain (CircuitPython)"],
    };
    expect(populated.inputs).toHaveLength(2);
    expect(populated.outputs).toHaveLength(2);
  });

  it("types inputs and outputs as string arrays", () => {
    expectTypeOf<MidiPorts["inputs"]>().toEqualTypeOf<string[]>();
    expectTypeOf<MidiPorts["outputs"]>().toEqualTypeOf<string[]>();
  });

  it("round-trips input and output lists independently", async () => {
    fakeBridge.setPorts(["Kemper Player In"], ["Kemper Player Out", "Captain Out"]);
    const ports = await midiListPorts();
    expect(ports).toEqual({
      inputs: ["Kemper Player In"],
      outputs: ["Kemper Player Out", "Captain Out"],
    });
    // The backend saw the exact command the Rust side must register.
    expect(fakeBridge.calls).toEqual([{ cmd: "midi_list_ports", args: undefined }]);
  });
});

// ---------------------------------------------------------------------------
// 3. MIDI message filtering
// ---------------------------------------------------------------------------

describe("MIDI message filtering", () => {
  it("drops a bare MIDI clock (0xF8) message", () => {
    expect(fakeBridge.forwardKemperToCaptain([0xf8])).toEqual([]);
    expect(fakeBridge.forwardCaptainToKemper([0xf8])).toEqual([]);
  });

  it("drops a bare active sensing (0xFE) message", () => {
    expect(fakeBridge.forwardKemperToCaptain([0xfe])).toEqual([]);
    expect(fakeBridge.forwardCaptainToKemper([0xfe])).toEqual([]);
  });

  it("drops a buffer made only of realtime noise", () => {
    expect(fakeBridge.forwardKemperToCaptain([0xf8, 0xfe, 0xf8, 0xfe])).toEqual([]);
  });

  it("lets regular channel-voice messages through untouched", () => {
    const noteOn = [0x90, 0x3c, 0x64];
    expect(fakeBridge.forwardKemperToCaptain(noteOn)).toEqual(noteOn);
    const cc = [0xb0, 0x07, 0x7f];
    expect(fakeBridge.forwardCaptainToKemper(cc)).toEqual(cc);
    const programChange = [0xc0, 0x05];
    expect(fakeBridge.forwardKemperToCaptain(programChange)).toEqual(programChange);
  });

  it("lets SYSEX pass through unchanged (Kemper bidirectional protocol)", () => {
    // Kemper beacon: manufacturer 0x00 0x20 0x33, function 0x7E. The pedal
    // must see it or the Profiler/Player never starts broadcasting state.
    const beacon = [0xf0, 0x00, 0x20, 0x33, 0x01, 0x7e, 0x00, 0xf7];
    expect(fakeBridge.forwardCaptainToKemper(beacon)).toEqual(beacon);

    // A larger rig-name broadcast with payload bytes.
    const broadcast = [0xf0, 0x00, 0x20, 0x33, 0x01, 0x41, 0x0c, 0x44, 0x43, 0x33, 0x30, 0xf7];
    expect(fakeBridge.forwardKemperToCaptain(broadcast)).toEqual(broadcast);
  });

  it("strips clock/active-sensing interleaved in a regular stream", () => {
    // The Android FilteringForwarder scans the whole buffer and removes the
    // noise bytes wherever they appear (the desktop should_forward only looks
    // at the first byte, so this is the Android contract specifically).
    expect(fakeBridge.forwardKemperToCaptain([0xf8, 0x90, 0x3c, 0x64])).toEqual([
      0x90, 0x3c, 0x64,
    ]);
    expect(fakeBridge.forwardKemperToCaptain([0x90, 0xf8, 0x3c, 0xfe, 0x64])).toEqual([
      0x90, 0x3c, 0x64,
    ]);
  });

  it("strips realtime bytes interleaved inside a SYSEX payload", () => {
    // MIDI allows realtime messages to be interleaved in a SYSEX stream; the
    // noise bytes are dropped but the message itself must survive intact.
    expect(
      fakeBridge.forwardCaptainToKemper([0xf0, 0x00, 0x20, 0x33, 0xf8, 0x7e, 0xf7]),
    ).toEqual([0xf0, 0x00, 0x20, 0x33, 0x7e, 0xf7]);
  });
});

// ---------------------------------------------------------------------------
// 4. Bridge state machine (through the protocol functions)
// ---------------------------------------------------------------------------

describe("bridge state machine", () => {
  it("reports inactive before anything is started", async () => {
    const status = await midiBridgeStatus();
    expect(status).toEqual({ active: false, kemper_port: null, pedal_port: null });
    expect(fakeBridge.calls).toEqual([{ cmd: "midi_bridge_status", args: undefined }]);
  });

  it("activates on start and returns the matched devices", async () => {
    const status = await midiBridgeStart();
    expect(status).toEqual({
      active: true,
      kemper_port: "Kemper Profiler Player",
      pedal_port: "PaintAudio MIDI Captain (CircuitPython)",
    });
    expect(fakeBridge.openCount()).toBe(2);
  });

  it("starting an already-active bridge returns the current status unchanged", async () => {
    const first = await midiBridgeStart();
    // A second start with different hints must NOT reopen or relabel: the
    // Kotlin singleton returns status() before doing any work when active.
    const second = await midiBridgeStart("totally-different-hint", "another-hint");
    expect(second).toEqual(first);
    expect(fakeBridge.openCount()).toBe(2); // no re-open, no leak
    expect(fakeBridge.activeDeviceIds()).toEqual({ kemper: 0, captain: 1 });
  });

  it("stop tears the bridge down and clears the port labels", async () => {
    await midiBridgeStart();
    await midiBridgeStop();
    const status = await midiBridgeStatus();
    expect(status).toEqual({ active: false, kemper_port: null, pedal_port: null });
    expect(fakeBridge.openCount()).toBe(0);
    expect(fakeBridge.activeDeviceIds()).toEqual({ kemper: -1, captain: -1 });
  });

  it("stopping an inactive bridge is idempotent", async () => {
    await midiBridgeStop(); // never started
    await midiBridgeStop(); // again
    const status = await midiBridgeStatus();
    expect(status).toEqual({ active: false, kemper_port: null, pedal_port: null });
    expect(fakeBridge.openCount()).toBe(0);
  });

  it("status reflects the current state after every transition", async () => {
    const states: BridgeStatus[] = [];
    states.push(await midiBridgeStatus()); // inactive
    states.push(await midiBridgeStart()); // active
    await midiBridgeStop();
    states.push(await midiBridgeStatus()); // inactive again
    expect(states[0]).toEqual({ active: false, kemper_port: null, pedal_port: null });
    expect(states[1].active).toBe(true);
    expect(states[1].kemper_port).toBe("Kemper Profiler Player");
    expect(states[2]).toEqual({ active: false, kemper_port: null, pedal_port: null });
  });
});

// ---------------------------------------------------------------------------
// 5. Device name matching
// ---------------------------------------------------------------------------

describe("device name matching", () => {
  it("matches the Kemper by the default patterns (profiler/kemper)", async () => {
    fakeBridge.setDevices(["Kemper Profiler Player", "PaintAudio MIDI Captain (CircuitPython)"]);
    const status = await midiBridgeStart();
    expect(status.kemper_port).toBe("Kemper Profiler Player");
    expect(status.active).toBe(true);
  });

  it("matches a device carrying only the 'kemper' pattern", async () => {
    fakeBridge.setDevices(["Kemper Stage", "PaintAudio MIDI Captain (CircuitPython)"]);
    const status = await midiBridgeStart();
    expect(status.kemper_port).toBe("Kemper Stage");
  });

  it("matches the pedal by the default patterns (circuitpython/captain)", async () => {
    fakeBridge.setDevices(["Kemper Profiler Player", "PaintAudio MIDI Captain (CircuitPython)"]);
    const status = await midiBridgeStart();
    expect(status.pedal_port).toBe("PaintAudio MIDI Captain (CircuitPython)");
  });

  it("matches a device carrying only the 'captain' pattern", async () => {
    fakeBridge.setDevices(["Kemper Profiler Player", "MIDI Captain 10"]);
    const status = await midiBridgeStart();
    expect(status.pedal_port).toBe("MIDI Captain 10");
  });

  it("a hint overrides the default matching", async () => {
    // 'audiobox' matches no default pattern; only the hint finds it.
    fakeBridge.setDevices(["AudioBox USB MIDI", "PaintAudio MIDI Captain (CircuitPython)"]);
    expect((await midiBridgeStart()).active).toBe(false);
    expect((await midiBridgeStart("audiobox")).kemper_port).toBe("AudioBox USB MIDI");
  });

  it("hint matching is case-insensitive", async () => {
    fakeBridge.setDevices(["AUDIOBOX USB MIDI", "PaintAudio MIDI Captain (CircuitPython)"]);
    const status = await midiBridgeStart("audiobox");
    expect(status.kemper_port).toBe("AUDIOBOX USB MIDI");
  });

  it("a pedal hint selects a different pedal than the defaults would", async () => {
    fakeBridge.setDevices([
      "Kemper Profiler Player",
      "PaintAudio MIDI Captain (CircuitPython)",
      "External USB Synth",
    ]);
    const status = await midiBridgeStart(undefined, "synth");
    expect(status.pedal_port).toBe("External USB Synth");
  });

  it("treats a blank hint as absent (Android isNullOrBlank semantics)", async () => {
    fakeBridge.setDevices(["Kemper Profiler Player", "PaintAudio MIDI Captain (CircuitPython)"]);
    const status = await midiBridgeStart("");
    expect(status.active).toBe(true);
    expect(status.kemper_port).toBe("Kemper Profiler Player");
  });

  it("a non-matching hint excludes default-pattern devices", async () => {
    // Replace semantics: with a hint that matches nothing, no default-pattern
    // device is eligible, so the bridge cannot start. (This is the Android
    // spec; the Kotlin as written falls back to patterns on a miss, which
    // would let the hint-less "Kemper Profiler Player" match anyway.)
    fakeBridge.setDevices(["Kemper Profiler Player", "PaintAudio MIDI Captain (CircuitPython)"]);
    const status = await midiBridgeStart("zzz-no-match");
    expect(status).toEqual({
      active: false,
      kemper_port: null,
      pedal_port: "PaintAudio MIDI Captain (CircuitPython)",
    });
  });
});

// ---------------------------------------------------------------------------
// 6. Edge cases
// ---------------------------------------------------------------------------

describe("edge cases", () => {
  it("passes null hints to the backend when none are given", async () => {
    await midiBridgeStart();
    await midiBridgeStart(undefined, undefined);
    const startCalls = fakeBridge.calls.filter((c) => c.cmd === "midi_bridge_start");
    expect(startCalls).toHaveLength(2);
    expect(startCalls[0].args).toEqual({ kemper: null, pedal: null });
    expect(startCalls[1].args).toEqual({ kemper: null, pedal: null });
  });

  it("passes partial hints through with the missing side as null", async () => {
    await midiBridgeStart("stage");
    await midiBridgeStart(undefined, "captain");
    const startCalls = fakeBridge.calls.filter((c) => c.cmd === "midi_bridge_start");
    expect(startCalls[0].args).toEqual({ kemper: "stage", pedal: null });
    expect(startCalls[1].args).toEqual({ kemper: null, pedal: "captain" });
  });

  it("returns an inactive status (not an error) when no devices are present", async () => {
    fakeBridge.setDevices([]);
    const status = await midiBridgeStart();
    expect(status).toEqual({ active: false, kemper_port: null, pedal_port: null });
  });

  it("returns a partial status when only one device is present", async () => {
    fakeBridge.setDevices(["PaintAudio MIDI Captain (CircuitPython)"]);
    const status = await midiBridgeStart();
    expect(status).toEqual({
      active: false,
      kemper_port: null,
      pedal_port: "PaintAudio MIDI Captain (CircuitPython)",
    });
  });

  it("lists empty ports when the backend has no devices", async () => {
    fakeBridge.setDevices([]);
    const ports = await midiListPorts();
    expect(ports).toEqual({ inputs: [], outputs: [] });
  });

  it("survives rapid start/stop cycles without leaking", async () => {
    for (let i = 0; i < 25; i++) {
      const started = await midiBridgeStart();
      expect(started.active).toBe(true);
      await midiBridgeStop();
    }
    const status = await midiBridgeStatus();
    expect(status).toEqual({ active: false, kemper_port: null, pedal_port: null });
    expect(fakeBridge.openCount()).toBe(0);
    expect(fakeBridge.activeDeviceIds()).toEqual({ kemper: -1, captain: -1 });
  });

  it("auto-stops when a bridged device is unplugged mid-bridge", async () => {
    await midiBridgeStart();
    expect((await midiBridgeStatus()).active).toBe(true);

    fakeBridge.unplug("Kemper Profiler Player"); // onDeviceRemoved -> stop()

    const status = await midiBridgeStatus();
    expect(status).toEqual({ active: false, kemper_port: null, pedal_port: null });
    expect(fakeBridge.openCount()).toBe(0);
    expect(fakeBridge.activeDeviceIds()).toEqual({ kemper: -1, captain: -1 });
  });

  it("keeps the bridge running when an unrelated device is unplugged", async () => {
    fakeBridge.setDevices([
      "Kemper Profiler Player",
      "PaintAudio MIDI Captain (CircuitPython)",
      "Some Other Synth",
    ]);
    await midiBridgeStart();

    fakeBridge.unplug("Some Other Synth");

    const status = await midiBridgeStatus();
    expect(status.active).toBe(true);
    expect(status.kemper_port).toBe("Kemper Profiler Player");
    expect(status.pedal_port).toBe("PaintAudio MIDI Captain (CircuitPython)");
  });

  it("calls exactly the four commands the Android backend registers", async () => {
    await midiListPorts();
    await midiBridgeStart();
    await midiBridgeStatus();
    await midiBridgeStop();
    const cmds = fakeBridge.calls.map((c) => c.cmd);
    expect(cmds).toEqual([
      "midi_list_ports",
      "midi_bridge_start",
      "midi_bridge_status",
      "midi_bridge_stop",
    ]);
  });
});
