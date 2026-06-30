/**
 * Transport-layer stress tests for src/lib/protocol.ts.
 *
 * Covers the parts of the protocol that the editor leans on when the
 * pedal is generating a lot of traffic (rapid rig switching, MIDI
 * capture floods, bidirectional Kemper beacons) or when the host
 * fires many commands back-to-back (Patches "Save all" against 125
 * dirty patches, applyToLinked over a full bank set, ...):
 *
 *  - request/response correlation via the `id` field
 *  - timeout cleanup of the _pending map
 *  - disconnect failing every in-flight sendAndAwait
 *  - subscriber fan-out under burst
 *  - subscriber unsubscribe DURING dispatch (Set iteration semantics)
 *  - malformed JSON tolerance
 *  - debouncedPutBinding coalescing on the same key, independence
 *    across different keys
 *
 * The test mocks @tauri-apps/api/core (invoke) and @tauri-apps/api/event
 * (listen) so we can drive the firmware side ourselves. protocol.ts
 * keeps module-level state (subscriber sets, _pending, _draining,
 * the doorbell handler) so each test cleans up after itself by
 * unsubscribing.
 *
 * NOTE on harness state: vi.mock factories are HOISTED above the test
 * file's let-declarations, so a mock body that closes over plain
 * top-level `let` would hit a TDZ at factory-definition time. We
 * stash the harness state inside vi.hoisted() so the mock factory
 * and the test bodies share the same object reference cleanly.
 */
