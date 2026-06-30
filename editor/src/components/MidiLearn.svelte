<script lang="ts">
  import {
    patchIdOf,
    type BridgeStatus,
    type MidiLearnEntry,
    type MidiLearnTable,
    type PatchSummary,
  } from "../lib/protocol";

  /** A PC capture paired with the most-recent Bank MSB seen on the same port+channel. */
  export type PatchCapture = {
    port: "din" | "usb";
    channel: number;
    bank_msb: number;
    pc: number;
    ts: number;
  };

  type Props = {
    learning: boolean;
    table: MidiLearnTable;
    captures: PatchCapture[];
    patches: PatchSummary[];
    currentBank: number;
    currentSlot: number;
    bridge: BridgeStatus;
    onStartBridge: () => void;
    onStopBridge: () => void;
    onUpdate: (table: MidiLearnTable) => void;
    onClearCapture: (idx: number) => void;
    onClearAllCaptures: () => void;
  };

  let { learning, table, captures, patches, currentBank, currentSlot,
        bridge, onStartBridge, onStopBridge,
        onUpdate, onClearCapture, onClearAllCaptures }: Props = $props();

  let assigningIdx = $state<number | null>(null);
  let assignPatchId = $state<string>("");
  let manualMode = $state(false);
  let manual = $state<MidiLearnEntry>({ channel: 16, bank_msb: 0, pc: 0, captain_patch: "" });

  function upsert(entries: MidiLearnEntry[], entry: MidiLearnEntry): MidiLearnEntry[] {
    const i = entries.findIndex(
      e => e.channel === entry.channel && e.bank_msb === entry.bank_msb && e.pc === entry.pc,
    );
    const copy = [...entries];
    if (i >= 0) copy[i] = entry;
    else copy.push(entry);
    return copy;
  }

  function startAssign(idx: number) {
    assigningIdx = idx;
    assignPatchId = patchIdOf(currentBank, currentSlot);
  }

  function confirmAssign(cap: PatchCapture) {
    if (!assignPatchId) return;
    const entry: MidiLearnEntry = {
      channel:  cap.channel,
      bank_msb: cap.bank_msb,
      pc:       cap.pc,
      captain_patch: assignPatchId,
    };
    onUpdate({ pc_to_patch: upsert(table.pc_to_patch, entry) });
    if (assigningIdx !== null) onClearCapture(assigningIdx);
    assigningIdx = null;
  }

  function deleteEntry(i: number) {
    const copy = [...table.pc_to_patch];
    copy.splice(i, 1);
    onUpdate({ pc_to_patch: copy });
  }

  function addManual() {
    if (!manual.captain_patch.match(/^\d+\/\d+$/)) return;
    onUpdate({ pc_to_patch: upsert(table.pc_to_patch, { ...manual }) });
    manualMode = false;
    manual = { channel: 16, bank_msb: 0, pc: 0, captain_patch: "" };
  }

  function fmtPatchLabel(id: string): string {
    const p = patches.find(p => patchIdOf(p.bank, p.slot) === id);
    return p ? `${id} · ${p.name || "(unnamed)"}` : id;
  }
</script>

