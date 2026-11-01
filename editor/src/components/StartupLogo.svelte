<script lang="ts">
  import { onDestroy, untrack } from "svelte";
  import { cmd } from "../lib/protocol";
  import {
    deletePedalLogo, hubLogoInfo, prepareLogo, resetHubLogo, uploadHubLogo, uploadPedalLogo,
    PI_BACKGROUND, type HubLogoInfo, type PedalLogoState, type PreparedLogo,
  } from "../lib/boot-logo";
  import bosunIcon from "../../src-tauri/icons/128x128@2x.png";

  type Props = {
    connected: boolean;
    /** DEVICE_INFO.boot_logo; null when the firmware cannot show a startup logo. */
    pedalLogo: PedalLogoState | null;
    /** Connected through a Raspberry Pi hub. */
    networkSession: boolean;
    version?: string;
  };
  let { connected, pedalLogo, networkSession, version = "" }: Props = $props();

  let piInfo = $state<HubLogoInfo | null>(null);
  let pending = $state<PreparedLogo | null>(null);
  let busy = $state(false);
  let progress = $state("");
  let message = $state("");
  let failure = $state("");
  let fileInput: HTMLInputElement | undefined = $state();
  let destroyed = false;

  let pedalReady = $derived(connected && pedalLogo !== null);
  let piReady = $derived(connected && networkSession && piInfo?.supported === true);
  let piPng = $derived(piInfo?.custom && piInfo.png ? `data:image/png;base64,${piInfo.png}` : "");
  let anyCustom = $derived((pedalReady && pedalLogo === "custom") || (piReady && piInfo?.custom === true));
  // What the previews show: the picked image, else the Pi's stored logo, else Bosun.
  let shown = $derived(pending?.previewUrl || piPng);
  let pedalCustomUnseen = $derived(!pending && pedalLogo === "custom" && !piPng);

  async function refreshPi() {
    if (!connected || !networkSession) { piInfo = null; return; }
    try { piInfo = await hubLogoInfo(); }
    catch { piInfo = null; }
  }
  $effect(() => {
    void connected; void networkSession;
    untrack(() => { void refreshPi(); });
  });

  /** The early boot pictures are rewritten by a root service a moment later. */
  async function followEarlyStatus() {
    for (let attempt = 0; attempt < 8 && piInfo?.early === "pending" && !destroyed; ++attempt) {
      await new Promise(resolve => setTimeout(resolve, 1500));
      await refreshPi();
    }
  }

  function discardPending() { pending = null; }
  onDestroy(() => { destroyed = true; discardPending(); });

  async function picked(event: Event) {
    const input = event.currentTarget as HTMLInputElement;
    const file = input.files?.[0];
    input.value = "";
    if (!file) return;
    failure = ""; message = "";
    busy = true; progress = "Preparing the logo…";
    try {
      const prepared = await prepareLogo(file);
      discardPending();
      pending = prepared;
    } catch (error) {
      failure = error instanceof Error ? error.message : String(error);
    } finally { busy = false; progress = ""; }
  }

  async function run(action: "apply" | "reset") {
    failure = ""; message = "";
    busy = true;
    const done: string[] = [];
    try {
      if (pedalReady) {
        progress = "Updating the pedal…";
        if (action === "apply" && pending) {
          await uploadPedalLogo(pending.tft, fraction => { progress = `Sending to the pedal… ${Math.round(fraction * 100)}%`; });
        } else await deletePedalLogo();
        done.push("pedal");
        void cmd.getDeviceInfo().catch(() => {});
      }
      if (piReady) {
        progress = "Updating the Raspberry Pi…";
        if (action === "apply" && pending) {
          await uploadHubLogo(pending.piPng, pending.piPicture,
            fraction => { progress = `Sending to the Raspberry Pi… ${Math.round(fraction * 100)}%`; });
        } else await resetHubLogo();
        done.push("Raspberry Pi");
        await refreshPi();
      }
      if (action === "apply") discardPending();
      message = action === "apply"
        ? `Startup logo saved on the ${done.join(" and ")}.`
        : `Bosun logo restored on the ${done.join(" and ")}.`;
      if (done.includes("pedal")) message += " The pedal screen shows it for a few seconds now.";
    } catch (error) {
      const reason = error instanceof Error ? error.message : String(error);
      failure = done.length
        ? `Saved on the ${done.join(" and ")}, but the next step failed: ${reason}`
        : reason;
    } finally {
      busy = false; progress = "";
    }
    void followEarlyStatus();
  }

  const earlyText: Record<string, string> = {
    applied: "Shown from early in the Raspberry Pi startup and on the Stage loading screen.",
    pending: "Updating the early startup picture on the Raspberry Pi…",
    unavailable: "Shown on the Stage loading screen. This Raspberry Pi has no early startup picture installed.",
  };
</script>

