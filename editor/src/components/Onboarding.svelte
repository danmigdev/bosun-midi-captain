<script lang="ts">
  import { onMount } from "svelte";
  import { cmd, type Manifest } from "../lib/protocol";
  import ColorField from "./ColorField.svelte";

  type Props = {
    connected: boolean;
    hasActiveProfile: boolean;
    manifest: Manifest | null;
    onClose: () => void;
  };
  let { connected, hasActiveProfile, manifest, onClose }: Props = $props();

  // Steps: welcome -> connect -> profile -> done
  type Step = "welcome" | "connect" | "profile" | "done";
  let step = $state<Step>("welcome");

  // Profile creation form
  let profileId = $state("");
  let profileName = $state("");
  let profileKind = $state("");
  // Optional profile colour (hex); passed to CREATE_PROFILE, ignored by
  // firmware without colour support.
  let profileColor = $state("#6fd99b");
  let busy = $state(false);
  let err = $state("");

  // Available kinds from manifest plugins (+ "other" generic).
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

  // Default kind once the list is known
  $effect(() => { if (!profileKind && kinds.length) profileKind = kinds[0].id; });

  // Auto-advance based on external state
  $effect(() => {
    if (step === "connect" && connected) step = "profile";
  });
  $effect(() => {
    if (step === "profile" && hasActiveProfile) step = "done";
  });

  function next() {
    if (step === "welcome") step = connected ? (hasActiveProfile ? "done" : "profile") : "connect";
    else if (step === "connect") step = "profile";
    else if (step === "profile") step = "done";
  }

  function finish() {
    try { localStorage.setItem("BOSUN_ONBOARDED", "1"); } catch {}
    onClose();
  }

  function skip() {
    try { localStorage.setItem("BOSUN_ONBOARDED", "1"); } catch {}
    onClose();
  }

  async function createProfile() {
    if (!profileId || !profileName) { err = "Pick an id and a name"; return; }
    busy = true; err = "";
    try {
      await cmd.createProfile(profileId, profileName, profileKind, profileColor);
      step = "done";
    } catch (e) {
      err = String(e);
    } finally {
      busy = false;
    }
  }

  // Auto-fill id from name
  $effect(() => {
    if (!profileId && profileName) {
      profileId = profileName.toLowerCase().replace(/[^\w-]+/g, "-").replace(/^-+|-+$/g, "");
    }
  });
</script>

