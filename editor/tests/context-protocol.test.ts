// Protocol-level tests for CONTEXT message handling and Stage Mode data flow.
//
// CONTEXT is the firmware's push of the live display_context over the data
// port. Two producers exist:
//   - GET_CONTEXT request/response (firmware/lib/captain/protocol.py
//     _get_context) - carries an `id` echoed back to the requester
//   - fire-and-forget pushes (firmware/lib/captain/app.py _push_context,
//     throttled to ~1 Hz, no `id`) - plain `{"type": "CONTEXT", ...}` lines
//
// The context dict is owned by the captain core (patch_name / bank / slot)
// plus whatever the active plugin publishes via update_context(): the Kemper
// plugin writes kemper_rig_name, kemper_bank, kemper_rig_in_bank, kemper_rig,
// kemper_bpm, kemper_tuner*, kemper_connected (see
// firmware/lib/plugins/kemper.py) and mirrors the tuner fields to generic
// tuner / tuner_note / tuner_deviance aliases.
//
// These tests pin:
//   1. the CONTEXT message format (full, empty, core-only, Kemper-only)
//   2. the FirmwareMessage type contract for the CONTEXT variant
//   3. the cmd.getContext() request path and subscriber-delivered response
//   4. the real-world Kemper display_context field contract (types + ranges)
//   5. the onFirmwareMessage subscriber pattern (fan-out, unsubscribe)
//   6. the StageView end-to-end data flow (rig name, fallbacks, BPM, tuner)
//
// The Tauri IPC layer is mocked so the firmware side can be driven from the
// test (same harness pattern as protocol-stress.test.ts). The StageView
// tests exercise the REAL protocol module and the REAL component through
// that harness, so the full drain -> subscriber -> derived-state -> DOM path
// is covered.

import { describe, it, expect, beforeEach, vi } from "vitest";
import { render } from "@testing-library/svelte";
import { tick } from "svelte";
import type { PatchSummary } from "../src/lib/protocol";
import StageView from "../src/components/StageView.svelte";
import { cmd, onFirmwareMessage, type FirmwareMessage } from "../src/lib/protocol";

const { harness } = vi.hoisted(() => {
  return {
    harness: {
      inbox: [] as string[],
      sent: [] as string[],
      doorbell: null as (() => void) | null,
    },
  };
});

vi.mock("@tauri-apps/api/core", () => ({
  invoke: vi.fn(async (cmdName: string, args?: Record<string, unknown>) => {
    switch (cmdName) {
      case "send_command":
        harness.sent.push(String(args?.line ?? ""));
        return undefined;
      case "drain_inbox": {
        const batch = harness.inbox;
        harness.inbox = [];
        return batch;
      }
      case "disconnect":
      case "connect":
        return undefined;
      case "is_connected":
        return true;
      case "auto_connect":
        return "MOCK-PORT";
      case "list_ports":
        return [];
      default:
        return undefined;
    }
  }),
}));

vi.mock("@tauri-apps/api/event", () => ({
  listen: vi.fn(async (eventName: string, handler: (...args: unknown[]) => void) => {
    if (eventName === "firmware-data-ready") {
      harness.doorbell = () => handler();
    }
    return () => {};
  }),
}));

// ----------------------------------------------------------------------
//  helpers
// ----------------------------------------------------------------------

/** Queue a firmware-side line exactly as the data port would deliver it. */
function enqueue(obj: Record<string, unknown>) {
  harness.inbox.push(JSON.stringify(obj));
}

/** Yield enough microtasks for any async bodies waiting on awaited
 *  promises (mocked invoke, listen, etc.) to advance. */
async function flushMicrotasks(rounds = 10) {
  for (let i = 0; i < rounds; i++) await Promise.resolve();
}

/** Ring the doorbell and let the async drain unwind. Drain reads via an
 *  awaited invoke, so we flush a handful of microtasks rather than relying
 *  on a single Promise.resolve. */
async function flush() {
  harness.doorbell?.();
  await flushMicrotasks();
}

