/**
 * App.svelte handler stress: state consistency under burst.
 *
 * Follows the same convention as tests/app-events.test.ts - we mirror
 * the relevant App.svelte handler bodies into local test functions and
 * exercise THEM rather than mounting the full Svelte component. The
 * code under test in App.svelte is documented and small; if either
 * drifts from the other, this file must be updated in lockstep.
 *
 * What we cover:
 *  - PATCH response filter: only adopt when it matches the currently-open
 *    bank/slot. Background GET_PATCH calls fired by link-maintenance
 *    helpers must NOT hijack the editor.
 *  - handleCapture: bank-MSB cache, capture buffer cap of 20.
 *  - pushLog: log cap of 200.
 *  - Manifest retry budget: stops at MANIFEST_MAX_RETRIES = 5, flips
 *    manifestGaveUp exactly once.
 *  - patch_switched event updates the local bank/slot view.
 *  - dirty_state_changed sets the dirtyIds list wholesale.
 *
 * These are the handlers most likely to break consistency under stress
 * (rapid bidirectional Kemper traffic, fast rig changes, mass-save):
 * none of them have rate limiters of their own, so the caps are the
 * only thing standing between the firmware spew and a runaway UI.
 */
import { describe, it, expect, vi, beforeEach } from "vitest";

// ----------------------------------------------------------------------
//  PATCH filter
// ----------------------------------------------------------------------

interface Loc { bank: number; slot: number; }
type PatchEnvelope = { bank: number; slot: number; patch: { name: string; bindings: unknown[] } };

/**
 * Mirrors App.svelte's handleMessage `case "PATCH"` branch. Background
 * GET_PATCH calls (link maintenance) deliver PATCH responses too; we
 * only adopt them when the response matches the open patch's location.
 *
 * Source: src/App.svelte handleMessage > PATCH case.
 */
function handlePatch(
  msg: { type: "PATCH" } & PatchEnvelope,
  currentPatch: PatchEnvelope | null,
): PatchEnvelope | null {
  if (!currentPatch
      || (msg.bank === currentPatch.bank && msg.slot === currentPatch.slot)) {
    return { bank: msg.bank, slot: msg.slot, patch: msg.patch };
  }
  return currentPatch;
}

describe("App.handleMessage PATCH filter: no hijacking by background fetches", () => {
  it("adopts the response when it matches the open patch", () => {
    const open: PatchEnvelope = { bank: 1, slot: 1, patch: { name: "stale", bindings: [] } };
    const next = handlePatch(
      { type: "PATCH", bank: 1, slot: 1, patch: { name: "fresh", bindings: [] } },
      open,
    );
    expect(next?.patch.name).toBe("fresh");
  });

  it("ignores 100 background PATCH responses targeting other slots while editing 1/1", () => {
    const open: PatchEnvelope = { bank: 1, slot: 1, patch: { name: "editing", bindings: [] } };
    let current = open;
    for (let i = 0; i < 100; i++) {
      const bank = ((i % 25) + 1);
      const slot = ((i % 5) + 1);
      // Skip the one match - background fetches are for OTHER patches.
      if (bank === 1 && slot === 1) continue;
      current = handlePatch(
        { type: "PATCH", bank, slot, patch: { name: `bg-${i}`, bindings: [] } },
        current,
      ) ?? current;
    }
    // The currentPatch stayed pinned on (1,1) with the editing payload.
    expect(current).toEqual(open);
  });

  it("when nothing is open, the next PATCH becomes the open patch", () => {
    const next = handlePatch(
      { type: "PATCH", bank: 5, slot: 3, patch: { name: "x", bindings: [] } },
      null,
    );
    expect(next?.bank).toBe(5);
    expect(next?.slot).toBe(3);
  });
});

// ----------------------------------------------------------------------
//  handleCapture: bank-MSB cache + buffer cap
// ----------------------------------------------------------------------

interface MidiInCapture {
  port: "din" | "usb";
  channel: number;
  kind: "cc" | "pc" | "note_on" | "note_off" | "poly_pressure" | "channel_pressure" | "pitch_bend" | "unknown";
  data: number[];
}

type PatchCapture = { port: "din" | "usb"; channel: number; bank_msb: number; pc: number; ts: number; };

const CAPTURE_CAP = 20;

