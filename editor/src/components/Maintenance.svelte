<script lang="ts">
  import { onMount, onDestroy } from "svelte";
  import { invoke } from "@tauri-apps/api/core";
  import { cmd, waitForReboot, type DeviceStats, type FirmwareFile } from "../lib/protocol";

  type Props = { connected: boolean; onClose: () => void };
  let { connected, onClose }: Props = $props();

  let stats = $state<DeviceStats | null>(null);
  let statsErr = $state<string>("");
  let pushing = $state(false);
  let rebooting = $state(false);
  let progress = $state<{ done: number; total: number; current: string }>({ done: 0, total: 0, current: "" });
  let pushLog = $state<string[]>([]);

  /** base64 chunk size in characters. 512 ≈ 384 binary bytes, fits CDC buffer. */
  const CHUNK_B64 = 512;
  const STATS_POLL_MS = 3000;

  let statsTimer: ReturnType<typeof setInterval> | null = null;

  onMount(() => {
    if (connected) startStatsPoll();
  });
  onDestroy(() => stopStatsPoll());

  $effect(() => {
    if (connected) startStatsPoll();
    else stopStatsPoll();
  });

  function startStatsPoll() {
    if (statsTimer) return;
    pollStats();
    statsTimer = setInterval(pollStats, STATS_POLL_MS);
  }
  function stopStatsPoll() {
    if (statsTimer) { clearInterval(statsTimer); statsTimer = null; }
  }
  async function pollStats() {
    if (pushing || rebooting) return;
    try {
      const s = await cmd.getStats();
      stats = s;
      statsErr = "";
    } catch (e) {
      // Timeouts are a race against the next poll - don't flash an error if
      // we already have data from a prior tick. Only show real connection drops.
      const msg = String(e);
      if (!stats || !msg.includes("timeout")) statsErr = msg;
    }
  }

  function log(s: string) {
    const next = [...pushLog, s];
    if (next.length > 60) next.splice(0, next.length - 60);
    pushLog = next;
  }

  async function doPush() {
    pushing = true;
    stopStatsPoll();
    pushLog = [];
    progress = { done: 0, total: 0, current: "" };
    try {
      log("Listing firmware files...");
      const files = await invoke<FirmwareFile[]>("list_firmware_files");
      log(`${files.length} files, total ${humanBytes(files.reduce((a, f) => a + f.size, 0))}`);
      progress.total = files.length;
      for (const file of files) {
        progress.current = file.dst;
        const b64 = await invoke<string>("read_firmware_file_b64", { rel: file.rel });
        await cmd.putFileBegin(file.dst);
        for (let i = 0; i < b64.length; i += CHUNK_B64) {
          await cmd.putFileChunk(file.dst, b64.slice(i, i + CHUNK_B64));
        }
        await cmd.putFileEnd(file.dst);
        progress.done += 1;
        log(`✓ ${file.dst}  (${humanBytes(file.size)})`);
      }
      log("All files pushed.");
    } catch (e) {
      log("ERROR: " + String(e));
    } finally {
      pushing = false;
      startStatsPoll();
    }
  }

  async function doReboot() {
    rebooting = true;
    stopStatsPoll();
    try {
      await cmd.reboot();
      log("REBOOT sent; waiting for firmware to come back...");
    } catch (e) {
      // expected - port goes away during reset
    }
    const ok = await waitForReboot(15000);
    log(ok ? "Firmware back online." : "Firmware not back yet - reconnect manually if needed.");
    rebooting = false;
    startStatsPoll();
  }

  function humanBytes(n: number): string {
    if (n < 1024) return `${n} B`;
    if (n < 1024 * 1024) return `${(n / 1024).toFixed(1)} KB`;
    return `${(n / 1024 / 1024).toFixed(2)} MB`;
  }
  function humanMs(ms: number): string {
    const s = Math.floor(ms / 1000);
    if (s < 60) return `${s}s`;
    const m = Math.floor(s / 60); const r = s % 60;
    if (m < 60) return `${m}m ${r}s`;
    const h = Math.floor(m / 60); const rm = m % 60;
    return `${h}h ${rm}m`;
  }
</script>

