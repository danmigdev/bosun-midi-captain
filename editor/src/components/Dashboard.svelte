<script lang="ts">
  import { onMount, onDestroy } from "svelte";
  import {
    cmd,
    type DeviceStats,
    type ProfileInfo,
    type PatchSummary,
    type Manifest,
  } from "../lib/protocol";
  import DeviceMirror from "./DeviceMirror.svelte";

  type Props = {
    connected: boolean;
    deviceInfo: { fw: string; device: string; bank: number; slot: number; profile?: string } | null;
    activeProfile: ProfileInfo | null;
    activeKind: string;
    manifest: Manifest | null;
    patches: PatchSummary[];
    connectedPortName: string;
    /** Navigate the parent App to a specific page. */
    onNavigate: (page: string) => void;
  };
  let {
    connected, deviceInfo, activeProfile, activeKind,
    manifest, patches, connectedPortName, onNavigate,
  }: Props = $props();

  let stats = $state<DeviceStats | null>(null);
  let statsTimer: ReturnType<typeof setInterval> | null = null;
  // Timestamp of the last observed MIDI activity (rx/tx count change between
  // polls), so the live mirror can pulse. Coarse (5s poll) but enough to show
  // the pedal is talking.
  let lastActivityMs = $state<number | null>(null);
  let prevRx = -1;
  let prevTx = -1;

  onMount(() => { if (connected) startPoll(); });
  onDestroy(() => stopPoll());
  $effect(() => { connected ? startPoll() : stopPoll(); });

  function startPoll() {
    if (statsTimer) return;
    pollOnce();
    statsTimer = setInterval(pollOnce, 5000);
  }
  function stopPoll() {
    if (statsTimer) { clearInterval(statsTimer); statsTimer = null; }
  }
  async function pollOnce() {
    try {
      const s = await cmd.getStats();
      if (prevRx >= 0 && (s.midi_rx_count !== prevRx || s.midi_tx_count !== prevTx)) {
        lastActivityMs = Date.now();
      }
      prevRx = s.midi_rx_count; prevTx = s.midi_tx_count;
      stats = s;
    } catch { /* ignore poll errors */ }
  }

  function humanMs(ms: number): string {
    const s = Math.floor(ms / 1000);
    if (s < 60) return `${s}s`;
    const m = Math.floor(s / 60); const r = s % 60;
    if (m < 60) return `${m}m ${r}s`;
    const h = Math.floor(m / 60); const rm = m % 60;
    return `${h}h ${rm}m`;
  }

  // Derive a few at-a-glance summaries.
  let pluginLabels = $derived(
    manifest
      ? Object.entries(manifest.plugins)
          .filter(([id]) => !activeKind || id === activeKind)
          .map(([, p]) => p.label)
          .join(", ")
      : "",
  );
  let activePatch = $derived(
    deviceInfo
      ? patches.find(p => p.bank === deviceInfo.bank && p.slot === deviceInfo.slot)
      : null,
  );
  let bankCount = $derived(new Set(patches.map(p => p.bank)).size);
</script>