/**
 * Mirrors App.svelte's handleCapture - tracks bank-MSB CCs per
 * (port, channel) and groups them with the next PC into a single
 * PatchCapture. Buffer is FIFO with a 20-entry cap (newest first).
 *
 * Source: src/App.svelte handleCapture.
 */
function handleCapture(
  msg: MidiInCapture,
  state: { captures: PatchCapture[]; msbCache: Map<string, number>; nowMs: () => number },
): void {
  const { port, channel, kind } = msg;
  const data = msg.data ?? [];
  const key = `${port}:${channel}`;
  if (kind === "cc" && data[0] === 0) { state.msbCache.set(key, data[1] ?? 0); return; }
  if (kind === "pc" && data.length > 0) {
    const capture: PatchCapture = {
      port, channel,
      bank_msb: state.msbCache.get(key) ?? 0,
      pc: data[0], ts: state.nowMs(),
    };
    const next = [capture, ...state.captures];
    if (next.length > CAPTURE_CAP) next.length = CAPTURE_CAP;
    state.captures = next;
  }
}

describe("App.handleCapture: buffer cap holds against MIDI flood", () => {
  function newState() {
    return {
      captures: [] as PatchCapture[],
      msbCache: new Map<string, number>(),
      nowMs: () => 0,
    };
  }

  it("a 1000-PC flood never blows past CAPTURE_CAP = 20", () => {
    const s = newState();
    for (let i = 0; i < 1000; i++) {
      handleCapture({ port: "usb", channel: 1, kind: "pc", data: [i % 128] }, s);
    }
    expect(s.captures.length).toBeLessThanOrEqual(CAPTURE_CAP);
    expect(s.captures.length).toBe(CAPTURE_CAP);
  });

  it("the most recent PC is at the front of the buffer (FIFO from the head)", () => {
    const s = newState();
    let counter = 0;
    s.nowMs = () => ++counter;
    for (let i = 0; i < 100; i++) {
      handleCapture({ port: "usb", channel: 1, kind: "pc", data: [i % 128] }, s);
    }
    // Latest insertion has highest ts.
    const tss = s.captures.map(c => c.ts);
    for (let i = 1; i < tss.length; i++) {
      expect(tss[i - 1]).toBeGreaterThan(tss[i]);
    }
  });

  it("a bank-MSB CC followed by a PC groups them into one capture", () => {
    const s = newState();
    handleCapture({ port: "usb", channel: 1, kind: "cc", data: [0, 7] }, s);
    handleCapture({ port: "usb", channel: 1, kind: "pc", data: [11] }, s);
    expect(s.captures).toHaveLength(1);
    expect(s.captures[0]).toMatchObject({ port: "usb", channel: 1, bank_msb: 7, pc: 11 });
  });

  it("bank-MSB cache is keyed per port+channel (din 1 doesn't leak into usb 1)", () => {
    const s = newState();
    handleCapture({ port: "din", channel: 1, kind: "cc", data: [0, 99] }, s);
    handleCapture({ port: "usb", channel: 1, kind: "pc", data: [3] }, s);
    expect(s.captures[0].bank_msb).toBe(0);    // usb cache was empty
    handleCapture({ port: "din", channel: 1, kind: "pc", data: [4] }, s);
    expect(s.captures[0].bank_msb).toBe(99);
    expect(s.captures[0].pc).toBe(4);
  });

  it("unknown/exotic kinds (note_on, pitch_bend) are silently ignored", () => {
    const s = newState();
    handleCapture({ port: "usb", channel: 1, kind: "note_on", data: [60, 100] }, s);
    handleCapture({ port: "usb", channel: 1, kind: "pitch_bend", data: [0, 64] }, s);
    expect(s.captures).toHaveLength(0);
  });
});

// ----------------------------------------------------------------------
//  pushLog: cap holds
// ----------------------------------------------------------------------

const LOG_CAP = 200;

type LogEntry = { ts: number; raw: unknown };

function pushLog(log: LogEntry[], raw: unknown, ts: number): LogEntry[] {
  const next = [...log, { ts, raw }];
  if (next.length > LOG_CAP) next.splice(0, next.length - LOG_CAP);
  return next;
}

