/**
 * Endurance / leak tests for src/lib/protocol.ts.
 *
 * The stress suite (protocol-stress.test.ts) proves the transport is
 * *correct* under a single burst. This suite proves it is *durable*:
 * across hours of real-world use the editor connects, floods traffic,
 * loses the link, reconnects, and repeats, hundreds of times. Every one
 * of those cycles must leave the module's internal bookkeeping back at
 * baseline. If any of `_pending`, `_firmwareSubscribers`,
 * `_firmwareRawSubscribers`, or `_debouncers` grows without bound, the
 * app would accumulate dangling resolvers/timers and eventually thrash
 * or leak memory during a long gig.
 *
 * We drive the firmware side ourselves by mocking @tauri-apps/api/core
 * (invoke) and @tauri-apps/api/event (listen), exactly like the stress
 * suite, and read the module's internal map/set sizes through the
 * test-only `__getInternalSizes()` observer (a pure getter that never
 * mutates state).
 *
 * Coverage:
 *   1. CONNECT/DISCONNECT CHURN - 500 connect->traffic->disconnect
 *      cycles; every in-flight sendAndAwait settles, _pending returns to
 *      0 and never exceeds a small bound.
 *   2. SUSTAINED MESSAGE FLUX - 100k inbound lines (matched responses,
 *      unmatched EVENTs, malformed JSON); correlated promises resolve,
 *      garbage never accumulates, subscriber fan-out stays exact, and no
 *      structure grows unbounded (sampled start/middle/end).
 *   3. TIMEOUT CLEANUP UNDER LOAD - many never-answered sendAndAwait
 *      calls all reject on their timers and _pending drains to 0.
 *   4. DEBOUNCER HYGIENE - many keys x many repeats; each key fires
 *      exactly once and the debouncer map empties (no leaked timers).
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
  __getInternalSizes,
  type Binding,
  type FirmwareMessage,
} from "../src/lib/protocol";

function enqueue(obj: Record<string, unknown>) {
  harness.inbox.push(JSON.stringify(obj));
}
function enqueueRaw(line: string) {
  harness.inbox.push(line);
}

async function flushMicrotasks(rounds = 10) {
  for (let i = 0; i < rounds; i++) await Promise.resolve();
}

/** Trigger the doorbell and let the async drain unwind. */
async function flush() {
  harness.doorbell?.();
  await flushMicrotasks();
}

function fakeDisconnect(reason = "test cleanup") {
  window.dispatchEvent(new CustomEvent("rust-disconnected", { detail: reason }));
}

/** Suppress unhandled-rejection noise for one settle window - used when we
 *  intentionally reject a pile of in-flight promises via disconnect and
 *  observe them through allSettled a tick later. */
async function settleQuietly(rounds = 6) {
  const onUnhandled = () => {};
  window.addEventListener("unhandledrejection", onUnhandled);
  for (let i = 0; i < rounds; i++) await Promise.resolve();
  window.removeEventListener("unhandledrejection", onUnhandled);
}

function binding(label: string): Binding {
  return {
    switch: "1",
    mode: "tap",
    label,
    led: { on: "#fff" },
    actions: { press: { messages: [] } },
  };
}

beforeEach(() => {
  harness.inbox = [];
  harness.sent = [];
});

afterEach(async () => {
  // Fail any leftover pending so the next test starts from a clean map.
  const onUnhandled = () => {};
  window.addEventListener("unhandledrejection", onUnhandled);
  fakeDisconnect("afterEach");
  for (let i = 0; i < 4; i++) await Promise.resolve();
  window.removeEventListener("unhandledrejection", onUnhandled);
});

// ----------------------------------------------------------------------
//  1) connect / disconnect churn
// ----------------------------------------------------------------------

describe("endurance: connect/disconnect churn", () => {
  it("500 connect->traffic->disconnect cycles leave _pending at baseline, never unbounded", async () => {
    const CYCLES = 500;
    // Requests per cycle: some get answered before the disconnect, the
    // rest are killed by the disconnect. This mirrors a gig where the USB
    // cable is yanked mid-conversation over and over.
    const IN_FLIGHT = 8;
    let maxPending = 0;
    let totalSettled = 0;

    for (let c = 0; c < CYCLES; c++) {
      const promises = Array.from({ length: IN_FLIGHT }, () =>
        sendAndAwait({ type: "PING" }, 30_000).then(
          () => "resolved" as const,
          () => "rejected" as const,
        ),
      );
      // Let every body reach _pending.set (each awaits the doorbell first).
      await flushMicrotasks();

      const afterSend = __getInternalSizes().pending;
      if (afterSend > maxPending) maxPending = afterSend;
      // Sanity: the in-flight set is bounded by what we just sent.
      expect(afterSend).toBe(IN_FLIGHT);

      // Answer half of them by id, leave the other half hanging.
      const ids = harness.sent.map(l => (JSON.parse(l) as { id?: string }).id!);
      harness.sent = [];
      const answered = ids.slice(0, Math.floor(IN_FLIGHT / 2));
      for (const id of answered) enqueue({ type: "ACK", id });
      await flush();

      // Now the link dies. This must reject every remaining in-flight
      // promise and clear _pending.
      fakeDisconnect("cable yanked");
      await settleQuietly();

      const results = await Promise.all(promises);
      totalSettled += results.length;
      // None left hanging: exactly IN_FLIGHT settled this cycle.
      expect(results.length).toBe(IN_FLIGHT);
      // _pending must be empty at the bottom of every cycle.
      expect(__getInternalSizes().pending).toBe(0);
    }

    // Final assertions: baseline reached, growth bounded.
    expect(__getInternalSizes().pending).toBe(0);
    expect(maxPending).toBeLessThanOrEqual(IN_FLIGHT);
    expect(totalSettled).toBe(CYCLES * IN_FLIGHT);
  });
});

