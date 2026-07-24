<script lang="ts">
  import { onMount } from "svelte";
  import { cmd, autoConnect, disconnect, isConnected, type ProfileInfo, type Manifest } from "../lib/protocol";
  import ColorField from "./ColorField.svelte";

  type Props = { manifest?: Manifest | null };
  let { manifest = null }: Props = $props();

  let profiles = $state<ProfileInfo[]>([]);
  let active = $state<string>("");
  let open = $state(false);
  let busy = $state(false);
  let error = $state<string>("");

  // Create dialog state
  let creating = $state(false);
  let newId = $state("");
  let newName = $state("");
  let newKind = $state("");
  // Optional profile colour (hex). Seeded to the accent green; passed to
  // CREATE_PROFILE (ignored by firmware that predates colour support).
  let newColor = $state("#6fd99b");

  // Rename state - id of the profile currently being renamed
  let renamingId = $state<string | null>(null);
  let renameValue = $state("");

  // Derived from the firmware manifest so a new plugin shows up here
  // without an editor code change. Plugins contribute their NAME (id)
  // and LABEL; we always append a generic "Other / Generic MIDI"
  // option for users who aren't on one of the supported targets.
  let kinds = $derived.by<Array<{ id: string; label: string }>>(() => {
    const out: Array<{ id: string; label: string }> = [];
    if (manifest) {
      for (const [id, plug] of Object.entries(manifest.plugins)) {
        out.push({ id, label: plug.label || id });
      }
    }
    out.push({ id: "other", label: "Other / Generic MIDI" });
    return out;
  });
  // Default `newKind` to the first option once the list is known.
  $effect(() => {
    if (!newKind && kinds.length) newKind = kinds[0].id;
  });

  async function refresh() {
    busy = true; error = "";
    // Retry once on timeout: the firmware's heap can fragment after
    // many requests in a long session, causing the first attempt of a
    // ~KB response to drop. A second try after gc.collect (the
    // firmware does it on MemoryError now) usually lands.
    for (let attempt = 0; attempt < 2; attempt++) {
      try {
        const r = await cmd.listProfiles();
        profiles = r.profiles;
        active = r.active;
        error = "";
        break;
      } catch (e) {
        const msg = String(e);
        if (attempt === 0 && msg.includes("timeout")) continue;
        error = msg;
      }
    }
    busy = false;
  }

  onMount(refresh);

  function activeName() {
    return profiles.find(p => p.active)?.name ?? active ?? "-";
  }

  async function doSwitch(pid: string) {
    if (pid === active || busy) return;
    busy = true; error = "";
    try {
      await cmd.switchProfile(pid);
      // Firmware ACKs then reboots. USB CDC re-enumerates as the same
      // COM port so the OS doesn't notice a disconnect - but in-memory
      // state on the firmware is lost. App.svelte listens for the
      // "profile-switched" event below and handles the
      // disconnect/wait/reconnect/refetch cycle.
      open = false;
      window.dispatchEvent(new CustomEvent("profile-switched", { detail: { profile_id: pid } }));
    } catch (e) { error = String(e); }
    finally {
      busy = false;
    }
  }

  async function doDelete(pid: string) {
    if (pid === active) { error = "can't delete the active profile"; return; }
    if (!confirm(`Delete profile "${profiles.find(p => p.id === pid)?.name}"?`)) return;
    busy = true; error = "";
    try {
      await cmd.deleteProfile(pid);
      await refresh();
    } catch (e) { error = String(e); }
    finally { busy = false; }
  }

  function startRename(pid: string, currentName: string) {
    renamingId = pid;
    renameValue = currentName;
  }

  async function commitRename() {
    if (!renamingId) return;
    const trimmed = renameValue.trim();
    if (!trimmed) { renamingId = null; return; }
    const target = renamingId;
    renamingId = null;
    busy = true; error = "";
    try {
      await cmd.renameProfile(target, trimmed);
      await refresh();
    } catch (e) { error = String(e); }
    finally { busy = false; }
  }

  function cancelRename() {
    renamingId = null;
    renameValue = "";
  }

  function startCreate() {
    creating = true;
    newId = "";
    newName = "";
    newKind = "ampero_ii_stage";
    newColor = "#6fd99b";
  }

  async function doCreate() {
    if (!newId || !newName) { error = "id and name are required"; return; }
    busy = true; error = "";
    try {
      await cmd.createProfile(newId, newName, newKind, newColor);
      creating = false;
      await refresh();
    } catch (e) { error = String(e); }
    finally { busy = false; }
  }

  function slugify(s: string): string {
    return s.toLowerCase().replace(/[^a-z0-9]+/g, "-").replace(/^-|-$/g, "").slice(0, 32);
  }

  // Auto-fill id from name unless user typed something already.
  let idTouched = $state(false);
  $effect(() => {
    if (!idTouched && newName) newId = slugify(newName);
  });