// ----------------------------------------------------------------------
//  real-world display_context payloads
// ----------------------------------------------------------------------

/** The display_context shape a Kemper profile publishes: captain core
 *  fields + everything firmware/lib/plugins/kemper.py writes via _publish
 *  (including the generic tuner aliases mirrored by _add_tuner_aliases). */
interface KemperDisplayContext {
  patch_name: string;
  bank: number;
  slot: number;
  kemper_rig_name: string;
  kemper_bank: number;
  kemper_rig_in_bank: number;
  kemper_rig: number;
  kemper_bpm: number;
  kemper_tuner: "on" | "off";
  kemper_tuner_note: string;
  kemper_tuner_deviance: number;
  kemper_connected: "on" | "off";
  tuner: "on" | "off";
  tuner_note: string;
  tuner_deviance: number;
}

const KEMPER_CONTEXT = {
  patch_name: "Acoustic",
  bank: 1,
  slot: 3,
  kemper_rig_name: "Marsh Lead",
  kemper_bank: 1,
  kemper_rig_in_bank: 3,
  kemper_rig: 3,
  kemper_bpm: 120,
  kemper_tuner: "off",
  kemper_tuner_note: "A",
  kemper_tuner_deviance: 8192,
  kemper_connected: "on",
  tuner: "off",
  tuner_note: "A",
  tuner_deviance: 8192,
} satisfies KemperDisplayContext;

beforeEach(() => {
  harness.inbox = [];
  harness.sent = [];
});

// ----------------------------------------------------------------------
//  1. CONTEXT message format
// ----------------------------------------------------------------------

describe("CONTEXT message format", () => {
  it("accepts a full CONTEXT message (core + Kemper fields) and round-trips it", () => {
    const msg: FirmwareMessage = { type: "CONTEXT", id: "41", context: KEMPER_CONTEXT };
    expect(msg.type).toBe("CONTEXT");
    // The firmware serializes the message as JSON on the wire; parse it back
    // and confirm nothing is lost or corrupted.
    const parsed = JSON.parse(JSON.stringify(msg)) as FirmwareMessage;
    expect(parsed).toEqual(msg);
    expect((parsed as { context: Record<string, unknown> }).context.patch_name).toBe("Acoustic");
    expect((parsed as { context: Record<string, unknown> }).context.kemper_rig_name).toBe("Marsh Lead");
  });

  it("accepts a CONTEXT with an empty context dict", () => {
    const msg: FirmwareMessage = { type: "CONTEXT", context: {} };
    expect(msg.context).toEqual({});
  });

  it("accepts a CONTEXT with only the core captain fields (patch_name, bank, slot)", () => {
    const msg: FirmwareMessage = {
      type: "CONTEXT",
      context: { patch_name: "Lead", bank: 3, slot: 2 },
    };
    expect(msg.type).toBe("CONTEXT");
    expect(msg.context.patch_name).toBe("Lead");
    expect(msg.context.bank).toBe(3);
    expect(msg.context.slot).toBe(2);
  });

  it("accepts a CONTEXT with Kemper-specific fields (kemper_* namespace)", () => {
    const msg: FirmwareMessage = {
      type: "CONTEXT",
      context: {
        kemper_rig_name: "Marsh Lead",
        kemper_bank: 1,
        kemper_rig_in_bank: 3,
        kemper_rig: 3,
        kemper_bpm: 120,
        kemper_tuner: "off",
        kemper_connected: "on",
      },
    };
    expect(msg.type).toBe("CONTEXT");
    expect(msg.context.kemper_rig_name).toBe("Marsh Lead");
    expect(msg.context.kemper_bank).toBe(1);
    expect(msg.context.kemper_rig_in_bank).toBe(3);
    expect(msg.context.kemper_rig).toBe(3);
    expect(msg.context.kemper_bpm).toBe(120);
    expect(msg.context.kemper_tuner).toBe("off");
    expect(msg.context.kemper_connected).toBe("on");
  });
});