// ----------------------------------------------------------------------
//  2) sustained message flux
// ----------------------------------------------------------------------

describe("endurance: sustained message flux", () => {
  it("100k mixed inbound lines - correlated resolves, garbage never accumulates, fan-out exact, no unbounded growth", async () => {
    const TOTAL = 100_000;

    const seenBySub: FirmwareMessage[] = [];
    const rawSeen: string[] = [];
    const unsubMsg = await onFirmwareMessage(m => { seenBySub.push(m); });
    const unsubRaw = await onFirmwareRawLine(l => { rawSeen.push(l); });

    // Silence the "non-json firmware line" warnings the malformed lines
    // trigger, so the test output stays readable.
    const warn = vi.spyOn(console, "warn").mockImplementation(() => {});

    // Sizes sampled across the run must stay flat (subscribers=1 each,
    // pending bounded by outstanding correlated requests we drain each
    // batch). We assert start/middle/end.
    const sizeSamples: Array<ReturnType<typeof __getInternalSizes>> = [];

    let correlatedResolved = 0;
    let parsedCount = 0;   // JSON messages the subscriber received
    let rawCount = 0;      // every line (incl. garbage) the raw sub received
    let garbageEnqueued = 0;

    const BATCH = 1000;
    const batches = TOTAL / BATCH;
    for (let b = 0; b < batches; b++) {
      // Fire a handful of correlated sendAndAwait per batch and answer
      // them in the SAME batch, so _pending should be back to 0 by the
      // end of every flush.
      const CORR = 5;
      const corrPromises = Array.from({ length: CORR }, () =>
        sendAndAwait({ type: "PING" }, 30_000),
      );
      await flushMicrotasks();
      const corrIds = harness.sent.map(l => (JSON.parse(l) as { id?: string }).id!);
      harness.sent = [];

      // Build the batch: correlated ACKs, unmatched EVENTs, and garbage.
      for (const id of corrIds) enqueue({ type: "ACK", id });
      let remaining = BATCH - corrIds.length;
      for (let i = 0; i < remaining; i++) {
        const roll = i % 3;
        if (roll === 0) {
          // Unmatched EVENT - has no id in _pending, must not accumulate.
          enqueue({ type: "EVENT", event: "patch_switched", bank: 1, slot: (i % 5) + 1 });
        } else if (roll === 1) {
          // Malformed JSON - must be tolerated and never stored.
          enqueueRaw(i % 2 === 0 ? "not json {{{" : "");
          garbageEnqueued++;
        } else {
          // A stray ACK for an id nobody is awaiting - a late/duplicate
          // response. Must be a no-op against _pending.
          enqueue({ type: "ACK", id: `stale-${b}-${i}` });
        }
      }

      await flush();
      const settled = await Promise.all(
        corrPromises.map(p => p.then(() => true, () => false)),
      );
      correlatedResolved += settled.filter(Boolean).length;

      // After a fully-drained batch, no correlated request is outstanding.
      expect(__getInternalSizes().pending).toBe(0);

      if (b === 0 || b === Math.floor(batches / 2) || b === batches - 1) {
        sizeSamples.push(__getInternalSizes());
      }
    }

    parsedCount = seenBySub.length;
    rawCount = rawSeen.length;

    warn.mockRestore();
    unsubMsg();
    unsubRaw();

    // Every correlated request resolved (none dropped, none stuck).
    expect(correlatedResolved).toBe(batches * 5);

    // The raw subscriber saw *every* line pushed, including garbage.
    // Total lines = correlated ACKs + the "remaining" fillers per batch.
    // remaining = BATCH - CORR each batch (CORR=5 correlated ids).
    const totalLines = batches * BATCH;
    expect(rawCount).toBe(totalLines);

    // The parsed subscriber saw every *valid JSON* line but none of the
    // garbage. parsed == totalLines - garbageEnqueued.
    expect(parsedCount).toBe(totalLines - garbageEnqueued);
    expect(garbageEnqueued).toBeGreaterThan(0);

    // No unbounded growth: subscriber counts flat across the run, and
    // pending drained to 0 at every sample point.
    for (const s of sizeSamples) {
      expect(s.subscribers).toBe(1);
      expect(s.rawSubscribers).toBe(1);
      expect(s.pending).toBe(0);
    }
    expect(sizeSamples.length).toBe(3);

    // After unsubscribing, the sets are empty again - subscription is
    // symmetric, no leaked handlers.
    expect(__getInternalSizes().subscribers).toBe(0);
    expect(__getInternalSizes().rawSubscribers).toBe(0);
  });
});

