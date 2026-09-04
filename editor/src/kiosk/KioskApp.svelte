<script lang="ts">
  // Minimal shell around StageView for the Pi kiosk: holds the reactive
  // props StageView needs (deviceInfo, manifest, device.json, patches,
  // connected) and keeps them current from the firmware message bus, the
  // same way App.svelte does for the desktop/Android app - but with only
  // the Stage-relevant subset. StageView itself is used unchanged.
  import { onMount } from "svelte";
  import StageView from "../components/StageView.svelte";
  import {
    cmd,
    isConnected,
    onFirmwareMessage,
    onDisconnected,
    onReconnected,
    type FirmwareMessage,
    type Manifest,
    type PatchSummary,
  } from "../lib/protocol";

  type DeviceInfo = {
    fw: string;
    device: string;
    bank: number;
    slot: number;
    profile?: string;
  };

  let connected = $state(false);
  let deviceInfo = $state<DeviceInfo | null>(null);
  let manifest = $state<Manifest | null>(null);
  let globalDevice = $state<Record<string, unknown> | null>(null);
  let patches = $state<PatchSummary[]>([]);
  let everBooted = $state(false);

  let fullBootstrapDone = false;

  function resync(): void {
    // Cheap re-sync on every (re)connect: just the current bank/slot.
    // StageView re-pulls CONTEXT + PATCH itself on the same transition.
    cmd.getDeviceInfo();

    if (!fullBootstrapDone) {
      fullBootstrapDone = true;
      cmd.getGlobal();
      cmd.listPatches();
    }
    everBooted = true;

    // The manifest is deliberately NOT fetched. On the RP2040 it streams
    // field-by-field for ~7 s as a background generator, and a GET_PATCH
    // arriving mid-stream (every rig change) can wedge that generator,
    // permanently queueing every later CONTEXT push / EVENT behind it -
    // the Stage view then goes deaf to effect toggles. StageView only
    // uses the manifest for label fallbacks on UNLABELLED bindings
    // (uncommon - switches carry their own label), so the Stage view
    // does without it.
  }

  onMount(() => {
    const offs: Array<() => void> = [];
    let poll: ReturnType<typeof setInterval> | null = null;

    async function sync() {
      const up = await isConnected();
      if (up && !connected) {
        connected = true;
        resync();
      } else if (!up && connected) {
        connected = false;
      }
    }

    (async () => {
      offs.push(
        await onFirmwareMessage((msg: FirmwareMessage) => {
          switch (msg.type) {
            case "DEVICE_INFO":
              deviceInfo = {
                fw: msg.fw,
                device: msg.device,
                bank: msg.current?.bank ?? 1,
                slot: msg.current?.slot ?? 1,
              };
              break;
            case "MANIFEST":
              manifest = msg as unknown as Manifest;
              break;
            case "GLOBAL":
              globalDevice = msg.device ?? null;
              break;
            case "PATCH_LIST":
              patches = msg.patches ?? [];
              break;
            case "EVENT":
              if (msg.event === "patch_switched" && deviceInfo) {
                deviceInfo = {
                  ...deviceInfo,
                  bank: Number(msg.bank ?? deviceInfo.bank),
                  slot: Number(msg.slot ?? deviceInfo.slot),
                };
              }
              break;
          }
        }),
      );
      // React to link transitions immediately; the poll is just a backstop.
      offs.push(await onReconnected(() => void sync()));
      offs.push(await onDisconnected(() => void sync()));
      await sync();
      poll = setInterval(sync, 2000);
    })();

    return () => {
      for (const off of offs) off();
      if (poll) clearInterval(poll);
    };
  });
</script>

{#if everBooted}
  <StageView
    {deviceInfo}
    {manifest}
    device={globalDevice}
    {connected}
    {patches}
    onExit={() => location.reload()}
  />
  {#if !connected}
    <div class="kiosk-reconnecting">reconnecting…</div>
  {/if}
{:else}
  <div class="kiosk-waiting">Waiting for the pedal…</div>
{/if}

<style>
  .kiosk-reconnecting {
    position: fixed;
    top: 0.6rem;
    left: 50%;
    transform: translateX(-50%);
    z-index: 50;
    padding: 0.2rem 0.9rem;
    border-radius: 999px;
    font-size: 0.9rem;
    letter-spacing: 0.03em;
    color: var(--warn-text, #f4cd7a);
    background: var(--warn-bg, rgba(217, 155, 111, 0.18));
  }
</style>