<section class="block">
  <h3>Startup logo</h3>
  {#if !pedalReady && !piReady}
    <p class="hint">
      {#if !connected}Connect the pedal to change its startup logo.
      {:else}Update the pedal firmware to show a startup logo on its screen.{/if}
    </p>
  {:else}
    <p class="hint">
      Shown on the pedal screen at power-on{#if piReady} and on the Raspberry Pi display while it
      starts{/if}. Pick a PNG, JPEG, SVG or WebP image; it is scaled to fit, and a simple logo on a
      transparent or dark background works best.
    </p>

    <div class="previews">
      {#if pedalReady}
        <figure>
          <div class="tft" role="img" aria-label="Pedal screen preview">
            <div class="tftlogo">
              {#if shown}<img src={shown} alt="" />
              {:else if pedalCustomUnseen}<span class="custom">Custom logo</span>
              {:else}<img class="bosun" src={bosunIcon} alt="" />{/if}
            </div>
            <span class="tftversion">{version ? `v${version}` : ""}</span>
          </div>
          <figcaption>Pedal</figcaption>
        </figure>
      {/if}
      {#if piReady}
        <figure>
          <div class="pi" style:background={PI_BACKGROUND} role="img" aria-label="Raspberry Pi startup preview">
            {#if shown}
              <div class="pilogo"><img src={shown} alt="" /></div>
            {:else}
              <div class="pilogo default"><img src={bosunIcon} alt="" /><strong>Bosun</strong></div>
            {/if}
            <span class="piversion">{version ? `v${version}` : ""}</span>
            <span class="pibar"><span></span></span>
          </div>
          <figcaption>Raspberry Pi</figcaption>
        </figure>
      {/if}
    </div>

    <input bind:this={fileInput} type="file" accept="image/png,image/jpeg,image/svg+xml,image/webp" hidden onchange={picked} />
    <div class="row">
      {#if pending}
        <button class="primary" disabled={busy} onclick={() => run("apply")}>Use this logo</button>
        <button disabled={busy} onclick={discardPending}>Cancel</button>
      {:else}
        <button disabled={busy} onclick={() => fileInput?.click()}>Choose image…</button>
        <button disabled={busy || !anyCustom} onclick={() => run("reset")}>Use Bosun logo</button>
      {/if}
    </div>

    {#if progress}<p class="hint">{progress}</p>{/if}
    {#if message}<p class="ok">{message}</p>{/if}
    {#if failure}<p class="err">{failure}</p>{/if}
    {#if piReady && piInfo?.custom && piInfo.early}
      <p class="hint small">
        {#if piInfo.early === "error"}The early startup picture could not be updated: {piInfo.error || "unknown error"}.
        {:else}{earlyText[piInfo.early]}{/if}
      </p>
    {/if}
    {#if connected && networkSession && piInfo && !piInfo.supported}
      <p class="hint small">Update the Raspberry Pi software to show this logo on its startup screen too.</p>
    {/if}
  {/if}
</section>

<style>
  .block { background: var(--bg-card); border: 1px solid var(--border); border-radius: 6px; padding: 0.85rem 1rem; margin-bottom: 0.85rem; }
  h3 { color: var(--accent); margin: 0 0 0.6rem; font-size: 0.8rem; text-transform: uppercase; letter-spacing: 0.05em; font-weight: 600; }
  .hint { color: var(--text-muted); font-size: 0.8rem; margin: 0.2rem 0 0.4rem; }
  .hint.small { font-size: 0.75rem; }
  .ok { color: var(--accent); font-size: 0.8rem; margin: 0.4rem 0 0; }
  .err { color: var(--err); font-size: 0.8rem; margin: 0.4rem 0 0; }
  .row { display: flex; gap: 0.5rem; flex-wrap: wrap; margin: 0.6rem 0 0.2rem; }
  button { background: var(--bg-hover); color: var(--text); border: 1px solid var(--border-strong); padding: 0.45rem 0.9rem; border-radius: 4px; cursor: pointer; font-size: 0.85rem; }
  button:disabled { opacity: 0.45; cursor: not-allowed; }
  button.primary { background: var(--accent-bg); color: var(--accent); border-color: var(--accent-border); font-weight: 600; }
  button.primary:hover:not(:disabled) { background: var(--accent-hover-bg); }
  .previews { display: flex; gap: 1rem; flex-wrap: wrap; align-items: flex-end; margin-top: 0.5rem; }
  figure { margin: 0; display: flex; flex-direction: column; align-items: center; gap: 0.3rem; }
  figcaption { color: var(--text-muted); font-size: 0.75rem; }
  /* Both mock-ups keep the real geometry, scaled down: TFT 240x240 with a
     240x200 logo box; Pi 640x360 with a 400x222 box at (120, 28). */
  .tft { position: relative; width: 150px; height: 150px; background: #000; border: 1px solid var(--border-strong); border-radius: 3px; overflow: hidden; }
  .tftlogo { position: absolute; left: 0; top: 0; width: 100%; height: 83.33%; display: flex; align-items: center; justify-content: center; }
  .tftlogo img { max-width: 100%; max-height: 100%; }
  .tftlogo img.bosun { width: 73.33%; }
  .tftlogo .custom { color: #9aa1ad; font-size: 0.75rem; }
  .tftversion { position: absolute; left: 0; right: 0; top: 87%; text-align: center; color: #9aa1ad; font: 0.6rem ui-monospace, Consolas, monospace; }
  .pi { position: relative; width: 256px; height: 144px; border: 1px solid var(--border-strong); border-radius: 3px; overflow: hidden; }
  .pilogo { position: absolute; left: 18.75%; top: 7.78%; width: 62.5%; height: 61.67%; display: flex; align-items: center; justify-content: center; }
  .pilogo img { max-width: 100%; max-height: 100%; }
  .pilogo.default { flex-direction: column; justify-content: flex-start; }
  .pilogo.default img { height: 74.77%; }
  .pilogo.default strong { color: #e4e6eb; font-size: 0.8rem; font-weight: 400; margin-top: 0.3rem; }
  .piversion { position: absolute; left: 0; right: 0; top: 71.67%; text-align: center; color: #9aa1ad; font-size: 0.5rem; }
  .pibar { position: absolute; left: 32.8%; top: 81.67%; width: 34.4%; height: 2px; background: #2a2e36; border-radius: 1px; }
  .pibar span { display: block; width: 65%; height: 100%; background: #6fd99b; border-radius: inherit; }
</style>
