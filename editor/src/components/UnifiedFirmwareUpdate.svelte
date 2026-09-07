<script lang="ts">
  import { onMount, onDestroy } from "svelte";
  import {
    abortUnifiedUpload, checkUnifiedRecovery, forgetUpdate, isTerminalUpdate, readPendingUpdate,
    resumeUnifiedUpdate, startUnifiedUpdate, UnifiedUpdateConnectionError,
    type BundledUpdateManifest, type HubUpdateStatus, type PendingUpdate,
  } from "../lib/unified-update";

  type Props = {
    manifest: BundledUpdateManifest | null;
    installed?: string;
    endpoint: string;
    isCurrentConnection: () => boolean;
    onClose: () => void;
    onJob: (pending: PendingUpdate | null) => void;
    onComplete: () => void;
  };
  let { manifest, installed, endpoint, isCurrentConnection, onClose, onJob, onComplete }: Props = $props();
  let status = $state<HubUpdateStatus>({ type: "HUB_UPDATE", phase: "review" });
  let working = $state(false);
  let waiting = $state(false);
  let clientError = $state("");
  let alive = true;
  let completed = false;
  let poll: ReturnType<typeof setTimeout> | undefined;

  const labels: Record<string, string> = {
    review: "Update Bosun", uploading: "Sending update to Raspberry Pi", queued: "Preparing update",
    validating: "Checking update package", "backing-up": "Backing up the pedal",
    migrating: "Preparing your profiles", flashing: "Installing Bosun", rebooting: "Restarting the pedal",
    verifying: "Checking firmware and settings", done: "Bosun updated", error: "Update stopped",
    "rolled-back": "Previous firmware restored", "recovery-required": "Recovery required", aborted: "Upload cancelled",
  };
  const terminal = $derived(isTerminalUpdate(status));
  const percent = $derived(status.size ? Math.min(100, Math.round((status.received ?? 0) / status.size * 100)) : 0);
  const hasPendingHere = () => readPendingUpdate()?.endpoint === endpoint;
  const options = () => ({
    endpoint, isCurrentConnection: () => alive && isCurrentConnection(),
    onStatus: acceptStatus,
    onJob: (pending: PendingUpdate) => { if (alive) onJob(pending); },
  });

  function acceptStatus(next: HubUpdateStatus) {
    if (!alive) return;
    status = next;
    waiting = false;
    clientError = "";
    if (next.phase === "done" && !completed) {
      completed = true;
      onComplete();
    }
  }

  function schedulePoll() {
    if (!alive || isTerminalUpdate(status) || !readPendingUpdate()) return;
    if (poll !== undefined) clearTimeout(poll);
    poll = setTimeout(() => { poll = undefined; void run("resume"); }, 1000);
  }

  async function run(action: "start" | "resume" | "recover") {
    if (working || !alive) return;
    working = true;
    clientError = "";
    const previous = status;
    if (action === "recover") {
      // Keep polling after a lost recovery acknowledgement, even though the
      // last confirmed server state was terminal (recovery-required).
      status = { ...status, phase: "verifying", error: undefined, detail: undefined };
    }
    let retry = true;
    try {
      if (action === "recover") await checkUnifiedRecovery(options());
      else if (action === "resume") await resumeUnifiedUpdate(options());
      else await startUnifiedUpdate(options());
    } catch (error) {
      if (!alive) return;
      waiting = error instanceof UnifiedUpdateConnectionError;
      if (action === "recover" && !waiting) status = previous;
      clientError = String(error);
      retry = waiting;
    } finally {
      working = false;
      if (retry) schedulePoll();
    }
  }

  async function cancelUpload() {
    if (working) return;
    working = true;
    if (poll !== undefined) { clearTimeout(poll); poll = undefined; }
    try {
      acceptStatus(await abortUnifiedUpload(endpoint, isCurrentConnection));
      onJob(null);
    } catch (error) { clientError = String(error); }
    finally { working = false; }
  }

  function close() {
    // Unresolved recovery must remain discoverable after closing or restarting
    // the app: its full-flash backup is still needed and the hub blocks updates.
    if (terminal && status.phase !== "recovery-required" && status.job) {
      forgetUpdate(status.job); onJob(null);
    }
    onClose();
  }

  onMount(() => {
    if (readPendingUpdate()?.endpoint === endpoint) void run("resume");
  });
  onDestroy(() => {
    alive = false;
    if (poll !== undefined) clearTimeout(poll);
    // Closing this window never cancels a committed update. The hub owns it,
    // and the saved job can be resumed from this app after reconnecting.
  });
</script>

