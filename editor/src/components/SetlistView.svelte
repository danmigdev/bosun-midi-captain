<script lang="ts">
  import type { PatchSummary } from "../lib/protocol";
  import type { Setlist, SetlistItem } from "../lib/setlists";
  import {
    listSetlists,
    createSetlist,
    updateSetlistItems,
    renameSetlist,
    deleteSetlist,
  } from "../lib/setlists";

  let {
    patches,
    deviceSetlist = null,
    onSend,
  }: {
    patches: PatchSummary[];
    deviceSetlist?: { name?: string; items: SetlistItem[] } | null;
    onSend: (payload: { name: string; items: SetlistItem[] }) => void;
  } = $props();

  // Local mirror of the persisted setlists. Every mutating call refreshes this
  // from listSetlists() so the UI and storage never drift.
  const _initial = listSetlists();
  let setlists = $state<Setlist[]>(_initial);
  let selectedId = $state<string | null>(_initial[0]?.id ?? null);
  let query = $state("");

  // The currently selected setlist object (or undefined if none / stale id).
  let selected = $derived(setlists.find((s) => s.id === selectedId));

  // Fast lookup from "bank/slot" to patch name for rendering item rows.
  let patchByKey = $derived.by(() => {
    const m = new Map<string, PatchSummary>();
    for (const p of patches) m.set(`${p.bank}/${p.slot}`, p);
    return m;
  });

  let normQuery = $derived(query.trim().toLowerCase());

  // Patches matching the search box, by name (case-insensitive) or "BB/SS" id.
  let matches = $derived.by(() => {
    if (!normQuery) return patches;
    return patches.filter((p) => {
      const id = `${p.bank}/${p.slot}`;
      const idPadded = `${String(p.bank).padStart(2, "0")}/${String(p.slot).padStart(2, "0")}`;
      return (
        p.name.toLowerCase().includes(normQuery) ||
        id.includes(normQuery) ||
        idPadded.includes(normQuery)
      );
    });
  });

  // "on pedal" badge: the selected setlist's items deep-equal the device's,
  // order-sensitive.
  let onPedal = $derived.by(() => {
    if (!selected || !deviceSetlist) return false;
    const a = selected.items;
    const b = deviceSetlist.items;
    if (a.length !== b.length) return false;
    return a.every((it, i) => it.bank === b[i].bank && it.slot === b[i].slot);
  });

  function refresh() {
    setlists = listSetlists();
    // Keep the selection valid; fall back to the first setlist if it vanished.
    if (!setlists.some((s) => s.id === selectedId)) {
      selectedId = setlists[0]?.id ?? null;
    }
  }

  function patchName(item: SetlistItem): string {
    return patchByKey.get(`${item.bank}/${item.slot}`)?.name ?? "(missing)";
  }

  function fmtId(item: SetlistItem): string {
    return `${String(item.bank).padStart(2, "0")}/${String(item.slot).padStart(2, "0")}`;
  }

  function onCreate() {
    const s = createSetlist("New setlist");
    refresh();
    selectedId = s.id;
    // Focus the inline name field so the user can rename immediately.
    queueMicrotask(() => nameInput?.focus());
    queueMicrotask(() => nameInput?.select());
  }

  function onRename(value: string) {
    if (!selected) return;
    renameSetlist(selected.id, value);
    refresh();
  }

  function onDelete(id: string) {
    deleteSetlist(id);
    refresh();
  }

  function setItems(next: SetlistItem[]) {
    if (!selected) return;
    updateSetlistItems(selected.id, next);
    refresh();
  }

  function addItem(p: PatchSummary) {
    if (!selected) return;
    setItems([...selected.items, { bank: p.bank, slot: p.slot }]);
  }

  function removeAt(i: number) {
    if (!selected) return;
    setItems(selected.items.filter((_, idx) => idx !== i));
  }

  function move(i: number, dir: -1 | 1) {
    if (!selected) return;
    const j = i + dir;
    if (j < 0 || j >= selected.items.length) return;
    const next = selected.items.slice();
    [next[i], next[j]] = [next[j], next[i]];
    setItems(next);
  }

  function reorder(from: number, to: number) {
    if (!selected) return;
    if (from === to || from < 0 || to < 0) return;
    const next = selected.items.slice();
    const [moved] = next.splice(from, 1);
    next.splice(to, 0, moved);
    setItems(next);
  }

  function send() {
    if (!selected) return;
    onSend({ name: selected.name, items: selected.items });
  }

  // --- drag-and-drop reordering state ---
  let dragIndex = $state<number | null>(null);
  let overIndex = $state<number | null>(null);

  function onDragStart(i: number) {
    dragIndex = i;
  }
  function onDragOver(e: DragEvent, i: number) {
    e.preventDefault();
    overIndex = i;
  }
  function onDrop(i: number) {
    if (dragIndex !== null) reorder(dragIndex, i);
    dragIndex = null;
    overIndex = null;
  }
  function onDragEnd() {
    dragIndex = null;
    overIndex = null;
  }

  let nameInput: HTMLInputElement | null = $state(null);
