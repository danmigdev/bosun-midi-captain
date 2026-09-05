<script lang="ts">
  import { onDestroy, untrack } from "svelte";
  import { discoverHubs, type DiscoveredHub } from "../lib/protocol";

  type Props = {
    mode?: "usb" | "network";
    host?: string;
    port?: string;
    busy?: boolean;
  };
  let {
    mode = $bindable("usb"),
    host = $bindable(""),
    port = $bindable("9876"),
    busy = false,
  }: Props = $props();

  let searching = $state(false);
  let searched = $state(false);
  let searchError = $state("");
  let hubs = $state<DiscoveredHub[]>([]);
  let generation = 0;
  let entrySearched = false;
  let destroyed = false;

  // Read the editable address only when a search starts: typing must never
  // restart discovery, and an older reply must not overwrite a later visit.
  $effect(() => {
    if (mode !== "network") {
      entrySearched = false;
      untrack(() => {
        generation += 1;
        searching = false;
        searched = false;
        searchError = "";
        hubs = [];
      });
      return;
    }
    if (!busy && !entrySearched) {
      entrySearched = true;
      untrack(() => { void search(); });
    }
  });

  onDestroy(() => {
    destroyed = true;
    generation += 1;
  });

  async function search() {
    if (busy || searching || mode !== "network") return;
    const request = ++generation;
    searching = true;
    searchError = "";
    hubs = [];
    try {
      const found = await discoverHubs(host.trim() || undefined);
      if (destroyed || request !== generation) return;
      hubs = found;
    } catch (error) {
      if (destroyed || request !== generation) return;
      searchError = String(error);
    } finally {
      if (!destroyed && request === generation) {
        searching = false;
        searched = true;
      }
    }
  }

  function selectHub(hub: DiscoveredHub) {
    host = hub.host;
    port = String(hub.tcp_port);
  }

  function address(hub: DiscoveredHub): string {
    const name = hub.host.includes(":") ? `[${hub.host}]` : hub.host;
    return `${name}:${hub.tcp_port}`;
  }
</script>

<div class="connection">
  <div class="connection__modes" role="group" aria-label="Connection method">
    <button type="button" aria-pressed={mode === "usb"} disabled={busy} onclick={() => { mode = "usb"; }}>
      USB
    </button>
    <button type="button" aria-pressed={mode === "network"} disabled={busy} onclick={() => { mode = "network"; }}>
      Raspberry Pi (network)
    </button>
  </div>

  {#if mode === "network"}
    <p class="connection__hint">
      Connect the MIDI Captain to the Raspberry Pi by USB. Connect this device and the Raspberry Pi to the same network.
    </p>
    <div class="connection__fields">
      <label class="connection__host">
        <span>IP address or hostname</span>
        <input type="text" bind:value={host} disabled={busy} placeholder="192.168.1.100 or bosun-hub.local"
          autocomplete="off" autocapitalize="none" spellcheck={false} />
      </label>
      <label class="connection__port">
        <span>Port</span>
        <input type="text" inputmode="numeric" bind:value={port} disabled={busy} placeholder="9876" autocomplete="off" />
      </label>
    </div>

    <div class="connection__discovery">
      <button type="button" onclick={search} disabled={busy || searching}>
        {searching ? "Searching…" : "Find Raspberry Pi"}
      </button>
      <div class="connection__status" role="status" aria-live="polite" aria-atomic="true">
        {#if searching}
          Searching the local network… You can also enter an address above.
        {:else if searchError}
          <span class="connection__error">Search unavailable: {searchError}</span>
          Enter the Raspberry Pi address manually, or try again.
        {:else if searched && hubs.length === 0}
          No Raspberry Pi found. Enter its address manually, or check that Bosun Hub is running and up to date.
        {:else if hubs.length > 0}
          {hubs.length === 1 ? "1 Raspberry Pi found." : `${hubs.length} Raspberry Pis found.`} Select one, then connect.
        {/if}
      </div>
    </div>

    {#if hubs.length > 0}
      <ul class="connection__hubs" aria-label="Available Raspberry Pis">
        {#each hubs as hub (address(hub))}
          <li>
            <button type="button" disabled={busy} onclick={() => selectHub(hub)}
              aria-pressed={host.trim() === hub.host && port.trim() === String(hub.tcp_port)}>
              <span class="connection__hub-name">{hub.name}</span>
              <span class="connection__hub-address">{address(hub)}</span>
            </button>
          </li>
        {/each}
      </ul>
    {/if}
  {:else}
    <p class="connection__hint">Connect the MIDI Captain directly to this device by USB.</p>
  {/if}
</div>

<style>
  .connection { margin: 1.1rem 0; text-align: left; }
  .connection__modes { display: flex; gap: 0.5rem; }
  .connection__modes button { flex: 1; min-width: 0; padding: 0.65rem 0.5rem; }
  button[aria-pressed="true"] {
    color: var(--accent); background: var(--accent-bg); border-color: var(--accent-border);
  }
  button[aria-pressed="true"]:hover:not(:disabled) {
    background: var(--accent-hover-bg); border-color: var(--accent-hover-border);
  }
  .connection__hint { margin: 0.8rem 0; color: var(--text-muted); font-size: 0.82rem; line-height: 1.5; }
  .connection__fields { display: flex; gap: 0.65rem; }
  .connection__fields label { display: flex; flex-direction: column; gap: 0.35rem; min-width: 0; }
  .connection__fields label span { color: var(--text-soft); font-size: 0.8rem; }
  .connection__host { flex: 1; }
  .connection__port { flex: 0 0 5rem; }
  .connection__fields input {
    box-sizing: border-box; width: 100%; min-width: 0; padding: 0.55rem 0.6rem;
    border: 1px solid var(--border-strong); border-radius: 5px; background: var(--bg-input);
    color: var(--text); font: inherit; font-size: 0.85rem;
  }
  .connection__fields input:focus-visible { outline: 2px solid var(--accent); outline-offset: 1px; }
  .connection__fields input:disabled { opacity: 0.5; }
  .connection__discovery { margin-top: 0.8rem; }
  .connection__status { margin-top: 0.5rem; color: var(--text-muted); font-size: 0.78rem; line-height: 1.45; overflow-wrap: anywhere; }
  .connection__error { display: block; color: var(--err); }
  .connection__hubs { padding: 0; margin: 0.65rem 0 0; list-style: none; display: grid; gap: 0.4rem; }
  .connection__hubs button { width: 100%; display: flex; justify-content: space-between; gap: 0.5rem; padding: 0.6rem; text-align: left; }
  .connection__hub-name { font-weight: 600; overflow-wrap: anywhere; }
  .connection__hub-address { color: var(--text-muted); font-size: 0.78rem; overflow-wrap: anywhere; }
  @media (max-width: 380px) {
    .connection__fields { gap: 0.45rem; }
    .connection__port { flex-basis: 4.3rem; }
    .connection__hubs button { flex-direction: column; gap: 0.2rem; }
  }
</style>
