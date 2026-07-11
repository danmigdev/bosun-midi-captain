<script lang="ts">
  let { text, label = "Help" }: { text: string; label?: string } = $props();

  let open = $state(false);
  let root: HTMLSpanElement | undefined = $state();

  function show() { open = true; }
  function hide() { open = false; }
  function toggle() { open = !open; }

  function onKeydown(e: KeyboardEvent) {
    if (e.key === "Escape" && open) {
      open = false;
      e.stopPropagation();
    }
  }

  // Collapse when focus leaves the whole affordance (button + popover).
  function onFocusOut(e: FocusEvent) {
    const next = e.relatedTarget as Node | null;
    if (root && next && root.contains(next)) return;
    open = false;
  }
</script>

<span
  class="helptip"
  bind:this={root}
  onfocusout={onFocusOut}
>
  <button
    type="button"
    class="badge"
    aria-label={label}
    aria-expanded={open}
    onclick={toggle}
    onfocus={show}
    onmouseenter={show}
    onmouseleave={hide}
    onkeydown={onKeydown}
  >?</button>
  {#if open}
    <span class="popover" role="tooltip">{text}</span>
  {/if}
</span>

<style>
  .helptip {
    position: relative;
    display: inline-flex;
    vertical-align: middle;
    line-height: 0;
  }
  .badge {
    display: inline-flex;
    align-items: center;
    justify-content: center;
    width: 15px;
    height: 15px;
    padding: 0;
    border-radius: 50%;
    border: 1px solid var(--border-strong);
    background: var(--bg-hover);
    color: var(--text-muted);
    font-size: 0.66rem;
    font-weight: 600;
    font-family: inherit;
    line-height: 1;
    cursor: help;
    transition: all 0.12s ease;
  }
  .badge:hover,
  .badge:focus-visible {
    background: var(--accent-bg);
    border-color: var(--accent-border);
    color: var(--accent);
    outline: none;
  }
  .popover {
    position: absolute;
    left: 50%;
    bottom: calc(100% + 6px);
    transform: translateX(-50%);
    z-index: 50;
    width: max-content;
    max-width: 220px;
    padding: 0.4rem 0.55rem;
    border-radius: 5px;
    border: 1px solid var(--border-strong);
    background: var(--bg-elevated);
    color: var(--text);
    font-size: 0.72rem;
    font-weight: 400;
    line-height: 1.4;
    text-align: left;
    white-space: normal;
    box-shadow: var(--shadow-card);
    pointer-events: none;
  }
</style>