// ----------------------------------------------------------------------
//  3) timeout cleanup under load
// ----------------------------------------------------------------------

describe("endurance: timeout cleanup under load", () => {
  it("2000 never-answered requests all reject on their timers and _pending drains to 0", async () => {
    vi.useFakeTimers();
    try {
      const N = 2000;
      const promises = Array.from({ length: N }, () =>
        sendAndAwait({ type: "PING" }, 5000).then(
          () => "resolved" as const,
          e => (String(e).includes("timeout") ? "timed-out" : "other-reject") as const,
        ),
      );

      // sendAndAwait awaits _ensureAwaitListener (a real microtask chain)
      // before touching _pending. Under fake timers, advance microtasks
      // without advancing the clock so every body registers its timer
      // and resolver first.
      await vi.advanceTimersByTimeAsync(0);
      expect(__getInternalSizes().pending).toBe(N);

      // No response ever arrives. Cross the timeout window.
      await vi.advanceTimersByTimeAsync(5001);

      const results = await Promise.all(promises);
      expect(results).toHaveLength(N);
      expect(results.every(r => r === "timed-out")).toBe(true);

      // Every timer fired its cleanup: _pending is empty, no leaked
      // resolvers or timers left behind.
      expect(__getInternalSizes().pending).toBe(0);
    } finally {
      vi.useRealTimers();
    }
  });
});

// ----------------------------------------------------------------------
//  4) debouncer hygiene
// ----------------------------------------------------------------------

describe("endurance: debouncer hygiene", () => {
  it("many keys x many repeats - each key fires exactly once and the debouncer map empties", async () => {
    vi.useFakeTimers();
    try {
      const KEYS = 250;
      const REPEATS = 40;
      const keys = Array.from({ length: KEYS }, (_, i) => `sw${i}@1/1`);

      for (const k of keys) {
        for (let r = 0; r < REPEATS; r++) {
          debouncedPutBinding(1, 1, binding(`${k}-v${r}`), k, 300);
        }
      }

      // Before the window elapses: nothing dispatched, but every key has
      // exactly one live timer (coalesced) - map size equals key count.
      expect(harness.sent.length).toBe(0);
      expect(__getInternalSizes().debouncers).toBe(KEYS);

      vi.advanceTimersByTime(300);

      // Exactly one PUT_BINDING per key, each carrying that key's LAST
      // payload - no double-fires, no lost keys.
      expect(harness.sent.length).toBe(KEYS);
      const labels = harness.sent
        .map(s => (JSON.parse(s) as { binding: { label: string } }).binding.label)
        .sort();
      const expected = keys.map(k => `${k}-v${REPEATS - 1}`).sort();
      expect(labels).toEqual(expected);

      // The map fully drained - every timer deleted its own key on fire,
      // no leaked timers or keys.
      expect(__getInternalSizes().debouncers).toBe(0);
    } finally {
      vi.useRealTimers();
    }
  });

  it("repeated rounds of debounce churn never let the debouncer map grow unbounded", async () => {
    vi.useFakeTimers();
    try {
      const ROUNDS = 200;
      const KEYS_PER_ROUND = 5;
      let maxDebouncers = 0;

      for (let round = 0; round < ROUNDS; round++) {
        const keys = Array.from({ length: KEYS_PER_ROUND }, (_, i) => `k${i}@1/1`);
        for (const k of keys) {
          for (let r = 0; r < 10; r++) {
            debouncedPutBinding(1, 1, binding(`${k}-${round}-${r}`), k, 300);
          }
        }
        const live = __getInternalSizes().debouncers;
        if (live > maxDebouncers) maxDebouncers = live;
        vi.advanceTimersByTime(300);
        // Each round fully drains before the next begins.
        expect(__getInternalSizes().debouncers).toBe(0);
      }

      // Keys reused each round: the map is bounded by the per-round key
      // set, never the cumulative total across rounds.
      expect(maxDebouncers).toBe(KEYS_PER_ROUND);
      expect(harness.sent.length).toBe(ROUNDS * KEYS_PER_ROUND);
    } finally {
      vi.useRealTimers();
    }
  });
});
