<script lang="ts">
  import { onMount } from "svelte";
  import { pushFirmware, type FirmwarePushState } from "../lib/firmware-push";

  type Props = { onClose: () => void; source?: string };
  let { onClose, source }: Props = $props();

  // Named `pushState` instead of plain `state` because Svelte 4 store
  // heuristics in svelte-check flag every `state.foo` reference as a
  // store dereference; the rename sidesteps that and is more descriptive
  // anyway.
  let pushState: FirmwarePushState = $state({
    phase: "idle",
    progress: { total: 0, done: 0, current: "", log: [] },
    error: "",
  });
  let started = $state(false);

  onMount(async () => {
    if (started) return;
    started = true;
    try {
      await pushFirmware(s => (pushState = s), { source });
    } catch { /* pushState.error is already populated */ }
  });

  const pct = $derived(pushState.progress.total > 0
    ? Math.round(pushState.progress.done / pushState.progress.total * 100)
    : 0);
</script>

<div class="overlay" role="presentation"></div>
<div class="modal" role="dialog" aria-modal="true">
  <header>
    <h2>
      {#if pushState.phase === "backing-up"}Backing up pedal state
      {:else if pushState.phase === "listing"}Preparing firmware
      {:else if pushState.phase === "pushing"}Updating firmware - {pct}%
      {:else if pushState.phase === "rebooting"}Rebooting pedal
      {:else if pushState.phase === "done"}Firmware updated
      {:else if pushState.phase === "error"}Update failed
      {:else}Updating firmware{/if}
    </h2>
    {#if pushState.phase === "done" || pushState.phase === "error"}
      <button class="close" onclick={onClose} aria-label="Close">×</button>
    {/if}
  </header>

  <div class="content">
    <div class="progress">
      <div class="bar"
           class:done={pushState.phase === "done"}
           class:err={pushState.phase === "error"}
           style:width="{pushState.phase === 'done' ? 100 : pct}%"></div>
    </div>
    <p class="status">
      {#if pushState.phase === "backing-up"}
        Saving every profile on the pedal to a timestamped folder under
        Documents/bosun-backups/ - so your patches survive the update.
      {:else if pushState.phase === "pushing" && pushState.progress.current}
        Pushing {pushState.progress.current}  ({pushState.progress.done} of {pushState.progress.total})
      {:else if pushState.phase === "listing"}
        Reading bundled firmware tree from the editor
      {:else if pushState.phase === "rebooting"}
        Sending REBOOT - the pedal will be back in a couple of seconds
      {:else if pushState.phase === "done"}
        All files pushed and the pedal is back up. You can close this window.
      {:else if pushState.phase === "error"}
        {pushState.error || "Something went wrong. See the log below for details."}
      {/if}
    </p>

    {#if pushState.progress.log.length > 0}
      <details>
        <summary>Log ({pushState.progress.log.length})</summary>
        <pre>{pushState.progress.log.join("\n")}</pre>
      </details>
    {/if}

    {#if pushState.phase === "error"}
      <div class="actions">
        <button onclick={onClose}>Close</button>
      </div>
    {:else if pushState.phase === "done"}
      <div class="actions">
        <button class="primary" onclick={onClose}>Done</button>
      </div>
    {/if}
  </div>
</div>

<style>
  .overlay {
    position: fixed; inset: 0;
    background: rgba(8, 10, 14, 0.72);
    backdrop-filter: blur(6px); -webkit-backdrop-filter: blur(6px);
    z-index: 90;
    animation: fadein 0.18s ease;
  }
  .modal {
    position: fixed; top: 50%; left: 50%; transform: translate(-50%, -50%);
    background: var(--bg-card); border: 1px solid var(--border); border-radius: 10px;
    z-index: 100; width: min(560px, 92vw); max-height: 85vh; overflow: auto;
    box-shadow: var(--shadow-modal);
    animation: popin 0.2s cubic-bezier(0.16, 1, 0.3, 1);
  }
  @keyframes fadein { from { opacity: 0; } }
  @keyframes popin {
    from { opacity: 0; transform: translate(-50%, -48%) scale(0.96); }
    to   { opacity: 1; transform: translate(-50%, -50%) scale(1); }
  }
  header {
    display: flex; align-items: center; justify-content: space-between;
    padding: 0.95rem 1.15rem; border-bottom: 1px solid var(--border);
    background: linear-gradient(180deg, var(--bg-card) 0%, var(--bg-card) 100%);
    border-radius: 10px 10px 0 0;
  }
  h2 {
    margin: 0; font-size: 0.95rem; color: var(--text); font-weight: 600;
    letter-spacing: -0.005em;
    font-variant-numeric: tabular-nums;
  }
  .close {
    background: transparent; border: none; color: var(--text-dim);
    font-size: 1.4rem; cursor: pointer; padding: 0 0.4rem; line-height: 1;
    transition: color 0.15s ease;
  }
  .close:hover { color: var(--text); }
  .content { padding: 1.15rem; color: var(--text); }
  .progress {
    background: var(--bg); border: 1px solid var(--border);
    border-radius: 999px; overflow: hidden; height: 8px;
    margin-bottom: 0.85rem;
  }
  .bar {
    height: 100%;
    background: linear-gradient(90deg, var(--accent) 0%, var(--accent-hover-border) 100%);
    transition: width 0.25s ease;
  }
  .bar.done { background: var(--accent); }
  .bar.err  { background: var(--err); }
  .status {
    color: var(--text-muted); font-size: 0.85rem; line-height: 1.5;
    margin: 0 0 0.85rem;
    min-height: 1.2rem;
  }
  details summary {
    cursor: pointer; color: var(--text-dim); font-size: 0.78rem;
    text-transform: uppercase; letter-spacing: 0.06em;
    padding: 0.35rem 0; outline: none;
  }
  details summary:hover { color: var(--text-muted); }
  pre {
    background: var(--bg); border: 1px solid var(--border); border-radius: 4px;
    padding: 0.55rem 0.7rem; font-size: 0.74rem;
    max-height: 200px; overflow: auto; color: var(--text-muted);
    font-family: ui-monospace, Consolas, monospace;
    margin: 0.4rem 0 0;
  }
  .actions {
    display: flex; gap: 0.5rem; justify-content: flex-end;
    margin-top: 1rem;
  }
  button {
    background: var(--bg-hover); color: var(--text); border: 1px solid var(--border-strong);
    padding: 0.5rem 1rem; border-radius: 5px; cursor: pointer;
    font-size: 0.85rem; font-family: inherit;
    transition: all 0.15s ease;
  }
  button:hover { background: var(--bg-hover); }
  button.primary {
    background: var(--accent-bg); color: var(--accent); border-color: var(--accent-border); font-weight: 600;
  }
  button.primary:hover { background: var(--accent-hover-bg); border-color: var(--accent-hover-border); }
</style>