<section class="midilearn">
  <header>
    <h3>MIDI Learn - auto-follow patch select</h3>
    <span class="status" class:on={learning}>{learning ? "capturing" : "stopped"}</span>
  </header>

  <!-- USB-MIDI bridge. Only needed when the amp and the pedal are both plugged
       into THIS PC over USB and so can't see each other's MIDI directly (the
       Kemper Player case). If they're wired straight to each other - a direct
       USB-MIDI link, or DIN OUT -> pedal DIN IN - their MIDI already crosses and
       the bridge is unnecessary. -->
  <div class="bridge" class:on={bridge.active}>
    <span class="bdot"></span>
    <span class="btext">
      {#if bridge.active}
        MIDI bridge on: <code>{bridge.kemper_port}</code> &harr; <code>{bridge.pedal_port}</code>
      {:else}
        MIDI bridge off
      {/if}
    </span>
    {#if bridge.active}
      <button onclick={onStopBridge}>Stop bridge</button>
    {:else}
      <button class="ok" onclick={onStartBridge}>Start bridge</button>
    {/if}
  </div>
  <p class="bridge-hint">
    Relays MIDI between your amp and the pedal <strong>through this PC</strong>. You
    need it only when both are connected to the PC by USB and can't reach each
    other directly (e.g. a Kemper Player). If the amp and pedal are connected
    straight to each other - a direct USB-MIDI link or a DIN MIDI cable - their
    MIDI already crosses, so the bridge isn't needed.
  </p>

  {#if captures.length > 0}
    <div class="captures">
      <div class="capshead">
        <strong>Recent patch-select captures</strong>
        <button onclick={onClearAllCaptures}>Clear all</button>
      </div>
      {#each captures as cap, i}
        <div class="capture">
          <code>ch&nbsp;{cap.channel} · bank&nbsp;{cap.bank_msb} · PC&nbsp;{cap.pc}</code>
          <span class="src">{cap.port}</span>
          {#if assigningIdx === i}
            <select bind:value={assignPatchId}>
              {#each patches as p}
                <option value={patchIdOf(p.bank, p.slot)}>{patchIdOf(p.bank, p.slot)} · {p.name || "(unnamed)"}</option>
              {/each}
            </select>
            <button onclick={() => confirmAssign(cap)} class="ok">OK</button>
            <button onclick={() => assigningIdx = null}>cancel</button>
          {:else}
            <button onclick={() => startAssign(i)} class="assign">Assign to…</button>
            <button onclick={() => onClearCapture(i)} class="x">×</button>
          {/if}
        </div>
      {/each}
    </div>
  {:else if learning}
    <div class="hint">Listening… switch a preset on your controller or device (with MIDI broadcast enabled) and the incoming PC will appear here.</div>
  {/if}

  <div class="mappings">
    <div class="caphead">
      <strong>Mappings ({table.pc_to_patch.length})</strong>
      <button onclick={() => manualMode = !manualMode}>+ Add manual</button>
    </div>

    {#if manualMode}
      <div class="manual">
        <label>ch <input type="number" min="1" max="16" bind:value={manual.channel} /></label>
        <label>bank <input type="number" min="0" max="2" bind:value={manual.bank_msb} /></label>
        <label>PC <input type="number" min="0" max="127" bind:value={manual.pc} /></label>
        <label>→ patch
          <select bind:value={manual.captain_patch}>
            <option value="">…</option>
            {#each patches as p}
              <option value={patchIdOf(p.bank, p.slot)}>{patchIdOf(p.bank, p.slot)} · {p.name || "(unnamed)"}</option>
            {/each}
          </select>
        </label>
        <button onclick={addManual} class="ok" disabled={!manual.captain_patch}>Add</button>
        <button onclick={() => manualMode = false}>cancel</button>
      </div>
    {/if}

    {#if table.pc_to_patch.length === 0}
      <div class="empty-state">
        <div class="empty-state__title">No mappings yet</div>
        <p class="empty-state__hint">
          Map an incoming PC + bank MSB combination to a Captain patch so
          your controller (or DAW) can switch presets remotely. Hit
          <em>Start learn</em> and press the source button, or add a row
          by hand.
        </p>
      </div>
    {:else}
      <table>
        <thead><tr>
          <th>channel</th><th>bank MSB</th><th>PC</th><th>→ Captain patch</th><th></th>
        </tr></thead>
        <tbody>
          {#each table.pc_to_patch as entry, i}
            <tr>
              <td>{entry.channel}</td>
              <td>{entry.bank_msb}</td>
              <td>{entry.pc}</td>
              <td>{fmtPatchLabel(entry.captain_patch)}</td>
              <td><button onclick={() => deleteEntry(i)} class="x">×</button></td>
            </tr>
          {/each}
        </tbody>
      </table>
    {/if}
  </div>
</section>

<style>
  .midilearn { background: var(--bg-card); border: 1px solid var(--border); border-radius: 6px; padding: 0.75rem; }
  header { display: flex; align-items: center; gap: 0.75rem; margin-bottom: 0.5rem; }
  h3 { margin: 0; font-size: 0.95rem; color: var(--text); }
  .status { font-size: 0.7rem; padding: 0.15rem 0.5rem; border-radius: 999px; background: var(--border); color: var(--text-dim); }
  .status.on { background: var(--warn-bg); color: var(--warn-text); animation: pulse 1.5s ease-in-out infinite; }
  @keyframes pulse { 50% { opacity: 0.65; } }

  .bridge {
    display: flex; align-items: center; gap: 0.5rem;
    margin: 0.4rem 0 0.2rem; padding: 0.4rem 0.55rem;
    background: var(--bg); border: 1px solid var(--border); border-radius: 4px;
    font-size: 0.8rem; color: var(--text-muted);
  }
  .bridge .bdot { width: 8px; height: 8px; border-radius: 50%; background: var(--text-dim); flex-shrink: 0; }
  .bridge.on { color: var(--text); border-color: var(--accent-border); background: var(--accent-bg); }
  .bridge.on .bdot { background: var(--accent); }
  .bridge .btext { flex: 1; }
  .bridge code { background: var(--bg-hover); padding: 0.05rem 0.3rem; border-radius: 3px; font-family: ui-monospace, Consolas, monospace; font-size: 0.75rem; }
  .bridge-hint { margin: 0 0 0.4rem; color: var(--text-muted); font-size: 0.75rem; line-height: 1.5; }
  .bridge-hint strong { color: var(--text-soft); font-weight: 600; }

  .hint { color: var(--text-muted); font-size: 0.85rem; padding: 0.5rem 0; font-style: italic; }
  .captures, .mappings { margin-top: 0.5rem; }
  .capshead, .caphead { display: flex; justify-content: space-between; align-items: center; margin-bottom: 0.3rem; }
  strong { color: var(--text-muted); font-size: 0.8rem; font-weight: 600; }

  .capture { display: flex; gap: 0.5rem; align-items: center; padding: 0.4rem 0.5rem; background: var(--bg); border-radius: 3px; margin-bottom: 0.25rem; font-size: 0.85rem; }
  .capture code { background: var(--bg-hover); padding: 0.15rem 0.4rem; border-radius: 3px; color: var(--warn-text); font-family: ui-monospace, Consolas, monospace; }
  .capture .src { color: var(--text-dim); font-size: 0.75rem; }
  .capture .assign { background: var(--accent-bg); color: var(--accent); border: 1px solid var(--accent-border); margin-left: auto; }
  .capture .x, .x { background: var(--err-bg); border: 1px solid var(--err-border); color: var(--err); padding: 0.2rem 0.5rem; cursor: pointer; border-radius: 3px; }
  .capture select { flex: 1; }

  button { background: var(--bg-hover); border: 1px solid var(--border-strong); color: var(--text); padding: 0.25rem 0.6rem; border-radius: 3px; font-size: 0.8rem; cursor: pointer; }
  button.ok { background: var(--accent-bg); color: var(--accent); border-color: var(--accent-border); }
  button:disabled { opacity: 0.45; cursor: not-allowed; }

  select, input { background: var(--bg); color: var(--text); border: 1px solid var(--border-strong); padding: 0.25rem 0.4rem; border-radius: 3px; font-size: 0.8rem; }

  .manual { display: flex; gap: 0.4rem; align-items: end; padding: 0.5rem; background: var(--bg); border-radius: 3px; margin-bottom: 0.5rem; flex-wrap: wrap; }
  .manual label { display: flex; flex-direction: column; gap: 0.15rem; font-size: 0.7rem; color: var(--text-muted); }
  .manual input { width: 4rem; }

  table { width: 100%; border-collapse: collapse; font-size: 0.85rem; margin-top: 0.4rem; }
  th { text-align: left; padding: 0.3rem 0.5rem; color: var(--text-dim); font-weight: 500; font-size: 0.75rem; border-bottom: 1px solid var(--border); }
  td { padding: 0.35rem 0.5rem; color: var(--text); border-bottom: 1px solid var(--bg-card); }
  td:last-child { text-align: right; }
  tr:hover td { background: var(--bg-elevated); }

  .empty-state {
    display: flex; flex-direction: column; align-items: center;
    text-align: center; padding: 3rem 1rem;
    max-width: 460px; margin: 1rem auto 0;
  }
  .empty-state__title { font-size: 1.05rem; font-weight: 600; color: var(--text); margin-bottom: 0.45rem; }
  .empty-state__hint  { margin: 0; color: var(--text-muted); font-size: 0.85rem; line-height: 1.55; }
  .empty-state__hint em { font-style: normal; color: var(--text); font-weight: 500; }
</style>
