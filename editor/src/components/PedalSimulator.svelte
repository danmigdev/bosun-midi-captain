<script lang="ts">
  import { onMount, onDestroy } from "svelte";
  import PedalMap from "./PedalMap.svelte";
  import { SwitchFsm } from "../lib/switch-fsm";
  import { ledColorFor } from "../lib/led-color";
  import { decodeMidi, type MidiMonitorEvent } from "../lib/midi-decode";
  import {
    cmd, onFirmwareMessage, summarizeMessage,
    type Binding, type FirmwareMessage,
  } from "../lib/protocol";

  let {
    bindings,
    device = null,
    connected,
  }: {
    bindings: Binding[];
    device?: Record<string, unknown> | null;
    connected: boolean;
  } = $props();

  type Mode = "model" | "live";
  let mode = $state<Mode>("model");

  // Inferred latched state per switch. Offline: driven by the JS FSM. Live:
  // inferred from binding_fired (toggle_on/off). Both colour via ledColorFor.
  let latched = $state<Record<string, boolean>>({});
  let pressed = $state<string | null>(null);

  type LogRow = { id: number; ts: number; kind: "action" | "midi"; text: string; sub?: string };
  let log = $state<LogRow[]>([]);
  let seq = 0;
  const MAX_LOG = 400;

  // --- device switch-timing (mirrors app.py SwitchArray construction) ---
  function num(key: string, def: number): number {
    const v = device?.[key];
    return typeof v === "number" ? v : def;
  }
  function bool(key: string, def: boolean): boolean {
    const v = device?.[key];
    return typeof v === "boolean" ? v : def;
  }

  // One FSM per bound switch, rebuilt when bindings or timing change. Rebuilding
  // resets latched/pressed (editing the patch restarts the simulation).
  let fsms: Map<string, SwitchFsm> = new Map();
  function rebuild(): void {
    const longPressMs = num("long_press_ms", 600);
    const doubleTapWindowMs = num("double_tap_window_ms", 250);
    const autoMomentaryMs = num("auto_momentary_ms", 500);
    const deviceAm = bool("auto_momentary_on_hold", true);
    const next = new Map<string, SwitchFsm>();
    for (const b of bindings) {
      next.set(b.switch, new SwitchFsm({
        longPressMs, doubleTapWindowMs, autoMomentaryMs,
        autoMomentaryOnHold: b.auto_momentary ?? deviceAm,
      }));
    }
    fsms = next;
    latched = {};
    pressed = null;
  }

  // Rebuild whenever the patch bindings or the device timing change.
  $effect(() => {
    // touch the reactive inputs so the effect re-runs on change
    void bindings; void device;
    rebuild();
  });

  function bindingFor(sw: string): Binding | undefined {
    return bindings.find((b) => b.switch === sw);
  }

  function colorFor(sw: string): string {
    const b = bindingFor(sw);
    if (!b) return "#2a2a2a";
    return ledColorFor(b, latched[sw] ?? false);
  }

  function pushLog(row: Omit<LogRow, "id" | "ts">): void {
    const full: LogRow = { id: seq++, ts: Date.now(), ...row };
    log = log.length >= MAX_LOG ? [full, ...log.slice(0, MAX_LOG - 1)] : [full, ...log];
  }

  function now(): number {
    return typeof performance !== "undefined" ? performance.now() : Date.now();
  }

  // --- offline ("model") ---
  function runKeys(sw: string, b: Binding, keys: string[]): void {
    const fsm = fsms.get(sw);
    if (fsm) latched = { ...latched, [sw]: fsm.latchedOn };
    for (const key of keys) {
      const msgs = b.actions?.[key]?.messages ?? [];
      const sub = msgs.length
        ? msgs.map((m) => summarizeMessage(m)).join("  |  ")
        : "(no messages)";
      pushLog({ kind: "action", text: `${sw} - ${key}`, sub });
    }
  }

  function simPress(sw: string): void {
    if (mode !== "model") return;
    const b = bindingFor(sw);
    if (!b) return;
    const fsm = fsms.get(sw);
    if (!fsm) return;
    pressed = sw;
    runKeys(sw, b, fsm.press(now(), b.mode));
  }

  function simRelease(sw: string): void {
    if (mode !== "model") return;
    // Ignore stray releases (e.g. pointerleave without a prior press).
    if (pressed !== sw) return;
    const b = bindingFor(sw);
    const fsm = fsms.get(sw);
    pressed = null;
    if (!b || !fsm) return;
    runKeys(sw, b, fsm.release(now(), b.mode));
  }

  // Tick loop resolves long-press and double-tap timeouts while in model mode.
  let ticker: ReturnType<typeof setInterval> | null = null;
  function startTicker(): void {
    if (ticker) return;
    ticker = setInterval(() => {
      const t = now();
      for (const [sw, fsm] of fsms) {
        const b = bindingFor(sw);
        if (!b) continue;
        const keys = fsm.tick(t, b.mode);
        if (keys.length) runKeys(sw, b, keys);
      }
    }, 20);
  }
  function stopTicker(): void {
    if (ticker) { clearInterval(ticker); ticker = null; }
  }

  // --- live ("mirror") ---
  let unlisten: (() => void) | null = null;
  let monitorOn = false;

  function onFirmware(msg: FirmwareMessage): void {
    if (mode !== "live") return;
    if ((msg as { type?: string }).type !== "EVENT") return;
    const ev = msg as { event?: string; switch?: string; action?: string };
    switch (ev.event) {
      case "switch_pressed":
        if (ev.switch) pressed = ev.switch;
        break;
      case "switch_released":
        if (ev.switch && pressed === ev.switch) pressed = null;
        break;
      case "binding_fired": {
        const sw = ev.switch, action = ev.action;
        if (!sw || !action) break;
        if (action === "toggle_on") latched = { ...latched, [sw]: true };
        else if (action === "toggle_off") latched = { ...latched, [sw]: false };
        const b = bindingFor(sw);
        const msgs = b?.actions?.[action]?.messages ?? [];
        pushLog({
          kind: "action",
          text: `${sw} - ${action}`,
          sub: msgs.length ? msgs.map((m) => summarizeMessage(m)).join("  |  ") : undefined,
        });
        break;
      }
      case "midi": {
        const m = msg as unknown as MidiMonitorEvent;
        if (!Array.isArray(m.raw)) break;
        pushLog({ kind: "midi", text: `${m.dir === "in" ? "in" : "out"}  ${decodeMidi(m.raw).label}` });
        break;
      }
    }
  }

  // Enable the firmware MIDI stream only in live mode while connected, so the
  // log can show real traffic. Reuses the MIDI-monitor gate.
  async function setMonitor(on: boolean): Promise<void> {
    if (on === monitorOn) return;
    try { await cmd.setMidiMonitor(on); monitorOn = on; }
    catch { monitorOn = false; }
  }

  $effect(() => {
    if (mode === "model") { startTicker(); void setMonitor(false); }
    else { stopTicker(); void setMonitor(connected); }
  });

  function switchMode(m: Mode): void {
    mode = m;
    pressed = null;
    latched = {};
  }

  function clear(): void { log = []; }

  function fmtTs(ts: number): string {
    const d = new Date(ts);
    const p = (n: number, w = 2) => String(n).padStart(w, "0");
    return `${p(d.getHours())}:${p(d.getMinutes())}:${p(d.getSeconds())}.${p(d.getMilliseconds(), 3)}`;
  }

  onMount(async () => { unlisten = await onFirmwareMessage(onFirmware); });
  onDestroy(() => { stopTicker(); if (unlisten) unlisten(); void setMonitor(false); });
