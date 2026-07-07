<script lang="ts">
  import { cmd, patchIdOf, type BindingMode, type Patch, type PatchSummary } from "../lib/protocol";
  import { defaultLedFor } from "../lib/switch-colors";

  type Props = {
    patches: PatchSummary[];
    currentPatchEnvelope: { bank: number; slot: number; patch: Patch } | null;
  };

  let { patches, currentPatchEnvelope }: Props = $props();

  type DialogKind = null | "new" | "clone" | "delete";
  let dialog = $state<DialogKind>(null);
  let targetBank = $state(1);
  let targetSlot = $state(1);
  let targetName = $state("");

  const SWITCH_ORDER = ["1","2","3","4","up","A","B","C","D","down"];

  function nextFreeSlot(): { bank: number; slot: number } {
    const used = new Set(patches.map(p => `${p.bank}/${p.slot}`));
    for (let b = 1; b <= 99; b++) {
      for (let s = 1; s <= 99; s++) {
        if (!used.has(`${b}/${s}`)) return { bank: b, slot: s };
      }
    }
    return { bank: 1, slot: 1 };
  }

  function blankPatch(name: string): Patch {
    return {
      name,
      tft_color: "#00ff88",
      bindings: SWITCH_ORDER.map(sw => ({
        switch: sw,
        mode: "tap" as BindingMode,
        label: "",
        // Per-switch default color so the LED is visible from the start.
        // Same color across patches when a switch is used (see
        // lib/switch-colors.ts).
        led: { on: defaultLedFor(sw) },
        actions: { press: { messages: [] } },
      })),
    };
  }

  function clonePatch(src: Patch, name: string): Patch {
    const clone = JSON.parse(JSON.stringify(src)) as Patch;
    clone.name = name;
    return clone;
  }

  function openNew() {
    const next = nextFreeSlot();
    targetBank = next.bank;
    targetSlot = next.slot;
    targetName = "New patch";
    dialog = "new";
  }

  function openClone() {
    if (!currentPatchEnvelope) return;
    const next = nextFreeSlot();
    targetBank = next.bank;
    targetSlot = next.slot;
    targetName = (currentPatchEnvelope.patch.name ?? "Patch") + " copy";
    dialog = "clone";
  }

  function openDelete() {
    if (!currentPatchEnvelope) return;
    dialog = "delete";
  }

  async function confirm() {
    try {
      if (dialog === "new") {
        const patch = blankPatch(targetName.trim() || "New patch");
        await cmd.putPatch(targetBank, targetSlot, patch);
        await cmd.listPatches();
        await cmd.switchPatch(targetBank, targetSlot);
      } else if (dialog === "clone" && currentPatchEnvelope) {
        const patch = clonePatch(currentPatchEnvelope.patch, targetName.trim() || "Clone");
        await cmd.putPatch(targetBank, targetSlot, patch);
        await cmd.listPatches();
        await cmd.switchPatch(targetBank, targetSlot);
      } else if (dialog === "delete" && currentPatchEnvelope) {
        await cmd.discard();  // drop any unsaved RAM changes for the patch first
        await sendDelete(currentPatchEnvelope.bank, currentPatchEnvelope.slot);
        await cmd.listPatches();
        // Move to first remaining patch
        const remaining = patches.filter(p =>
          !(p.bank === currentPatchEnvelope!.bank && p.slot === currentPatchEnvelope!.slot));
        if (remaining[0]) await cmd.switchPatch(remaining[0].bank, remaining[0].slot);
      }
    } catch (e) {
      console.error("patch action failed:", e);
    } finally {
      dialog = null;
    }
  }

  async function sendDelete(bank: number, slot: number) {
    const { invoke } = await import("@tauri-apps/api/core");
    await invoke("send_command", {
      line: JSON.stringify({ type: "DELETE_PATCH", id: `del-${bank}-${slot}`, bank, slot }),
    });
  }

  let targetIsUsed = $derived(
    patches.some(p => p.bank === targetBank && p.slot === targetSlot) &&
    !(currentPatchEnvelope &&
      currentPatchEnvelope.bank === targetBank &&
      currentPatchEnvelope.slot === targetSlot && dialog === "clone")
  );
</script>

<div class="actions">
  <button onclick={openNew}>+ New patch</button>
  <button onclick={openClone} disabled={!currentPatchEnvelope}>Clone…</button>
  <button class="danger" onclick={openDelete} disabled={!currentPatchEnvelope || patches.length <= 1}>
    Delete…
  </button>
</div>

