<script lang="ts">
  import { patchIdOf, type PatchSummary } from "../lib/protocol";
  import { isSlotLocked, type LinkConfig } from "../lib/patch-links";

  type Props = {
    patches: PatchSummary[];
    deviceInfo: { bank: number; slot: number } | null;
    dirtyIds: Array<{ bank: number; slot: number }>;
    /** device.patch_link - drives the per-column padlock state. */
    linkConfig?: LinkConfig;
    /** Toggle a slot column's lock (patches linked across banks). */
    onToggleLock?: (slot: number) => void;
    onOpen: (bank: number, slot: number) => void;
    onCreate: (bank: number, slot: number) => void;
  };

  let { patches, deviceInfo, dirtyIds, linkConfig, onToggleLock, onOpen, onCreate }: Props = $props();

  // Non-destructive name filter. When non-empty, only patches whose name
  // matches (case-insensitive) stay visible; non-matching filled tiles and
  // the create-placeholders are dimmed out of the way. Clearing the box
  // restores the full grid.
  let query = $state("");
  let normQuery = $derived(query.trim().toLowerCase());
  function matchesQuery(p: PatchSummary): boolean {
    if (!normQuery) return true;
    return (p.name ?? "").toLowerCase().includes(normQuery);
  }
  let matchCount = $derived(normQuery ? patches.filter(matchesQuery).length : patches.length);

  // Rows are exactly the banks that already contain at least one
  // patch - empty banks are not rendered. The slot axis pads to at
  // least MIN_SLOTS so each bank shows a few "empty next slot"
  // affordances.
  const MIN_SLOTS = 5;
  const MAX_DIM   = 9;

  let banks = $derived.by(() => {
    const set = new Set<number>();
    for (const p of patches) set.add(p.bank);
    return Array.from(set).sort((a, b) => a - b);
  });

  let slotCount = $derived.by(() => {
    let maxSlot = MIN_SLOTS;
    for (const p of patches) if (p.slot > maxSlot) maxSlot = p.slot;
    return Math.min(MAX_DIM, maxSlot);
  });

  let bySlot = $derived.by(() => {
    const m = new Map<string, PatchSummary>();
    for (const p of patches) m.set(`${p.bank}/${p.slot}`, p);
    return m;
  });

  let dirtySet = $derived(new Set(dirtyIds.map(d => `${d.bank}/${d.slot}`)));
</script>