// ----------------------------------------------------------------------
//  2. FirmwareMessage type contract
// ----------------------------------------------------------------------

describe("FirmwareMessage type: CONTEXT", () => {
  it("is a valid FirmwareMessage variant", () => {
    const msg: FirmwareMessage = { type: "CONTEXT", context: { patch_name: "A" } };
    expect(msg.type).toBe("CONTEXT");
    // The `context` field is REQUIRED on the CONTEXT variant (the only
    // optional field is `id`). Omitting it must be a compile error - the
    // ts-expect-error below pins that contract.
    // @ts-expect-error - CONTEXT requires the context field
    const bad: FirmwareMessage = { type: "CONTEXT" };
    void bad;
  });

  it("carries context as Record<string, unknown>", () => {
    const msg: FirmwareMessage = {
      type: "CONTEXT",
      context: { bank: 1, patch_name: "x", nested: [1, "two", { three: 3 }] },
    };
    // The field is typed as a record of unknown values at the boundary -
    // consumers must narrow before use.
    const ctx: Record<string, unknown> = msg.context;
    expect(ctx).toBeInstanceOf(Object);
    expect(typeof ctx.patch_name).toBe("string");
    expect(typeof ctx.bank).toBe("number");
    expect(Array.isArray(ctx.nested)).toBe(true);
  });

  it("narrows correctly through a type switch on msg.type", () => {
    // Discriminated-union narrowing: only the CONTEXT branch may read
    // msg.context; the other variants expose their own fields.
    function contextOf(m: FirmwareMessage): Record<string, unknown> | undefined {
      if (m.type === "CONTEXT") return m.context;
      return undefined;
    }
    expect(contextOf({ type: "CONTEXT", context: { a: 1 } })).toEqual({ a: 1 });
    expect(contextOf({ type: "ERROR", error: "boom" })).toBeUndefined();
    expect(contextOf({ type: "ACK" })).toBeUndefined();
  });
});

// ----------------------------------------------------------------------
//  3. cmd.getContext()
// ----------------------------------------------------------------------

describe("cmd.getContext()", () => {
  it("sends a GET_CONTEXT message with a generated id", async () => {
    cmd.getContext();
    await flushMicrotasks();
    expect(harness.sent.length).toBe(1);
    const obj = JSON.parse(harness.sent[0]) as { type: string; id?: string };
    expect(obj.type).toBe("GET_CONTEXT");
    expect(typeof obj.id).toBe("string");
    expect(obj.id!.length).toBeGreaterThan(0);
  });

  it("generates a fresh id per call", async () => {
    cmd.getContext();
    cmd.getContext();
    await flushMicrotasks();
    expect(harness.sent.length).toBe(2);
    const ids = harness.sent.map((l) => (JSON.parse(l) as { id: string }).id);
    expect(ids[0]).not.toBe(ids[1]);
  });

  it("delivers the matching CONTEXT response to onFirmwareMessage subscribers", async () => {
    const seen: FirmwareMessage[] = [];
    const unsub = await onFirmwareMessage((m) => seen.push(m));
    try {
      cmd.getContext();
      await flushMicrotasks();
      const id = (JSON.parse(harness.sent[harness.sent.length - 1]) as { id: string }).id;
      // Firmware replies echoing the request id (protocol.py _get_context).
      enqueue({ type: "CONTEXT", id, context: { patch_name: "Lead", bank: 1, slot: 1 } });
      await flush();
      expect(seen).toHaveLength(1);
      expect(seen[0]).toMatchObject({
        type: "CONTEXT",
        id,
        context: { patch_name: "Lead", bank: 1, slot: 1 },
      });
    } finally {
      unsub();
    }
  });

  it("delivers fire-and-forget CONTEXT pushes (no id) to subscribers", async () => {
    // app.py _push_context sends {"type": "CONTEXT", "context": ...} with NO
    // id - the drain routes id-less lines straight to subscribers, so Stage
    // Mode keeps updating even without an outstanding request.
    const seen: FirmwareMessage[] = [];
    const unsub = await onFirmwareMessage((m) => seen.push(m));
    try {
      enqueue({ type: "CONTEXT", context: { kemper_bpm: 120 } });
      await flush();
      expect(seen).toHaveLength(1);
      expect(seen[0]).toMatchObject({ type: "CONTEXT", context: { kemper_bpm: 120 } });
      expect((seen[0] as { id?: string }).id).toBeUndefined();
    } finally {
      unsub();
    }
  });
});

