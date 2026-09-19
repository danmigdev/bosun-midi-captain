<script lang="ts">
  import { onMount, onDestroy } from "svelte";
  import { invoke } from "@tauri-apps/api/core";
  import { startUsbUpdate, startFactoryInstall, recoverUsbUpdate, usbUpdateStatus, usbUpdateRunning, usbUpdateTerminal, type UsbUpdateJob } from "../lib/usb-update";
  type Props = {
    port: string; installed?: string; version: string; job: UsbUpdateJob | null;
    canStart: () => boolean; onPreparing: () => void;
    candidateId?: string;
    onJob: (job: UsbUpdateJob | null) => void; onClose: () => void;
  };
  let { port, installed, version, job, canStart, onPreparing, onJob, onClose, candidateId }: Props = $props();
  let working = $state(false);
  let error = $state("");
  let waiting = $state(false);
  let ignoredJobId: string | undefined;
  let alive = true;
  let poll: ReturnType<typeof setTimeout> | undefined;
  let dialog: HTMLDialogElement;
  const running = $derived(working || waiting || usbUpdateRunning(job));
  const labels: Record<string,string> = {
    preflight: "Checking the Captain", bootloader: "Starting USB bootloader", backup: "Backing up the Captain",
    writing: "Installing firmware", rebooting: "Verifying the update", restoring: "Restoring backup",
    done: "Bosun updated", restored: "Original firmware restored", failed: "Update stopped", "recovery-required": "Restore the Captain",
  };
  function schedulePoll() {
    if (!alive || usbUpdateTerminal(job) || (!job && !waiting)) return;
    poll = setTimeout(async () => {
      try {
        const next = await usbUpdateStatus();
        if (alive) { waiting = false; onJob(next?.id === ignoredJobId ? null : next); }
      }
      catch (e) { if (alive) error = String(e); }
      schedulePoll();
    }, 750);
  }
  async function run(recover: boolean) {
    if (running || (!recover && !canStart())) return;
    working = true; error = "";
    onPreparing();
    if (poll !== undefined) clearTimeout(poll);
    let previousId: string | undefined;
    try {
      previousId = (await usbUpdateStatus())?.id;
      ignoredJobId = recover ? undefined : previousId;
      if (recover) { await recoverUsbUpdate(); onJob(await usbUpdateStatus()); }
      else onJob(candidateId ? await startFactoryInstall(candidateId) : await startUsbUpdate(port));
    } catch (e) {
      error = String(e);
      // A lost IPC reply may still have started the worker. Status is read-only:
      // never replay the start or recovery command as part of polling.
      try {
        const next = await usbUpdateStatus();
        onJob(recover || next?.id !== previousId ? next : null);
      } catch { waiting = true; }
    } finally { working = false; schedulePoll(); }
  }
  async function showBackup() {
    if (!job?.backup_path) return;
    try { await invoke("open_in_file_manager", { path: job.backup_path.replace(/[\\/][^\\/]+$/, "") }); }
    catch (e) { error = String(e); }
  }
  onMount(() => { dialog.showModal(); if (job) schedulePoll(); });
  onDestroy(() => { alive = false; if (poll !== undefined) clearTimeout(poll); });
</script>

<dialog bind:this={dialog} class="update-dialog" aria-labelledby="usb-update-title"
        oncancel={event => { event.preventDefault(); if (!running) onClose(); }}>
  <header><h2 id="usb-update-title">{job ? (job.mode === "install" && job.phase === "done" ? "Bosun installed" : labels[job.phase]) : candidateId ? "First Bosun installation" : "Update Bosun over USB"}</h2></header>
  <div class="content">
    {#if !job}
      {#if candidateId}
      <p>Install <strong>{version}</strong>. Bosun saves and verifies the original firmware, installs native firmware and prepares storage for new profiles.</p>
      <p>Press Install to start. Keep the Captain on the same USB port and keep the computer powered on.</p>
      {:else}
      <p>Install <strong>{version}</strong> on the Captain connected to <strong>{port}</strong>{installed ? ` (currently ${installed})` : ""}.</p>
      <p>Bosun saves a complete recovery backup on this computer, preserves your saved profiles, and checks the firmware and settings after restarting the Captain.</p>
      <p>Save your edits first. Keep the Captain on the same USB port and keep this computer awake until the update finishes. MIDI and Stage pause during the update.</p>
      {/if}
    {:else}
      <p role="status">{job.message}</p>
      {#if usbUpdateRunning(job)}
        {#if job.phase === "writing" || job.phase === "restoring" || (job.mode === "install" && job.phase === "backup")}
          <progress max="100" value={job.percent} aria-label="USB firmware progress"></progress>
          <p>{job.percent}%</p>
        {:else}<progress aria-label="USB firmware progress"></progress>{/if}
        <p class="muted">Keep the Captain connected and powered. Bosun will allow this app to close when the operation has finished.</p>
      {:else if job.phase === "recovery-required"}
        <p>Connect the same Captain to the same USB port in BOOTSEL mode, then choose <strong>Restore backup</strong>. This restores the firmware and settings saved before this update.</p>
      {/if}
      {#if job.backup_path}<p class="backup">Backup: <code>{job.backup_path}</code></p>{/if}
    {/if}
    {#if error}<p class="error" role="alert">{error}</p>{/if}
  </div>
  <footer>
    {#if job?.backup_path}<button onclick={showBackup}>Show backup</button>{/if}
    {#if job?.phase === "recovery-required"}<button class="primary" disabled={working} onclick={() => run(true)}>Restore backup</button>{/if}
    <button disabled={running} onclick={onClose}>{job ? "Close" : "Cancel"}</button>
    {#if !job}<button class="primary" disabled={working || !canStart()} onclick={() => run(false)}>{working ? "Preparing…" : candidateId ? "Install" : "Update"}</button>{/if}
  </footer>
</dialog>

<style>
  .update-dialog { width: min(560px, 92vw); max-height: 85vh; overflow: auto; padding: 0; background: var(--bg-card); border: 1px solid var(--border); border-radius: 10px; box-shadow: var(--shadow-modal); color: var(--text); }
  .update-dialog::backdrop { background: rgba(8, 10, 14, 0.72); backdrop-filter: blur(6px); }
  header { padding: 1rem 1.2rem; border-bottom: 1px solid var(--border); }
  h2 { margin: 0; font-size: 1rem; }
  .content { padding: 0.5rem 1.2rem; font-size: 0.88rem; line-height: 1.6; }
  .muted { color: var(--text-muted); }
  .error { color: var(--err); overflow-wrap: anywhere; }
  .backup { font-size: 0.8rem; overflow-wrap: anywhere; }
  progress { width: 100%; margin-top: 1rem; accent-color: var(--accent); }
  footer { display: flex; flex-wrap: wrap; justify-content: flex-end; gap: 0.6rem; padding: 1rem 1.2rem; }
  button { background: var(--bg-hover); color: var(--text); border: 1px solid var(--border-strong); border-radius: 4px; padding: 0.45rem 0.9rem; cursor: pointer; }
  button.primary { background: var(--accent); color: var(--bg); border-color: var(--accent); }
  button:disabled { opacity: 0.45; cursor: not-allowed; }
</style>
