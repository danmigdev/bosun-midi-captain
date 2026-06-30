<script lang="ts">
  import { onMount, onDestroy } from "svelte";
  import {
    detectPedal,
    flashCircuitPython,
    installFirmware,
    rebootToBootloader,
    phaseOf,
    type DeviceState,
    type InstallPhase,
  } from "../lib/installer";
  import { disconnect, isConnected } from "../lib/protocol";

  type Props = {
    onClose: () => void;
    // When true (auto-opened because an unflashed pedal was detected), show a
    // confirmation gate first that warns the install erases the pedal, before
    // any destructive step runs.
    requireConfirm?: boolean;
    // Called once the firmware has been copied. A running bosun hides its
    // CIRCUITPY drive, so detect_pedal can't confirm success - the app handles
    // closing the wizard and reconnecting to the rebooted pedal instead.
    onInstalled?: () => void;
  };
  let { onClose, requireConfirm = false, onInstalled }: Props = $props();

  let confirmed = $state(false);
  let device = $state<DeviceState | null>(null);
  let busy = $state(false);
  let logs = $state<string[]>([]);
  let pollHandle: ReturnType<typeof setInterval> | null = null;

  let phase = $derived<InstallPhase>(phaseOf(device));

  onMount(async () => {
    // If a serial connection is open, drop it: flashing reboots the device
    // and the open serial handle would error noisily.
    if (await isConnected()) {
      try { await disconnect(); log("Disconnected serial before installer."); } catch {}
    }
    await refresh();
    pollHandle = setInterval(refresh, 2000);
  });

  onDestroy(() => {
    if (pollHandle) clearInterval(pollHandle);
  });

  function log(s: string) {
    logs = [...logs, `${new Date().toLocaleTimeString()}  ${s}`];
  }

  async function refresh() {
    try {
      device = await detectPedal();
    } catch (e) {
      log("detect failed: " + String(e));
    }
  }

  // One-click full recovery: reboot into the bootloader (if needed), flash
  // CircuitPython, then install the firmware. Each step waits for the next
  // device state to appear, so it survives the USB re-enumerations between
  // them. Falls back to a manual instruction in the log if the auto-reboot
  // can't reach a REPL.
  async function doAutoSetup() {
    busy = true;
    try {
      // 1. Reach the UF2 bootloader (RPI-RP2).
      if (!device?.bootloader_drive) {
        log("Rebooting the pedal into the bootloader over USB…");
        try {
          const ports = await rebootToBootloader();
          log("Sent reboot to " + ports + ".");
        } catch (e) {
          log("Couldn't reboot over USB (" + String(e) + "). Do it manually: "
            + "unplug, hold the top-left footswitch, plug the USB back in.");
        }
        await waitFor(s => s.bootloader_drive !== null, 30_000);
        log("Bootloader ready.");
      }
      // 2. Flash CircuitPython (wipes the drive, fixes an old/factory version).
      if (device?.bootloader_drive) {
        log("Flashing CircuitPython 9.2.7…");
        await flashCircuitPython(device.bootloader_drive);
        await waitFor(s => s.circuitpy_drive !== null, 60_000);
        log("CIRCUITPY ready.");
      }
      // 3. Install the firmware onto the fresh CircuitPython.
      if (device?.circuitpy_drive && !device?.has_captain_firmware) {
        log("Installing firmware…");
        const written = await installFirmware(device.circuitpy_drive);
        log(`Copied ${written.length} files. The pedal reboots and reconnects on its own.`);
        if (onInstalled) { onInstalled(); return; }
        await refresh();
      } else if (device?.circuitpy_drive && device?.has_captain_firmware && device?.circuitpython_ok) {
        log("Firmware already installed.");
      }
    } catch (e) {
      log("Auto setup stopped: " + String(e) + ".");
    } finally {
      busy = false;
    }
  }

  async function doFlash() {
    if (!device?.bootloader_drive) return;
    busy = true;
    log("Copying CircuitPython UF2 to " + device.bootloader_drive);
    try {
      await flashCircuitPython(device.bootloader_drive);
      log("UF2 copied. The pedal is rebooting. Waiting for CIRCUITPY to appear…");
      await waitFor(s => s.circuitpy_drive !== null, 60_000);
      log("CIRCUITPY ready.");
    } catch (e) {
      log("Flash error: " + String(e));
    } finally {
      busy = false;
    }
  }

  async function doInstall() {
    if (!device?.circuitpy_drive) return;
    busy = true;
    log("Installing firmware to " + device.circuitpy_drive);
    try {
      const written = await installFirmware(device.circuitpy_drive);
      log(`Copied ${written.length} files. CircuitPython will auto-reload.`);
      if (onInstalled) { onInstalled(); return; }
      await refresh();
    } catch (e) {
      log("Install error: " + String(e));
    } finally {
      busy = false;
    }
  }

  async function waitFor(pred: (s: DeviceState) => boolean, timeoutMs: number) {
    const start = Date.now();
    while (Date.now() - start < timeoutMs) {
      await new Promise(r => setTimeout(r, 1000));
      try { device = await detectPedal(); }
      catch {}
      if (device && pred(device)) return;
    }
    throw new Error("timed out");
  }
