<script lang="ts">
  import { onMount, onDestroy } from "svelte";
  import StageSelect from "./StageSelect.svelte";
  import type { BankSelectionMode } from "../lib/stage-behavior";

  type Props = {
    banks: { bank: number; count: number; color?: string }[];
    currentBank: number | null;
    loading: boolean;
    pending: boolean;
    error: string;
    selectionMode?: BankSelectionMode;
    onmodechange?: (mode: BankSelectionMode) => void;
    onselect: (bank: number) => void;
    onclose: () => void;
    onretry: () => void;
  };
  let { banks, currentBank, loading, pending, error, selectionMode = "immediate",
    onmodechange, onselect, onclose, onretry }: Props = $props();
  let dialog: HTMLDialogElement;
  let previousFocus: HTMLElement | null = null;
  const modes: { value: BankSelectionMode; label: string }[] = [
    { value: "immediate", label: "Load rig immediately" },
    { value: "preselect", label: "Preselect bank" },
  ];

  onMount(() => {
    previousFocus = document.activeElement instanceof HTMLElement ? document.activeElement : null;
    // Native modal dialogs keep keyboard focus inside the picker and make
    // the underlying Stage controls inert, including in fullscreen mode.
    if (typeof dialog.showModal === "function") dialog.showModal();
    else dialog.setAttribute("open", "");
  });

  onDestroy(() => {
    if (dialog?.open && typeof dialog.close === "function") dialog.close();
    // Also restore focus in environments without the native dialog methods.
    if (previousFocus?.isConnected) previousFocus.focus({ preventScroll: true });
  });

  function close() {
    if (!pending) onclose();
  }

  function cancel(event: Event) {
    event.preventDefault();
    close();
  }
</script>