</script>

<button class="picker" onclick={() => { open = !open; if (open) refresh(); }} disabled={busy}>
  <span class="dot"></span>
  <span class="label">{activeName()}</span>
  <span class="chevron">▾</span>
</button>

{#if open}
  <div class="overlay" onclick={() => { open = false; creating = false; }} role="presentation"></div>
  <div class="panel" role="dialog">
    <h3>Profiles</h3>
    {#if error}<p class="err">{error}</p>{/if}
    {#if profiles.length === 0}
      <p class="muted">No profiles found.</p>
    {:else}
      <ul class="plist">
        {#each profiles as p (p.id)}
          <li class:active={p.active}>
            {#if renamingId === p.id}
              <!-- svelte-ignore a11y_autofocus -->
              <input class="renamebox"
                     bind:value={renameValue}
                     onkeydown={(e) => {
                       if (e.key === "Enter") commitRename();
                       else if (e.key === "Escape") cancelRename();
                     }}
                     onblur={commitRename}
                     autofocus />
            {:else}
              <button class="row"
                      onclick={() => doSwitch(p.id)}
                      ondblclick={() => startRename(p.id, p.name)}
                      disabled={busy || p.active}
                      title={p.active ? "Double-click to rename" : "Click to switch · Double-click to rename"}>
                <span class="swatch" style:background={p.color || "var(--border-strong)"}
                      title={p.color ? `Colour ${p.color}` : "No colour set"}></span>
                <span class="pname">{p.name}</span>
                <span class="pkind">{p.kind}</span>
                {#if p.active}<span class="active-tag">active</span>{/if}
              </button>
              <button class="iconbtn"
                      title="Rename"
                      onclick={() => startRename(p.id, p.name)}
                      disabled={busy}>✎</button>
              {#if !p.active}
                <button class="danger" title="Delete" onclick={() => doDelete(p.id)} disabled={busy}>×</button>
              {/if}
            {/if}
          </li>
        {/each}
      </ul>
    {/if}

    {#if !creating}
      <button class="primary" onclick={startCreate} disabled={busy}>+ New profile</button>
    {:else}
      <div class="createform">
        <label>Name
          <input bind:value={newName} placeholder="Live Kemper" />
        </label>
        <label>Profile id
          <input bind:value={newId} oninput={() => idTouched = true} placeholder="live-kemper" />
        </label>
        <label>Target device
          <select bind:value={newKind}>
            {#each kinds as k}<option value={k.id}>{k.label}</option>{/each}
          </select>
        </label>
        <label class="colorrow">Colour
          <ColorField bind:value={newColor} />
        </label>
        <div class="actions">
          <button onclick={() => creating = false} disabled={busy}>Cancel</button>
          <button class="primary" onclick={doCreate} disabled={busy || !newId || !newName}>Create</button>
        </div>
      </div>
    {/if}

    <p class="hint">Switching profile reboots the pedal. Patches in other profiles stay intact.</p>
  </div>
{/if}

<style>
  .picker {
    display: inline-flex; align-items: center; gap: 0.45rem;
    background: var(--bg-hover); border: 1px solid var(--border-strong); color: var(--text);
    padding: 0.35rem 0.7rem; border-radius: 4px; cursor: pointer; font-size: 0.8rem;
    min-width: 8rem; justify-content: flex-start;
  }
  .picker:hover { background: var(--bg-hover); }
  .picker .dot { width: 7px; height: 7px; border-radius: 50%; background: var(--accent); }
  .picker .label { flex: 1; text-align: left; color: var(--text); font-weight: 500; }
  .picker .chevron { color: var(--text-dim); font-size: 0.7rem; }

  .overlay { position: fixed; inset: 0; background: var(--overlay-bg); z-index: 90; }
  .panel {
    position: fixed; top: 3.2rem; right: 1rem;
    background: var(--bg-card); border: 1px solid var(--border); border-radius: 6px;
    padding: 0.85rem 1rem; z-index: 100; min-width: 320px; max-width: 420px;
    box-shadow: var(--shadow-modal);
  }
  .panel h3 { margin: 0 0 0.6rem; color: var(--accent); font-size: 0.8rem; text-transform: uppercase; letter-spacing: 0.05em; }
  .err  { color: var(--err); font-size: 0.8rem; margin: 0 0 0.4rem; }
  .muted { color: var(--text-muted); font-size: 0.85rem; }

  ul.plist { list-style: none; margin: 0 0 0.65rem; padding: 0; display: flex; flex-direction: column; gap: 0.25rem; }
  ul.plist li {
    display: flex; align-items: stretch; gap: 0.25rem;
  }
  ul.plist li .row {
    flex: 1; display: flex; align-items: center; gap: 0.5rem;
    background: var(--bg-hover); border: 1px solid var(--border);
    padding: 0.4rem 0.6rem; border-radius: 3px; cursor: pointer;
    text-align: left; color: var(--text); font-size: 0.85rem;
  }
  ul.plist li .row:hover:not(:disabled) { background: var(--bg-hover); }
  ul.plist li .row:disabled { cursor: default; }
  ul.plist li.active .row { background: var(--accent-bg); border-color: var(--accent-border); color: var(--text); }
  .swatch {
    width: 12px; height: 12px; border-radius: 3px; flex-shrink: 0;
    border: 1px solid var(--border-strong);
  }
  .pname { flex: 1; font-weight: 500; }
  .pkind { color: var(--text-muted); font-size: 0.7rem; font-family: ui-monospace, Consolas, monospace; }
  .active-tag { background: var(--accent-bg); color: var(--accent); font-size: 0.62rem; padding: 0.05rem 0.4rem; border-radius: 3px; text-transform: uppercase; }
  .danger { background: rgba(239,155,155,0.08); border: 1px solid rgba(239,155,155,0.35); color: var(--err); padding: 0 0.55rem; border-radius: 3px; cursor: pointer; font-size: 0.85rem; }
  .danger:hover:not(:disabled) { background: rgba(239,155,155,0.08); }
  .danger:disabled { opacity: 0.4; cursor: not-allowed; }
  .iconbtn {
    background: var(--bg-hover); border: 1px solid var(--border-strong); color: var(--text-muted);
    padding: 0 0.55rem; border-radius: 3px; cursor: pointer; font-size: 0.85rem;
  }
  .iconbtn:hover:not(:disabled) { background: var(--bg-hover); color: var(--text); }
  .iconbtn:disabled { opacity: 0.4; cursor: not-allowed; }
  .renamebox {
    flex: 1; background: var(--bg); border: 1px solid var(--accent); color: var(--text);
    padding: 0.4rem 0.6rem; border-radius: 3px; font-size: 0.85rem;
    outline: none;
  }

  button.primary {
    width: 100%; background: var(--accent-bg); color: var(--accent); border: 1px solid var(--accent-border);
    padding: 0.45rem; border-radius: 3px; cursor: pointer; font-size: 0.82rem; font-weight: 500;
  }
  button.primary:hover:not(:disabled) { background: var(--accent-hover-bg); }
  button.primary:disabled { opacity: 0.4; cursor: not-allowed; }

  .createform { display: flex; flex-direction: column; gap: 0.4rem; padding: 0.5rem 0.6rem; background: var(--bg-hover); border-radius: 4px; }
  .createform label { display: flex; flex-direction: column; gap: 0.15rem; font-size: 0.7rem; color: var(--text-muted); }
  .createform input, .createform select {
    background: var(--bg); border: 1px solid var(--border-strong); color: var(--text);
    padding: 0.3rem 0.5rem; border-radius: 3px; font-size: 0.82rem;
  }
  .createform .colorrow { flex-direction: row; align-items: center; gap: 0.5rem; }
  .createform .actions { display: flex; gap: 0.4rem; margin-top: 0.3rem; }
  .createform .actions button {
    flex: 1; padding: 0.35rem; background: var(--bg-hover); border: 1px solid var(--border-strong);
    color: var(--text); border-radius: 3px; cursor: pointer; font-size: 0.78rem;
  }
  .createform .actions button.primary { background: var(--accent-bg); color: var(--accent); border-color: var(--accent-border); }

  .hint { color: var(--text-dim); font-size: 0.7rem; margin: 0.6rem 0 0; font-style: italic; }

  /* ---------- mobile ---------- */
  @media (max-width: 767px) {
    .profiles { gap: 0.3rem; }
    .profile-btn { font-size: 0.72rem; padding: 0.35rem 0.5rem; }
    .profile-modal { width: calc(100vw - 2rem); max-width: none; }
    .profile-form { gap: 0.5rem; }
    .profile-form input, .profile-form select {
      width: 100%;
      font-size: 0.9rem;
      padding: 0.45rem 0.5rem;
      min-height: 44px;
    }
    .color-swatch { width: 36px; height: 36px; }
    .color-grid { gap: 0.3rem; }
    .color-swatch-btn { width: 28px; height: 28px; }
  }
</style>
