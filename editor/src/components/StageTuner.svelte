<script lang="ts">
  import { onMount, onDestroy } from "svelte";
  import { tunerReading } from "../lib/stage-tuner";

  let { note, deviance, onclose }: {
    note: unknown;
    deviance: unknown;
    onclose: () => void;
  } = $props();
  let reading = $derived(tunerReading(note, deviance));
  let pointer = $derived(500 + reading.position * 460);
  let dialog: HTMLDialogElement;
  let previousFocus: HTMLElement | null = null;

  onMount(() => {
    previousFocus = document.activeElement instanceof HTMLElement ? document.activeElement : null;
    if (typeof dialog.showModal === "function") dialog.showModal();
    else dialog.setAttribute("open", "");
    // Opening from MIDI must not highlight the only button as a keyboard
    // selection. Keep focus in the modal; Tab still reaches the close action.
    dialog.focus({ preventScroll: true });
  });
  onDestroy(() => {
    if (dialog?.open && typeof dialog.close === "function") dialog.close();
    if (previousFocus?.isConnected) previousFocus.focus({ preventScroll: true });
  });
</script>

<dialog bind:this={dialog} class="tuner-screen" class:tuner-screen--center={reading.direction === "center"}
  class:tuner-screen--waiting={!reading.valid} aria-label="Tuner"
  oncancel={(event) => { event.preventDefault(); onclose(); }}>
  <div class="tuner-screen__sheet">
    <button type="button" class="tuner-screen__close stage-control-icon stage-control-icon--dialog stage-control-icon--close" aria-label="Close tuner"
      title="Return to Stage; the Kemper tuner stays on" onclick={onclose}>
      <svg viewBox="0 0 24 24" aria-hidden="true"><path d="m6 6 12 12M18 6 6 18" /></svg>
    </button>

    <div class="tuner-screen__body">
      <div class="tuner-screen__note-panel">
        <span class="tuner-screen__note" aria-label={`Note ${reading.note}${reading.accidental}`}>
          {reading.note}<sup>{reading.accidental}</sup>
        </span>
      </div>

      <div class="tuner-screen__instrument" data-direction={reading.direction}>
        <span class="tuner-screen__accidental tuner-screen__flat" class:active={reading.direction === "flat"} aria-hidden="true">♭</span>
        <svg class="tuner-screen__meter" viewBox="0 0 1000 180" preserveAspectRatio="none" role="img"
          aria-label={reading.valid ? `${reading.note}${reading.accidental}: ${reading.status}` : "Waiting for pitch feedback"}>
          <rect x="484" y="24" width="32" height="132" rx="12" class="tuner-screen__target" />
          <line x1="40" y1="100" x2="960" y2="100" class="tuner-screen__rail" />
          {#each Array.from({ length: 41 }, (_, i) => i) as i}
            {@const major = i % 5 === 0}
            {@const x = 40 + i * 23}
            <rect x={x - (i === 20 ? 4 : 2)} y={i === 20 ? 40 : major ? 62 : 76}
              width={i === 20 ? 8 : 4} height={i === 20 ? 96 : major ? 52 : 24} rx="2"
              class="tuner-screen__tick" class:tuner-screen__tick--center={i === 20}
              class:tuner-screen__tick--lit={reading.valid && Math.abs(x - pointer) < 35} />
          {/each}
          {#if reading.valid}
            <g class="tuner-screen__needle" transform={`translate(${pointer}, 0)`}>
              <path d="M-13 8 H13 L0 25 Z" />
              <rect x="-3" y="30" width="6" height="112" rx="3" />
              <path d="M-13 166 H13 L0 149 Z" />
            </g>
          {/if}
        </svg>
        <span class="tuner-screen__accidental tuner-screen__sharp" class:active={reading.direction === "sharp"} aria-hidden="true">♯</span>
      </div>
    </div>
  </div>
</dialog>

<style>
  .tuner-screen {
    --tuner-accent: #ffb454;
    position: fixed; inset: 0; margin: 0; padding: 0;
    width: 100%; height: 100%; max-width: none; max-height: none;
    box-sizing: border-box; overflow: hidden;
    border: 2px solid #42545f; border-radius: var(--stage-corner-radius, 24px);
    color: #edf5f7;
    background: radial-gradient(ellipse at 65% 50%, #152b30 0%, #0b151b 40%, #080c10 80%);
    font-family: var(--stage-tuner-font, var(--stage-font, "Inter", -apple-system, sans-serif));
  }
  .tuner-screen::backdrop { background: #000; }
  /* The modal is the initial focus container, not an interactive control. */
  .tuner-screen:focus { outline: none; }
  .tuner-screen--center { --tuner-accent: #8befa2; }
  .tuner-screen--waiting { --tuner-accent: #9aafb8; }
  .tuner-screen__sheet {
    display: flex; flex-direction: column; height: 100%; box-sizing: border-box;
    padding: clamp(14px, 3vh, 32px) clamp(18px, 3vw, 56px);
  }
  .tuner-screen__close {
    position: absolute; z-index: 1;
    /* Header geometry is measured in viewport coordinates. Account for the
       dialog's own border so this action overlays the Stage X exactly. */
    top: calc(var(--stage-action-top, 14px) - 2px);
    left: calc(var(--stage-action-left, calc(100vw - 18px - var(--stage-action-width, 48px))) - 2px);
  }
  .tuner-screen__body { display: grid; grid-template-columns: minmax(0, 1fr) minmax(0, 2.5fr); align-items: center; gap: 2vw; flex: 1; min-height: 0; }
  .tuner-screen__note-panel { display: flex; flex-direction: column; align-items: center; justify-content: center; min-width: 0; height: 100%; overflow: hidden; }
  .tuner-screen__note {
    display: flex; align-items: baseline;
    font-size: calc(min(76vh, 25vw) * var(--stage-tuner-scale, 2) / 2);
    line-height: .95; font-weight: 700; letter-spacing: -.055em;
    color: var(--stage-tuner-color, var(--tuner-accent));
  }
  .tuner-screen__note sup { font-size: .42em; align-self: flex-start; line-height: 1.3; letter-spacing: 0; }
  .tuner-screen__instrument { display: grid; grid-template-columns: auto minmax(0, 1fr) auto; align-items: center; gap: 1vw; min-width: 0; }
  .tuner-screen__accidental { color: #9cafb9; font-size: min(34vh, 9vw); line-height: 1; }
  .tuner-screen__accidental.active { color: var(--tuner-accent); }
  .tuner-screen__meter { display: block; width: 100%; height: min(56vh, 25vw); overflow: visible; }
  .tuner-screen__target { fill: #8befa20b; stroke: #8befa22b; stroke-width: 1; }
  .tuner-screen--center .tuner-screen__target { fill: #8befa225; stroke: #8befa266; }
  .tuner-screen__rail { stroke: #29404a; stroke-width: 2; }
  .tuner-screen__tick { fill: #536771; }
  .tuner-screen__tick--center { fill: #8befa280; }
  .tuner-screen__tick--lit, .tuner-screen__needle { fill: var(--tuner-accent); }
  @media (orientation: portrait) {
    .tuner-screen__body { grid-template-columns: minmax(0, 1fr); grid-template-rows: minmax(0, 1fr) auto; gap: 3vh; }
    .tuner-screen__note { font-size: calc(min(52vh, 72vw) * var(--stage-tuner-scale, 2) / 2); }
    .tuner-screen__meter { height: min(25vh, 32vw); }
    .tuner-screen__accidental { font-size: clamp(40px, 14vw, 80px); }
    .tuner-screen__sheet { padding-top: calc(var(--stage-action-height, 36px) + 5vh); padding-bottom: 6vh; }
  }
  @media (max-height: 360px) and (orientation: landscape) {
    .tuner-screen__sheet { padding: 10px 16px; }
  }
</style>