<div class="searchbar">
  <span class="searchicon" aria-hidden="true">⌕</span>
  <input
    class="searchbox"
    type="search"
    placeholder="Filter patches by name…"
    bind:value={query}
    aria-label="Filter patches by name" />
  {#if normQuery}
    <span class="searchcount">{matchCount} match{matchCount === 1 ? "" : "es"}</span>
    <button class="searchclear" onclick={() => query = ""} title="Clear filter" aria-label="Clear filter">×</button>
  {/if}
</div>

<div class="grid" class:filtering={!!normQuery} style="--cols: {slotCount}">
  <!-- Column header row: slot number + per-column padlock. A closed lock
       means every patch at this slot is linked across banks - editing one
       propagates to all the others in the same column. Replaces the old
       link-line overlay. -->
  <div class="head corner" aria-hidden="true"></div>
  {#each Array(slotCount) as _, sx}
    {@const slot = sx + 1}
    {@const locked = isSlotLocked(slot, linkConfig, patches)}
    <div class="head colhead">
      <span class="slotno">{String(slot).padStart(2,"0")}</span>
      <button class="lock" class:locked
              onclick={() => onToggleLock?.(slot)}
              aria-pressed={locked}
              aria-label={locked ? `Unlock slot ${slot}` : `Lock slot ${slot} across banks`}
              title={locked
                ? `Slot ${slot} locked across banks - edits propagate to every bank. Click to unlock.`
                : `Lock slot ${slot} across banks so edits propagate to every bank.`}>
        <svg class="lockicon" viewBox="0 0 24 24" aria-hidden="true">
          <rect x="5" y="11" width="14" height="9" rx="2" />
          {#if locked}
            <path d="M8 11 V8 a4 4 0 0 1 8 0 V11" />
          {:else}
            <path d="M8 11 V8 a4 4 0 0 1 8 0" />
          {/if}
        </svg>
      </button>
    </div>
  {/each}

  {#each banks as bank (bank)}
    <div class="head rowhead">B{String(bank).padStart(2,"0")}</div>
    {#each Array(slotCount) as _, sx}
      {@const slot = sx + 1}
      {@const key = `${bank}/${slot}`}
      {@const p = bySlot.get(key)}
      {@const active = deviceInfo && deviceInfo.bank === bank && deviceInfo.slot === slot}
      {@const isDirty = dirtySet.has(key)}
      {@const locked = isSlotLocked(slot, linkConfig, patches)}
      {@const dimmed = !!normQuery && !!p && !matchesQuery(p)}
      <div class="cell" class:active class:dirty={isDirty} class:locked class:dimmed>
        {#if p}
          <button class="tile filled" onclick={() => onOpen(p.bank, p.slot)}>
            <span class="tile__id">{patchIdOf(p.bank, p.slot)}</span>
            <span class="tile__name">{p.name || "(unnamed)"}</span>
            <span class="tile__flags">
              {#if isDirty}<span class="dot dirty" title="unsaved">●</span>{/if}
              {#if active}<span class="dot live" title="live">●</span>{/if}
            </span>
          </button>
        {:else if !normQuery}
          <button class="tile placeholder"
                  onclick={() => onCreate(bank, slot)}
                  title="Create patch at {bank}/{slot}">
            <span class="tile__id">{patchIdOf(bank, slot)}</span>
            <span class="tile__plus">+</span>
          </button>
        {/if}
      </div>
    {/each}
  {/each}
</div>

<style>
  .searchbar {
    display: flex; align-items: center; gap: 0.5rem;
    margin-bottom: 0.75rem;
    background: var(--bg-card); border: 1px solid var(--border);
    border-radius: 6px; padding: 0.35rem 0.6rem;
    max-width: 420px;
  }
  .searchbar .searchicon { color: var(--text-dim); font-size: 0.95rem; }
  .searchbox {
    flex: 1; background: transparent; border: none; color: var(--text);
    font: inherit; font-size: 0.85rem; outline: none;
  }
  .searchbox::placeholder { color: var(--text-dim); }
  .searchcount { color: var(--text-muted); font-size: 0.72rem; white-space: nowrap; }
  .searchclear {
    background: transparent; border: none; color: var(--text-dim);
    cursor: pointer; font-size: 1.1rem; line-height: 1; padding: 0 0.2rem;
  }
  .searchclear:hover { color: var(--text); }

  /* Non-matching tiles during a filter: dimmed and non-interactive so the
     matches stand out, without removing them from the grid layout. */
  .grid.filtering .cell.dimmed { opacity: 0.18; pointer-events: none; filter: grayscale(0.6); }

  .grid {
    position: relative;
    display: grid;
    gap: 0.45rem;
    align-items: stretch;
    grid-template-columns: 2.4rem repeat(var(--cols, 5), minmax(110px, 1fr));
  }
  .head {
    font-size: 0.72rem;
    font-weight: 600;
    text-transform: uppercase;
    letter-spacing: 0.06em;
    color: var(--text-dim);
    padding: 0.3rem 0.2rem;
    text-align: center;
  }
  .head.colhead {
    display: flex; align-items: center; justify-content: center;
    gap: 0.3rem;
  }
  .head.rowhead { text-align: right; padding-right: 0.5rem; }
  .head.corner { background: transparent; }

  .lock {
    background: transparent; border: none; cursor: pointer;
    padding: 0.1rem; line-height: 0; opacity: 0.55;
    transition: opacity 0.12s ease;
  }
  .lock:hover { opacity: 1; }
  .lock.locked { opacity: 1; }
  .lockicon { width: 14px; height: 14px; display: block; }
  .lockicon rect, .lockicon path {
    fill: none; stroke: var(--text-dim); stroke-width: 1.7;
    stroke-linecap: round; stroke-linejoin: round;
  }
  .lock.locked .lockicon rect { fill: var(--accent); stroke: var(--accent); }
  .lock.locked .lockicon path { stroke: var(--accent); }

  .cell {
    position: relative;
    min-height: 56px;
  }
  .tile {
    width: 100%; height: 100%;
    display: flex; flex-direction: column;
    align-items: flex-start; justify-content: space-between;
    padding: 0.45rem 0.55rem;
    background: var(--bg-card);
    border: 1px solid var(--border);
    border-radius: 5px;
    color: var(--text-soft);
    cursor: pointer;
    text-align: left;
    transition: border-color 0.12s ease, background 0.12s ease;
    font: inherit;
  }
  .tile:hover { border-color: var(--border-strong); background: var(--bg-hover); }
  .tile__id { color: var(--text-dim); font-family: ui-monospace, Consolas, monospace; font-size: 0.7rem; }
  .tile__name { color: var(--text); font-weight: 500; font-size: 0.82rem; line-height: 1.15; }
  .tile__flags { display: flex; gap: 0.3rem; align-self: flex-end; }
  .dot { font-size: 0.6rem; line-height: 1; }
  .dot.dirty { color: var(--warn); }
  .dot.live  { color: var(--accent); }

  .tile.placeholder {
    background: transparent;
    border-style: dashed;
    border-color: var(--border);
    color: var(--text-dim);
    justify-content: center; align-items: center;
    opacity: 0.6;
  }
  .tile.placeholder:hover {
    opacity: 1;
    border-color: var(--accent-border);
    background: var(--accent-bg);
    color: var(--accent);
  }
  .tile.placeholder .tile__id { position: absolute; top: 0.3rem; left: 0.45rem; opacity: 0.65; }
  .tile.placeholder .tile__plus { font-size: 1.1rem; font-weight: 600; }

  .cell.active .tile.filled { border-color: var(--accent); background: var(--accent-bg); }
  .cell.dirty  .tile.filled { border-left: 3px solid var(--warn); }
  /* Locked column: a faint accent tint on the left edge so the linked
     group reads as a unit down the column. */
  .cell.locked .tile.filled { border-top: 2px solid var(--accent-border); }
</style>