<dialog bind:this={dialog} class="bank-picker" aria-label="Choose bank" oncancel={cancel}>
  <div class="bank-picker__sheet" aria-busy={pending || loading}>
    <header class="bank-picker__header">
      <div class="bank-picker__actions">
      <StageSelect id="bank-selection" label="Bank selection" listLabel="Bank selection mode"
        prefix="bank-picker__mode" value={selectionMode} options={modes} disabled={pending}
        onchange={(mode) => onmodechange?.(mode as BankSelectionMode)} />
      <button
        type="button"
        class="bank-picker__close stage-control-icon stage-control-icon--dialog"
        aria-label="Close bank selection"
        disabled={pending}
        onclick={close}
      >
        <svg viewBox="0 0 24 24" aria-hidden="true"><path d="m6 6 12 12M18 6 6 18" /></svg>
      </button>
      </div>
    </header>

    <div class="bank-picker__body">
      {#if error}
        <div class="bank-picker__error" role="alert">
          <p>{error}</p>
          {#if !pending}
            <button type="button" class="bank-picker__retry stage-control-button" disabled={loading} onclick={onretry}>Retry</button>
          {/if}
        </div>
      {/if}
      {#if loading}
        <p class="bank-picker__status" role="status">Loading banks…</p>
      {:else if pending}
        <p class="bank-picker__status" role="status">Changing bank…</p>
      {:else if !banks.length && !error}
        <p class="bank-picker__status" role="status">No banks available.</p>
      {/if}

      {#if banks.length}
        <div class="bank-picker__grid">
          {#each banks as bank (bank.bank)}
            <button
              type="button"
              class="bank-picker__bank"
              class:bank-picker__bank--current={bank.bank === currentBank}
              style:--bank-color={bank.color || "#baff3f"}
              aria-label={`Bank ${bank.bank}`}
              aria-current={bank.bank === currentBank ? "true" : undefined}
              disabled={loading || pending}
              onclick={() => onselect(bank.bank)}
            >
              <span class="bank-picker__bank-label">Bank</span>
              <span class="bank-picker__number">{bank.bank}</span>
              <span class="bank-picker__count">{bank.count} {bank.count === 1 ? "rig" : "rigs"}</span>
              <span class="bank-picker__current">{bank.bank === currentBank ? "Current" : "\u00a0"}</span>
            </button>
          {/each}
        </div>
      {/if}
    </div>
  </div>
</dialog>

<style>
  .bank-picker {
    position: fixed;
    inset: 0;
    box-sizing: border-box;
    width: 100%;
    height: 100%;
    max-width: none;
    max-height: none;
    margin: 0;
    padding: 0;
    overflow: hidden;
    border: 0;
    background: #080f14;
    color: #edf5f7;
    font-family: var(--stage-font, "Inter", -apple-system, sans-serif);
  }
  .bank-picker::backdrop { background: #080f14; }
  .bank-picker__sheet { height: 100%; min-height: 0; display: flex; flex-direction: column; }
  .bank-picker__header {
    position: relative;
    box-sizing: border-box;
    flex: 0 0 var(--stage-header-height, 76px);
    width: var(--stage-header-width, calc(100% - 16px));
    height: var(--stage-header-height, 76px);
    margin-top: var(--stage-header-top, 8px);
    margin-left: var(--stage-header-left, 8px);
    padding: 0;
    font-family: var(--stage-control-font, "Inter", -apple-system, sans-serif);
    font-size: var(--stage-control-font-size, 1rem);
  }
  .bank-picker__actions {
    position: absolute;
    top: var(--stage-action-offset-top, 0px);
    right: var(--stage-action-offset-right, 0px);
    display: grid;
    grid-template-columns: minmax(0, 18.75em) var(--stage-action-width, 44px);
    align-items: start;
    gap: clamp(4px, 1vw, 12px);
    width: min(calc(100% - var(--stage-action-offset-right, 0px)), calc(18.75em + var(--stage-action-width, 44px) + clamp(4px, 1vw, 12px)));
  }
  .bank-picker__bank {
    box-sizing: border-box;
    font: inherit;
    color: inherit;
    cursor: pointer;
    touch-action: manipulation;
    -webkit-tap-highlight-color: transparent;
  }
  .bank-picker__bank:focus-visible { outline: 2px solid var(--stage-control-accent, #baff3f); outline-offset: -4px; }
  .bank-picker__bank:disabled { cursor: default; opacity: 0.55; }
  .bank-picker__body {
    flex: 1;
    min-height: 0;
    overflow: auto;
    overscroll-behavior: contain;
    scrollbar-color: #627a87 #111b23;
    padding: clamp(12px, 2vw, 24px);
  }
  .bank-picker__grid {
    display: grid;
    grid-template-columns: repeat(2, minmax(0, 1fr));
    gap: clamp(10px, 1.2vw, 18px);
    max-width: 1600px;
    margin: 0 auto;
  }
  .bank-picker__bank {
    position: relative;
    display: flex;
    flex-direction: column;
    align-items: center;
    justify-content: center;
    min-width: 64px;
    min-height: 144px;
    padding: 12px 8px 14px;
    overflow: hidden;
    border: 1px solid #344752;
    border-radius: 3px;
    background: linear-gradient(145deg, #18252d, #10191f);
  }
  .bank-picker__bank::after {
    content: "";
    position: absolute;
    right: 0;
    bottom: 0;
    left: 0;
    height: 4px;
    background: var(--bank-color);
    opacity: 0.55;
  }
  .bank-picker__bank--current {
    background: linear-gradient(145deg, #354e28, #182b23);
    box-shadow: inset 0 0 0 1px #baff3f80;
  }
  .bank-picker__bank--current::after { background: #baff3f; opacity: 1; }
  .bank-picker__bank-label { color: #a8bac5; font-size: 12px; line-height: 1.3; letter-spacing: 0.14em; text-transform: uppercase; }
  .bank-picker__number { font-size: clamp(46px, 5vw, 76px); font-weight: 650; line-height: 1.15; font-variant-numeric: tabular-nums; }
  .bank-picker__count { color: #c2d0d7; font-size: 14px; line-height: 1.5; }
  .bank-picker__current { color: #baff3f; font-size: 12px; line-height: 1.5; letter-spacing: 0.06em; text-transform: uppercase; }
  .bank-picker__status, .bank-picker__error {
    max-width: 1600px;
    margin: 0 auto 16px;
    font-size: 16px;
    line-height: 1.5;
  }
  .bank-picker__status { color: #c2d0d7; }
  .bank-picker__error { padding: 16px; border: 1px solid #344752; border-radius: 3px; background: #301d1c; color: #ffb8a8; }
  .bank-picker__error p { margin: 0; }
  .bank-picker__retry { min-width: 6em; margin-top: 12px; }
  @media (hover: hover) {
    .bank-picker__bank:hover:not(:disabled) { background: #25383a; }
    .bank-picker__bank--current:hover:not(:disabled) { background: #3b5630; }
  }
  .bank-picker__bank:active:not(:disabled) { background: #355044; }
  @media (min-width: 480px) { .bank-picker__grid { grid-template-columns: repeat(3, minmax(0, 1fr)); } }
  @media (min-width: 700px) { .bank-picker__grid { grid-template-columns: repeat(4, minmax(0, 1fr)); } }
  @media (min-width: 960px) { .bank-picker__grid { grid-template-columns: repeat(5, minmax(0, 1fr)); } }
  @media (min-width: 1200px) { .bank-picker__grid { grid-template-columns: repeat(6, minmax(0, 1fr)); } }
  @media (min-width: 1440px) { .bank-picker__grid { grid-template-columns: repeat(8, minmax(0, 1fr)); } }
</style>
