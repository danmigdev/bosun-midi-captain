<script lang="ts">
  import { collectPatchCatalog } from "../lib/patch-catalog";
  import { MAX_BANKS } from "../lib/bank-layout";
  // Minimal shell around StageView for the Pi kiosk: holds the reactive
  // props StageView needs (deviceInfo, manifest, device.json, patches,
  // connected) and keeps them current from the firmware message bus, the
  // same way App.svelte does for the desktop/Android app - but with only
  // the Stage-relevant subset. StageView itself is used unchanged.
  import { onMount } from "svelte";
  import StageView from "../components/StageView.svelte";
  import {
    cmd,
    sendAndAwait,
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
    stage_input?: boolean;
  };

  let connected = $state(false);
  let deviceInfo = $state<DeviceInfo | null>(null);
  let manifest = $state<Manifest | null>(null);
  let globalDevice = $state<Record<string, unknown> | null>(null);
  let patches = $state<PatchSummary[]>([]);
  let everBooted = $state(false);

  let hasGlobal = false;
  let hasPatchList = false;
  let deviceInfoRetry: ReturnType<typeof setTimeout> | null = null;
  let deviceInfoRefreshPending = false;
  let globalRetry: ReturnType<typeof setTimeout> | null = null;
  let patchListRetry: ReturnType<typeof setTimeout> | null = null;
  let patchListGeneration = 0;
  let patchListRequest: { generation: number; started: number } | null = null;
  let patchListProfile: string | undefined;
  const DEVICE_INFO_RETRY_MS = 1_500;
  const PATCH_LIST_RETRY_MS = 2_500;
  const GLOBAL_RETRY_MS = 10_000;

  function isRecord(value: unknown): value is Record<string, unknown> {
    return value !== null && typeof value === "object" && !Array.isArray(value);
  }

  function requestDeviceInfo(): void {
    if (deviceInfoRetry !== null) return;
    cmd.getDeviceInfo();
    deviceInfoRetry = setTimeout(() => {
      deviceInfoRetry = null;
      if (connected) requestDeviceInfo();
    }, DEVICE_INFO_RETRY_MS);
  }

  function requestPatchList(): void {
    if (!connected || hasPatchList || patchListRequest || patchListRetry !== null) return;
    const request = { generation: patchListGeneration, started: Date.now() };
    patchListRequest = request;
    // Own the response as well as its timeout. An older list may finish after
    // a profile change or a mutation announced by another editor.
    void collectPatchCatalog(page => sendAndAwait<FirmwareMessage>(page, PATCH_LIST_RETRY_MS)).then(({ message }) => {
      const msg = message as FirmwareMessage & { profile?: string };
      if (!connected || patchListRequest !== request) return;
      patchListRequest = null;
      if (request.generation !== patchListGeneration) {
        requestPatchList();
        return;
      }
      const profile = typeof msg.profile === "string" && msg.profile ? msg.profile : undefined;
      if (profile && deviceInfo?.profile && profile !== deviceInfo.profile) {
        // A delayed DEVICE_INFO can still describe the previous profile.
        // Reconcile with the compact identity, without fetching GLOBAL.
        requestDeviceInfo();
        retryPatchList(request.started);
        return;
      }
      if (msg.type !== "PATCH_LIST" || !Array.isArray(msg.patches)
          || !msg.patches.every((patch) => isRecord(patch)
            && Number.isInteger(patch.bank) && Number(patch.bank) >= 1 && Number(patch.bank) <= MAX_BANKS
            && Number.isInteger(patch.slot) && Number(patch.slot) >= 1 && Number(patch.slot) <= 10)) {
        retryPatchList(request.started);
        return;
      }
      patches = msg.patches;
      patchListProfile = profile;
      hasPatchList = true;
    }).catch(() => {
      if (!connected || patchListRequest !== request) return;
      patchListRequest = null;
      if (request.generation !== patchListGeneration) requestPatchList();
      else retryPatchList(request.started);
    });
  }

  function retryPatchList(started: number): void {
    const remaining = PATCH_LIST_RETRY_MS - (Date.now() - started);
    if (remaining <= 0) {
      requestPatchList();
      return;
    }
    patchListRetry = setTimeout(() => {
      patchListRetry = null;
      requestPatchList();
    }, remaining);
  }

  function invalidatePatchList(clear = false): void {
    ++patchListGeneration;
    hasPatchList = false;
    if (clear) {
      patches = [];
      patchListProfile = undefined;
      if (patchListRetry !== null) clearTimeout(patchListRetry);
      patchListRetry = null;
    }
    // Coalesce changes while a list is in flight. Its completion starts one
    // replacement read, and never publishes the older inventory.
    requestPatchList();
  }

  function requestGlobalFallback(): void {
    if (!hasGlobal && globalRetry === null) {
      cmd.getGlobal();
      globalRetry = setTimeout(() => {
        globalRetry = null;
        requestGlobalFallback();
      }, GLOBAL_RETRY_MS);
    }
  }

  function resync(): void {
    // Re-sync current location and inventory on every (re)connect; patches
    // may have been edited while this browser was disconnected.
    // StageView re-pulls CONTEXT + PATCH itself on the same transition.
    requestDeviceInfo();

    // PATCH_LIST is small and supplies the lower-row rig names. It is sent
    // immediately, independently of navigation config. New firmware carries
    // the tiny preset_navigation subtree in DEVICE_INFO; legacy firmware is
    // detected from that response and only then falls back to GET_GLOBAL.
    invalidatePatchList(true);
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
    let disposed = false;
    let announcedLocation = "";
    let announcedUntil = 0;
    const keep = (off: () => void) => disposed ? off() : offs.push(off);

    async function sync() {
      const up = await isConnected();
      // The component may have been torn down while the transport query was
      // pending. Never let that stale continuation recreate bootstrap timers.
      if (disposed) return;
      if (up && !connected) {
        connected = true;
        resync();
      } else if (up) {
        // A PATCH_LIST response can be lost without a link transition.
        requestPatchList();
      } else if (!up && connected) {
        connected = false;
      }
    }

    (async () => {
      keep(await onFirmwareMessage((msg: FirmwareMessage) => {
          switch (msg.type) {
            case "DEVICE_INFO": {
              if (deviceInfoRetry) clearTimeout(deviceInfoRetry);
              deviceInfoRetry = null;
              const rawProfile = (msg as typeof msg & { profile?: unknown }).profile;
              const profile = typeof rawProfile === "string" && rawProfile ? rawProfile : undefined;
              const profileChanged = profile !== undefined
                && ((deviceInfo?.profile !== undefined && profile !== deviceInfo.profile)
                    || (patchListProfile !== undefined && profile !== patchListProfile));
              deviceInfo = {
                fw: msg.fw,
                device: msg.device,
                bank: msg.current?.bank ?? 1,
                slot: msg.current?.slot ?? 1,
                profile,
                stage_input: msg.stage_input === true,
              };
              if (profileChanged) invalidatePatchList(true);
              if (isRecord(msg.tft_colors)) {
                globalDevice = { ...(globalDevice ?? {}), tft_colors: msg.tft_colors };
              }
              if (isRecord(msg.tft_labels)) {
                globalDevice = { ...(globalDevice ?? {}), tft_labels: msg.tft_labels };
              }
              if (isRecord(msg.preset_navigation)) {
                // StageView uses this compact config. Avoiding the full
                // multi-KB GLOBAL stream removes several seconds from boot.
                globalDevice = {
                  ...(globalDevice ?? {}),
                  preset_navigation: msg.preset_navigation,
                  // Absent on older firmware; discard the previous profile's limit.
                  bank_count: msg.bank_count,
                };
                hasGlobal = true;
                if (globalRetry) clearTimeout(globalRetry);
                globalRetry = null;
              } else if (!hasGlobal) {
                // Backward compatibility with firmware that predates the
                // DEVICE_INFO fast path. LIST_PATCHES is already in flight.
                requestGlobalFallback();
              }
              if (deviceInfoRefreshPending) {
                deviceInfoRefreshPending = false;
                requestDeviceInfo();
              }
              break;
            }
            case "MANIFEST":
              manifest = msg as unknown as Manifest;
              break;
            case "GLOBAL":
              // A malformed response must not permanently disarm the retry:
              // preset_navigation comes from this object and without it the
              // lower row cannot be mapped even if PATCH_LIST succeeded.
              if (!isRecord(msg.device)) break;
              globalDevice = msg.device;
              hasGlobal = true;
              if (globalRetry) clearTimeout(globalRetry);
              globalRetry = null;
              break;
            case "CONTEXT": {
              const bank = Number(msg.context?.bank);
              const slot = Number(msg.context?.slot);
              const location = `${bank}/${slot}`;
              if (announcedLocation && Date.now() < announcedUntil
                  && Number.isFinite(bank) && Number.isFinite(slot)
                  && location !== announcedLocation) {
                break;
              }
              if (deviceInfo && Number.isFinite(bank) && Number.isFinite(slot)
                  && (bank !== deviceInfo.bank || slot !== deviceInfo.slot)) {
                deviceInfo = { ...deviceInfo, bank, slot };
              }
              break;
            }
            case "EVENT":
              if (connected && (msg.event === "dirty_state_changed"
                  || msg.event === "saved" || msg.event === "discarded")) {
                // Both CP and native emit these for patch edits/creation,
                // saving and discarding (including removal of dirty patches).
                invalidatePatchList();
              }
              if (msg.event === "global_changed" && connected) {
                // A save may overtake an older DEVICE_INFO snapshot. Read
                // once after that reply rather than keeping its old colors.
                if (deviceInfoRetry !== null) deviceInfoRefreshPending = true;
                else requestDeviceInfo();
              } else if (msg.event === "patch_switched" && deviceInfo) {
                const bank = Number(msg.bank ?? deviceInfo.bank);
                const slot = Number(msg.slot ?? deviceInfo.slot);
                if (connected && (msg.source === "editor"
                    || !patches.some((patch) => patch.bank === bank && patch.slot === slot))) {
                  invalidatePatchList();
                }
                announcedLocation = `${bank}/${slot}`;
                announcedUntil = Date.now() + 2000;
                deviceInfo = {
                  ...deviceInfo,
                  bank,
                  slot,
                };
              }
              break;
          }
        }));
      // Link events already describe the transition. Do not query the current
      // state from both callbacks: a fast down/up pair can make both async
      // queries observe only the final "up" state, losing the reconnect and
      // therefore the bootstrap re-sync.
      keep(await onReconnected(() => {
        connected = true;
        resync();
      }));
      keep(await onDisconnected(() => {
        connected = false;
        if (deviceInfo) deviceInfo = { ...deviceInfo, stage_input: false };
        // Anything in flight on the old CDC session is gone; allow the next
        // link-up to retry immediately rather than waiting for the watchdog.
        if (deviceInfoRetry) clearTimeout(deviceInfoRetry);
        if (globalRetry) clearTimeout(globalRetry);
        if (patchListRetry) clearTimeout(patchListRetry);
        deviceInfoRetry = null;
        deviceInfoRefreshPending = false;
        globalRetry = null;
        patchListRetry = null;
        patchListRequest = null;
        ++patchListGeneration;
      }));
      if (disposed) return;
      await sync();
      if (!disposed) poll = setInterval(sync, 2000);
    })();

    return () => {
      disposed = true;
      for (const off of offs) off();
      if (poll) clearInterval(poll);
      if (deviceInfoRetry) clearTimeout(deviceInfoRetry);
      if (globalRetry) clearTimeout(globalRetry);
      if (patchListRetry) clearTimeout(patchListRetry);
      deviceInfoRetry = null;
      globalRetry = null;
      patchListRetry = null;
      patchListRequest = null;
      ++patchListGeneration;
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