</script>

<div class="sim">
  <div class="toolbar">
    <div class="modes" role="tablist" aria-label="Simulator mode">
      <button role="tab" class="chip" class:active={mode === "model"}
              aria-selected={mode === "model"} onclick={() => switchMode("model")}>Model (offline)</button>
      <button role="tab" class="chip" class:active={mode === "live"}
              aria-selected={mode === "live"} onclick={() => switchMode("live")}>Live (device)</button>
    </div>
    <div class="actions">
      <button onclick={clear}>Clear log</button>
    </div>
  </div>

  {#if mode === "model"}
    <p class="muted hint">
      Click and hold a switch to simulate a press, just like on the pedal. LEDs
      update from the patch; latched switches toggle; hold for long-press, click
      twice for double-tap. Nothing is sent over MIDI - this is a preview of what
      each switch would do.
    </p>
  {:else if !connected}
    <p class="muted hint">Connect the pedal to mirror its switches, LEDs and MIDI live.</p>
  {:else}
    <p class="muted hint">
      Mirroring the connected pedal. Step a switch on the hardware (or change a
      preset on your device) and it lights up here, with the MIDI it sends.
    </p>
  {/if}

  <PedalMap
    {bindings}
    selected={pressed}
    {colorFor}
    onPress={mode === "model" ? simPress : undefined}
    onRelease={mode === "model" ? simRelease : undefined}
  />

  <div class="logwrap">
    {#if log.length === 0}
      <div class="empty-state empty-state--inline">
        <div class="empty-state__title">{mode === "model" ? "Press a switch" : "Waiting for the pedal"}</div>
        <p class="empty-state__hint">
          {mode === "model"
            ? "The messages each switch would fire appear here."
            : "Switch presses, LED changes and MIDI from the connected pedal appear here."}
        </p>
      </div>
    {:else}
      <div class="rows">
        {#each log as r (r.id)}
          <div class="row" class:midi={r.kind === "midi"}>
            <span class="ts">{fmtTs(r.ts)}</span>
            <span class="txt">{r.text}</span>
            {#if r.sub}<span class="sub">{r.sub}</span>{/if}
          </div>
        {/each}
      </div>
    {/if}
  </div>
</div>

<style>
  .sim { display: flex; flex-direction: column; gap: 0.7rem; }
  .toolbar { display: flex; align-items: center; gap: 1rem; flex-wrap: wrap; }
  .modes { display: flex; gap: 0.3rem; }
  .chip {
    padding: 0.25rem 0.8rem; border-radius: 999px; font-size: 0.85rem;
    border: 1px solid var(--border); background: transparent; cursor: pointer;
  }
  .chip.active { background: var(--accent); color: #fff; border-color: transparent; }
  .actions { margin-left: auto; }
  .hint { margin: 0; }

  .logwrap { margin-top: 0.2rem; }
  .rows {
    display: flex; flex-direction: column;
    border: 1px solid var(--border); border-radius: 8px;
    overflow: auto; max-height: 40vh;
  }
  .row {
    display: grid; grid-template-columns: 7rem 9rem 1fr; gap: 0.7rem;
    align-items: baseline; padding: 0.28rem 0.7rem;
    font-family: ui-monospace, "Cascadia Code", Consolas, monospace; font-size: 0.8rem;
    border-left: 3px solid var(--accent);
  }
  .row.midi { border-left-color: #6fb2ff; }
  .row:nth-child(odd) { background: color-mix(in srgb, var(--text, #fff) 3%, transparent); }
  .ts { color: var(--text-dim, #888); }
  .txt { color: var(--text, #eee); white-space: nowrap; }
  .sub { color: var(--text-muted, #aaa); overflow: hidden; text-overflow: ellipsis; }
</style>
