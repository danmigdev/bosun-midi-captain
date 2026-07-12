<script lang="ts">
  import { onMount, onDestroy } from "svelte";
  import { cmd, onFirmwareMessage, type FirmwareMessage } from "../lib/protocol";
  import { decodeMidi, toHex, type MidiMonitorEvent, type DecodedMidi } from "../lib/midi-decode";

  let { connected }: { connected: boolean } = $props();

  type Row = {
    id: number;
    ts: number;
    dir: "in" | "out";
    port: string;
    decoded: DecodedMidi;
  };

  // Cap the buffer so a long session (or an expression sweep firing a CC per
  // move) can't grow the DOM unbounded. Newest kept, oldest dropped.
  const MAX_ROWS = 1000;

  let rows = $state<Row[]>([]);
  let paused = $state(false);
  let filter = $state<"all" | "in" | "out">("all");
  let inCount = $state(0);
  let outCount = $state(0);
  let dropped = $state(0);

  let seq = 0;
  let unlisten: (() => void) | null = null;
  // Tracks the state we last pushed to the firmware, so we only send a toggle
  // when it actually changes (mount/unmount, connect/disconnect).
  let firmwareOn = false;

  function isMidiEvent(msg: FirmwareMessage): msg is MidiMonitorEvent {
    return (msg as { type?: string }).type === "EVENT"
      && (msg as { event?: string }).event === "midi"
      && Array.isArray((msg as { raw?: unknown }).raw);
  }

  function onMsg(msg: FirmwareMessage): void {
    if (!isMidiEvent(msg)) return;
    if (msg.dir === "in") inCount++; else outCount++;
    if (paused) { dropped++; return; }
    const row: Row = {
      id: seq++,
      ts: Date.now(),
      dir: msg.dir,
      port: msg.port ?? "",
      decoded: decodeMidi(msg.raw),
    };
    // Prepend newest; trim the tail past the cap.
    rows = rows.length >= MAX_ROWS ? [row, ...rows.slice(0, MAX_ROWS - 1)] : [row, ...rows];
  }

  async function setFirmware(on: boolean): Promise<void> {
    if (on === firmwareOn) return;
    try {
      await cmd.setMidiMonitor(on);
      firmwareOn = on;
    } catch {
      // Disconnected or firmware too old to know the command: leave the panel
      // usable (it just won't receive events) rather than surfacing an error.
      firmwareOn = false;
    }
  }

  function clear(): void {
    rows = [];
    inCount = 0;
    outCount = 0;
    dropped = 0;
  }

  function fmtTs(ts: number): string {
    const d = new Date(ts);
    const p = (n: number, w = 2) => String(n).padStart(w, "0");
    return `${p(d.getHours())}:${p(d.getMinutes())}:${p(d.getSeconds())}.${p(d.getMilliseconds(), 3)}`;
  }

  let visible = $derived(filter === "all" ? rows : rows.filter((r) => r.dir === filter));

  onMount(async () => {
    unlisten = await onFirmwareMessage(onMsg);
  });

  // Enable the firmware stream only while mounted AND connected; disable it the
  // moment either goes away, so it never runs during normal play.
  $effect(() => {
    void setFirmware(connected);
  });

  onDestroy(() => {
    if (unlisten) unlisten();
    // Best-effort: turn the stream off when leaving the panel.
    void setFirmware(false);
  });
</script>

<div class="monitor">
  <div class="toolbar">
    <div class="filters">
      <button class="chip" class:active={filter === "all"} onclick={() => (filter = "all")}>All</button>
      <button class="chip" class:active={filter === "in"} onclick={() => (filter = "in")}>In</button>
      <button class="chip" class:active={filter === "out"} onclick={() => (filter = "out")}>Out</button>
    </div>
    <div class="counts">
      <span class="count in">in {inCount}</span>
      <span class="count out">out {outCount}</span>
      {#if dropped > 0}<span class="count muted">paused {dropped}</span>{/if}
    </div>
    <div class="actions">
      <button onclick={() => (paused = !paused)}>{paused ? "Resume" : "Pause"}</button>
      <button onclick={clear}>Clear</button>
    </div>
  </div>

  {#if !connected}
    <div class="empty-state empty-state--inline">
      <div class="empty-state__title">Not connected</div>
      <p class="empty-state__hint">Connect the pedal to watch its MIDI traffic here.</p>
    </div>
  {:else if visible.length === 0}
    <div class="empty-state empty-state--inline">
      <div class="empty-state__title">{paused ? "Paused" : "Listening for MIDI"}</div>
      <p class="empty-state__hint">
        Every message the pedal sends or receives appears here, newest first.
        Step a switch, move an expression pedal, or change a preset on your
        device to see traffic. The stream runs only while this panel is open.
      </p>
    </div>
  {:else}
    <div class="rows">
      {#each visible as r (r.id)}
        <div class="row" class:in={r.dir === "in"} class:out={r.dir === "out"}>
          <span class="ts">{fmtTs(r.ts)}</span>
          <span class="dir">{r.dir === "in" ? "◀ in" : "out ▶"}{r.port ? ` ${r.port}` : ""}</span>
          <span class="label">{r.decoded.label}</span>
          <span class="hex">{toHex(r.decoded.raw)}</span>
        </div>
      {/each}
    </div>
  {/if}
</div>

<style>
  .monitor { display: flex; flex-direction: column; gap: 0.6rem; min-height: 0; }
  .toolbar {
    display: flex; align-items: center; gap: 1rem; flex-wrap: wrap;
  }
  .filters { display: flex; gap: 0.3rem; }
  .chip {
    padding: 0.2rem 0.7rem; border-radius: 999px; font-size: 0.85rem;
    border: 1px solid var(--border, #3a3a3a); background: transparent; cursor: pointer;
  }
  .chip.active { background: var(--accent, #4a7); color: #fff; border-color: transparent; }
  .counts { display: flex; gap: 0.7rem; font-variant-numeric: tabular-nums; font-size: 0.85rem; }
  .count.in { color: #6fb2ff; }
  .count.out { color: #6fd99b; }
  .count.muted { color: var(--muted, #888); }
  .actions { margin-left: auto; display: flex; gap: 0.4rem; }

  .rows {
    display: flex; flex-direction: column;
    border: 1px solid var(--border, #3a3a3a); border-radius: 8px;
    overflow: auto; max-height: 62vh; font-variant-numeric: tabular-nums;
  }
  .row {
    display: grid;
    grid-template-columns: 7.5rem 5.5rem 1fr minmax(0, auto);
    gap: 0.8rem; align-items: baseline;
    padding: 0.28rem 0.7rem;
    border-left: 3px solid transparent;
    font-family: ui-monospace, "Cascadia Code", Consolas, monospace;
    font-size: 0.82rem;
  }
  .row:nth-child(odd) { background: color-mix(in srgb, var(--fg, #fff) 3%, transparent); }
  .row.in { border-left-color: #6fb2ff; }
  .row.out { border-left-color: #6fd99b; }
  .ts { color: var(--muted, #888); }
  .dir { color: var(--muted, #aaa); white-space: nowrap; }
  .label { color: var(--fg, #eee); overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
  .hex { color: var(--muted, #888); white-space: nowrap; overflow: hidden; text-overflow: ellipsis; }
</style>
