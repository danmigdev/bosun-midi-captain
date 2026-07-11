<script lang="ts">
  import type { Binding } from "../lib/protocol";
  import { defaultLedFor } from "../lib/switch-colors";
  import {
    DEFAULT_LAYOUT,
    labelForSwitch,
    isBound,
    moveSwitch,
    type PedalLayout,
  } from "../lib/pedal-layout";

  let {
    bindings,
    selected = null,
    onSelect,
    colorFor,
    layout,
    editable = false,
    onLayoutChange,
  }: {
    bindings: Binding[];
    selected?: string | null;
    onSelect: (sw: string) => void;
    colorFor?: (sw: string) => string;
    layout?: PedalLayout;
    editable?: boolean;
    onLayoutChange?: (layout: PedalLayout) => void;
  } = $props();

  // The layout to render. Makes NO assumption about physical positions -
  // whatever the user (or the default) describes is exactly what we draw.
  let view = $derived<PedalLayout>(layout ?? DEFAULT_LAYOUT);

  /** LED color for a switch, in priority order:
   *  1. an explicit colorFor(sw) override,
   *  2. the binding's own led.on,
   *  3. the per-switch default. */
  function ledColor(sw: string): string {
    if (colorFor) return colorFor(sw);
    const b = bindings.find((x) => x.switch === sw);
    return b?.led?.on ?? defaultLedFor(sw);
  }

  // ---- drag-and-drop reordering (only when editable) ----
  // Track the dragged switch and whether a drag actually happened, so a plain
  // click (no drag) still fires onSelect rather than being swallowed.
  let dragging = $state<string | null>(null);
  let didDrag = false;

  function onDragStart(e: DragEvent, sw: string) {
    if (!editable) return;
    dragging = sw;
    didDrag = true;
    if (e.dataTransfer) {
      e.dataTransfer.effectAllowed = "move";
      // Some browsers require data to be set for a drag to start.
      try { e.dataTransfer.setData("text/plain", sw); } catch { /* ignore */ }
    }
  }

  function onDragEnd() {
    dragging = null;
    // Clear the flag on the next tick so the click that follows a real drag
    // is suppressed, but an ordinary click still registers.
    setTimeout(() => { didDrag = false; }, 0);
  }

  function onDragOver(e: DragEvent) {
    if (!editable || dragging === null) return;
    e.preventDefault(); // allow drop
    if (e.dataTransfer) e.dataTransfer.dropEffect = "move";
  }

  function onDrop(e: DragEvent, toRow: number, toCol: number) {
    if (!editable || dragging === null) return;
    e.preventDefault();
    const sw = dragging;
    dragging = null;
    const next = moveSwitch(view, sw, toRow, toCol);
    onLayoutChange?.(next);
  }

  function onCellClick(sw: string) {
    // Ignore the synthetic click that fires at the end of a real drag.
    if (didDrag) { didDrag = false; return; }
    onSelect(sw);
  }
</script>

<div class="pedalmap" role="group" aria-label="Pedal switch map">
  {#each view as row, r (r)}
    <div class="row">
      {#each row as sw, c (`${r}:${c}:${sw}`)}
        {#if sw === ""}
          <div
            class="cell spacer"
            role="presentation"
            ondragover={onDragOver}
            ondrop={(e) => onDrop(e, r, c)}
          ></div>
        {:else}
          {@const bound = isBound(bindings, sw)}
          {@const label = labelForSwitch(bindings, sw)}
          <button
            type="button"
            class="cell stomp"
            class:bound
            class:selected={selected === sw}
            class:dragging={dragging === sw}
            class:editable
            aria-pressed={selected === sw}
            aria-label={`Switch ${sw}${label ? ` - ${label}` : ""}${bound ? "" : " (empty)"}`}
            draggable={editable}
            ondragstart={(e) => onDragStart(e, sw)}
            ondragend={onDragEnd}
            ondragover={onDragOver}
            ondrop={(e) => onDrop(e, r, c)}
            onclick={() => onCellClick(sw)}
          >
            <span class="led" style={`--led:${ledColor(sw)}`} aria-hidden="true"></span>
            <span class="name">{sw}</span>
            {#if label}
              <span class="label" title={label}>{label}</span>
            {/if}
          </button>
        {/if}
      {/each}
    </div>
  {/each}
</div>

{#if editable}
  <p class="hint">Drag switches to rearrange the map. Your layout is saved automatically.</p>
{/if}

<style>
  .pedalmap {
    display: flex;
    flex-direction: column;
    gap: 0.6rem;
    padding: 0.4rem;
  }

  .row {
    display: flex;
    justify-content: center;
    gap: 0.55rem;
    flex-wrap: wrap;
  }

  .cell {
    flex: 1 1 0;
    min-width: 4.2rem;
    max-width: 7rem;
  }

  .spacer {
    min-height: 4.4rem;
    border-radius: 10px;
    border: 1px dashed transparent;
  }

  .stomp {
    position: relative;
    display: flex;
    flex-direction: column;
    align-items: center;
    justify-content: center;
    gap: 0.3rem;
    min-height: 4.4rem;
    padding: 0.6rem 0.4rem 0.55rem;
    border-radius: 10px;
    border: 1px solid var(--border);
    background: linear-gradient(180deg, var(--bg-card) 0%, var(--bg-elevated) 100%);
    color: var(--text-muted);
    font-family: inherit;
    cursor: pointer;
    transition: all 0.12s ease;
    box-shadow: var(--shadow-card);
  }
  .stomp:hover {
    border-color: var(--border-strong);
    color: var(--text);
  }
  .stomp.editable {
    cursor: grab;
  }
  .stomp.dragging {
    opacity: 0.4;
    cursor: grabbing;
  }

  /* Unbound switches read as empty/dimmed. */
  .stomp:not(.bound) {
    opacity: 0.55;
    border-style: dashed;
  }
  /* Bound switches are solid and fully lit. */
  .stomp.bound {
    color: var(--text);
    border-color: var(--border-strong);
    opacity: 1;
  }

  .stomp.selected {
    border-color: var(--accent);
    box-shadow: 0 0 0 2px var(--accent-border, rgba(111, 217, 155, 0.5));
  }
  .stomp:focus-visible {
    outline: none;
    border-color: var(--accent);
    box-shadow: 0 0 0 2px var(--accent-border, rgba(111, 217, 155, 0.5));
  }

  .led {
    width: 14px;
    height: 14px;
    border-radius: 50%;
    background: var(--led, #888);
    border: 1px solid rgba(0, 0, 0, 0.35);
  }
  /* Dimmed indicator when there's nothing bound to the switch. */
  .stomp:not(.bound) .led {
    background: transparent;
    border: 1px dashed var(--border-strong);
  }
  /* Glow only when the switch is active. */
  .stomp.bound .led {
    box-shadow: 0 0 6px 1px var(--led);
  }

  .name {
    font-size: 0.9rem;
    font-weight: 600;
    letter-spacing: 0.01em;
    line-height: 1;
  }

  .label {
    max-width: 100%;
    font-size: 0.68rem;
    color: var(--text-dim);
    line-height: 1.15;
    text-align: center;
    overflow: hidden;
    text-overflow: ellipsis;
    white-space: nowrap;
  }

  .hint {
    margin: 0.5rem 0 0;
    text-align: center;
    color: var(--text-dim);
    font-size: 0.72rem;
  }

  @media (max-width: 420px) {
    .stomp { min-height: 3.8rem; }
    .name { font-size: 0.82rem; }
  }
</style>