</script>

<div class="setlist-view">
  <aside class="side">
    <div class="side-head">
      <h3>Setlists</h3>
      <button class="btn small" onclick={onCreate}>+ New</button>
    </div>

    {#if setlists.length === 0}
      <p class="muted empty">No setlists yet. Create one to start building a gig.</p>
    {:else}
      <ul class="side-list">
        {#each setlists as sl (sl.id)}
          <li>
            <button
              class="side-item"
              class:active={sl.id === selectedId}
              onclick={() => (selectedId = sl.id)}
            >
              <span class="side-item-name">{sl.name || "(untitled)"}</span>
              <span class="side-item-count">{sl.items.length}</span>
            </button>
            <button
              class="icon-btn"
              title="Delete setlist"
              aria-label="Delete setlist {sl.name}"
              onclick={() => onDelete(sl.id)}>×</button
            >
          </li>
        {/each}
      </ul>
    {/if}
  </aside>

  <section class="main">
    {#if !selected}
      <p class="muted empty">Select or create a setlist to edit it.</p>
    {:else}
      <header class="main-head">
        <input
          class="name-input"
          bind:this={nameInput}
          value={selected.name}
          aria-label="Setlist name"
          oninput={(e) => onRename((e.currentTarget as HTMLInputElement).value)}
        />
        {#if onPedal}
          <span class="badge" title="This setlist matches the one on the pedal">on pedal</span>
        {/if}
        <span class="grow"></span>
        <button class="btn primary" onclick={send}>Send to pedal</button>
      </header>

      <div class="panes">
        <div class="pane search-pane">
          <label class="searchbar">
            <span class="searchicon" aria-hidden="true">⌕</span>
            <input
              class="searchbox"
              type="search"
              placeholder="Search patches by name or BB/SS"
              bind:value={query}
            />
          </label>

          {#if patches.length === 0}
            <p class="muted note">No patches loaded from the pedal.</p>
          {:else if matches.length === 0}
            <p class="muted note">No patches match "{query}".</p>
          {:else}
            <ul class="result-list">
              {#each matches as p (`${p.bank}/${p.slot}`)}
                <li class="result-row">
                  <span class="mono id">{fmtId(p)}</span>
                  <span class="result-name">{p.name}</span>
                  <button class="btn small" onclick={() => addItem(p)}>Add</button>
                </li>
              {/each}
            </ul>
          {/if}
        </div>

        <div class="pane items-pane">
          <h4>Order ({selected.items.length})</h4>
          {#if selected.items.length === 0}
            <p class="muted note">Empty setlist. Add patches from the left.</p>
          {:else}
            <ol class="item-list">
              {#each selected.items as item, i (i)}
                <li
                  class="item-row"
                  class:dragging={dragIndex === i}
                  class:over={overIndex === i && dragIndex !== null && dragIndex !== i}
                  draggable="true"
                  ondragstart={() => onDragStart(i)}
                  ondragover={(e) => onDragOver(e, i)}
                  ondrop={() => onDrop(i)}
                  ondragend={onDragEnd}
                >
                  <span class="grip" aria-hidden="true">⋮⋮</span>
                  <span class="pos">{i + 1}</span>
                  <span class="mono id">{fmtId(item)}</span>
                  <span class="item-name" class:missing={patchName(item) === "(missing)"}
                    >{patchName(item)}</span
                  >
                  <span class="item-ctrls">
                    <button
                      class="icon-btn"
                      title="Move up"
                      aria-label="Move up"
                      disabled={i === 0}
                      onclick={() => move(i, -1)}>▲</button
                    >
                    <button
                      class="icon-btn"
                      title="Move down"
                      aria-label="Move down"
                      disabled={i === selected.items.length - 1}
                      onclick={() => move(i, 1)}>▼</button
                    >
                    <button
                      class="icon-btn"
                      title="Remove"
                      aria-label="Remove from setlist"
                      onclick={() => removeAt(i)}>×</button
                    >
                  </span>
                </li>
              {/each}
            </ol>
          {/if}
        </div>
      </div>
    {/if}
  </section>
</div>

<style>
  .setlist-view {
    display: grid;
    grid-template-columns: minmax(180px, 240px) minmax(0, 1fr);
    gap: 1rem;
    align-items: start;
  }

  h3 {
    color: var(--accent);
    margin: 0;
    font-size: 0.8rem;
    text-transform: uppercase;
    letter-spacing: 0.05em;
    font-weight: 600;
  }
  h4 {
    color: var(--text-muted);
    margin: 0 0 0.5rem;
    font-size: 0.72rem;
    text-transform: uppercase;
    letter-spacing: 0.05em;
    font-weight: 600;
  }

  .muted {
    color: var(--text-muted);
  }
  .empty {
    font-size: 0.82rem;
    padding: 0.5rem 0;
  }
  .note {
    font-size: 0.78rem;
    margin: 0.4rem 0 0;
  }
  .grow {
    flex: 1;
  }
  .mono {
    font-family: ui-monospace, Consolas, monospace;
  }

  /* --- sidebar --- */
  .side {
    background: var(--bg-card);
    border: 1px solid var(--border);
    border-radius: 6px;
    padding: 0.75rem;
  }
  .side-head {
    display: flex;
    align-items: center;
    justify-content: space-between;
    margin-bottom: 0.6rem;
  }
  .side-list {
    list-style: none;
    margin: 0;
    padding: 0;
    display: flex;
    flex-direction: column;
    gap: 0.25rem;
  }
  .side-list li {
    display: flex;
    align-items: center;
    gap: 0.25rem;
  }
  .side-item {
    flex: 1;
    display: flex;
    align-items: center;
    justify-content: space-between;
    gap: 0.4rem;
    background: transparent;
    border: 1px solid transparent;
    border-radius: 4px;
    padding: 0.35rem 0.5rem;
    color: var(--text-soft);
    cursor: pointer;
    text-align: left;
    font: inherit;
    font-size: 0.82rem;
  }
  .side-item:hover {
    background: var(--bg-hover);
    border-color: var(--border);
  }
  .side-item.active {
    background: var(--accent-bg);
    border-color: var(--accent-border);
    color: var(--accent);
  }
  .side-item-name {
    overflow: hidden;
    text-overflow: ellipsis;
    white-space: nowrap;
  }
  .side-item-count {
    color: var(--text-dim);
    font-size: 0.72rem;
    font-family: ui-monospace, Consolas, monospace;
  }

  /* --- main --- */
  .main {
    display: flex;
    flex-direction: column;
    gap: 0.75rem;
    min-width: 0;
  }
  .main-head {
    display: flex;
    align-items: center;
    gap: 0.6rem;
  }
  .name-input {
    background: var(--bg);
    color: var(--text);
    border: 1px solid var(--border-strong);
    padding: 0.4rem 0.6rem;
    border-radius: 4px;
    font-size: 0.95rem;
    font-weight: 500;
    flex: 1 1 8rem;
    min-width: 0;
  }

  .badge {
    background: var(--accent-bg);
    color: var(--accent);
    border: 1px solid var(--accent-border);
    border-radius: 999px;
    padding: 0.15rem 0.55rem;
    font-size: 0.68rem;
    text-transform: uppercase;
    letter-spacing: 0.05em;
    font-weight: 600;
  }

  .panes {
    display: grid;
    grid-template-columns: minmax(0, 1fr) minmax(0, 1fr);
    gap: 1rem;
    align-items: start;
  }
  .pane {
    background: var(--bg-card);
    border: 1px solid var(--border);
    border-radius: 6px;
    padding: 0.75rem;
    min-width: 0;
  }

  .searchbar {
    display: flex;
    align-items: center;
    gap: 0.5rem;
    background: var(--bg);
    border: 1px solid var(--border-strong);
    border-radius: 5px;
    padding: 0.3rem 0.55rem;
    margin-bottom: 0.5rem;
  }
  .searchicon {
    color: var(--text-dim);
    font-size: 0.95rem;
  }
  .searchbox {
    flex: 1;
    min-width: 0;
    background: transparent;
    border: none;
    color: var(--text);
    font: inherit;
    font-size: 0.85rem;
    outline: none;
  }
  .searchbox::placeholder {
    color: var(--text-dim);
  }

  .result-list,
  .item-list {
    list-style: none;
    margin: 0;
    padding: 0;
    display: flex;
    flex-direction: column;
    gap: 0.25rem;
    max-height: 60vh;
    overflow-y: auto;
  }
  .item-list {
    counter-reset: none;
  }

  .result-row,
  .item-row {
    display: flex;
    align-items: center;
    gap: 0.5rem;
    background: var(--bg);
    border: 1px solid var(--border);
    border-radius: 4px;
    padding: 0.35rem 0.5rem;
    font-size: 0.82rem;
  }
  .id {
    color: var(--text-dim);
    font-size: 0.72rem;
    white-space: nowrap;
  }
  .result-name,
  .item-name {
    flex: 1;
    min-width: 0;
    color: var(--text);
    overflow: hidden;
    text-overflow: ellipsis;
    white-space: nowrap;
  }
  .item-name.missing {
    color: var(--text-dim);
    font-style: italic;
  }

  .item-row {
    cursor: grab;
  }
  .item-row.dragging {
    opacity: 0.5;
  }
  .item-row.over {
    border-color: var(--accent);
    box-shadow: 0 -2px 0 var(--accent) inset;
  }
  .grip {
    color: var(--text-dim);
    font-size: 0.7rem;
    letter-spacing: -2px;
    cursor: grab;
  }
  .pos {
    color: var(--text-muted);
    font-size: 0.72rem;
    font-family: ui-monospace, Consolas, monospace;
    min-width: 1.2rem;
    text-align: right;
  }
  .item-ctrls {
    display: flex;
    gap: 0.15rem;
  }

  /* --- buttons --- */
  .btn {
    background: var(--bg-elevated);
    color: var(--text);
    border: 1px solid var(--border-strong);
    border-radius: 4px;
    padding: 0.35rem 0.7rem;
    font: inherit;
    font-size: 0.82rem;
    cursor: pointer;
  }
  .btn:hover {
    background: var(--bg-hover);
  }
  .btn.small {
    padding: 0.2rem 0.5rem;
    font-size: 0.75rem;
  }
  .btn.primary {
    background: var(--accent-bg);
    color: var(--accent);
    border-color: var(--accent-border);
    font-weight: 600;
  }
  .btn.primary:hover {
    background: var(--accent-hover-bg);
  }

  .icon-btn {
    background: transparent;
    border: 1px solid transparent;
    color: var(--text-dim);
    border-radius: 3px;
    cursor: pointer;
    font-size: 0.85rem;
    line-height: 1;
    padding: 0.15rem 0.35rem;
  }
  .icon-btn:hover:not(:disabled) {
    color: var(--text);
    background: var(--bg-hover);
  }
  .icon-btn:disabled {
    opacity: 0.3;
    cursor: not-allowed;
  }

  /* Stack the setlists sidebar and the two panes once the pedal's window gets
     narrow (small window at high UI zoom). The breakpoint accounts for the
     ~190px app sidebar that eats into this component's available width. */
  @media (max-width: 940px) {
    .setlist-view {
      grid-template-columns: 1fr;
    }
    .panes {
      grid-template-columns: 1fr;
    }
  }
  @media (max-width: 767px) {
    .icon-btn { min-width: 36px; min-height: 36px; font-size: 1rem; }
  }
</style>