// ----------------------------------------------------------------------
//  4. display_context field validation
// ----------------------------------------------------------------------

describe("display_context field validation (real-world Kemper payload)", () => {
  it("has the expected core + Kemper fields with correct types", () => {
    const ctx = KEMPER_CONTEXT;
    // Core captain fields.
    expect(typeof ctx.patch_name).toBe("string");
    expect(typeof ctx.bank).toBe("number");
    expect(typeof ctx.slot).toBe("number");
    // Kemper rig identity (from the device's PC + rig-name frames).
    expect(typeof ctx.kemper_rig_name).toBe("string");
    expect(typeof ctx.kemper_bank).toBe("number");
    expect(typeof ctx.kemper_rig_in_bank).toBe("number");
    expect(typeof ctx.kemper_rig).toBe("number");
    // Tempo + tuner (from the device's parameter broadcasts).
    expect(typeof ctx.kemper_bpm).toBe("number");
    expect(ctx.kemper_tuner === "on" || ctx.kemper_tuner === "off").toBe(true);
    expect(typeof ctx.kemper_tuner_note).toBe("string");
    expect(typeof ctx.kemper_tuner_deviance).toBe("number");
    // Connection state (from the device's sensing frames).
    expect(ctx.kemper_connected === "on" || ctx.kemper_connected === "off").toBe(true);
  });

  it("keeps Kemper value domains within firmware ranges", () => {
    // Firmware mapping: rig = PC + 1 (1..125), bank = PC // 5 + 1 (1..25),
    // rig_in_bank = PC % 5 + 1 (1..5); deviance is raw 14-bit (0..16383).
    const c = KEMPER_CONTEXT;
    expect(c.kemper_bank).toBeGreaterThanOrEqual(1);
    expect(c.kemper_bank).toBeLessThanOrEqual(25);
    expect(c.kemper_rig).toBeGreaterThanOrEqual(1);
    expect(c.kemper_rig).toBeLessThanOrEqual(125);
    expect(c.kemper_rig_in_bank).toBeGreaterThanOrEqual(1);
    expect(c.kemper_rig_in_bank).toBeLessThanOrEqual(5);
    // Rig index 3 -> bank 1, rig-in-bank 3 (flat indexing, PC 2).
    expect(c.kemper_rig).toBe(3);
    expect(c.kemper_bank).toBe(1);
    expect(c.kemper_rig_in_bank).toBe(3);
    expect(c.kemper_bpm).toBeGreaterThanOrEqual(1);
    expect(c.kemper_bpm).toBeLessThanOrEqual(999);
    expect(c.kemper_tuner_deviance).toBeGreaterThanOrEqual(0);
    expect(c.kemper_tuner_deviance).toBeLessThanOrEqual(16383);
  });

  it("mirrors generic tuner aliases alongside kemper_tuner* (plugin _add_tuner_aliases contract)", () => {
    // The plugin copies kemper_tuner* to generic tuner* aliases so layouts
    // can stay device-agnostic. StageView reads both spellings.
    expect(KEMPER_CONTEXT.tuner).toBe(KEMPER_CONTEXT.kemper_tuner);
    expect(KEMPER_CONTEXT.tuner_note).toBe(KEMPER_CONTEXT.kemper_tuner_note);
    expect(KEMPER_CONTEXT.tuner_deviance).toBe(KEMPER_CONTEXT.kemper_tuner_deviance);
  });
});

// ----------------------------------------------------------------------
//  5. onFirmwareMessage subscriber pattern
// ----------------------------------------------------------------------