<div class="dashboard">
  <header class="hero">
    <h1>Welcome back</h1>
    <p class="lede">
      {#if connected}
        Your pedal is online and ready.
      {:else}
        Plug in your pedal to get started.
      {/if}
    </p>
  </header>

  <div class="grid">
    <!-- Connection / firmware card -->
    <div class="card status">
      <div class="cardhead">
        <span class="cardtitle">Connection</span>
        <span class="pill" class:ok={connected}>
          <span class="dot"></span>
          {connected ? "Online" : "Offline"}
        </span>
      </div>
      {#if connected && deviceInfo}
        <dl>
          <div><dt>Port</dt><dd>{connectedPortName || "-"}</dd></div>
          <div><dt>Firmware</dt><dd>v{deviceInfo.fw}</dd></div>
          <div><dt>Current patch</dt><dd>{String(deviceInfo.bank).padStart(2,"0")}/{String(deviceInfo.slot).padStart(2,"0")}</dd></div>
          {#if stats}
            <div><dt>Uptime</dt><dd>{humanMs(stats.uptime_ms)}</dd></div>
          {/if}
        </dl>
      {:else}
        <p class="muted small">
          The pedal will appear here once it shows up over USB.
        </p>
      {/if}
    </div>

    <!-- Profile card -->
    <div class="card profile">
      <div class="cardhead">
        <span class="cardtitle">Active profile</span>
      </div>
      {#if activeProfile}
        <div class="profilename">{activeProfile.name}</div>
        <p class="muted small">{pluginLabels || "Generic profile"}</p>
        <div class="metrics">
          <div><b>{patches.length}</b><span>patches</span></div>
          <div><b>{bankCount}</b><span>{bankCount === 1 ? "bank" : "banks"}</span></div>
        </div>
      {:else if connected}
        <p class="muted small">No profile selected yet. Create one from the topbar.</p>
      {:else}
        <p class="muted small">Connect to see the active profile.</p>
      {/if}
    </div>

    <!-- Current patch / activity -->
    <div class="card patch">
      <div class="cardhead">
        <span class="cardtitle">Current patch</span>
      </div>
      {#if activePatch}
        <div class="patchname">{activePatch.name || "(unnamed)"}</div>
        <p class="muted small">
          Slot {String(activePatch.bank).padStart(2,"0")}/{String(activePatch.slot).padStart(2,"0")}
          {#if activePatch.dirty} - <span class="dirty">unsaved changes</span>{/if}
        </p>
        <div class="quickactions">
          <button onclick={() => onNavigate("editor")}>Edit patch</button>
          <button onclick={() => onNavigate("patches")}>All patches</button>
        </div>
      {:else if connected && patches.length === 0}
        <p class="muted small">No patches yet. Use the Patches tab to create one.</p>
        <div class="quickactions">
          <button onclick={() => onNavigate("patches")}>Open Patches</button>
        </div>
      {:else if connected}
        <p class="muted small">The pedal didn't report a current patch.</p>
      {:else}
        <p class="muted small">Connect to see what's loaded.</p>
      {/if}
    </div>

    <!-- Live device mirror -->
    <DeviceMirror
      {connected}
      current={deviceInfo ? { bank: deviceInfo.bank, slot: deviceInfo.slot } : null}
      patchName={activePatch?.name ?? ""}
      {lastActivityMs}
      {stats}
    />

    <!-- Quick actions card -->
    <div class="card quick">
      <div class="cardhead">
        <span class="cardtitle">Quick actions</span>
      </div>
      <div class="actiongrid">
        <button onclick={() => onNavigate("patches")}>
          <span class="ico">▣</span><span class="lbl">Patches</span>
        </button>
        <button onclick={() => onNavigate("editor")}>
          <span class="ico">✎</span><span class="lbl">Editor</span>
        </button>
        <button onclick={() => onNavigate("tft")}>
          <span class="ico">▭</span><span class="lbl">Screen layout</span>
        </button>
        <button onclick={() => onNavigate("settings")}>
          <span class="ico">⚙</span><span class="lbl">Settings</span>
        </button>
        <button onclick={() => onNavigate("learn")}>
          <span class="ico">↻</span><span class="lbl">MIDI Learn</span>
        </button>
        <button onclick={() => onNavigate("maint")}>
          <span class="ico">⊕</span><span class="lbl">Maintenance</span>
        </button>
      </div>
    </div>
  </div>
</div>

<style>
  .dashboard { padding: 0.5rem 0.25rem 2rem; }
  .hero { margin: 0.5rem 0 1.75rem; }
  .hero h1 {
    margin: 0 0 0.3rem;
    font-size: 1.7rem; font-weight: 600;
    color: var(--text); letter-spacing: -0.01em;
  }
  .hero .lede { margin: 0; color: var(--text-muted); font-size: 0.95rem; }

  .grid {
    display: grid;
    grid-template-columns: repeat(auto-fit, minmax(280px, 1fr));
    gap: 0.85rem;
  }

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

  /* connection pill in dashboard card */
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

  dl { margin: 0; display: grid; gap: 0.45rem; }
  dl > div { display: flex; align-items: baseline; justify-content: space-between; gap: 0.5rem; font-size: 0.82rem; }
  dt { color: var(--text-dim); }
  dd { margin: 0; color: var(--text); font-variant-numeric: tabular-nums; }

  .profilename, .patchname {
    color: var(--text); font-weight: 600; font-size: 1.05rem;
    margin: 0 0 0.25rem;
    letter-spacing: -0.005em;
  }

  .metrics {
    display: grid; grid-template-columns: 1fr 1fr; gap: 0.4rem;
    margin-top: 0.85rem; padding-top: 0.85rem;
    border-top: 1px solid var(--border);
  }
  .metrics > div {
    display: flex; flex-direction: column; gap: 0.1rem;
  }
  .metrics b { color: var(--accent); font-size: 1.15rem; font-weight: 600; font-variant-numeric: tabular-nums; }
  .metrics span { color: var(--text-dim); font-size: 0.72rem; text-transform: uppercase; letter-spacing: 0.06em; }

  .quickactions { display: flex; gap: 0.4rem; margin-top: 0.85rem; }
  .quickactions button {
    background: var(--bg-hover); color: var(--text); border: 1px solid var(--border-strong);
    padding: 0.4rem 0.75rem; border-radius: 5px; cursor: pointer;
    font-size: 0.78rem; font-family: inherit;
    transition: all 0.12s ease;
  }
  .quickactions button:hover { background: var(--bg-hover); border-color: var(--border-strong); color: var(--text); }

  .actiongrid {
    display: grid; grid-template-columns: repeat(3, 1fr); gap: 0.5rem;
  }
  .actiongrid button {
    display: flex; flex-direction: column; align-items: center; gap: 0.35rem;
    background: var(--bg-hover); color: var(--text); border: 1px solid var(--border-strong);
    padding: 0.7rem 0.5rem; border-radius: 6px; cursor: pointer;
    font-family: inherit; transition: all 0.12s ease;
  }
  .actiongrid button:hover {
    background: var(--accent-bg); border-color: var(--accent-border); color: var(--accent);
  }
  .actiongrid .ico { font-size: 1.05rem; opacity: 0.8; }
  .actiongrid .lbl { font-size: 0.74rem; }

  .dirty { color: var(--warn); }
  .muted { color: var(--text-muted); }
  .small { font-size: 0.85rem; line-height: 1.5; }
  .muted.small { margin: 0; }
</style>