<div class="backdrop" role="presentation"></div>
<div class="update-dialog" role="dialog" aria-modal="true" aria-labelledby="bosun-update-title">
  <header><h2 id="bosun-update-title">{waiting ? "Waiting for Raspberry Pi" : status.phase === "error" && status.recovery_verified ? "Pedal recovery verified" : labels[status.phase] ?? "Updating Bosun"}</h2></header>
  <div class="content" aria-live="polite">
    {#if status.phase === "review" && !hasPendingHere()}
      <p>Update Bosun {installed ? `from ${installed} ` : ""}to <strong>{manifest?.release}</strong>.</p>
      <p>Your profiles and settings are backed up and transferred automatically. The pedal will be unavailable until the update finishes.</p>
      <p class="muted">Keep the pedal and Raspberry Pi powered on until the update is complete.</p>
    {:else if waiting}
      <p>The Raspberry Pi may still be updating the pedal. Reconnect to <strong>{endpoint.replace(/^tcp:\/\//, "")}</strong> to check progress.</p>
      <p class="muted">Closing this window does not stop an update that has already started.</p>
    {:else if status.phase === "done"}
      <p>Bosun {status.release ?? manifest?.release} is installed. Your profiles have been preserved and the pedal has passed verification.</p>
    {:else if status.phase === "rolled-back"}
      <p>The update could not be completed. The Raspberry Pi restored the previous firmware and settings.</p>
    {:else if status.phase === "recovery-required"}
      <p>The update could not finish and the pedal needs manual recovery. Keep the job details.</p>
      <p>After physically reconnecting the pedal, check its firmware and settings. This check does not write to the pedal.</p>
    {:else if status.phase === "error" && status.recovery_verified}
      <p>The original firmware and settings match the saved state. The update was not installed. You can try again.</p>
    {:else if status.phase === "error"}
      <p>The update stopped. {status.detail ?? "Check the error below before trying again."}</p>
    {:else if status.phase === "aborted"}
      <p>The upload was cancelled before installation started.</p>
    {:else if status.phase === "uploading"}
      <progress max="100" value={percent} aria-label="Update upload progress"></progress>
      <p>{percent}% sent to the Raspberry Pi.</p>
    {:else}
      <p>{labels[status.phase] ?? status.phase}…</p>
      <p class="muted">The Raspberry Pi is managing the update. Keep both devices powered on.</p>
    {/if}
    {#if status.error}<p class="error">{status.recovery_verified ? "Original update error: " : ""}{status.error}</p>{/if}
    {#if status.detail && status.phase !== "error"}<p class="muted">{status.detail}</p>{/if}
    {#if clientError && !waiting}<p class="error" role="alert">{clientError}</p>{/if}
    {#if status.backup}<p class="backup">Backup on Raspberry Pi: <code>{status.backup}</code></p>{/if}
  </div>
  <footer>
    {#if status.phase === "review" && !hasPendingHere()}
      <button disabled={working} onclick={close}>Cancel</button>
      <button class="primary" disabled={working || !manifest || !isCurrentConnection()} onclick={() => run("start")}>{working ? "Preparing…" : "Update"}</button>
    {:else}
      {#if status.phase === "uploading" && !working}<button onclick={cancelUpload}>Cancel upload</button>{/if}
      {#if status.phase === "recovery-required"}
        <button class="primary" disabled={working || !hasPendingHere() || !isCurrentConnection()} onclick={() => run("recover")}>Check recovery</button>
      {/if}
      {#if clientError && !waiting && !terminal}<button disabled={working || !isCurrentConnection()} onclick={() => run("resume")}>Check again</button>{/if}
      <button onclick={close}>{terminal && status.phase !== "recovery-required" ? "Done" : "Close"}</button>
    {/if}
  </footer>
</div>

<style>
  .backdrop { position: fixed; inset: 0; background: rgba(8, 10, 14, 0.72); backdrop-filter: blur(6px); z-index: 90; }
  .update-dialog { position: fixed; top: 50%; left: 50%; transform: translate(-50%, -50%); z-index: 100; width: min(560px, 92vw); max-height: 85vh; overflow: auto; background: var(--bg-card); border: 1px solid var(--border); border-radius: 10px; box-shadow: var(--shadow-modal); color: var(--text); }
  header { padding: 1rem 1.2rem; border-bottom: 1px solid var(--border); }
  h2 { margin: 0; font-size: 1rem; }
  .content { padding: 0.5rem 1.2rem; font-size: 0.88rem; line-height: 1.6; }
  .muted { color: var(--text-muted); }
  .error { color: var(--err); overflow-wrap: anywhere; }
  .backup { font-size: 0.8rem; overflow-wrap: anywhere; }
  progress { width: 100%; margin-top: 1rem; accent-color: var(--accent); }
  footer { display: flex; justify-content: flex-end; gap: 0.6rem; padding: 1rem 1.2rem; }
  button { background: var(--bg-hover); color: var(--text); border: 1px solid var(--border-strong); border-radius: 4px; padding: 0.45rem 0.9rem; cursor: pointer; }
  button.primary { background: var(--accent); color: var(--bg); border-color: var(--accent); }
  button:disabled { opacity: 0.45; cursor: not-allowed; }
</style>