describe("App.pushLog: cap holds, latest stays at the tail", () => {
  it("a 10000-event flood is capped at LOG_CAP", () => {
    let log: LogEntry[] = [];
    for (let i = 0; i < 10_000; i++) {
      log = pushLog(log, { type: "EVENT", n: i }, i);
    }
    expect(log.length).toBe(LOG_CAP);
    // The LAST 200 entries are what remains; latest is at the tail.
    expect((log[log.length - 1].raw as { n: number }).n).toBe(9999);
    expect((log[0].raw as { n: number }).n).toBe(10_000 - LOG_CAP);
  });

  it("under the cap, every event is preserved in order", () => {
    let log: LogEntry[] = [];
    for (let i = 0; i < 50; i++) {
      log = pushLog(log, { type: "EVENT", n: i }, i);
    }
    expect(log).toHaveLength(50);
    for (let i = 0; i < 50; i++) {
      expect((log[i].raw as { n: number }).n).toBe(i);
    }
  });
});

// ----------------------------------------------------------------------
//  Manifest retry budget
// ----------------------------------------------------------------------

const MANIFEST_MAX_RETRIES = 5;

interface ManifestState {
  connected: boolean;
  manifest: unknown | null;
  retries: number;
  gaveUp: boolean;
  sent: number;
}

/**
 * Mirrors App.svelte's manifest retry tick. The real one runs inside
 * a $effect that re-fires a setTimeout chain; here we just run the
 * tick body directly N times.
 */
function manifestTick(s: ManifestState, getManifest: () => void): void {
  if (s.manifest || !s.connected) return;
  if (s.retries >= MANIFEST_MAX_RETRIES) {
    s.gaveUp = true;
    return;
  }
  s.retries += 1;
  getManifest();
  s.sent += 1;
}

describe("Manifest retry budget", () => {
  it("never sends more than MANIFEST_MAX_RETRIES GET_MANIFESTs", () => {
    const s: ManifestState = { connected: true, manifest: null, retries: 0, gaveUp: false, sent: 0 };
    const getManifest = vi.fn();
    for (let i = 0; i < 100; i++) {
      manifestTick(s, getManifest);
    }
    expect(getManifest).toHaveBeenCalledTimes(MANIFEST_MAX_RETRIES);
    expect(s.gaveUp).toBe(true);
  });

  it("a successful response (manifest set) stops further sends, even if the tick keeps firing", () => {
    const s: ManifestState = { connected: true, manifest: null, retries: 0, gaveUp: false, sent: 0 };
    const getManifest = vi.fn(() => { s.manifest = {/* arrived */}; });
    for (let i = 0; i < 10; i++) {
      manifestTick(s, getManifest);
    }
    expect(getManifest).toHaveBeenCalledTimes(1);
    expect(s.gaveUp).toBe(false);
  });

  it("retries reset to zero and gaveUp clears when the user clicks Retry", () => {
    const s: ManifestState = { connected: true, manifest: null, retries: MANIFEST_MAX_RETRIES, gaveUp: true, sent: 0 };
    // App.retryManifest resets these.
    s.retries = 0;
    s.gaveUp = false;
    const getManifest = vi.fn();
    for (let i = 0; i < 5; i++) {
      manifestTick(s, getManifest);
    }
    expect(getManifest).toHaveBeenCalledTimes(MANIFEST_MAX_RETRIES);
    expect(s.gaveUp).toBe(false);
  });

  it("disconnect mid-retry pauses ticking until reconnect", () => {
    const s: ManifestState = { connected: true, manifest: null, retries: 0, gaveUp: false, sent: 0 };
    const getManifest = vi.fn();
    manifestTick(s, getManifest);
    expect(getManifest).toHaveBeenCalledTimes(1);
    s.connected = false;
    for (let i = 0; i < 5; i++) manifestTick(s, getManifest);
    expect(getManifest).toHaveBeenCalledTimes(1);
    s.connected = true;
    manifestTick(s, getManifest);
    expect(getManifest).toHaveBeenCalledTimes(2);
  });
});

// ----------------------------------------------------------------------
//  patch_switched / dirty_state_changed events
// ----------------------------------------------------------------------

interface AppState {
  deviceInfo: { fw: string; device: string; bank: number; slot: number } | null;
  dirtyIds: Loc[];
}

/** Mirrors App.svelte's "patch_switched" + "dirty_state_changed" cases. */
function applyEvent(
  msg: { type: "EVENT"; event: string; bank?: number; slot?: number; patches?: Loc[] },
  state: AppState,
  cmd: { getPatch: (b: number, s: number) => void },
): void {
  if (msg.event === "patch_switched") {
    if (state.deviceInfo) state.deviceInfo = { ...state.deviceInfo, bank: msg.bank!, slot: msg.slot! };
    cmd.getPatch(msg.bank!, msg.slot!);
  } else if (msg.event === "dirty_state_changed") {
    state.dirtyIds = msg.patches ?? [];
  }
}