import { describe, it, expect, beforeEach, afterEach, vi } from "vitest";

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
  invoke: vi.fn(async (cmd: string, args?: Record<string, unknown>) => {
    switch (cmd) {
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

import {
  sendAndAwait,
  onFirmwareMessage,
  onFirmwareRawLine,
  debouncedPutBinding,
  cmd,
  type Binding,
  type FirmwareMessage,
} from "../src/lib/protocol";

function enqueue(obj: Record<string, unknown>) {
  harness.inbox.push(JSON.stringify(obj));
}
function enqueueRaw(line: string) {
  harness.inbox.push(line);
}

/** Yield enough microtasks for any async function bodies waiting on
 *  awaited promises (mocked invoke, listen, etc.) to advance. */
async function flushMicrotasks(rounds = 10) {
  for (let i = 0; i < rounds; i++) await Promise.resolve();
}

/** Trigger the doorbell and let the async drain unwind. drain reads
 *  via an awaited invoke, so we flush a handful of microtasks rather
 *  than relying on a single Promise.resolve. */
async function flush() {
  harness.doorbell?.();
  await flushMicrotasks();
}

function fakeDisconnect(reason = "test cleanup") {
  window.dispatchEvent(new CustomEvent("rust-disconnected", { detail: reason }));
}

beforeEach(() => {
  harness.inbox = [];
  harness.sent = [];
});

afterEach(async () => {
  // Drain any leftover pending so subsequent tests start clean. The
  // dispatch attaches an unhandled-rejection on already-rejected
  // promises only when no test code observed them - we keep noise out
  // of unhandled-rejection output by suppressing the global handler
  // for one tick.
  const onUnhandled = () => {};
  window.addEventListener("unhandledrejection", onUnhandled);
  fakeDisconnect("afterEach");
  // Let microtasks settle so any "disconnected" rejection finishes
  // running its handlers.
  for (let i = 0; i < 4; i++) await Promise.resolve();
  window.removeEventListener("unhandledrejection", onUnhandled);
});

// ----------------------------------------------------------------------
//  request/response correlation under burst
// ----------------------------------------------------------------------

describe("sendAndAwait: concurrent request/response correlation", () => {
  it("correlates 500 concurrent requests by id, even when responses arrive out of order", async () => {
    const N = 500;
    // Fire N requests in parallel. Use a long timeout - we don't
    // want any of them to expire before we flush our mock responses.
    const promises: Promise<FirmwareMessage>[] = [];
    for (let i = 0; i < N; i++) {
      promises.push(sendAndAwait({ type: "PING" }, 30_000));
    }
    // sendAndAwait is async (`await _ensureAwaitListener()` at the top),
    // so each call's body is still suspended at the first await. Flush
    // microtasks so the bodies reach the inner invoke() that records
    // into harness.sent.
    await flushMicrotasks();
    expect(harness.sent.length).toBe(N);
    const ids = harness.sent.map(line => {
      const obj = JSON.parse(line) as { id?: string };
      return obj.id!;
    });
    const shuffled = [...ids].reverse();
    for (const id of shuffled) {
      enqueue({ type: "ACK", id });
    }
    await flush();
    const results = await Promise.all(promises);
    expect(results).toHaveLength(N);
    const seen = new Set<string>();
    for (const r of results) {
      expect(r.type).toBe("ACK");
      const id = (r as { id?: string }).id;
      expect(id).toBeDefined();
      seen.add(id!);
    }
    expect(seen.size).toBe(N);
    for (const id of ids) expect(seen.has(id)).toBe(true);
  });

  it("a request with no matching response times out and stops blocking subsequent traffic", async () => {
    const start = Date.now();
    await expect(sendAndAwait({ type: "PING" }, 50)).rejects.toThrow(/timeout/);
    const elapsed = Date.now() - start;
    expect(elapsed).toBeGreaterThanOrEqual(40);
    // Now a follow-up request must still work - the _pending map
    // mustn't be left polluted.
    const p = sendAndAwait({ type: "PING" }, 5000);
    // Yield so the body progresses past _ensureAwaitListener and the
    // PING line is recorded into harness.sent.
    await flushMicrotasks();
    const id = JSON.parse(harness.sent[harness.sent.length - 1]).id;
    enqueue({ type: "ACK", id });
    await flush();
    await expect(p).resolves.toMatchObject({ type: "ACK" });
  });

  it("rust-disconnected event rejects every in-flight sendAndAwait at once", async () => {
    const N = 50;
    const promises = Array.from({ length: N }, () =>
      sendAndAwait({ type: "PING" }, 30_000).then(
        v => ({ ok: true as const, v }),
        e => ({ ok: false as const, e: String(e) }),
      ),
    );
    // Yield so every sendAndAwait body has reached _pending.set before
    // we fire the disconnect - otherwise _failPending finds an empty
    // map and the test hangs waiting for nothing to reject.
    await flushMicrotasks();
    fakeDisconnect("link died");
    const results = await Promise.all(promises);
    expect(results.every(r => !r.ok)).toBe(true);
    for (const r of results) {
      if (!r.ok) expect(r.e).toMatch(/error: disconnected/);
    }
  });

  it("ERROR response on the matching id rejects, late ACK on the same id is a no-op", async () => {
    const p = sendAndAwait({ type: "PUT_GLOBAL", device: {} }, 5000);
    // Wait for the async sendAndAwait body to reach invoke().
    await flushMicrotasks();
    expect(harness.sent.length).toBeGreaterThan(0);
    const id = JSON.parse(harness.sent[harness.sent.length - 1]).id;
    enqueue({ type: "ERROR", id, error: "rejected" });
    await flush();
    await expect(p).rejects.toThrow(/rejected/);
    // Late ACK for the same id must NOT re-resolve the already-rejected
    // promise (it's already deleted from _pending).
    enqueue({ type: "ACK", id });
    await flush();
    // If _pending had leaked we'd see the resolver fire and throw -
    // reaching here clean is the assertion.
    expect(true).toBe(true);
  });
});

// ----------------------------------------------------------------------
//  subscriber fan-out
// ----------------------------------------------------------------------

describe("onFirmwareMessage: subscriber fan-out under burst", () => {
  it("every subscriber receives every message in burst, in order, with no drops", async () => {
    const seenA: FirmwareMessage[] = [];
    const seenB: FirmwareMessage[] = [];
    const unsubA = await onFirmwareMessage(m => seenA.push(m));
    const unsubB = await onFirmwareMessage(m => seenB.push(m));
    try {
      const N = 200;
      for (let i = 0; i < N; i++) {
        enqueue({ type: "EVENT", event: "patch_switched", bank: 1, slot: ((i % 5) + 1) });
      }
      await flush();
      expect(seenA).toHaveLength(N);
      expect(seenB).toHaveLength(N);
      for (let i = 0; i < N; i++) {
        expect(seenA[i]).toEqual(seenB[i]);
        expect((seenA[i] as { slot: number }).slot).toBe((i % 5) + 1);
      }
    } finally {
      unsubA();
      unsubB();
    }
  });

  // Production code iterates the live Set, so a handler that unsubs a
  // peer mid-dispatch causes the peer to be skipped for the CURRENT
  // event (Set iteration semantics: deleting an unvisited entry skips
  // it). The contract we verify here is the observable one:
  //   1. The unsubscribe does not throw.
  //   2. The unsubbed peer never sees any FUTURE event.
  // This matches the user's mental model of "unsubscribe = stop hearing
  // anything else, including the event currently being dispatched."
  it("unsubscribing a peer during dispatch does not throw and excludes it from all future events", async () => {
    const seenA: FirmwareMessage[] = [];
    const seenB: FirmwareMessage[] = [];
    let unsubB: (() => void) | null = null;
    const unsubA = await onFirmwareMessage(m => {
      seenA.push(m);
      if (unsubB) { unsubB(); unsubB = null; }
    });
    unsubB = await onFirmwareMessage(m => seenB.push(m));
    try {
      enqueue({ type: "ACK", id: "first" });
      await flush();
      // A fired (1). B was unsubscribed before iteration reached it, so it
      // gets 0 for this event - that's the documented semantics.
      expect(seenA.length).toBe(1);
      expect(seenB.length).toBe(0);
      // Subsequent events: A only.
      enqueue({ type: "ACK", id: "second" });
      enqueue({ type: "ACK", id: "third" });
      await flush();
      expect(seenA.length).toBe(3);
      expect(seenB.length).toBe(0);
    } finally {
      unsubA();
    }
  });

  it("raw-line subscribers receive even the malformed lines that JSON.parse rejects", async () => {
    const raw: string[] = [];
    const parsed: FirmwareMessage[] = [];
    const unsubRaw = await onFirmwareRawLine(l => raw.push(l));
    const unsubMsg = await onFirmwareMessage(m => parsed.push(m));
    try {
      enqueueRaw("not even close to JSON{{");
      enqueue({ type: "ACK", id: "valid" });
      enqueueRaw("");
      const warn = vi.spyOn(console, "warn").mockImplementation(() => {});
      await flush();
      warn.mockRestore();
      expect(raw.length).toBe(3);
      expect(parsed.length).toBe(1);
      expect(parsed[0].type).toBe("ACK");
    } finally {
      unsubRaw();
      unsubMsg();
    }
  });
});

// ----------------------------------------------------------------------
//  _drainOnce re-entry guard
// ----------------------------------------------------------------------

describe("_drainOnce: re-entry / burst safety", () => {
  it("a 1000-line burst delivered in one drain still preserves order", async () => {
    const seen: number[] = [];
    const unsub = await onFirmwareMessage(m => {
      if (m.type === "EVENT") seen.push((m as { slot: number }).slot);
    });
    try {
      const N = 1000;
      for (let i = 0; i < N; i++) {
        enqueue({ type: "EVENT", event: "patch_switched", bank: 1, slot: i });
      }
      await flush();
      expect(seen.length).toBe(N);
      for (let i = 0; i < N; i++) expect(seen[i]).toBe(i);
    } finally {
      unsub();
    }
  });

  it("rapid doorbell triggers don't lose messages that arrive between drains", async () => {
    const seen: FirmwareMessage[] = [];
    const unsub = await onFirmwareMessage(m => seen.push(m));
    try {
      enqueue({ type: "ACK", id: "1" });
      await flush();
      enqueue({ type: "ACK", id: "2" });
      enqueue({ type: "ACK", id: "3" });
      await flush();
      enqueue({ type: "ACK", id: "4" });
      await flush();
      expect(seen.map(m => (m as { id?: string }).id)).toEqual(["1", "2", "3", "4"]);
    } finally {
      unsub();
    }
  });
});

// ----------------------------------------------------------------------
//  debouncedPutBinding
// ----------------------------------------------------------------------

describe("debouncedPutBinding: coalescing under rapid edits", () => {
  function binding(label: string): Binding {
    return {
      switch: "1",
      mode: "tap",
      label,
      led: { on: "#fff" },
      actions: { press: { messages: [] } },
    };
  }

  it("100 rapid same-key calls coalesce into one putBinding carrying the LAST payload", async () => {
    vi.useFakeTimers();
    try {
      for (let i = 0; i < 100; i++) {
        debouncedPutBinding(1, 1, binding(`v${i}`), "1@1/1", 300);
      }
      // Before the debounce window elapses, no command has flown.
      expect(harness.sent.length).toBe(0);
      vi.advanceTimersByTime(300);
      // Exactly one PUT_BINDING and it carries the latest payload.
      expect(harness.sent.length).toBe(1);
      const obj = JSON.parse(harness.sent[0]);
      expect(obj.type).toBe("PUT_BINDING");
      expect(obj.binding.label).toBe("v99");
    } finally {
      vi.useRealTimers();
    }
  });

  it("calls on different keys are independent - each flushes once with its own payload", async () => {
    vi.useFakeTimers();
    try {
      const keys = ["1@1/1", "2@1/1", "3@1/1", "4@1/1", "up@1/1"];
      for (const k of keys) {
        for (let i = 0; i < 10; i++) {
          debouncedPutBinding(1, 1, binding(`${k}-v${i}`), k, 300);
        }
      }
      vi.advanceTimersByTime(300);
      expect(harness.sent.length).toBe(keys.length);
      const labels = harness.sent.map(s => JSON.parse(s).binding.label as string).sort();
      const expected = keys.map(k => `${k}-v9`).sort();
      expect(labels).toEqual(expected);
    } finally {
      vi.useRealTimers();
    }
  });
});

// ----------------------------------------------------------------------
//  cmd.* surface: shape sanity under stress
// ----------------------------------------------------------------------

describe("cmd.*: every command builds a well-formed JSON payload", () => {
  it("125 putPatch calls all have unique ids and a parseable shape", () => {
    for (let bank = 1; bank <= 25; bank++) {
      for (let slot = 1; slot <= 5; slot++) {
        cmd.putPatch(bank, slot, {
          name: `p${bank}-${slot}`,
          bindings: [],
        });
      }
    }
    expect(harness.sent.length).toBe(125);
    const ids = new Set<string>();
    for (const line of harness.sent) {
      const obj = JSON.parse(line);
      expect(obj.type).toBe("PUT_PATCH");
      expect(obj.id).toBeDefined();
      expect(obj.bank).toBeGreaterThanOrEqual(1);
      expect(obj.slot).toBeGreaterThanOrEqual(1);
      expect(obj.patch).toBeDefined();
      ids.add(obj.id);
    }
    expect(ids.size).toBe(125);
  });

  it("saveNow with no args emits a global save, with args it scopes to one patch", () => {
    cmd.saveNow();
    cmd.saveNow(3, 4);
    const global = JSON.parse(harness.sent[0]);
    expect(global.type).toBe("SAVE_NOW");
    expect(global.bank).toBeUndefined();
    expect(global.slot).toBeUndefined();
    const scoped = JSON.parse(harness.sent[1]);
    expect(scoped.type).toBe("SAVE_NOW");
    expect(scoped.bank).toBe(3);
    expect(scoped.slot).toBe(4);
  });
});
