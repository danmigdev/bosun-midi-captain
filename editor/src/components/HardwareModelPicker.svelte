<script lang="ts">
  // Choose which MIDI Captain the connected pedal is. The firmware stores the
  // model outside every profile and restarts when it changes; a wrong choice
  // only maps switches and LEDs incorrectly and can be changed back here.
  import { cmd, waitForReboot } from "../lib/protocol";
  import { KNOWN_HARDWARE, hardwareLabel, knownHardware, type HardwareLayout } from "../lib/hardware";

  type Props = {
    hardware: HardwareLayout;
    disabled?: boolean;
    /** Called after the firmware stored the model (and restarted, if needed). */
    onApplied?: (model: HardwareLayout) => void;
    /** Busy state for hosts that pause their own pedal traffic meanwhile. */
    onBusy?: (busy: boolean) => void;
  };
  let { hardware, disabled = false, onApplied, onBusy }: Props = $props();

  let options = $derived(KNOWN_HARDWARE.filter((h) => hardware.models.includes(h.model)));
  let choice = $state("");
  let busy = $state(false);
  let message = $state("");
  let failed = $state(false);

  // Follow the pedal: a new DEVICE_INFO (reconnect, other pedal) resets the choice.
  $effect(() => { choice = hardware.model; });

  let target = $derived(knownHardware(choice));
  let restarts = $derived(!!target && target.model !== hardware.model);
  let unchanged = $derived(!!target && !restarts && hardware.configured);

  async function apply() {
    if (!target || busy || disabled || unchanged) return;
    const selected = target;
    busy = true; failed = false; onBusy?.(true);
    message = restarts ? `Restarting the pedal as ${selected.name}...` : "Saving...";
    try {
      const reply = await cmd.setHardware(selected.model);
      if (reply.reboot) {
        const back = await waitForReboot(15000);
        if (!back) {
          failed = true;
          message = "The pedal did not come back within 15 s. Reconnect it manually.";
          return;
        }
      } else {
        cmd.getDeviceInfo().catch(() => {});
      }
      message = `This pedal is set up as a ${hardwareLabel(selected)}.`;
      onApplied?.(selected);
    } catch (e) {
      failed = true;
      message = `Could not change the model: ${String(e)}`;
    } finally {
      busy = false; onBusy?.(false);
    }
  }
</script>

{#if options.length === 0}
  <p class="muted small">
    This firmware always runs as the {hardwareLabel(hardware)}. Install Bosun 0.8 or
    later to use a MIDI Captain Mini 6.
  </p>
{:else}
  <fieldset class="models" disabled={disabled || busy}>
    <legend class="sr-only">Pedal model</legend>
    {#each options as option (option.model)}
      <label class="model" class:selected={choice === option.model}>
        <input type="radio" name="hardware-model" value={option.model} bind:group={choice} />
        <span class="name">{hardwareLabel(option)}</span>
        {#if option.model === hardware.model}
          <span class="tag">{hardware.configured ? "current" : "current, not confirmed"}</span>
        {/if}
      </label>
    {/each}
  </fieldset>
  <p class="muted small">
    Pick the model printed on your pedal. The Mini 6 has switches 1, 2, 3, A, B and C
    and no expression jacks.
  </p>
  <div class="row">
    <button class="primary" onclick={apply} disabled={disabled || busy || !target || unchanged}>
      {#if unchanged}Model confirmed{:else if restarts}Apply and restart{:else}Confirm model{/if}
    </button>
  </div>
{/if}
{#if message}
  <p class="status" class:err={failed} role="status">{message}</p>
{/if}

<style>
  .models { border: 0; margin: 0 0 0.4rem; padding: 0; display: flex; flex-direction: column; gap: 0.35rem; }
  .model {
    display: flex; align-items: center; gap: 0.55rem; padding: 0.45rem 0.6rem;
    border: 1px solid var(--border); border-radius: 5px; cursor: pointer; background: var(--bg);
  }
  .model.selected { border-color: var(--accent-border); }
  .name { flex: 1; }
  .tag { font-size: 0.75rem; color: var(--text-muted); }
  .row { display: flex; gap: 0.5rem; margin: 0.4rem 0; }
  .muted { color: var(--text-muted); }
  .small { font-size: 0.8rem; }
  .status { color: var(--text-muted); font-size: 0.8rem; margin: 0.4rem 0 0; }
  .status.err { color: var(--err); }
  .sr-only { position: absolute; width: 1px; height: 1px; overflow: hidden; clip: rect(0 0 0 0); white-space: nowrap; }
</style>
