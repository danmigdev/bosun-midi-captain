<script lang="ts">
  import { onMount } from "svelte";

  type Props = {
    id: string;
    label: string;
    value: string;
    options: { value: string; label: string }[];
    disabled?: boolean;
    onchange?: (value: string) => void;
    listLabel?: string;
    /** Compatibility classes for existing Stage integrations. */
    prefix?: string;
  };
  let { id, label, value, options, disabled = false, onchange, listLabel, prefix }: Props = $props();
  let container: HTMLDivElement;
  let trigger: HTMLButtonElement;
  let list = $state<HTMLDivElement>();
  let open = $state(false);
  let activeValue = $state("");
  let optionHeight = $state<number | null>(null);
  let popupStyle = $state("");

  const optionId = (optionValue: string) => `${id}-${encodeURIComponent(optionValue) || "default"}`;
  const extraClass = (suffix: string) => prefix ? `${prefix}-${suffix}` : "";

  function close(restoreFocus = false) {
    open = false;
    if (restoreFocus) trigger?.focus({ preventScroll: true });
  }

  function measure() {
    if (!trigger) return;
    const rect = trigger.getBoundingClientRect();
    if (rect.height > 0) optionHeight = rect.height;
    const viewport = window.visualViewport;
    const viewportLeft = viewport?.offsetLeft ?? 0;
    const viewportTop = viewport?.offsetTop ?? 0;
    const viewportWidth = viewport?.width ?? window.innerWidth;
    const viewportHeight = viewport?.height ?? window.innerHeight;
    const margin = 8;
    const gap = 8;
    // A compact closed control can truncate a long label; give the open list
    // enough width for the full labels, including padding and its scrollbar.
    const triggerStyle = getComputedStyle(trigger);
    const labelWidth = Math.max(0, ...Array.from(container.querySelectorAll<HTMLElement>(".stage-select__sizer"), element => element.scrollWidth));
    const labelPadding = parseFloat(triggerStyle.paddingLeft) + parseFloat(triggerStyle.paddingRight);
    const width = Math.min(Math.max(rect.width, labelWidth + labelPadding + 24), Math.max(0, viewportWidth - margin * 2));
    const left = Math.min(Math.max(rect.left, viewportLeft + margin), viewportLeft + viewportWidth - margin - width);
    const below = Math.max(0, viewportTop + viewportHeight - margin - rect.bottom - gap);
    const above = Math.max(0, rect.top - gap - viewportTop - margin);
    const desiredHeight = options.length * ((optionHeight ?? 48) + 4) + 2;
    const upward = desiredHeight > below && above > below;
    const available = upward ? above : below;
    const top = upward ? rect.top - gap - Math.min(desiredHeight, available) : rect.bottom + gap;
    popupStyle = `left:${left}px;top:${top}px;width:${width}px;max-height:${available}px`;
  }

  function toggle() {
    if (disabled || !options.length) return;
    if (open) close();
    else {
      activeValue = options.some(option => option.value === value) ? value : options[0].value;
      measure();
      open = true;
    }
  }

  function choose(nextValue: string) {
    if (disabled || !options.some(option => option.value === nextValue)) return;
    if (nextValue !== value) onchange?.(nextValue);
    close(true);
  }

  function keydown(event: KeyboardEvent) {
    if (disabled) return;
    if (event.key === "Escape" && open) {
      event.preventDefault();
      event.stopPropagation();
      close(true);
    } else if (event.key === "Tab") {
      close();
    } else if (event.key === "Enter" || event.key === " ") {
      event.preventDefault();
      if (open) choose(activeValue);
      else toggle();
    } else if (options.length && ["ArrowDown", "ArrowUp", "Home", "End"].includes(event.key)) {
      event.preventDefault();
      let index = Math.max(0, options.findIndex(option => option.value === (open ? activeValue : value)));
      if (event.key === "Home") index = 0;
      else if (event.key === "End") index = options.length - 1;
      else if (open) index = Math.max(0, Math.min(options.length - 1, index + (event.key === "ArrowDown" ? 1 : -1)));
      activeValue = options[index].value;
      measure();
      open = true;
    }
  }

  function outsidePointer(event: PointerEvent) {
    if (open && event.target instanceof Node && !container?.contains(event.target)) close();
  }

  function outsideFocus(event: FocusEvent) {
    if (open && event.target instanceof Node && !container?.contains(event.target)) close();
  }

  function parentScroll(event: Event) {
    // Scrolling the options must not dismiss their own list. Scrolling the
    // appearance panel dismisses it instead of leaving it detached from its field.
    if (open && !(event.target instanceof Node && list?.contains(event.target))) close();
  }

  $effect(() => { if (disabled || !options.length) close(); });
  $effect(() => {
    if (!open || !list) return;
    const option = document.getElementById(optionId(activeValue));
    if (!option) return;
    // Only scroll this list, never the Stage or the containing settings panel.
    if (option.offsetTop < list.scrollTop) list.scrollTop = option.offsetTop;
    else if (option.offsetTop + option.offsetHeight > list.scrollTop + list.clientHeight) {
      list.scrollTop = option.offsetTop + option.offsetHeight - list.clientHeight;
    }
  });

  onMount(() => {
    let mounted = true;
    measure();
    const observer = typeof ResizeObserver !== "undefined" ? new ResizeObserver(measure) : null;
    observer?.observe(trigger);
    document.addEventListener("scroll", parentScroll, true);
    document.addEventListener("focusin", outsideFocus);
    document.fonts?.addEventListener?.("loadingdone", measure);
    void document.fonts?.ready.then(() => { if (mounted) measure(); });
    window.visualViewport?.addEventListener("resize", measure);
    window.visualViewport?.addEventListener("scroll", measure);
    return () => {
      mounted = false;
      observer?.disconnect();
      document.removeEventListener("scroll", parentScroll, true);
      document.removeEventListener("focusin", outsideFocus);
      document.fonts?.removeEventListener?.("loadingdone", measure);
      window.visualViewport?.removeEventListener("resize", measure);
      window.visualViewport?.removeEventListener("scroll", measure);
    };
  });
