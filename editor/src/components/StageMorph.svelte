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
    if (!connected || identity !== previousIdentity) {
      ++requestEpoch; busy = false; error = ""; open = false;
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
  <button type="button" class="readout" aria-label="Morph controls" aria-expanded={open}
    onclick={() => open = !open}>
    <span class="endpoint base">BASE</span>
    <span class="meter" role="meter" aria-label="Last commanded Morph position"
      aria-valuemin="0" aria-valuemax="100" aria-valuenow={percent ?? undefined}
      aria-valuetext={percent === null ? "Unknown" : `Set ${percent}%`}
      class:unknown={percent === null}>
      {#if percent !== null}<span class="fill" style:width={`${percent}%`}></span>{/if}
    </span>
    <span class="endpoint target">MORPH</span>
    <span class="position">{percent === null ? "?" : `Set ${percent}%`}</span>
    <span aria-hidden="true">{open ? "−" : "+"}</span>
  </button>
  {#if open}
    <div class="controls">
      <button type="button" disabled={!ready || busy} onclick={() => send("position", 0)}>Base</button>
      <label>Position
        <input type="range" min="0" max="100" step="1" aria-label="Morph position"
          bind:value={draft} disabled={!ready || busy} onchange={() => send("position", Number(draft))} />
        <span>{draft}%</span>
      </label>
      <button type="button" disabled={!ready || busy} onclick={() => send("position", 100)}>Morph</button>
      <button type="button" disabled={!ready || busy} onclick={() => send("trigger")}>Trigger Morph</button>
    </div>
    <p>“Set” is the last position sent by Bosun, not measured progress. Trigger Morph taps the Kemper button using its rig settings; its resulting position is unknown. Use a physical footswitch to hold a momentary Morph.</p>
    {#if !ready}<p>Connect the Kemper and wait for the current rig to be ready.</p>{/if}
    {#if error}<p class="error" role="alert">{error}</p>{/if}
  {/if}
</section>

<style>
  .morph { flex: 0 0 auto; width: 100%; min-width: 0; color: #dce4ee; font: 500 clamp(12px, 1.1vw, 16px)/1.3 var(--stage-font, sans-serif); }
  .readout { width: 100%; display: flex; align-items: center; gap: 0.65em; padding: 0.45em 0; border: 0; background: transparent; color: inherit; cursor: pointer; font: inherit; }
  .endpoint { font-size: 0.8em; letter-spacing: 0.08em; font-weight: 700; }
  .base { color: #83b7fc; } .target { color: #ff919c; }
  .meter { display: block; height: 6px; position: relative; flex: 1; min-width: 24px; overflow: hidden; border-radius: 4px; background: #293342; }
  .fill { display: block; height: 100%; background: linear-gradient(90deg, #5799e5, #ef767a); }
  .unknown { background: repeating-linear-gradient(120deg, #293342 0 5px, #394353 5px 7px); }
  .position { min-width: 5.5em; text-align: right; font-variant-numeric: tabular-nums; }
  .controls { display: flex; align-items: center; gap: 0.6em; flex-wrap: wrap; padding-top: 0.4em; }
  .controls button { min-height: 44px; padding: 0.4em 0.8em; border: 1px solid #526079; border-radius: 6px; background: #1b2432; color: inherit; font: inherit; cursor: pointer; }
  .controls button:disabled { opacity: 0.4; cursor: default; }
  label { display: flex; align-items: center; gap: 0.5em; flex: 1; min-width: 180px; }
  input { flex: 1; width: 70px; min-width: 0; min-height: 44px; accent-color: #ef767a; }
  p { margin: 0.6em 0; font-size: 0.85em; color: #aebbcf; max-width: 85em; }
  .error { color: #ff9b9b; }
  button:focus-visible, input:focus-visible { outline: 2px solid #fff; outline-offset: 3px; }
  @media (pointer: coarse) { .readout { min-height: 44px; } }
</style>
