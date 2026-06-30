<script lang="ts">
  import { onMount, onDestroy } from "svelte";
  import { invoke } from "@tauri-apps/api/core";
  import { cmd, waitForReboot, type DeviceStats, type ProfileInfo } from "../lib/protocol";
  import {
    exportConfig, backupFilename, timestampedFolderName,
    validateBackup, importConfig, inferKindFromDevice,
    type BackupProgress, type RestoreProgress, type ConfigBackup,
  } from "../lib/config-backup";
  import { pickFirmwareSource, prepareFirmwareSource } from "../lib/installer";

  type Props = { connected: boolean; activeProfile?: ProfileInfo | null };
  let { connected, activeProfile = null }: Props = $props();

  let stats = $state<DeviceStats | null>(null);
  let statsErr = $state<string>("");
  let rebooting = $state(false);
  let rebootMsg = $state<string>("");

  // Firmware update source picking (folder or zip). The actual push UI is
  // the shared FirmwarePushOverlay, opened via a window event so it lives in
  // App and survives a page switch.
  let fwSrcBusy = $state(false);
  let fwSrcMsg = $state<string>("");

  function openPush(source?: string) {
    window.dispatchEvent(new CustomEvent("bosun-open-firmware-push",
      source ? { detail: { source } } : undefined));
  }

  async function pickAndPush(zip: boolean) {
    fwSrcMsg = ""; fwSrcBusy = true;
    try {
      const src = await pickFirmwareSource(zip);
      if (!src) return;                       // cancelled
      const root = await prepareFirmwareSource(src);
      openPush(root);
    } catch (e) {
      fwSrcMsg = String(e);
    } finally {
      fwSrcBusy = false;
    }
  }

  // Backup state
  let showExportDialog = $state(false);
  type ExportScope = "selected" | "all";
  let exportScope = $state<ExportScope>("selected");
  /** Ids of the profiles the user has ticked in the "specific profiles"
   * list. Ignored when scope is "all". */
  let selectedProfileIds = $state<Record<string, boolean>>({});
  let allProfiles = $state<ProfileInfo[]>([]);
  let backupBusy = $state(false);
  let backupMsg = $state<string>("");
  let backupProgress = $state<BackupProgress | null>(null);
  let backupSavedFolder = $state<string>("");

  let restoreBusy = $state(false);
  let restoreMsg = $state<string>("");
  let restoreProgress = $state<RestoreProgress | null>(null);
  let fileInput: HTMLInputElement | undefined = $state();

  // Inline import dialog state. Two paths: overwrite the active profile
  // (legacy behavior) or create a new profile from the backup. We hold
  // the parsed backup here so the user can see what they're restoring
  // before they pick a destination.
  let pendingBackup = $state<ConfigBackup | null>(null);
  let importMode = $state<"overwrite" | "new">("new");
  let newProfileName = $state<string>("");
  function slugify(s: string): string {
    return s.toLowerCase().replace(/[^a-z0-9]+/g, "_").replace(/^_+|_+$/g, "").slice(0, 32) || "profile";
  }

  // Batch import: when the user picks multiple files at once we skip
  // the per-file confirm dialog and bulk-create them as new profiles.
  // Overwrite-active doesn't make sense across N files (which one
  // wins?), so this path is always "create new profile per file".
  type BatchEntry = { file: File; backup: ConfigBackup; suggestedName: string };
  let pendingBatch = $state<BatchEntry[] | null>(null);
  let batchProgress = $state<{ current: string; done: number; total: number } | null>(null);

  async function openExportDialog() {
    backupMsg = ""; backupSavedFolder = "";
    try {
      const r = await cmd.listProfiles();
      allProfiles = r.profiles;
      // Default: pre-tick the active profile so a "just hit Export"
      // user-flow still does something sensible.
      if (Object.keys(selectedProfileIds).length === 0) {
        const next: Record<string, boolean> = {};
        for (const p of allProfiles) next[p.id] = p.active;
        selectedProfileIds = next;
      }
    } catch { allProfiles = []; }
    showExportDialog = true;
  }

  /** Pick which profiles end up in the export, based on the chosen scope. */
  function profilesToExport(): ProfileInfo[] {
    if (exportScope === "all") return allProfiles;
    return allProfiles.filter(p => selectedProfileIds[p.id]);
  }

  async function doExport() {
    const targets = profilesToExport();
    if (targets.length === 0) { backupMsg = "Nothing to export."; return; }

    let folder: string | null;
    try {
      folder = await invoke<string | null>("pick_export_folder");
    } catch (e) {
      backupMsg = "Folder picker failed: " + String(e);
      return;
    }
    if (!folder) return; // user cancelled

    const subfolderName = timestampedFolderName();
    backupBusy = true; backupMsg = ""; backupProgress = null;
    backupSavedFolder = "";
    showExportDialog = false;

    try {
      for (let i = 0; i < targets.length; i++) {
        const t = targets[i];
        // Cross-profile read: pass `profile_id` to exportConfig and the
        // firmware reads straight from disk for that profile - no
        // SWITCH_PROFILE / reboot cycle. The active profile stays put,
        // so multi-profile export is zero-reconnect on firmware 0.3.2+.
        // For the active profile we omit profileId so the firmware
        // serves the in-memory state (slightly faster, no disk hit).
        const backup = await exportConfig(
          p => (backupProgress = p),
          t.name || t.id,
          t.kind,
          t.active ? undefined : t.id,
        );

        const filename = backupFilename(backup);
        const rel = `${subfolderName}/${filename}`;
        const savedPath = await invoke<string>("write_export_file", {
          folder, relative: rel, content: JSON.stringify(backup, null, 2),
        });
        backupSavedFolder = savedPath.replace(/[\\/][^\\/]+$/, "");
      }
      backupMsg = targets.length === 1
        ? `Exported 1 profile to ${backupSavedFolder}`
        : `Exported ${targets.length} profiles to ${backupSavedFolder}`;
      try {
        window.dispatchEvent(new CustomEvent("bosun-toast", {
          detail: { level: "ok", message: backupMsg },
        }));
      } catch {}
    } catch (e) {
      backupMsg = "Export failed: " + String(e);
      try {
        window.dispatchEvent(new CustomEvent("bosun-toast", {
          detail: { level: "error", message: backupMsg },
        }));
      } catch {}
    } finally {
      backupBusy = false;
    }
  }

  async function openSavedFolder() {
    if (!backupSavedFolder) return;
    try { await invoke("open_in_file_manager", { path: backupSavedFolder }); }
    catch (e) { backupMsg = "Couldn't open folder: " + String(e); }
  }

  async function doImport(ev: Event) {
    const input = ev.target as HTMLInputElement;
    const files = input.files;
    if (!files || files.length === 0) return;
    restoreMsg = "";
    const arr = Array.from(files);
    try {
      if (arr.length === 1) {
        // Single file: existing per-file dialog with overwrite / new
        // choice. Keeps power-user flow intact.
        const text = await arr[0].text();
        const parsed = JSON.parse(text);
        const backup = validateBackup(parsed);
        pendingBackup = backup;
        importMode = "new";
        newProfileName = backup.profile_label || arr[0].name.replace(/\.json$/i, "") || "Imported profile";
      } else {
        // Multi-file: pre-validate all of them up front so the user
        // sees parse errors before committing to the batch op. Failed
        // files are dropped from the batch with a warning toast.
        const valid: BatchEntry[] = [];
        const errors: string[] = [];
        for (const f of arr) {
          try {
            const text = await f.text();
            const backup = validateBackup(JSON.parse(text));
            valid.push({
              file: f,
              backup,
              suggestedName: backup.profile_label || f.name.replace(/\.json$/i, "") || "Imported profile",
            });
          } catch (e) {
            errors.push(`${f.name}: ${String(e)}`);
          }
        }
        if (errors.length) {
          try {
            window.dispatchEvent(new CustomEvent("bosun-toast", {
              detail: { level: "error", message: `${errors.length} file(s) couldn't be parsed - dropped from the batch` },
            }));
          } catch {}
        }
        if (valid.length === 0) {
          restoreMsg = "Import failed: nothing valid in the selection.";
        } else {
          pendingBatch = valid;
        }
      }
    } catch (e) {
      restoreMsg = "Import failed: " + String(e);
    } finally {
      input.value = "";
    }
  }

  async function confirmBatchImport() {
    if (!pendingBatch || pendingBatch.length === 0) return;
    const batch = pendingBatch;
    restoreBusy = true; restoreMsg = ""; restoreProgress = null;
    batchProgress = { current: "", done: 0, total: batch.length };
    let ok = 0;
    let lastProfileId: string | null = null;
    const fails: string[] = [];
    for (let i = 0; i < batch.length; i++) {
      const { backup, suggestedName, file } = batch[i];
      batchProgress = { current: suggestedName, done: i, total: batch.length };
      try {
        const kind = backup.kind || inferKindFromDevice(backup.device);
        if (!kind) throw new Error("unknown plugin kind in backup");
        const profile_id = slugify(suggestedName) + "_" + Math.random().toString(36).slice(2, 6);
        await importConfig(backup, p => (restoreProgress = p), {
          asNewProfile: { profile_id, name: suggestedName, kind },
        });
        ok += 1;
        lastProfileId = profile_id;
      } catch (e) {
        fails.push(`${file.name}: ${String(e)}`);
      }
    }
    batchProgress = { current: "", done: batch.length, total: batch.length };
    pendingBatch = null;
    restoreBusy = false;
    // Activate the last imported profile so the editor lands on real data
    // instead of the (untouched) previously-active one. App reconnects and
    // refetches on "profile-switched".
    if (lastProfileId) {
      try { await cmd.switchProfile(lastProfileId); } catch {}
      window.dispatchEvent(new CustomEvent("profile-switched", { detail: { profile_id: lastProfileId } }));
    }
    if (fails.length === 0) {
      restoreMsg = `Imported ${ok} profile(s).`;
      try {
        window.dispatchEvent(new CustomEvent("bosun-toast", {
          detail: { level: "ok", message: restoreMsg },
        }));
      } catch {}
    } else {
      restoreMsg = `Imported ${ok}/${batch.length}; ${fails.length} failed: ${fails[0]}${fails.length > 1 ? "…" : ""}`;
      try {
        window.dispatchEvent(new CustomEvent("bosun-toast", {
          detail: { level: "error", message: restoreMsg },
        }));
      } catch {}
    }
    batchProgress = null;
  }

  function cancelBatchImport() {
    pendingBatch = null;
    batchProgress = null;
    restoreMsg = "";
  }

  async function confirmImport() {
    if (!pendingBackup) return;
    // No active profile means there is nothing to overwrite: an "overwrite"
    // import would write to a non-existent profile and create a bogus
    // "undefined" one. Force the create-new path instead.
    if (!activeProfile) importMode = "new";
    const backup = pendingBackup;
    restoreBusy = true; restoreMsg = ""; restoreProgress = null;
    // Set to the new profile id when importing as a new profile so we can
    // activate it afterwards (import-as-new writes to disk but does NOT make
    // it the active profile - without this the pedal stays on the old empty
    // profile and the editor shows nothing until a manual refresh).
    let switchTo: string | null = null;
    try {
      if (importMode === "new") {
        const name = newProfileName.trim() || "Imported profile";
        const kind = backup.kind || inferKindFromDevice(backup.device);
        if (!kind) {
          throw new Error("Couldn't infer plugin kind from backup. Update the backup or overwrite the active profile instead.");
        }
        const profile_id = slugify(name) + "_" + Math.random().toString(36).slice(2, 6);
        await importConfig(backup, p => (restoreProgress = p), {
          asNewProfile: { profile_id, name, kind },
        });
        switchTo = profile_id;
        restoreMsg = `Created profile "${name}" with ${backup.patches.length} patches.`;
      } else {
        await importConfig(backup, p => (restoreProgress = p));
        // Overwrite writes patches into the ACTIVE profile's in-memory store,
        // which marks them dirty - with autosave off they'd sit unsaved until
        // a manual Save. Persist them now so import "just works".
        try { await cmd.saveNow(); } catch {}
        restoreMsg = `Overwrote active profile with ${backup.patches.length} patches.`;
      }
      pendingBackup = null;
      try {
        window.dispatchEvent(new CustomEvent("bosun-toast", {
          detail: { level: "ok", message: restoreMsg },
        }));
      } catch {}
      // Land the editor (and the pedal) on the imported data. A new profile
      // gets activated (SWITCH_PROFILE reboots the pedal into it); App's
      // "profile-switched" handler then reconnects and refetches. Overwrite
      // stays on the active profile - just refetch in place.
      if (switchTo) {
        try { await cmd.switchProfile(switchTo); } catch {}
        window.dispatchEvent(new CustomEvent("profile-switched", { detail: { profile_id: switchTo } }));
      } else {
        window.dispatchEvent(new CustomEvent("connection-resynced"));
      }
    } catch (e) {
      restoreMsg = "Import failed: " + String(e);
      try {
        window.dispatchEvent(new CustomEvent("bosun-toast", {
          detail: { level: "error", message: restoreMsg },
        }));
      } catch {}
    } finally {
      restoreBusy = false;
    }
  }

  function cancelImport() {
    pendingBackup = null;
    restoreMsg = "";
  }

  const STATS_POLL_MS = 3000;
  let statsTimer: ReturnType<typeof setInterval> | null = null;

  onMount(() => {
    if (connected) startStatsPoll();
  });
  onDestroy(() => stopStatsPoll());
  // Pause stats polling whenever a long-running protocol op is in
  // flight (export, import, reboot). Without this, a STATS request can
  // hit the firmware mid-MANIFEST/PATCH stream and time out, which
  // would surface to the user as a misleading "Error: timeout: STATS#N"
  // banner even though everything is fine. The $effect re-evaluates
  // automatically when any of these state vars flip.
  $effect(() => {
    if (connected && !backupBusy && !restoreBusy && !rebooting) {
      startStatsPoll();
    } else {
      stopStatsPoll();
    }
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
    // Defense in depth: even with the $effect above, an in-flight
    // request might land here if the timer fires concurrently with the
    // state flip. Skip when busy so we don't compete with the real op.
    if (rebooting || backupBusy || restoreBusy) return;
    try {
      const s = await cmd.getStats();
      stats = s; statsErr = "";
    } catch (e) {
      // Polling errors are noise to the user. Only surface the first one
      // (no prior stats) or non-timeout errors (real protocol breakage).
      const msg = String(e);
      if (!stats || !msg.includes("timeout")) statsErr = msg;
    }
  }

  async function doReboot() {
    rebooting = true; rebootMsg = "Sending REBOOT"; stopStatsPoll();
    try { await cmd.reboot(); } catch {}
    rebootMsg = "Waiting for firmware to come back";
    const ok = await waitForReboot(15000);
    rebootMsg = ok
      ? "Firmware back online"
      : "Firmware did not respond within 15 s - reconnect manually if needed";
    rebooting = false; startStatsPoll();
    setTimeout(() => rebootMsg = "", 3000);
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

{#if !connected}
  <p class="muted">Not connected.</p>
{:else}
  <section class="block">
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

  <section class="block">
    <h3>Backup &amp; restore</h3>
    <p class="muted small">
      Save profiles - device settings, every patch, MIDI Learn - to JSON
      files on disk. Each export run creates a fresh timestamped folder
      so you never overwrite a previous backup.
    </p>
    <div class="row">
      <button onclick={openExportDialog} disabled={backupBusy || restoreBusy || !connected}>
        {backupBusy ? "Exporting…" : "Export config…"}
      </button>
      <button onclick={() => fileInput?.click()} disabled={backupBusy || restoreBusy || !connected}>
        {restoreBusy ? "Importing…" : "Import config…"}
      </button>
      <input type="file" accept="application/json,.json" hidden multiple
             bind:this={fileInput} onchange={doImport} />
    </div>

    {#if showExportDialog}
      <div class="dialog">
        <div class="dialoghead">
          <span>What do you want to export?</span>
          <button class="x" onclick={() => showExportDialog = false} aria-label="Cancel">×</button>
        </div>
        <div class="scopelist">
          <label class="scope">
            <input type="radio" bind:group={exportScope} value="selected" disabled={allProfiles.length === 0} />
            <div>
              <b>Specific profiles</b>
              <div class="profilechecks">
                {#each allProfiles as p (p.id)}
                  <label class="check">
                    <input type="checkbox"
                           bind:checked={selectedProfileIds[p.id]}
                           disabled={exportScope !== "selected"} />
                    <span>{p.name}{p.active ? " (active)" : ""}</span>
                  </label>
                {/each}
                {#if allProfiles.length === 0}
                  <span class="muted small">No profiles yet.</span>
                {/if}
              </div>
            </div>
          </label>
          <label class="scope">
            <input type="radio" bind:group={exportScope} value="all" disabled={allProfiles.length === 0} />
            <div>
              <b>All profiles</b>
              <span class="muted small">{allProfiles.length} total - each one in its own JSON file</span>
            </div>
          </label>
        </div>
        <div class="row">
          <button onclick={() => showExportDialog = false}>Cancel</button>
          <button class="primary" onclick={doExport}>Choose folder &amp; export…</button>
        </div>
      </div>
    {/if}

    {#if pendingBatch && !restoreBusy}
      <div class="dialog">
        <div class="dialoghead">
          <span>Import {pendingBatch.length} backups as new profiles</span>
          <button class="x" onclick={cancelBatchImport} aria-label="Cancel">×</button>
        </div>
        <p class="muted small" style="margin: 0 0 0.6rem;">
          Each file becomes its own profile. The active profile is not
          touched. Names default to the backup label (or the filename);
          you can rename later from the Profile picker.
        </p>
        <ul class="batchlist">
          {#each pendingBatch as entry (entry.file.name)}
            <li>
              <span class="batchlist__name">{entry.suggestedName}</span>
              <span class="batchlist__meta">{entry.backup.patches.length} patches{entry.backup.kind ? ` · ${entry.backup.kind}` : ""}</span>
            </li>
          {/each}
        </ul>
        <div class="row">
          <button onclick={cancelBatchImport}>Cancel</button>
          <button class="primary" onclick={confirmBatchImport}>Import {pendingBatch.length} as new profiles</button>
        </div>
      </div>
    {/if}

    {#if pendingBackup && !restoreBusy}
      <div class="dialog">
        <div class="dialoghead">
          <span>Restore "{pendingBackup.profile_label || "backup"}" - {pendingBackup.patches.length} patches</span>
          <button class="x" onclick={cancelImport} aria-label="Cancel">×</button>
        </div>
        <p class="muted small" style="margin: 0 0 0.6rem;">
          Generated {pendingBackup.generated_at}{pendingBackup.kind ? ` · kind ${pendingBackup.kind}` : ""}
        </p>
        {#if !activeProfile}
          <p class="muted small" style="margin: 0 0 0.6rem;">
            This pedal has no profile yet, so the import must create one. Give it a name below.
          </p>
        {/if}
        <div class="scopelist">
          <label class="scope">
            <input type="radio" bind:group={importMode} value="new" />
            <div>
              <b>Create new profile</b>
              <span class="muted small">Keeps your active profile untouched. Pedal reboots into the new one.</span>
              {#if importMode === "new"}
                <input type="text" class="renameinput"
                       bind:value={newProfileName}
                       placeholder="Profile name"
                       style="margin-top: 0.4rem; width: 100%; box-sizing: border-box;" />
              {/if}
            </div>
          </label>
          <label class="scope">
            <input type="radio" bind:group={importMode} value="overwrite" disabled={!activeProfile} />
            <div>
              <b>Overwrite active profile</b>
              <span class="muted small">
                {#if activeProfile}
                  Replaces the active profile's device, patches and MIDI Learn in place. Existing patches are lost.
                {:else}
                  No active profile to overwrite - create a new one above.
                {/if}
              </span>
            </div>
          </label>
        </div>
        <div class="row">
          <button onclick={cancelImport}>Cancel</button>
          <button class="primary" onclick={confirmImport}
                  disabled={importMode === "new" && !newProfileName.trim()}>
            {importMode === "new" ? "Create & restore" : "Overwrite & restore"}
          </button>
        </div>
      </div>
    {/if}

    {#if backupProgress && backupBusy}
      <p class="curr">
        {#if backupProgress.phase === "device"}Reading device.json
        {:else if backupProgress.phase === "patches"}Reading patch {backupProgress.current} ({backupProgress.done}/{backupProgress.total})
        {:else if backupProgress.phase === "midi_learn"}Reading midi_learn
        {:else}Done{/if}
      </p>
    {/if}
    {#if restoreProgress && restoreBusy}
      <p class="curr">
        {#if batchProgress && batchProgress.total > 1}({batchProgress.done + 1}/{batchProgress.total}) {batchProgress.current} -
        {/if}
        {#if restoreProgress.phase === "create_profile"}Creating profile {restoreProgress.current}…
        {:else if restoreProgress.phase === "switch_profile"}Switching to new profile…
        {:else if restoreProgress.phase === "device"}Pushing device.json
        {:else if restoreProgress.phase === "patches"}Pushing patch {restoreProgress.current} ({restoreProgress.done}/{restoreProgress.total})
        {:else if restoreProgress.phase === "midi_learn"}Pushing midi_learn
        {:else}Done{/if}
      </p>
    {/if}
    {#if backupMsg}
      <p class="curr">
        {backupMsg}
        {#if backupSavedFolder}
          <button class="link" onclick={openSavedFolder}>Open folder</button>
        {/if}
      </p>
    {/if}
    {#if restoreMsg}<p class="curr">{restoreMsg}</p>{/if}
  </section>

  <section class="block">
    <h3>Update firmware (OTA)</h3>
    <p class="muted small">
      Pushes a firmware tree to the pedal over USB - no bootloader, no drive,
      works in performance mode. Use the editor's bundled firmware, or point
      it at your own firmware folder or a <code>.zip</code>. The pedal reboots
      and reconnects when done.
    </p>
    <div class="row">
      <button class="primary" disabled={!connected || fwSrcBusy}
              onclick={() => openPush()}>
        Update from bundled
      </button>
      <button disabled={!connected || fwSrcBusy} onclick={() => pickAndPush(false)}>
        From folder…
      </button>
      <button disabled={!connected || fwSrcBusy} onclick={() => pickAndPush(true)}>
        From .zip…
      </button>
    </div>
    {#if !connected}<p class="muted small">Connect the pedal first.</p>{/if}
    {#if fwSrcBusy}<p class="curr">Reading the selected firmware…</p>{/if}
    {#if fwSrcMsg}<p class="curr err">{fwSrcMsg}</p>{/if}
  </section>

  <section class="block">
    <h3>Reboot</h3>
    <p class="muted small">
      Sends the firmware a REBOOT command. Useful after manual edits to
      <code>firmware/config/</code> or to recover from a stuck state.
    </p>
    <div class="row">
      <button onclick={doReboot} disabled={rebooting}>
        {rebooting ? "Rebooting…" : "Reboot pedal"}
      </button>
    </div>
    {#if rebootMsg}
      <p class="curr">{rebootMsg}</p>
    {/if}
  </section>
{/if}

<style>
  .block { background: var(--bg-card); border: 1px solid var(--border); border-radius: 6px; padding: 0.85rem 1rem; margin-bottom: 1rem; }
  h3 { color: var(--text-muted); margin: 0 0 0.6rem; font-size: 0.8rem; text-transform: uppercase; letter-spacing: 0.05em; font-weight: 600; }
  .stats { display: grid; grid-template-columns: repeat(2, 1fr); gap: 0.4rem 1rem; font-size: 0.85rem; }
  .stats > div { display: flex; justify-content: space-between; padding: 0.35rem 0.6rem; background: var(--bg); border-radius: 4px; }
  .stats > div span { color: var(--text-dim); }
  .stats > div b { color: var(--text); font-weight: 500; font-family: ui-monospace, Consolas, monospace; }
  .row { display: flex; gap: 0.5rem; margin-bottom: 0.5rem; }
  button { background: var(--bg-hover); color: var(--text); border: 1px solid var(--border-strong); padding: 0.45rem 0.9rem; border-radius: 4px; cursor: pointer; font-size: 0.85rem; }
  button:hover:not(:disabled) { background: var(--bg-hover); }
  button:disabled { opacity: 0.45; cursor: not-allowed; }
  .curr { color: var(--text-muted); font-size: 0.8rem; margin: 0.5rem 0 0; }
  code { background: var(--bg); padding: 0.1rem 0.4rem; border-radius: 3px; color: var(--warn-text); font-family: ui-monospace, Consolas, monospace; }
  .dialog {
    background: var(--bg); border: 1px solid var(--border); border-radius: 6px;
    padding: 0.75rem 0.95rem; margin: 0.75rem 0 0;
  }
  .dialoghead {
    display: flex; align-items: center; justify-content: space-between;
    margin-bottom: 0.7rem;
    color: var(--text); font-size: 0.88rem; font-weight: 500;
  }
  .dialoghead .x {
    background: transparent; border: none; color: var(--text-dim);
    font-size: 1.2rem; cursor: pointer; padding: 0 0.3rem; line-height: 1;
  }
  .dialoghead .x:hover { color: var(--text); }
  .batchlist {
    list-style: none; margin: 0 0 0.85rem; padding: 0;
    display: flex; flex-direction: column;
    border: 1px solid var(--border); border-radius: 5px;
    max-height: 220px; overflow: auto;
  }
  .batchlist li {
    display: flex; align-items: baseline; justify-content: space-between;
    padding: 0.4rem 0.65rem; font-size: 0.85rem;
    border-bottom: 1px solid var(--border);
  }
  .batchlist li:last-child { border-bottom: 0; }
  .batchlist__name { color: var(--text); }
  .batchlist__meta { color: var(--text-muted); font-size: 0.75rem; }
  .scopelist { display: flex; flex-direction: column; gap: 0.55rem; margin-bottom: 0.85rem; }
  .scope {
    display: flex; align-items: flex-start; gap: 0.65rem;
    padding: 0.55rem 0.65rem;
    background: var(--bg-card); border: 1px solid var(--border); border-radius: 5px;
    cursor: pointer; transition: border-color 0.12s ease, background 0.12s ease;
  }
  .scope:has(input:checked) { border-color: var(--accent-border); background: var(--accent-bg); }
  .scope:has(input:disabled) { opacity: 0.4; cursor: not-allowed; }
  .scope input { margin-top: 0.2rem; }
  .scope > div { display: flex; flex-direction: column; gap: 0.25rem; flex: 1; }
  .scope b { color: var(--text); font-weight: 500; font-size: 0.85rem; }
  .profilechecks {
    display: flex; flex-direction: column; gap: 0.3rem;
    margin-top: 0.35rem;
    padding-left: 0.1rem;
  }
  .check {
    display: flex; align-items: center; gap: 0.45rem;
    font-size: 0.82rem; color: var(--text); cursor: pointer;
    padding: 0.18rem 0;
  }
  .check input { margin: 0; }
  .check input:disabled + span { opacity: 0.4; }
  .link {
    background: transparent; border: none; color: var(--accent);
    text-decoration: underline; text-underline-offset: 2px;
    cursor: pointer; padding: 0; margin-left: 0.4rem; font-size: inherit;
  }
  .link:hover { color: var(--accent-hover-border); }
  button.primary { background: var(--accent-bg); color: var(--accent); border-color: var(--accent-border); font-weight: 600; }
  button.primary:hover:not(:disabled) { background: var(--accent-hover-bg); }
  .muted { color: var(--text-muted); }
  .small { font-size: 0.85rem; }
  .err { color: var(--err); font-family: ui-monospace, Consolas, monospace; font-size: 0.8rem; }
</style>