</script>

<svelte:window onresize={measure} onpointerdown={outsidePointer} />

<div bind:this={container} class={`stage-select ${prefix ?? ""}`}
  style:--stage-select-option-height={optionHeight ? `${optionHeight}px` : undefined}>
  <button
    bind:this={trigger}
    type="button"
    role="combobox"
    id={id}
    class={`stage-select__trigger ${extraClass("trigger")}`}
    aria-label={label}
    aria-haspopup="listbox"
    aria-expanded={open}
    aria-controls={`${id}-options`}
    aria-activedescendant={open ? optionId(activeValue) : undefined}
    data-value={value}
    data-mode={prefix ? value : undefined}
    disabled={disabled || !options.length}
    onclick={toggle}
    onkeydown={keydown}
  >
    <span class={`stage-select__label ${extraClass("label")}`}>{options.find(option => option.value === value)?.label ?? value}</span>
    {#each options as option (option.value)}
      <span class={`stage-select__sizer ${extraClass("sizer")}`} aria-hidden="true">{option.label}</span>
    {/each}
    <svg viewBox="0 0 24 24" aria-hidden="true"><path d="m6 9 6 6 6-6" /></svg>
  </button>
  {#if open}
    <div bind:this={list} id={`${id}-options`} class={`stage-select__list ${extraClass("list")}`}
      role="listbox" aria-label={listLabel ?? label} style={popupStyle}>
      {#each options as option (option.value)}
        <button
          type="button"
          role="option"
          id={optionId(option.value)}
          class={`stage-select__option ${extraClass("option")} ${activeValue === option.value ? extraClass("option--highlighted") : ""}`}
          class:stage-select__option--highlighted={activeValue === option.value}
          aria-selected={value === option.value}
          data-value={option.value}
          data-mode={prefix ? option.value : undefined}
          tabindex="-1"
          onclick={() => choose(option.value)}
          onkeydown={keydown}
        >{option.label}</button>
      {/each}
    </div>
  {/if}
</div>

<style>
  .stage-select {
    position: relative;
    min-width: 0;
    font-family: var(--stage-control-font, "Inter", -apple-system, sans-serif);
    font-size: var(--stage-control-font-size, 1rem);
    font-weight: 400;
    line-height: 1.3;
    color: var(--stage-control-color, #edf5f7);
  }
  .stage-select button {
    box-sizing: border-box;
    font: inherit;
    color: inherit;
    cursor: pointer;
    touch-action: manipulation;
    -webkit-tap-highlight-color: transparent;
  }
  .stage-select button:focus-visible { outline: 2px solid var(--stage-control-accent, #baff3f); outline-offset: -4px; }
  .stage-select button:disabled { cursor: default; opacity: 0.55; }
  .stage-select__trigger, .stage-select__option {
    display: flex;
    align-items: center;
    width: 100%;
    min-height: var(--stage-control-min-height, max(48px, 3em));
    padding: var(--stage-control-padding, 0.5em clamp(8px, 0.75em, 12px));
    border: 1px solid var(--stage-control-border, #344752);
    border-radius: var(--stage-control-radius, 3px);
    background: var(--stage-control-background, #111b23);
    line-height: 1.3;
    text-align: left;
    overflow-wrap: anywhere;
  }
  .stage-select__trigger { display: grid; grid-template-columns: minmax(0, 1fr) max(24px, 1.25em); gap: clamp(4px, 0.5em, 8px); height: var(--stage-control-row-height); }
  .stage-select__label, .stage-select__sizer { grid-area: 1 / 1; min-width: 0; white-space: nowrap; overflow: hidden; text-overflow: ellipsis; }
  /* Label copies provide the full text width when positioning the open menu. */
  .stage-select__sizer { visibility: hidden; }
  .stage-select__trigger svg { grid-area: 1 / 2; justify-self: end; width: 1.25em; height: 1.25em; fill: none; stroke: currentColor; stroke-width: 1.7; }
  .stage-select__list {
    position: fixed;
    z-index: 100;
    display: grid;
    grid-auto-rows: var(--stage-select-option-height, auto);
    align-content: start;
    gap: 4px;
    padding: 2px;
    border: 1px solid var(--stage-control-border, #344752);
    border-radius: var(--stage-control-radius, 3px);
    background: var(--stage-control-list-background, #080f14);
    box-shadow: 0 12px 28px #0009;
    box-sizing: border-box;
    overflow-y: auto;
    overscroll-behavior: contain;
    scrollbar-gutter: stable;
    scrollbar-color: #627a87 #111b23;
  }
  .stage-select__option { height: var(--stage-select-option-height, auto); }
  .stage-select__option[aria-selected="true"] { background: var(--stage-control-selected-background, #354e28); color: var(--stage-control-selected-color, #eeffd5); }
  .stage-select__option--highlighted { box-shadow: inset 0 0 0 2px var(--stage-control-accent, #baff3f); }
  @media (hover: hover) {
    .stage-select__option:hover { background: var(--stage-control-hover-background, #25383a); }
    .stage-select__option[aria-selected="true"]:hover { background: var(--stage-control-selected-hover-background, #3b5630); }
  }
</style>
