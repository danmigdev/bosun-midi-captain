<script lang="ts">
  import type { Binding } from "../lib/protocol";
  import { defaultLedFor } from "../lib/switch-colors";
  import {
    DEFAULT_LAYOUT,
    labelForSwitch,
    isBound,
  } from "../lib/pedal-layout";

  let {
    bindings,
    selected = null,
    onSelect,
    onPress,
    onRelease,
    colorFor,
  }: {
    bindings: Binding[];
    selected?: string | null;
    /** Click handler (used by the editor to jump to a switch row). Optional so
     *  the simulator can reuse this map purely for press/release. */
    onSelect?: (sw: string) => void;
    /** Press/release taps for the pedal simulator (pointer down / up). When
     *  omitted (the editor's normal use) the map is click-to-select only. */
    onPress?: (sw: string) => void;
    onRelease?: (sw: string) => void;
    colorFor?: (sw: string) => string;
  } = $props();

  // The switches are drawn in a fixed schematic layout.
  const view = DEFAULT_LAYOUT;

  /** LED color for a switch, in priority order:
   *  1. an explicit colorFor(sw) override,
   *  2. the binding's own led.on,
   *  3. the per-switch default. */
  function ledColor(sw: string): string {
    if (colorFor) return colorFor(sw);
    const b = bindings.find((x) => x.switch === sw);
    return b?.led?.on ?? defaultLedFor(sw);
  }

</script>

<div class="pedalmap" role="group" aria-label="Pedal switch map">
  {#each view as row, r (r)}
    <div class="row">
      {#each row as sw, c (`${r}:${c}:${sw}`)}
        {#if sw === ""}
          <div class="cell spacer" role="presentation"></div>
        {:else}
          {@const bound = isBound(bindings, sw)}
          {@const label = labelForSwitch(bindings, sw)}
          <button
            type="button"
            class="cell stomp"
            class:bound
            class:selected={selected === sw}
            aria-pressed={selected === sw}
            aria-label={`Switch ${sw}${label ? ` - ${label}` : ""}${bound ? "" : " (empty)"}`}
            onclick={() => onSelect?.(sw)}
            onpointerdown={onPress ? () => onPress(sw) : undefined}
            onpointerup={onRelease ? () => onRelease(sw) : undefined}
            onpointerleave={onRelease ? () => onRelease(sw) : undefined}
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

  @media (max-width: 420px) {
    .stomp { min-height: 3.8rem; }
    .name { font-size: 0.82rem; }
  }
</style>
