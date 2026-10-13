<script lang="ts">
  import { untrack } from "svelte";
  import { morphPercent } from "../lib/morph";

  type Props = {
    context: Record<string, unknown>;
    connected: boolean;
    ready: boolean;
    identity: string;
    oncommand: (action: "position" | "trigger", percent?: number) => Promise<void>;
  };
  let { context, connected, ready, identity, oncommand }: Props = $props();
  let open = $state(false);
  let busy = $state(false);
  let error = $state("");
  let draft = $state(0);
  let fence = $state<unknown>(undefined);
  let wasConnected = true;
  let requestEpoch = 0;
  let previousIdentity = untrack(() => identity);
  $effect(() => {
    if (!connected && wasConnected) fence = context.kemper_morph_revision;
    wasConnected = connected;
    // A rig change or a link drop orphans any request in flight. The panel
    // itself stays as the user left it: closing it on every rig change made
    // the Stage layout jump, and its inputs are disabled until `ready`.
    if (!connected || identity !== previousIdentity) {
      ++requestEpoch; busy = false; error = "";
      previousIdentity = identity;
    }
  });
  let value = $derived(context.kemper_morph_value);
  let known = $derived(connected && context.kemper_morph_ready === "on"
    && context.kemper_morph_source === "commanded"
    && typeof value === "number" && Number.isInteger(value) && value >= 0 && value <= 127
    && (fence === undefined || context.kemper_morph_revision !== fence));
  let percent = $derived(known ? morphPercent(value as number) : null);
  $effect(() => { if (!busy && percent !== null) draft = percent; });

  // Every rig change leaves the rig not ready for about half a second (the
  // Kemper is loading it). Explaining that would add and remove a line, and
  // move the switch grid, on every rig change while the panel is open; only a
  // lasting state (Kemper unplugged, no profile) is spelled out.
  const NOT_READY_NOTICE_MS = 2000;
  let notReadyNotice = $state(untrack(() => !ready));
  $effect(() => {
    if (ready) { notReadyNotice = false; return; }
    const timer = setTimeout(() => { notReadyNotice = true; }, NOT_READY_NOTICE_MS);
    return () => clearTimeout(timer);
  });

  async function send(action: "position" | "trigger", position?: number) {
    if (!ready || busy) return;
    const epoch = ++requestEpoch;
    busy = true; error = "";
    try { await oncommand(action, position); }
    catch (e) {
      if (epoch === requestEpoch) error = `Morph command failed: ${String(e).replace(/^Error:\s*/, "")}`;
    } finally { if (epoch === requestEpoch) busy = false; }
  }
</script>