<div class="overlay" role="presentation"></div>
<div class="modal" role="dialog" aria-modal="true">
  <header>
    <div class="brand">
      <svg class="logo" viewBox="0 0 24 24" aria-hidden="true">
        <circle cx="12" cy="5" r="2.4" fill="none" stroke="currentColor" stroke-width="1.6"/>
        <path d="M12 7.5 V18.5 M7 12 H17 M5 16 Q12 22 19 16" fill="none" stroke="currentColor" stroke-width="1.6" stroke-linecap="round"/>
      </svg>
      <span class="wordmark">Bosun</span>
    </div>
    <button class="skip" onclick={skip}>Skip</button>
  </header>

  <div class="dots">
    <span class="dot" class:on={step !== "welcome"}></span>
    <span class="dot" class:on={step === "profile" || step === "done"}></span>
    <span class="dot" class:on={step === "done"}></span>
  </div>

  <div class="content">
    {#if step === "welcome"}
      <h1>Welcome to Bosun</h1>
      <p class="lede">
        A friendly editor for your MIDI Captain pedal - patches, screen
        layouts, MIDI learn, and live state from your target amp.
      </p>
      <ul class="features">
        <li><span class="bullet">✓</span> Bidirectional with the Kemper Player</li>
        <li><span class="bullet">✓</span> One-click firmware updates over USB</li>
        <li><span class="bullet">✓</span> Backup, restore and share your config</li>
      </ul>
      <div class="actions">
        <button class="primary" onclick={next}>Get started</button>
      </div>

    {:else if step === "connect"}
      <h1>Plug in your pedal</h1>
      <p class="lede">
        Connect the MIDI Captain over USB. Bosun will autodetect it. If
        it's a brand new pedal that has never been flashed, use
        <em>Install firmware</em> from the welcome screen behind this
        wizard.
      </p>
      <div class="actions">
        <button onclick={skip}>I'll do it later</button>
      </div>

    {:else if step === "profile"}
      <h1>Choose your target</h1>
      <p class="lede">
        Each profile holds a set of patches matching one piece of gear.
        You can have several and switch between them.
      </p>
      <div class="form">
        <label>Target device
          <select bind:value={profileKind}>
            {#each kinds as k}<option value={k.id}>{k.label}</option>{/each}
          </select>
        </label>
        <label>Profile name
          <input type="text" bind:value={profileName} placeholder="e.g. Live rig" />
        </label>
        <label>Profile id
          <input type="text" bind:value={profileId} placeholder="auto from name" />
          <span class="hint">letters, numbers, dashes</span>
        </label>
        <label class="colorrow">Colour
          <ColorField bind:value={profileColor} />
        </label>
      </div>
      {#if err}<p class="err">{err}</p>{/if}
      <div class="actions">
        <button onclick={skip}>Maybe later</button>
        <button class="primary" onclick={createProfile} disabled={busy}>
          {busy ? "Creating…" : "Create profile"}
        </button>
      </div>

    {:else if step === "done"}
      <h1>You're all set</h1>
      <p class="lede">
        Head to <strong>Home</strong> for a quick status check, or jump
        straight to <strong>Patches</strong> to start editing.
      </p>
      <div class="actions">
        <button class="primary" onclick={finish}>Open Bosun</button>
      </div>
    {/if}
  </div>
</div>

<style>
  .overlay {
    position: fixed; inset: 0;
    background:
      radial-gradient(ellipse 70% 90% at 50% 30%, rgba(111, 217, 155, 0.08) 0%, transparent 65%),
      rgba(8, 10, 14, 0.85);
    backdrop-filter: blur(8px); -webkit-backdrop-filter: blur(8px);
    z-index: 90;
    animation: fadein 0.25s ease;
  }
  .modal {
    position: fixed; top: 50%; left: 50%; transform: translate(-50%, -50%);
    background: linear-gradient(180deg, var(--bg-card) 0%, var(--bg-elevated) 100%);
    border: 1px solid var(--border); border-radius: 12px;
    z-index: 100; width: min(540px, 92vw); max-height: 90vh; overflow: auto;
    box-shadow: var(--shadow-modal);
    animation: popin 0.28s cubic-bezier(0.16, 1, 0.3, 1);
  }
  @keyframes fadein { from { opacity: 0; } }
  @keyframes popin {
    from { opacity: 0; transform: translate(-50%, -47%) scale(0.95); }
    to   { opacity: 1; transform: translate(-50%, -50%) scale(1); }
  }
  header {
    display: flex; align-items: center; padding: 0.95rem 1.15rem;
    border-bottom: 1px solid var(--border);
  }
  .brand { display: flex; align-items: center; gap: 0.5rem; flex: 1; }
  .brand .logo { width: 22px; height: 22px; color: var(--accent); }
  .brand .wordmark {
    font-weight: 700; font-size: 1.02rem; letter-spacing: 0.02em;
    color: var(--text);
  }
  .skip {
    background: transparent; border: none; color: var(--text-dim);
    font-size: 0.78rem; cursor: pointer; padding: 0.3rem 0.5rem;
    transition: color 0.15s ease;
  }
  .skip:hover { color: var(--text-muted); }

  .dots {
    display: flex; gap: 0.45rem; justify-content: center;
    padding: 0.85rem 0 0;
  }
  .dots .dot {
    width: 7px; height: 7px; border-radius: 50%;
    background: var(--border-strong);
    transition: all 0.2s ease;
  }
  .dots .dot.on { background: var(--accent); box-shadow: 0 0 0 3px rgba(111,217,155,0.18); }

  .content { padding: 1.5rem 2rem 1.75rem; color: var(--text); text-align: center; }
  h1 {
    margin: 0 0 0.65rem; font-size: 1.5rem; font-weight: 600;
    color: var(--text); letter-spacing: -0.01em;
  }
  .lede {
    margin: 0 0 1.5rem; color: var(--text-muted); font-size: 0.92rem;
    line-height: 1.55; max-width: 420px; margin-left: auto; margin-right: auto;
  }
  .lede em { color: var(--accent); font-style: normal; font-weight: 500; }
  .lede strong { color: var(--text); font-weight: 600; }

  .features {
    list-style: none; padding: 0; margin: 0 auto 1.5rem;
    text-align: left; max-width: 320px;
    display: grid; gap: 0.55rem;
  }
  .features li { display: flex; gap: 0.55rem; font-size: 0.88rem; color: var(--text); }
  .features .bullet {
    color: var(--accent); font-weight: 600;
    flex-shrink: 0;
  }

  .form {
    display: grid; gap: 0.85rem; text-align: left;
    max-width: 360px; margin: 0 auto 1rem;
  }
  .form label {
    display: flex; flex-direction: column; gap: 0.3rem;
    font-size: 0.75rem; color: var(--text-muted);
  }
  .form input, .form select {
    background: var(--bg); color: var(--text); border: 1px solid var(--border-strong);
    padding: 0.5rem 0.65rem; border-radius: 5px;
    font-size: 0.88rem; font-family: inherit;
  }
  .form input:focus, .form select:focus {
    outline: none; border-color: var(--accent-border); background: var(--bg);
  }
  .form .hint { color: var(--text-dim); font-size: 0.7rem; }
  .form .colorrow { flex-direction: row; align-items: center; gap: 0.6rem; }

  .actions {
    display: flex; gap: 0.6rem; justify-content: center;
    margin-top: 0.5rem;
  }
  button {
    background: var(--bg-hover); color: var(--text); border: 1px solid var(--border-strong);
    padding: 0.6rem 1.3rem; border-radius: 6px; cursor: pointer;
    font-size: 0.88rem; font-family: inherit; font-weight: 500;
    transition: all 0.15s ease;
  }
  button:hover:not(:disabled) { background: var(--bg-hover); border-color: var(--border-strong); }
  button:disabled { opacity: 0.5; cursor: not-allowed; }
  button.primary {
    background: var(--accent-bg); color: var(--accent); border-color: var(--accent-border);
    font-weight: 600; padding: 0.65rem 1.6rem;
  }
  button.primary:hover:not(:disabled) { background: var(--accent-hover-bg); border-color: var(--accent-hover-border); }

  .err {
    color: var(--err); font-size: 0.82rem; margin: 0.5rem 0;
    background: rgba(239,155,155,0.08); padding: 0.4rem 0.6rem;
    border-radius: 4px;
  }
</style>