<div class="overlay" onclick={onClose} role="presentation"></div>
<div class="modal" role="dialog" aria-modal="true">
  <header>
    <h2>Pedal maintenance</h2>
    <button class="close" onclick={onClose} aria-label="Close">×</button>
  </header>

  <div class="content">
    {#if !connected}
      <p class="muted">Not connected. Connect the pedal first.</p>
    {:else}
      <section>
        <h3>Live stats</h3>
        {#if stats}
          <div class="stats">
            <div><span>uptime</span><b>{humanMs(stats.uptime_ms)}</b></div>
            <div><span>mem free</span><b>{humanBytes(stats.mem_free)}</b></div>
            <div><span>mem alloc</span><b>{humanBytes(stats.mem_alloc)}</b></div>
            <div><span>loop iters</span><b>{stats.loop_iters.toLocaleString()}</b></div>
            <div><span>MIDI rx</span><b>{stats.midi_rx_count}</b></div>
            <div><span>MIDI tx</span><b>{stats.midi_tx_count}</b></div>
            <div><span>cmds handled</span><b>{stats.protocol_cmd_count}</b></div>
            <div><span>current patch</span><b>{stats.current.bank}/{stats.current.slot}</b></div>
          </div>
        {:else if statsErr}
          <p class="err">{statsErr}</p>
        {:else}
          <p class="muted">…</p>
        {/if}
      </section>

      <section>
        <h3>Update firmware (OTA)</h3>
        <p class="muted small">
          Pushes the bundled <code>firmware/</code> tree to the pedal over USB
          CDC. No drive needed - works in performance mode.
        </p>
        <div class="row">
          <button class="primary" onclick={doPush} disabled={pushing || rebooting}>
            {pushing ? `Pushing… ${progress.done}/${progress.total}` : "Push firmware"}
          </button>
          <button onclick={doReboot} disabled={pushing || rebooting}>
            {rebooting ? "Rebooting…" : "Reboot"}
          </button>
        </div>
        {#if pushing && progress.current}
          <div class="progress">
            <div class="bar" style:width="{progress.total > 0 ? (progress.done / progress.total * 100) : 0}%"></div>
          </div>
          <p class="curr">{progress.current}</p>
        {/if}
        {#if pushLog.length > 0}
          <pre>{pushLog.join("\n")}</pre>
        {/if}
      </section>
    {/if}
  </div>
</div>

<style>
  .overlay { position: fixed; inset: 0; background: var(--overlay-bg); z-index: 90; }
  .modal {
    position: fixed; top: 50%; left: 50%; transform: translate(-50%, -50%);
    background: var(--bg-card); border: 1px solid var(--border); border-radius: 8px;
    z-index: 100; width: min(640px, 92vw); max-height: 88vh; overflow: auto;
    box-shadow: var(--shadow-modal);
  }
  header { display: flex; align-items: center; justify-content: space-between; padding: 0.85rem 1rem; border-bottom: 1px solid var(--border); }
  h2 { margin: 0; font-size: 1.1rem; color: var(--text); }
  h3 { color: var(--text-muted); margin: 1rem 0 0.5rem; font-size: 0.85rem; text-transform: uppercase; letter-spacing: 0.05em; }
  .close { background: transparent; border: none; color: var(--text-muted); font-size: 1.4rem; cursor: pointer; padding: 0 0.3rem; }
  .content { padding: 1rem; color: var(--text); }
  section { margin-bottom: 1.25rem; }

  .stats { display: grid; grid-template-columns: repeat(2, 1fr); gap: 0.4rem 1rem; font-size: 0.85rem; }
  .stats > div { display: flex; justify-content: space-between; padding: 0.3rem 0.6rem; background: var(--bg); border-radius: 4px; }
  .stats > div span { color: var(--text-dim); }
  .stats > div b { color: var(--text); font-weight: 500; font-family: ui-monospace, Consolas, monospace; }

  .row { display: flex; gap: 0.5rem; margin-bottom: 0.5rem; }
  button { background: var(--bg-hover); color: var(--text); border: 1px solid var(--border-strong); padding: 0.45rem 0.9rem; border-radius: 4px; cursor: pointer; font-size: 0.85rem; }
  button:hover:not(:disabled) { background: var(--bg-hover); }
  button:disabled { opacity: 0.45; cursor: not-allowed; }
  button.primary { background: var(--accent-bg); color: var(--accent); border-color: var(--accent-border); font-weight: 600; }
  button.primary:hover:not(:disabled) { background: var(--accent-hover-bg); }

  .progress { background: var(--bg); border-radius: 3px; overflow: hidden; height: 6px; margin-top: 0.5rem; }
  .bar { height: 100%; background: var(--accent); transition: width 0.2s; }
  .curr { color: var(--warn-text); font-family: ui-monospace, Consolas, monospace; font-size: 0.75rem; margin: 0.4rem 0 0; }

  code { background: var(--bg); padding: 0.1rem 0.4rem; border-radius: 3px; color: var(--warn-text); font-family: ui-monospace, Consolas, monospace; }
  pre { background: var(--bg); border: 1px solid var(--border); border-radius: 4px; padding: 0.5rem; font-size: 0.75rem; max-height: 200px; overflow: auto; color: var(--text-muted); margin-top: 0.5rem; }
  .muted { color: var(--text-muted); }
  .small { font-size: 0.85rem; }
  .err { color: var(--err); font-family: ui-monospace, Consolas, monospace; font-size: 0.8rem; }
</style>
