<script lang="ts">
  import { onMount, onDestroy } from "svelte";
  import { invoke } from "@tauri-apps/api/core";
  type Candidate = { id: string; label: string; port: string };
  type Discovery = { devices: Candidate[]; version: string | null; problem: string | null };
  let { onClose, onStart }: { onClose: () => void; onStart: (candidate: string, version: string) => void } = $props();
  let discovery = $state<Discovery | null>(null), error = $state("");
  let selected = $state(""), confirmed = $state(false);
  let dialog: HTMLDialogElement, poll: ReturnType<typeof setTimeout> | undefined, alive = true;
  async function refresh() {
    try {
      const next = await invoke<Discovery>("factory_install_discover");
      if (!alive) return;
      discovery = next; error = "";
      if (!next.devices.some(d => d.id === selected)) { selected = next.devices.length === 1 ? next.devices[0].id : ""; confirmed = false; }
    } catch (e) { if (alive) error = String(e); }
    if (alive) poll = setTimeout(refresh, 2500);
  }
  onMount(() => { dialog.showModal(); void refresh(); });
  onDestroy(() => { alive = false; clearTimeout(poll); });
</script>
<dialog bind:this={dialog} aria-labelledby="install-title" oncancel={event => { event.preventDefault(); onClose(); }}>
  <h2 id="install-title">Install native Bosun</h2>
  <p>Connect your MIDI Captain directly to the computer with a USB data cable. Bosun will install version <strong>{discovery?.version || "checking..."}</strong>, without installing CircuitPython first.</p>
  {#if discovery?.problem}<p role="alert">Installer package unavailable: {discovery.problem}</p><p>Download and extract the complete latest Bosun Desktop release.</p>{/if}
  {#if discovery?.devices.length}
    <label>Captain to install<select bind:value={selected} onchange={() => confirmed = false}><option value="">Select a device</option>{#each discovery.devices as d}<option value={d.id}>{d.label}</option>{/each}</select></label>
    <label class="confirm"><input type="checkbox" bind:checked={confirmed} /> This is my 10-switch RP2040 MIDI Captain with factory firmware. I want to install Bosun and create new profiles.</label>
  {:else}<p role="status">Waiting for the Captain. Connect it normally with a USB data cable and close other serial apps. Bosun requests the USB bootloader after you choose Install. Holding footswitch 1 opens USB Setup, not RPI-RP2.</p>{/if}
  <p>Bosun saves and verifies a complete backup before writing. The backup preserves factory settings; you create Bosun profiles after installation.</p>
  <p>Keep the Captain on the same USB port and keep your computer and pedal powered until the process finishes.</p>
  {#if error}<p role="alert">{error}</p>{/if}
  <footer><button onclick={onClose}>Cancel</button><button class="primary" disabled={!confirmed || !selected || !discovery?.version || !!discovery?.problem} onclick={() => onStart(selected,discovery!.version!)}>Continue</button></footer>
</dialog>
<style>
  dialog{width:min(590px,92vw);padding:1.5rem;background:var(--bg-card);color:var(--text);border:1px solid var(--border);border-radius:12px;max-height:88vh;overflow:auto}dialog::backdrop{background:#080d16cc}h2{margin-top:0;font-size:1.2rem}p,label{font-size:.9rem;line-height:1.6}label{display:block;margin:1rem 0}select{display:block;width:100%;margin-top:.4rem;padding:.6rem;background:var(--bg);color:var(--text);border:1px solid var(--border-strong)}.confirm{padding:.8rem;background:var(--bg);border-radius:6px}.confirm input{margin-right:.4rem}footer{display:flex;justify-content:flex-end;gap:.6rem;margin-top:1.5rem}button{padding:.6rem 1rem;border:1px solid var(--border-strong);border-radius:5px;background:var(--bg);color:var(--text);cursor:pointer}.primary{background:var(--accent);color:var(--bg)}button:disabled{opacity:.45;cursor:not-allowed}[role=alert]{color:var(--err);overflow-wrap:anywhere}
</style>
