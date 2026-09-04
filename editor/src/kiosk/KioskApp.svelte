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

  function bootstrap(): void {
    // Fire-and-forget: responses land in the subscriber below. Mirrors
    // App.svelte's refetchAll for the fields Stage needs.
    cmd.getDeviceInfo();
    cmd.getManifest();
    cmd.getGlobal();
    cmd.listPatches();
    everBooted = true;
  }

  onMount(() => {
    let unsub: (() => void) | null = null;
    let poll: ReturnType<typeof setInterval> | null = null;

    (async () => {
      unsub = await onFirmwareMessage((msg: FirmwareMessage) => {
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
      });

      const sync = async () => {
        const up = await isConnected();
        if (up && !connected) {
          connected = true;
          bootstrap();
        } else if (!up && connected) {
          connected = false;
        }
      };
      await sync();
      poll = setInterval(sync, 1000);
    })();

    return () => {
      unsub?.();
      if (poll) clearInterval(poll);
    };
  });
</script>

{#if connected || everBooted}
  <StageView
    {deviceInfo}
    {manifest}
    device={globalDevice}
    {connected}
    {patches}
    onExit={() => location.reload()}
  />
{:else}
  <div class="kiosk-waiting">Waiting for the pedal...</div>
{/if}