{#if dialog}
  <div class="overlay" onclick={() => dialog = null} role="presentation"></div>
  <div class="modal" role="dialog">
    {#if dialog === "new" || dialog === "clone"}
      <h3>{dialog === "new" ? "New patch" : "Clone patch"}</h3>
      {#if dialog === "clone" && currentPatchEnvelope}
        <p class="from">
          Copy from
          <code>{patchIdOf(currentPatchEnvelope.bank, currentPatchEnvelope.slot)}</code>
          · {currentPatchEnvelope.patch.name || "(unnamed)"}
        </p>
      {/if}
      <fieldset class="dest">
        <legend>{dialog === "clone" ? "Copy to" : "Location"}</legend>
        <div class="row">
          <label>Bank<input type="number" min="1" max="99" bind:value={targetBank} /></label>
          <label>Slot<input type="number" min="1" max="99" bind:value={targetSlot} /></label>
        </div>
        <label class="full">Name
          <input bind:value={targetName} />
        </label>
      </fieldset>
      {#if targetIsUsed}
        <p class="warn">⚠ Slot {patchIdOf(targetBank, targetSlot)} is already used - it will be overwritten.</p>
      {/if}
      <div class="row right">
        <button onclick={() => dialog = null}>Cancel</button>
        <button class="primary" onclick={confirm}>
          {dialog === "new" ? "Create" : "Clone"}
        </button>
      </div>
    {:else if dialog === "delete" && currentPatchEnvelope}
      <h3>Delete patch?</h3>
      <p>
        <code>{patchIdOf(currentPatchEnvelope.bank, currentPatchEnvelope.slot)}</code>
        · {currentPatchEnvelope.patch.name || "(unnamed)"}
      </p>
      <p class="muted">This removes the file from CIRCUITPY immediately. Not undoable.</p>
      <div class="row right">
        <button onclick={() => dialog = null}>Cancel</button>
        <button class="danger primary" onclick={confirm}>Delete</button>
      </div>
    {/if}
  </div>
{/if}

<style>
  .actions { display: flex; gap: 0.4rem; }
  button { padding: 0.35rem 0.7rem; background: var(--bg-hover); border: 1px solid var(--border-strong); color: var(--text); border-radius: 4px; cursor: pointer; font-size: 0.8rem; }
  button:hover:not(:disabled) { background: var(--bg-hover); }
  button:disabled { opacity: 0.45; cursor: not-allowed; }
  button.danger { color: var(--err); border-color: rgba(239,155,155,0.35); }
  button.danger:hover:not(:disabled) { background: rgba(239,155,155,0.08); }
  button.primary { background: var(--accent-bg); color: var(--accent); border-color: var(--accent-border); font-weight: 600; }
  button.primary.danger { background: var(--err-bg); color: var(--err); border-color: var(--err-border); }

  .overlay { position: fixed; inset: 0; background: var(--overlay-bg); z-index: 90; }
  .modal {
    position: fixed; top: 50%; left: 50%; transform: translate(-50%, -50%);
    background: var(--bg-card); border: 1px solid var(--border); border-radius: 6px;
    z-index: 100; min-width: 360px; padding: 1rem; color: var(--text);
    box-shadow: var(--shadow-modal);
  }
  h3 { margin: 0 0 0.75rem; color: var(--text); }
  .row { display: flex; gap: 0.5rem; align-items: end; margin-bottom: 0.5rem; }
  .row.right { justify-content: flex-end; margin-top: 1rem; }
  label { display: flex; flex-direction: column; gap: 0.2rem; font-size: 0.75rem; color: var(--text-muted); }
  label.full { display: block; margin-bottom: 0.4rem; }
  label.full input { width: 100%; box-sizing: border-box; margin-top: 0.2rem; }
  input { background: var(--bg); color: var(--text); border: 1px solid var(--border-strong); padding: 0.35rem 0.5rem; border-radius: 3px; font-size: 0.85rem; }
  input[type="number"] { width: 4rem; }
  .from { font-size: 0.85rem; color: var(--text-muted); margin: 0 0 0.6rem; }
  .dest { border: 1px solid var(--border); border-radius: 5px; padding: 0.6rem 0.7rem 0.3rem; margin: 0 0 0.6rem; }
  .dest legend { padding: 0 0.35rem; font-size: 0.72rem; text-transform: uppercase; letter-spacing: 0.04em; color: var(--accent); }
  .warn { background: var(--warn-bg); color: var(--warn-text); padding: 0.4rem 0.55rem; border-radius: 3px; font-size: 0.8rem; }
  .muted { color: var(--text-muted); font-size: 0.85rem; }
  code { background: var(--bg); padding: 0.1rem 0.4rem; border-radius: 3px; color: var(--warn-text); font-family: ui-monospace, Consolas, monospace; }
</style>