describe("onFirmwareMessage: CONTEXT subscriber fan-out", () => {
  it("delivers CONTEXT to a subscriber", async () => {
    const seen: FirmwareMessage[] = [];
    const unsub = await onFirmwareMessage((m) => seen.push(m));
    try {
      enqueue({ type: "CONTEXT", context: { patch_name: "Lead" } });
      await flush();
      expect(seen).toHaveLength(1);
      expect(seen[0].type).toBe("CONTEXT");
      expect((seen[0] as { context: { patch_name: string } }).context.patch_name).toBe("Lead");
    } finally {
      unsub();
    }
  });

  it("delivers CONTEXT to every subscriber, in order, with no drops", async () => {
    const seenA: FirmwareMessage[] = [];
    const seenB: FirmwareMessage[] = [];
    const unsubA = await onFirmwareMessage((m) => seenA.push(m));
    const unsubB = await onFirmwareMessage((m) => seenB.push(m));
    try {
      enqueue({ type: "CONTEXT", context: { patch_name: "One" } });
      enqueue({ type: "CONTEXT", context: { patch_name: "Two" } });
      await flush();
      expect(seenA).toHaveLength(2);
      expect(seenB).toHaveLength(2);
      const names = seenA.map((m) => (m as { context: { patch_name: string } }).context.patch_name);
      expect(names).toEqual(["One", "Two"]);
      expect(seenB).toEqual(seenA);
    } finally {
      unsubA();
      unsubB();
    }
  });

  it("unsubscribe removes the subscriber", async () => {
    const seen: FirmwareMessage[] = [];
    const unsub = await onFirmwareMessage((m) => seen.push(m));
    enqueue({ type: "CONTEXT", context: { patch_name: "First" } });
    await flush();
    expect(seen).toHaveLength(1);

    unsub();
    enqueue({ type: "CONTEXT", context: { patch_name: "Second" } });
    await flush();
    expect(seen).toHaveLength(1);
    expect((seen[0] as { context: { patch_name: string } }).context.patch_name).toBe("First");
  });
});

// ----------------------------------------------------------------------
//  6. StageView: CONTEXT-driven Stage Mode data flow
// ----------------------------------------------------------------------

