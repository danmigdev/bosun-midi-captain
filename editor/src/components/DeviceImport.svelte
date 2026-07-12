<script lang="ts">
  // "Import rig name from device (Kemper)" panel.
  //
  // Reads the CURRENT rig's name from a device that can report it (the Kemper
  // Player) over the firmware protocol and offers to apply it to the open
  // patch's name. This is a v1, single-rig flow on purpose:
  //
  //   Bulk scanning (walk all 125 rigs, read each name) is deliberately NOT
  //   implemented. Reading rig N's name requires SELECTING rig N on the Player,
  //   which actually CHANGES the sound the player hears. Sweeping every rig in
  //   the editor would audibly cycle the amp through 125 rigs - unacceptable as
  //   a background operation. So we only read the rig that is loaded RIGHT NOW.
  //   To name several patches, select each rig on the pedal and click Read.
  //
  // COLOUR: the Player does not report a real per-rig colour over MIDI. The
  // firmware returns a best-effort POSITION colour from the static
  // Bank-Farbcodes chart (keyed by rig position, not rig content), so we offer
  // it as an optional hint the user can apply to the patch's tft_color, clearly
  // labelled as a guess. Both firmware and this note are HARDWARE-UNVERIFIED.
  import { cmd, type Patch } from "../lib/protocol";

  type Props = {
    /** The patch currently open in the editor (with its bank/slot), or null. */
    envelope: { bank: number; slot: number; patch: Patch } | null;
    /** True when the live serial link is up - gates the Read button. */
    connected: boolean;
    /** Called after a successful apply so the parent can re-read the patch
     * and refresh its list (the name changed on the pedal). */
    onApplied?: (bank: number, slot: number) => void;
  };

  let { envelope, connected, onApplied }: Props = $props();

  type RigInfo = { name: string; rig: number | null; color: string | null; fresh: boolean };

  let info = $state<RigInfo | null>(null);
  let reading = $state(false);
  let applying = $state(false);
  let err = $state<string>("");

  function toast(level: "ok" | "error" | "info", message: string) {
    try {
      window.dispatchEvent(new CustomEvent("bosun-toast", { detail: { level, message } }));
    } catch { /* non-Tauri / test env */ }
  }

  // Read the current rig name from the device. The Player normally streams the
  // rig name UNSOLICITED on a rig change, so the firmware usually already has it
  // cached. We fire a fresh request first (best-effort), give the device a
  // moment to answer, then read the (possibly refreshed) cache.
  async function read() {
    if (!connected || reading) return;
    reading = true;
    err = "";
    try {
      // First call requests a refresh from the device (request=true default).
      await cmd.getRigInfo(true);
      // Give the Player a moment to answer the string request before the
      // cache read below (the response arrives asynchronously over SYSEX).
      await new Promise((r) => setTimeout(r, 350));
      // Second call reads the firmware cache only (no extra device round-trip).
      const r = await cmd.getRigInfo(false);
      info = { name: r.name ?? "", rig: r.rig ?? null, color: r.color ?? null, fresh: !!r.fresh };
      if (!info.name) {
        err = "The device hasn't reported a rig name yet. Change the rig on the pedal once, then read again.";
      }
    } catch (e) {
      const msg = String(e);
      // The firmware answers "no_rig_info" when the active profile has no
      // device that can report a rig name (e.g. a generic profile).
      if (msg.includes("no_rig_info")) {
        err = "This profile's device can't report rig names.";
      } else {
        err = "Couldn't read rig info: " + msg;
      }
    } finally {
      reading = false;
    }
  }

  async function applyName() {
    if (!envelope || !info?.name || applying) return;
    applying = true;
    try {
      const next: Patch = { ...envelope.patch, name: info.name };
      await cmd.putPatch(envelope.bank, envelope.slot, next);
      toast("ok", `Patch name set to "${info.name}"`);
      onApplied?.(envelope.bank, envelope.slot);
    } catch (e) {
      toast("error", "Couldn't set patch name: " + String(e));
    } finally {
      applying = false;
    }
  }

  async function applyColor() {
    if (!envelope || !info?.color || applying) return;
    applying = true;
    try {
      const next: Patch = { ...envelope.patch, tft_color: info.color };
      await cmd.putPatch(envelope.bank, envelope.slot, next);
      toast("ok", `Patch colour set to ${info.color}`);
      onApplied?.(envelope.bank, envelope.slot);
    } catch (e) {
      toast("error", "Couldn't set patch colour: " + String(e));
    } finally {
      applying = false;
    }
  }
</script>

<section class="device-import">
  <div class="di-head">
    <h3>Import rig name from device</h3>
    <button class="di-read" onclick={read} disabled={!connected || reading}>
      {reading ? "Reading…" : "Read current rig"}
    </button>
  </div>

  <p class="di-hint">
    Reads the name of the rig loaded on the Kemper right now and lets you use it
    as this patch's name. To name several patches, select each rig on the pedal
    and read again - the editor never sweeps rigs on its own (that would change
    the sound you hear).
  </p>

  {#if err}
    <p class="di-err">{err}</p>
  {/if}

  {#if info?.name}
    <div class="di-result">
      <div class="di-row">
        <span class="di-label">Rig name</span>
        <span class="di-value">{info.name}</span>
        {#if info.rig != null}<span class="di-rig">rig {info.rig}</span>{/if}
        {#if !info.fresh}
          <span class="di-stale" title="The cached name may be from a previous rig. Read again after changing the rig on the pedal.">may be stale</span>
        {/if}
      </div>
      <div class="di-actions">
        <button class="primary" onclick={applyName} disabled={applying || !envelope}>
          Use as patch name
        </button>
        {#if info.color}
          <button onclick={applyColor} disabled={applying || !envelope}
                  title="Best-effort colour from the rig position (the device does not report a real per-rig colour)">
            <span class="di-swatch" style="background:{info.color}"></span>
            Apply position colour
          </button>
        {/if}
      </div>
      {#if !envelope}
        <p class="di-hint">Open a patch in the editor to apply the name.</p>
      {/if}
    </div>
  {/if}
</section>

<style>
  .device-import {
    border: 1px solid var(--border);
    border-radius: 8px;
    padding: 0.85rem 1rem;
    background: var(--bg-card);
    margin: 0 0 1rem;
  }
  .di-head {
    display: flex; align-items: center; gap: 0.75rem;
    justify-content: space-between;
  }
  .di-head h3 { margin: 0; font-size: 0.95rem; }
  .di-hint { color: var(--text-muted); font-size: 0.8rem; margin: 0.5rem 0 0; }
  .di-err { color: var(--err); font-size: 0.82rem; margin: 0.5rem 0 0; }
  .di-result { margin-top: 0.75rem; }
  .di-row { display: flex; align-items: center; gap: 0.6rem; flex-wrap: wrap; }
  .di-label { color: var(--text-muted); font-size: 0.78rem; }
  .di-value { font-weight: 600; font-size: 0.95rem; }
  .di-rig { color: var(--text-dim); font-size: 0.75rem; }
  .di-stale {
    color: var(--warn-text); background: var(--warn-bg);
    font-size: 0.72rem; padding: 0.05rem 0.4rem; border-radius: 4px;
  }
  .di-actions { display: flex; gap: 0.5rem; margin-top: 0.6rem; flex-wrap: wrap; }
  .di-swatch {
    display: inline-block; width: 0.75rem; height: 0.75rem;
    border-radius: 3px; margin-right: 0.35rem; vertical-align: middle;
    border: 1px solid var(--border-strong);
  }
</style>