describe("App.applyEvent: patch_switched / dirty_state_changed", () => {
  let cmd: { getPatch: ReturnType<typeof vi.fn> };
  beforeEach(() => { cmd = { getPatch: vi.fn() }; });

  it("rapid patch_switched events all refresh the deviceInfo and fetch the new patch", () => {
    const state: AppState = {
      deviceInfo: { fw: "0.3.10", device: "captain", bank: 1, slot: 1 },
      dirtyIds: [],
    };
    for (let i = 0; i < 50; i++) {
      const bank = ((i % 25) + 1);
      const slot = ((i % 5) + 1);
      applyEvent({ type: "EVENT", event: "patch_switched", bank, slot }, state, cmd);
    }
    // Last switch wins for deviceInfo.
    expect(state.deviceInfo).toMatchObject({ bank: ((49 % 25) + 1), slot: ((49 % 5) + 1) });
    expect(cmd.getPatch).toHaveBeenCalledTimes(50);
  });

  it("dirty_state_changed replaces the dirtyIds list wholesale (no append)", () => {
    const state: AppState = {
      deviceInfo: { fw: "0.3.10", device: "x", bank: 1, slot: 1 },
      dirtyIds: [{ bank: 1, slot: 1 }, { bank: 2, slot: 2 }],
    };
    applyEvent(
      { type: "EVENT", event: "dirty_state_changed", patches: [{ bank: 3, slot: 3 }] },
      state, cmd,
    );
    expect(state.dirtyIds).toEqual([{ bank: 3, slot: 3 }]);
  });

  it("dirty_state_changed with no patches clears the list", () => {
    const state: AppState = {
      deviceInfo: { fw: "0.3.10", device: "x", bank: 1, slot: 1 },
      dirtyIds: [{ bank: 1, slot: 1 }],
    };
    applyEvent(
      { type: "EVENT", event: "dirty_state_changed", patches: [] },
      state, cmd,
    );
    expect(state.dirtyIds).toEqual([]);
  });

  it("patch_switched when deviceInfo is null still fetches the new patch", () => {
    const state: AppState = { deviceInfo: null, dirtyIds: [] };
    applyEvent({ type: "EVENT", event: "patch_switched", bank: 4, slot: 2 }, state, cmd);
    expect(state.deviceInfo).toBeNull();
    expect(cmd.getPatch).toHaveBeenCalledWith(4, 2);
  });
});

// ----------------------------------------------------------------------
//  Connecting depth counter
// ----------------------------------------------------------------------

/**
 * Mirrors App.svelte's bosun-connecting counter. Concurrent waitForReboot
 * calls (firmware push + manual reboot, profile switch + manual reboot)
 * must not deactivate each other's busy indicator.
 *
 * Source: src/App.svelte connecting-depth event handler.
 */
function applyConnecting(active: boolean, state: { depth: number; busy: boolean }, switchingProfile = false): void {
  state.depth = Math.max(0, state.depth + (active ? 1 : -1));
  state.busy = state.depth > 0 || state.busy;
  if (state.depth === 0 && !switchingProfile) state.busy = false;
}

describe("Connecting-depth counter: overlapping connect operations", () => {
  it("two overlapping waitForReboot calls don't deactivate each other prematurely", () => {
    const s = { depth: 0, busy: false };
    applyConnecting(true, s);
    applyConnecting(true, s);
    expect(s.busy).toBe(true);
    applyConnecting(false, s);
    expect(s.busy).toBe(true);    // one still in flight
    applyConnecting(false, s);
    expect(s.busy).toBe(false);
  });

  it("a stray inactive event when depth is already 0 does NOT go negative", () => {
    const s = { depth: 0, busy: false };
    applyConnecting(false, s);
    expect(s.depth).toBe(0);
    expect(s.busy).toBe(false);
  });

  it("100 alternating active/inactive events still leave depth = 0 at the end", () => {
    const s = { depth: 0, busy: false };
    for (let i = 0; i < 100; i++) applyConnecting(i % 2 === 0, s);
    expect(s.depth).toBe(0);
  });
});