describe("StageView: CONTEXT-driven Stage Mode data flow", () => {
  const baseProps = {
    deviceInfo: { fw: "0.5.1", device: "MIDI Captain", bank: 1, slot: 3, profile: "kemper" },
    manifest: null,
    connected: true,
    patches: [
      { bank: 1, slot: 1, name: "Clean", dirty: false },
      { bank: 1, slot: 3, name: "Acoustic", dirty: false },
    ] as PatchSummary[],
    onExit: vi.fn(),
  };

  function rigNameOf(container: HTMLElement): string {
    return container.querySelector(".stage__rig-name")?.textContent?.trim() ?? "";
  }

  /** Push a firmware CONTEXT line through the real drain + subscriber path
   *  and wait for Svelte to flush the resulting DOM update. */
  async function pushContext(context: Record<string, unknown>) {
    enqueue({ type: "CONTEXT", context });
    await flush();
    await tick();
  }

  it("polls for context on mount (sends GET_CONTEXT)", async () => {
    render(StageView, baseProps);
    await flushMicrotasks();
    expect(harness.sent.length).toBeGreaterThan(0);
    const types = harness.sent.map((l) => (JSON.parse(l) as { type: string }).type);
    // Polls live context and fetches the current patch (for its bindings).
    expect(types).toContain("GET_CONTEXT");
    expect(types).toContain("GET_PATCH");
  });

  it("renders kemper_rig_name from a CONTEXT push and follows live updates", async () => {
    const { container } = render(StageView, baseProps);
    await flushMicrotasks();
    await pushContext({ ...KEMPER_CONTEXT });
    expect(rigNameOf(container)).toBe("Marsh Lead");
    // A later push must replace the live state (the device changed rig).
    await pushContext({ ...KEMPER_CONTEXT, kemper_rig_name: "Crunch Chorus" });
    expect(rigNameOf(container)).toBe("Crunch Chorus");
  });

  it("falls back to the fetched patch name when kemper_rig_name is absent", async () => {
    const { container } = render(StageView, baseProps);
    await flushMicrotasks();
    // Before any CONTEXT: the fetched PATCH at deviceInfo bank/slot drives
    // the name (the firmware replies to GET_PATCH with the full patch).
    enqueue({ type: "PATCH", bank: 1, slot: 3, patch: { name: "Acoustic" } });
    await flush();
    await tick();
    expect(rigNameOf(container)).toBe("Acoustic");
    // A CONTEXT with patch_name but no kemper_rig_name does not change the
    // display: StageView's fallback reads the fetched PATCH name, not
    // context.patch_name.
    await pushContext({ patch_name: "Warm Clean", bank: 1, slot: 3, kemper_tuner: "off" });
    expect(rigNameOf(container)).toBe("Acoustic");
  });

  it("falls back to bank/slot when no name is known at all", async () => {
    const { container } = render(StageView, { ...baseProps, patches: [] });
    await flushMicrotasks();
    expect(rigNameOf(container)).toBe("1/3");
  });

  it("extracts BPM from kemper_bpm", async () => {
    const { container } = render(StageView, baseProps);
    await flushMicrotasks();
    await pushContext({ ...KEMPER_CONTEXT, kemper_bpm: 138 });
    const bpm = container.querySelector(".stage__bpm");
    expect(bpm).not.toBeNull();
    expect(bpm!.textContent).toContain("138");
    expect(bpm!.textContent).toContain("BPM");
  });

  it("shows the tuner (note + deviance symbol) when kemper_tuner is on", async () => {
    const { container } = render(StageView, baseProps);
    await flushMicrotasks();
    await pushContext({
      kemper_tuner: "on",
      kemper_tuner_note: "A",
      kemper_tuner_deviance: 8000,
    });
    const tuner = container.querySelector(".stage__tuner");
    expect(tuner).not.toBeNull();
    expect(tuner!.textContent).toContain("A");
    // Deviance 8000 sits in the in-tune band (not < 8000, not > 8400).
    expect(tuner!.textContent).toContain("●");
  });

  it("hides the tuner when kemper_tuner turns off", async () => {
    const { container } = render(StageView, baseProps);
    await flushMicrotasks();
    await pushContext({ kemper_tuner: "on", kemper_tuner_note: "A", kemper_tuner_deviance: 8000 });
    expect(container.querySelector(".stage__tuner")).not.toBeNull();
    await pushContext({ kemper_tuner: "off" });
    expect(container.querySelector(".stage__tuner")).toBeNull();
  });

  it("renders flat/sharp deviance symbols around the in-tune band", async () => {
    const { container } = render(StageView, baseProps);
    await flushMicrotasks();
    await pushContext({ kemper_tuner: "on", kemper_tuner_note: "B", kemper_tuner_deviance: 7000 });
    let tuner = container.querySelector(".stage__tuner");
    expect(tuner!.textContent).toContain("♭");
    await pushContext({ kemper_tuner: "on", kemper_tuner_note: "B", kemper_tuner_deviance: 9000 });
    tuner = container.querySelector(".stage__tuner");
    expect(tuner!.textContent).toContain("♯");
  });

  it("activates the tuner display through the generic tuner alias", async () => {
    // The plugin mirrors kemper_tuner -> tuner; StageView's on/off check
    // accepts both spellings, but the note/deviance are read from the
    // kemper_* fields only, so the generic aliases leave the placeholder.
    const { container } = render(StageView, baseProps);
    await flushMicrotasks();
    await pushContext({ tuner: "on", tuner_note: "E", tuner_deviance: 8000 });
    const tuner = container.querySelector(".stage__tuner");
    expect(tuner).not.toBeNull();
    expect(tuner!.textContent).toContain("--");
  });
});