<section class="morph" aria-label="Morph" aria-busy={busy}>
  <div class="morph__row">
    <div class="morph__readout">
      <span class="morph__end morph__end--base">BASE</span>
      <span class="morph__meter" role="meter" aria-label="Last commanded Morph position"
        aria-valuemin="0" aria-valuemax="100" aria-valuenow={percent ?? undefined}
        aria-valuetext={percent === null ? "Unknown" : `Set ${percent}%`}
        class:morph__meter--unknown={percent === null}>
        <span class="morph__track">
          {#if percent !== null}<span class="morph__fill" style:clip-path={`inset(0 ${100 - percent}% 0 0)`}></span>{/if}
        </span>
        {#if percent !== null}<span class="morph__marker" style:left={`${percent}%`}></span>{/if}
      </span>
      <span class="morph__end morph__end--target">MORPH</span>
      <span class="morph__position" class:morph__position--unknown={percent === null}>
        {percent === null ? "Unknown" : `Set ${percent}%`}
      </span>
    </div>
    <button type="button" class="morph__toggle stage-control-button" aria-label="Morph controls"
      aria-expanded={open} onclick={() => open = !open}>
      <span>Controls</span>
      <svg class="morph__chevron" viewBox="0 0 24 24" aria-hidden="true"><path d="m6 9 6 6 6-6" /></svg>
    </button>
  </div>
  {#if open}
    <div class="morph__panel">
      <div class="morph__controls">
        <button type="button" class="stage-control-button" disabled={!ready || busy} onclick={() => send("position", 0)}>Base</button>
        <label class="morph__slider">
          <span>Position</span>
          <input type="range" min="0" max="100" step="1" aria-label="Morph position"
            bind:value={draft} disabled={!ready || busy} onchange={() => send("position", Number(draft))} />
          <span class="morph__draft">{draft}%</span>
        </label>
        <button type="button" class="stage-control-button" disabled={!ready || busy} onclick={() => send("position", 100)}>Morph</button>
        <button type="button" class="stage-control-button" disabled={!ready || busy} onclick={() => send("trigger")}>Trigger Morph</button>
      </div>
      <p>Set is the last position Bosun sent: the Kemper does not report its Morph, so after a rig change or Trigger Morph it reads Unknown. Trigger Morph taps the Kemper's Morph button; hold a momentary Morph with a footswitch.</p>
      {#if !ready && notReadyNotice}<p>Connect the Kemper and wait for the current rig to be ready.</p>{/if}
      {#if error}<p class="morph__error" role="alert">{error}</p>{/if}
    </div>
  {/if}
</section>

<style>
  /* Sizes follow the Stage readouts: portrait by width, landscape bounded by
     both axes (the 1920x440 Pi panel is short). Stage appearance scales the
     bar and the controls panel separately; 100% writes a scale of 2. */
  .morph {
    --morph-text: clamp(14px, 3.4vw, 24px);
    --morph-controls-text: clamp(14px, 3.6vw, 18px);
    flex: 0 0 auto;
    display: grid; gap: 0.4em;
    width: 100%; min-width: 0;
    color: var(--stage-display-text, #edf5f7);
    font-family: var(--stage-morph-font, var(--stage-font, "Inter", -apple-system, sans-serif));
    font-size: calc(var(--morph-text) * var(--stage-morph-scale, 2) / 2);
    font-weight: 600;
    line-height: 1.2;
  }
  @media (orientation: landscape) {
    .morph {
      --morph-text: min(4.6vh, 1.7vw, 2rem);
      --morph-controls-text: min(4vh, 1.2vw, 1.25rem);
    }
  }
  .morph__row {
    display: flex; align-items: stretch; gap: clamp(4px, 0.7vw, 10px);
    min-width: 0;
  }
  .morph__readout {
    flex: 1 1 auto; min-width: 0;
    display: flex; align-items: center; gap: 0.6em;
    padding: 0.3em 0.6em;
    border: 1px solid var(--stage-display-edge, #42545f);
    border-radius: 3px;
    background: #090f14;
    box-shadow: inset 0 1px 3px #00000066;
  }
  .morph__end { flex: 0 0 auto; font-weight: 800; letter-spacing: 0.08em; }
  .morph__end--base { color: #83b7fc; }
  .morph__end--target { color: #ff919c; }
  .morph__meter {
    position: relative;
    flex: 1 1 auto; min-width: 3em;
    height: 0.6em;
  }
  .morph__track {
    position: absolute; inset: 0;
    overflow: hidden;
    border-radius: 0.3em;
    background: #1a2532;
    box-shadow: inset 0 0 0 1px #33434f;
  }
  /* Centre tick: the half-way point between the two sounds. */
  .morph__track::after {
    content: "";
    position: absolute; top: 0; bottom: 0; left: 50%;
    width: 2px; transform: translateX(-1px);
    background: #ffffff40;
  }
  .morph__meter--unknown .morph__track {
    background: repeating-linear-gradient(120deg, #1a2532 0 6px, #2c3846 6px 9px);
  }
  /* The full blue-to-red gradient, clipped at the position, so the colour at
     the edge says where between the two sounds the rig was set. */
  .morph__fill {
    position: absolute; inset: 0;
    background: linear-gradient(90deg, #5799e5, #ef767a);
  }
  .morph__marker {
    position: absolute; top: -0.2em; bottom: -0.2em;
    width: 0.22em; transform: translateX(-50%);
    border-radius: 2px;
    background: #ffffff;
    box-shadow: 0 0 0.3em #000000aa;
  }
  .morph__position {
    flex: 0 0 auto; min-width: 5.2em;
    text-align: right; white-space: nowrap;
    font-variant-numeric: tabular-nums;
  }
  .morph__position--unknown { color: var(--stage-display-muted, #9eafb9); }
  .morph .morph__toggle {
    flex: 0 0 auto;
    gap: 0.35em;
    min-height: 0;
    padding: 0 0.8em;
    font-family: inherit;
    font-size: 0.8em;
  }
  .morph__chevron {
    width: 1.2em; height: 1.2em;
    fill: none; stroke: currentColor; stroke-width: 2.5;
    transition: transform 0.15s ease;
  }
  .morph__toggle[aria-expanded="true"] .morph__chevron { transform: rotate(180deg); }

  .morph__panel {
    display: grid; gap: 0.5em;
    color: var(--stage-morph-controls-color, var(--stage-display-text, #edf5f7));
    font-family: var(--stage-morph-controls-font, var(--stage-font, "Inter", -apple-system, sans-serif));
    font-size: calc(var(--morph-controls-text) * var(--stage-morph-controls-scale, 2) / 2);
    font-weight: 600;
  }
  .morph__controls { display: flex; align-items: center; gap: 0.6em; flex-wrap: wrap; }
  .morph .morph__controls button {
    min-height: max(44px, 2.2em);
    padding: 0 1em;
    color: inherit;
    font: inherit;
  }
  .morph__slider { display: flex; align-items: center; gap: 0.6em; flex: 1 1 12em; min-width: 0; }
  .morph__slider input {
    flex: 1; width: 70px; min-width: 0; min-height: max(44px, 2.2em); margin: 0;
    accent-color: #ef767a; cursor: pointer;
  }
  .morph__slider input:disabled { cursor: default; }
  .morph__draft { min-width: 3.2em; text-align: right; font-variant-numeric: tabular-nums; }
  .morph__panel p { margin: 0; font-size: 0.85em; font-weight: 500; line-height: 1.35; opacity: 0.8; }
  .morph__panel .morph__error { color: #ffb8a8; opacity: 1; }
  .morph__slider input:focus-visible { outline: 2px solid var(--stage-control-accent, #baff3f); outline-offset: 3px; }
  @media (pointer: coarse) {
    .morph .morph__toggle { min-height: 44px; }
  }
  @media (prefers-reduced-motion: reduce) {
    .morph__chevron { transition: none; }
  }
</style>
