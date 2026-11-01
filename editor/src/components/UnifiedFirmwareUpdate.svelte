<script lang="ts">
  import { onMount, onDestroy } from "svelte";
  import { invoke } from "@tauri-apps/api/core";
  import { firmwareLabel } from "../lib/firmware-update";
  import { piInstallCommand } from "../lib/setup-guide";
  import {
    abortUnifiedUpload, checkUnifiedRecovery, confirmPiRelease, forgetUpdate, isTerminalUpdate,
    planUnifiedUpdate, PiVersionMismatchError, readCaptainFirmware, readPendingUpdate,
    resumeUnifiedUpdate, startPiUpdate, startUnifiedUpdate, unifiedUpdateAvailable, UnifiedUpdateConnectionError,
    type BundledPiUpdate, type BundledUpdateManifest, type HubInfo, type HubUpdateStatus, type PendingUpdate, type UpdatePlan,
  } from "../lib/unified-update";

  type Props = {
    manifest: BundledUpdateManifest | null;
    installed?: string;
    endpoint: string;
    /** HUB_INFO of the connected hub and the bundled signed Pi package. */
    hub?: HubInfo | null;
    piPackage?: BundledPiUpdate | null;
    isCurrentConnection: () => boolean;
    onClose: () => void;
    onJob: (pending: PendingUpdate | null) => void;
    onComplete: () => void;
  };
  let { manifest, installed, endpoint, hub = null, piPackage = null, isCurrentConnection, onClose, onJob, onComplete }: Props = $props();
  let status = $state<HubUpdateStatus>({ type: "HUB_UPDATE", phase: "review" });
  let working = $state(false);
  let waiting = $state(false);
  let clientError = $state("");
  // "pi" while the Raspberry Pi software job runs, then "captain".
  let stage = $state<"pi" | "captain">(savedStage());
  function savedStage(): "pi" | "captain" {
    const saved = readPendingUpdate();
    return saved?.endpoint === endpoint && saved.kind === "pi" ? "pi" : "captain";
  }
  // The plan confirmed in this window. A job resumed after an app restart has
  // none, so the pedal update then waits for a new confirmation.
  let chosen = $state<UpdatePlan | null>(null);
  let piMismatch = $state("");
  let piOnlyDone = $state(false);
  let captainReview = $state(false);
  let captainFirmware = $state<string | undefined>();
  let alive = true;
  let completed = false;
  let poll: ReturnType<typeof setTimeout> | undefined;

  const labels: Record<string, string> = {
    review: "Update Bosun", uploading: "Sending update to Raspberry Pi", queued: "Preparing update",
    validating: "Checking update package", "backing-up": "Backing up the pedal",
    flashing: "Installing Bosun", rebooting: "Restarting the pedal",
    verifying: "Checking firmware and settings", done: "Bosun updated", error: "Update stopped",
    "rolled-back": "Previous firmware restored", "recovery-required": "Recovery required", aborted: "Upload cancelled",
    installing: "Updating the Raspberry Pi",
  };
  const plan = $derived(planUnifiedUpdate(installed, manifest, hub, piPackage));
  const release = $derived(manifest?.release ?? "");
  const terminal = $derived(isTerminalUpdate(status));
  const piFailed = $derived(stage === "pi" && ["error", "rolled-back", "recovery-required"].includes(status.phase));
  const pedalOutdated = $derived(!!chosen?.captain || unifiedUpdateAvailable(captainFirmware ?? installed, manifest));
  const percent = $derived(status.size ? Math.min(100, Math.round((status.received ?? 0) / status.size * 100)) : 0);
  const title = $derived.by(() => {
    if (waiting) return "Waiting for Raspberry Pi";
    if (piMismatch) return "Raspberry Pi version mismatch";
    if (captainReview) return "Update the pedal";
    if (stage === "pi") {
      if (status.phase === "done") return piOnlyDone ? "Bosun updated" : "Raspberry Pi updated";
      if (status.phase === "rolled-back") return "Raspberry Pi update undone";
      if (piFailed) return "Raspberry Pi update stopped";
    }
    if (status.phase === "error" && status.recovery_verified) return "Pedal recovery verified";
    return labels[status.phase] ?? "Updating Bosun";
  });
  const hasPendingHere = () => readPendingUpdate()?.endpoint === endpoint;
  const options = () => ({
    endpoint, isCurrentConnection: () => alive && isCurrentConnection(),
    onStatus: acceptStatus,
    onJob: (pending: PendingUpdate) => { if (alive) onJob(pending); },
  });

  // One-time manual Pi update for hubs without self-update (0.8.5 and older).
  const piHost = $derived(endpoint.replace(/^tcp:\/\//, "").replace(/:\d+$/, ""));
  let piArchive = $state(""), piUser = $state(""), piCopied = $state(false), piSaving = $state(false);
  const piCommand = $derived(piInstallCommand(piArchive, piUser, piHost));

  async function savePiPackage() {
    piSaving = true;
    try { piArchive = await invoke<string | null>("export_pi_setup") || piArchive; piCopied = false; }
    catch (error) { clientError = String(error); }
    finally { piSaving = false; }
  }
  async function copyPiCommand() {
    try { await navigator.clipboard.writeText(piCommand); piCopied = true; }
    catch { clientError = "Select the command and copy it manually."; }
  }

  function acceptStatus(next: HubUpdateStatus) {
    if (!alive) return;
    status = next;
    waiting = false;
    clientError = "";
    if (next.phase === "done" && stage === "captain" && !completed) {
      completed = true;
      onComplete();
    }
  }

  function nextAction(): "resume" | "continue" | null {
    // A finished Pi job is followed by its version check and the pedal update.
    if (stage === "pi" && status.phase === "done") return piOnlyDone || captainReview || piMismatch ? null : "continue";
    return isTerminalUpdate(status) ? null : "resume";
  }

  function schedulePoll() {
    const action = nextAction();
    if (!alive || !action || !readPendingUpdate()) return;
    if (poll !== undefined) clearTimeout(poll);
    poll = setTimeout(() => { poll = undefined; void run(action); }, 1000);
  }

  async function start() {
    chosen = plan;
    stage = plan.pi ? "pi" : "captain";
    if (plan.pi) await startPiUpdate(options());
    else await startUnifiedUpdate(options());
  }

  async function continueAfterPi() {
    if (!manifest || readPendingUpdate()?.kind !== "pi") return;
    if (!isCurrentConnection()) throw new UnifiedUpdateConnectionError("Reconnect to the same Raspberry Pi to continue this update.");
    await confirmPiRelease(manifest.release);
    // Read the pedal through the restarted hub: it decides whether the pedal
    // still needs the update and gives the hub the identity it checks.
    const fw = await readCaptainFirmware();
    if (!alive) return;
    captainFirmware = fw;
    if (!(fw ? unifiedUpdateAvailable(fw, manifest) : !!chosen?.captain)) {
      piOnlyDone = true;
      if (!completed) { completed = true; onComplete(); }
    } else if (!chosen) {
      captainReview = true;
    } else {
      await startCaptain();
    }
  }

  async function startCaptain() {
    // The finished Pi job stays saved until the pedal job replaces it, so the
    // app keeps its update session through the switch.
    const pi = readPendingUpdate();
    if (pi?.kind === "pi" && pi.endpoint === endpoint) forgetUpdate(pi.job);
    stage = "captain";
    captainReview = false;
    piMismatch = "";
    status = { type: "HUB_UPDATE", phase: "queued" };
    try { await startUnifiedUpdate(options()); }
    catch (error) {
      if (alive && !readPendingUpdate()) {
        // No pedal job was created: offer it again and leave the update session.
        captainReview = true;
        status = { type: "HUB_UPDATE", phase: "review" };
        onJob(null);
      }
      throw error;
    }
  }

  async function run(action: "start" | "resume" | "recover" | "continue" | "captain") {
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
      else if (action === "continue") await continueAfterPi();
      else if (action === "captain") await startCaptain();
      else await start();
    } catch (error) {
      if (!alive) return;
      if (error instanceof PiVersionMismatchError) {
        piMismatch = error.message;
        retry = false;
        return;
      }
      waiting = error instanceof UnifiedUpdateConnectionError && !captainReview;
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
    // A Pi job has no pedal recovery, so any final Pi result can be dismissed.
    if (terminal && (status.phase !== "recovery-required" || stage === "pi") && status.job) {
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
  <header><h2 id="bosun-update-title">{title}</h2></header>
  <div class="content" aria-live="polite">
    {#if status.phase === "review" && !hasPendingHere() && !captainReview}
      {#if plan.pi && plan.captain}
        <p>Update Bosun on the Raspberry Pi and the pedal to <strong>{release}</strong>.</p>
        <p>The Raspberry Pi is updated first and restarts, then the pedal. Your profiles and settings are backed up and preserved automatically.</p>
      {:else if plan.pi}
        <p>Update Bosun on the Raspberry Pi {hub?.version ? `from ${hub.version} ` : ""}to <strong>{release}</strong>. The pedal is already up to date.</p>
        <p>The Raspberry Pi restarts during the update.</p>
      {:else}
        <p>Update Bosun {installed ? `from ${firmwareLabel(installed)} ` : ""}to <strong>{release}</strong>.</p>
        <p>Your profiles and settings are backed up and preserved automatically. The pedal will be unavailable until the update finishes.</p>
      {/if}
      <p class="muted">Keep the pedal and Raspberry Pi powered on until the update is complete.</p>
      {#if plan.piManual}
        <div class="notice">
          <p><strong>{hub?.pi_update ? "This Bosun build cannot update the Raspberry Pi." : "This Raspberry Pi cannot update itself yet."}</strong>
            It runs {hub?.version ? `Bosun ${hub.version}` : "an older Bosun version"}. Update it once with the Setup guide command; after that, Bosun updates it from here. You can still update the pedal now.</p>
          <div class="fields">
            <button onclick={savePiPackage} disabled={piSaving}>{piSaving ? "Saving…" : "Save Raspberry Pi package"}</button>
            <label>Username on the Pi<input bind:value={piUser} placeholder="e.g. musician" autocomplete="off" /></label>
          </div>
          {#if piCommand}
            <p>In PowerShell, run:</p><pre>{piCommand}</pre>
            <button onclick={copyPiCommand}>{piCopied ? "Copied" : "Copy command"}</button>
          {:else}
            <p class="muted">Save the package and enter your username to get the command.</p>
          {/if}
        </div>
      {/if}
    {:else if waiting}
      {#if stage === "pi"}
        <p>The Raspberry Pi is restarting to finish its update. Bosun reconnects to <strong>{endpoint.replace(/^tcp:\/\//, "")}</strong> automatically.</p>
        <p class="muted">Keep the Raspberry Pi powered on. Closing this window does not stop the update.</p>
      {:else}
        <p>The Raspberry Pi may still be updating the pedal. Reconnect to <strong>{endpoint.replace(/^tcp:\/\//, "")}</strong> to check progress.</p>
        <p class="muted">Closing this window does not stop an update that has already started.</p>
      {/if}
    {:else if piMismatch}
      <p>{piMismatch} The pedal was not updated.</p>
      <p>Try again later, or update the Raspberry Pi once with the Setup guide command.</p>
    {:else if captainReview}
      <p>The Raspberry Pi runs Bosun {release}. Update the pedal {captainFirmware ? `from ${firmwareLabel(captainFirmware)} ` : ""}to <strong>{release}</strong> now.</p>
      <p>Your profiles and settings are backed up and preserved automatically. The pedal will be unavailable until the update finishes.</p>
    {:else if stage === "pi" && status.phase === "done"}
      {#if piOnlyDone}
        <p>The Raspberry Pi runs Bosun {release}. The pedal is already up to date{captainFirmware ? ` (${firmwareLabel(captainFirmware)})` : ""}.</p>
      {:else}
        <p>The Raspberry Pi runs Bosun {status.version ?? release}. Checking the pedal…</p>
      {/if}
    {:else if stage === "pi" && status.phase === "rolled-back"}
      <p>The Raspberry Pi could not install Bosun {release} and went back to its previous version{status.version ? ` (${status.version})` : ""}. The pedal was not changed.</p>
    {:else if piFailed}
      <p>The Raspberry Pi did not install the update. The pedal was not changed.</p>
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
    {:else if stage === "pi"}
      <p>{labels[status.phase] ?? status.phase}…</p>
      <p class="muted">The Raspberry Pi installs the update and restarts. The pedal is updated afterwards.</p>
    {:else}
      <p>{labels[status.phase] ?? status.phase}…</p>
      <p class="muted">The Raspberry Pi is managing the update. Keep both devices powered on.</p>
    {/if}
    {#if status.error && !captainReview}<p class="error">{status.recovery_verified ? "Original update error: " : ""}{status.error}</p>{/if}
    {#if status.detail && status.phase !== "error" && !captainReview}<p class="muted">{status.detail}</p>{/if}
    {#if clientError && !waiting}<p class="error" role="alert">{clientError}</p>{/if}
    {#if status.backup}<p class="backup">Backup on Raspberry Pi: <code>{status.backup}</code></p>{/if}
  </div>
  <footer>
    {#if status.phase === "review" && !hasPendingHere() && !captainReview}
      <button disabled={working} onclick={close}>Cancel</button>
      <button class="primary" disabled={working || !manifest || !isCurrentConnection()} onclick={() => run("start")}>{working ? "Preparing…" : "Update"}</button>
    {:else if captainReview}
      <button disabled={working} onclick={close}>Close</button>
      <button class="primary" disabled={working || !manifest || !isCurrentConnection()} onclick={() => run("captain")}>{working ? "Preparing…" : "Update pedal"}</button>
    {:else}
      {#if status.phase === "uploading" && !working}<button onclick={cancelUpload}>Cancel upload</button>{/if}
      {#if status.phase === "recovery-required" && stage === "captain"}
        <button class="primary" disabled={working || !hasPendingHere() || !isCurrentConnection()} onclick={() => run("recover")}>Check recovery</button>
      {/if}
      {#if (piFailed || piMismatch) && pedalOutdated}
        <button disabled={working || !manifest || !isCurrentConnection()} onclick={() => run("captain")}>Update pedal only</button>
      {/if}
      {#if clientError && !waiting && !terminal}<button disabled={working || !isCurrentConnection()} onclick={() => run("resume")}>Check again</button>{/if}
      <button onclick={close}>{terminal && (status.phase !== "recovery-required" || stage === "pi") ? "Done" : "Close"}</button>
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
  .notice { border: 1px solid var(--border); border-radius: 6px; padding: 0.2rem 0.8rem 0.8rem; margin: 0.6rem 0; }
  .fields { display: flex; flex-wrap: wrap; align-items: end; gap: 0.6rem; }
  label { display: flex; flex-direction: column; font-size: 0.8rem; color: var(--text-muted); }
  input { background: var(--bg); color: var(--text); border: 1px solid var(--border-strong); border-radius: 4px; padding: 0.35rem 0.5rem; }
  pre { white-space: pre-wrap; overflow-wrap: anywhere; font-size: 0.75rem; background: var(--bg); border: 1px solid var(--border); border-radius: 4px; padding: 0.5rem; }
  progress { width: 100%; margin-top: 1rem; accent-color: var(--accent); }
  footer { display: flex; justify-content: flex-end; gap: 0.6rem; padding: 1rem 1.2rem; }
  button { background: var(--bg-hover); color: var(--text); border: 1px solid var(--border-strong); border-radius: 4px; padding: 0.45rem 0.9rem; cursor: pointer; }
  button.primary { background: var(--accent); color: var(--bg); border-color: var(--accent); }
  button:disabled { opacity: 0.45; cursor: not-allowed; }
</style>