</script>

<div class="overlay" onclick={onClose} role="presentation"></div>

<div class="modal" role="dialog" aria-modal="true">
  <header>
    <h2>Install firmware</h2>
    <button class="close" onclick={onClose} aria-label="Close">×</button>
  </header>

  <div class="content">
    {#if requireConfirm && !confirmed}
      <p class="big">A pedal without Bosun firmware is connected.</p>
      <p>Install Bosun on it now? This flashes <strong>CircuitPython 9.x</strong> and
        copies the Bosun firmware. The pedal then reboots and reconnects on its own.</p>
      <p class="backup-warn">
        ⚠ Installing <strong>erases the firmware and every file currently on the pedal</strong>
        (the factory firmware, fonts, any custom files). This cannot be undone from the
        editor. <strong>Back up anything you want to keep first</strong> - copy the pedal's
        files somewhere safe before continuing.
      </p>
      <div class="row">
        <button class="primary" onclick={() => { confirmed = true; doAutoSetup(); }}>
          Install
        </button>
        <button onclick={onClose}>Not now</button>
      </div>
    {:else if phase === "detecting"}
      <p class="big">Looking for the pedal…</p>
    {:else if phase === "assets_missing"}
      <p class="big error">Installer assets are missing.</p>
      <p>The installer needs the CircuitPython UF2 and Adafruit libraries bundled with this app.</p>
      <p>Run <code>tools\download-assets.ps1</code> from the project root, then restart the editor.</p>
      <details>
        <summary>Missing items ({device?.asset_problems.length})</summary>
        <ul>{#each device?.asset_problems ?? [] as p}<li><code>{p}</code></li>{/each}</ul>
      </details>
    {:else if phase === "no_device"}
      <p class="big">No drive detected.</p>
      <p>If the pedal is plugged in and running, Bosun can reboot it into the
        bootloader and set it up automatically:</p>
      <button class="primary" onclick={doAutoSetup} disabled={busy}>
        {busy ? "Working…" : "Set up pedal automatically"}
      </button>
      <p class="backup-warn">⚠ This <strong>erases everything on the pedal</strong> (factory
        firmware and any files). Back up anything you want to keep first.</p>
      <p class="muted">If nothing happens (no firmware to talk to), do it manually: unplug,
        hold the <strong>top-left footswitch</strong>, then plug the USB cable back in. A drive
        named <code>RPI-RP2</code> appears and this wizard takes over.</p>
    {:else if phase === "bootloader"}
      <p class="big">Bootloader detected at <code>{device?.bootloader_drive}</code>.</p>
      <p>Ready to flash CircuitPython and install the firmware. The pedal reboots
        and reconnects on its own when done.</p>
      <button class="primary" onclick={doAutoSetup} disabled={busy}>
        {busy ? "Working…" : "Flash & install automatically"}
      </button>
      <button onclick={doFlash} disabled={busy}>
        {busy ? "Flashing…" : "Flash CircuitPython only"}
      </button>
      <p class="backup-warn">⚠ Flashing <strong>erases everything on the pedal</strong>. Back
        up anything you want to keep first.</p>
    {:else if phase === "circuitpy_wrong_cp"}
      <p class="big error">Incompatible CircuitPython.</p>
      <p>
        This pedal runs <code>CircuitPython {device?.circuitpython_version ?? "(old)"}</code>,
        but the Bosun firmware needs <strong>CircuitPython 9.x</strong>. Installing on top
        of an older version just crashes on boot, so it has to be reflashed first.
      </p>
      <button class="primary" onclick={doAutoSetup} disabled={busy}>
        {busy ? "Working…" : "Reflash & install automatically"}
      </button>
      <p class="muted">This reboots the pedal into the bootloader over USB, flashes
        CircuitPython 9.2.7, and installs the firmware. If the reboot doesn't take, do it
        manually: unplug, hold the <strong>top-left footswitch</strong>, plug the USB back in -
        a drive named <code>RPI-RP2</code> appears and this wizard continues.</p>
      <p class="muted">Flashing CircuitPython wipes the CIRCUITPY drive (any factory files
        go away); your Bosun profiles live in <code>/config</code> and are only created later.</p>
    {:else if phase === "circuitpy_no_firmware"}
      <p class="big">CIRCUITPY at <code>{device?.circuitpy_drive}</code> - Captain firmware not present.</p>
      <button class="primary" onclick={doInstall} disabled={busy}>
        {busy ? "Installing…" : "Install firmware"}
      </button>
    {:else if phase === "installed"}
      <p class="big ok">Firmware installed.</p>
      <p>Version on device: <code>{device?.captain_version ?? "(unknown)"}</code></p>
      <div class="row">
        <button onclick={doInstall} disabled={busy}>
          {busy ? "Reinstalling…" : "Reinstall / Update"}
        </button>
        <button onclick={onClose}>Done</button>
      </div>
    {/if}

    {#if logs.length > 0}
      <h3>Log</h3>
      <pre>{logs.join("\n")}</pre>
    {/if}
  </div>
</div>

<style>
  .overlay {
    position: fixed; inset: 0; background: var(--overlay-bg); z-index: 90;
  }
  .modal {
    position: fixed; top: 50%; left: 50%; transform: translate(-50%, -50%);
    background: var(--bg-card); border: 1px solid var(--border); border-radius: 8px;
    z-index: 100; width: min(560px, 90vw); max-height: 85vh; overflow: auto;
    box-shadow: var(--shadow-modal);
  }
  header {
    display: flex; align-items: center; justify-content: space-between;
    padding: 0.85rem 1rem; border-bottom: 1px solid var(--border);
  }
  h2 { margin: 0; font-size: 1.1rem; color: var(--text); }
  h3 { color: var(--text-muted); margin: 1rem 0 0.4rem; font-size: 0.85rem; }
  .close { background: transparent; border: none; color: var(--text-muted); font-size: 1.4rem; cursor: pointer; padding: 0 0.3rem; }
  .content { padding: 1rem; color: var(--text); }
  .big { font-size: 1.05rem; color: var(--text); margin: 0 0 0.6rem; }
  .big.error { color: var(--err); }
  .big.ok { color: var(--accent); }
  .muted { color: var(--text-muted); font-size: 0.85rem; }
  .backup-warn {
    background: var(--warn-bg, rgba(220, 160, 40, 0.12));
    border: 1px solid var(--warn-border, rgba(220, 160, 40, 0.4));
    color: var(--warn-text, #b5852a);
    border-radius: 4px; padding: 0.55rem 0.7rem; font-size: 0.85rem; line-height: 1.45;
    margin: 0.7rem 0;
  }
  p { line-height: 1.5; }
  code { background: var(--bg); padding: 0.1rem 0.4rem; border-radius: 3px; font-family: ui-monospace, Consolas, monospace; color: var(--warn-text); }
  button { background: var(--bg-hover); color: var(--text); border: 1px solid var(--border-strong); padding: 0.5rem 1rem; border-radius: 4px; cursor: pointer; }
  button:hover:not(:disabled) { background: var(--bg-hover); }
  button:disabled { opacity: 0.45; cursor: not-allowed; }
  button.primary { background: var(--accent-bg); color: var(--accent); border-color: var(--accent-border); font-weight: 600; padding: 0.6rem 1.2rem; }
  button.primary:hover:not(:disabled) { background: var(--accent-hover-bg); }
  .row { display: flex; gap: 0.5rem; }
  pre { background: var(--bg); border: 1px solid var(--border); border-radius: 4px; padding: 0.5rem; font-size: 0.78rem; max-height: 180px; overflow: auto; color: var(--text-muted); }
  details summary { cursor: pointer; color: var(--text-muted); font-size: 0.85rem; }
  ul { font-size: 0.85rem; }
</style>
