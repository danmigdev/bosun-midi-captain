<script lang="ts">
  import type { DeviceStats } from "../lib/protocol";

  let {
    connected,
    current = null,
    patchName = "",
    lastActivityMs = null,
    stats = null,
  }: {
    connected: boolean;
    current?: { bank: number; slot: number } | null;
    patchName?: string;
    lastActivityMs?: number | null;
    stats?: DeviceStats | null;
  } = $props();

  // Lights up when the last activity was recent. Only recomputes on prop
  // changes (deliberately no timer/interval) - it's fine if it lingers on
  // until the next prop update.
  let recentlyActive = $derived(
    lastActivityMs != null && Date.now() - lastActivityMs <= 1500,
  );
</script>

<div class="card mirror">
  <div class="cardhead">
    <span class="cardtitle">Live on device</span>
    <span class="pill" class:ok={connected}>
      <span class="dot"></span>
      {connected ? "Online" : "Offline"}
    </span>
  </div>

  {#if connected}
    {#if current}
      <div class="patchname">Bank {current.bank} &middot; Slot {current.slot}</div>
      <p class="muted small patchsub">{patchName || "(unnamed)"}</p>
    {:else}
      <div class="patchname muted">-</div>
      <p class="muted small patchsub">No patch reported</p>
    {/if}

    <div class="midi-line">
      <span class="pulse" class:live={recentlyActive} aria-hidden="true"></span>
      {#if stats}
        <span class="midi-text">
          MIDI in <b>{stats.midi_rx_count}</b> / out <b>{stats.midi_tx_count}</b>
        </span>
      {:else}
        <span class="midi-text muted">Waiting for MIDI stats&hellip;</span>
      {/if}
    </div>
  {:else}
    <p class="muted small notconnected">Not connected</p>
  {/if}
</div>

<style>
  .card {
    background: linear-gradient(180deg, var(--bg-card) 0%, var(--bg-elevated) 100%);
    border: 1px solid var(--border); border-radius: 8px;
    padding: 0.95rem 1.1rem;
    box-shadow: var(--shadow-card);
  }
  .cardhead {
    display: flex; align-items: center; justify-content: space-between;
    margin-bottom: 0.75rem;
  }
  .cardtitle {
    color: var(--text-dim); font-size: 0.72rem; font-weight: 600;
    text-transform: uppercase; letter-spacing: 0.08em;
  }

  /* connection pill - mirrors Dashboard.svelte */
  .pill {
    display: inline-flex; align-items: center; gap: 0.35rem;
    background: var(--bg-hover); border: 1px solid var(--border-strong); color: var(--text-muted);
    padding: 0.18rem 0.5rem; border-radius: 999px;
    font-size: 0.7rem; font-weight: 500;
  }
  .pill .dot {
    width: 6px; height: 6px; border-radius: 50%; background: var(--border-strong);
  }
  .pill.ok { color: var(--accent); border-color: var(--accent-border); background: var(--accent-bg); }
  .pill.ok .dot {
    background: var(--accent);
    box-shadow: 0 0 0 3px rgba(111,217,155,0.18);
  }

  .patchname {
    color: var(--text); font-weight: 600; font-size: 1.05rem;
    margin: 0 0 0.25rem;
    letter-spacing: -0.005em;
    font-variant-numeric: tabular-nums;
  }
  .patchname.muted { color: var(--text-muted); }
  .patchsub { margin: 0; }

  .midi-line {
    display: flex; align-items: center; gap: 0.5rem;
    margin-top: 0.85rem; padding-top: 0.85rem;
    border-top: 1px solid var(--border);
    font-size: 0.82rem;
  }
  .midi-text { color: var(--text); font-variant-numeric: tabular-nums; }
  .midi-text b { color: var(--accent); font-weight: 600; }

  .pulse {
    flex: 0 0 auto;
    width: 8px; height: 8px; border-radius: 50%;
    background: var(--border-strong);
    transition: background 0.2s ease, box-shadow 0.2s ease;
  }
  .pulse.live {
    background: var(--accent);
    box-shadow: 0 0 0 3px rgba(111,217,155,0.18);
    animation: mirror-pulse 1s ease-out;
  }
  @keyframes mirror-pulse {
    0%   { box-shadow: 0 0 0 0   rgba(111,217,155,0.45); }
    100% { box-shadow: 0 0 0 3px rgba(111,217,155,0.18); }
  }

  .muted { color: var(--text-muted); }
  .small { font-size: 0.85rem; line-height: 1.5; }
  .notconnected { margin: 0; }

  @media (prefers-reduced-motion: reduce) {
    .pulse.live { animation: none; }
  }
</style>
