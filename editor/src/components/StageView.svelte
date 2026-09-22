<script lang="ts">
  import { onMount, onDestroy, tick } from "svelte";
  import "../lib/stage-surface.css";
  import {
    cmd,
    sendAndAwait,
    onDisconnected,
    onReconnecting,
    onFirmwareMessage,
    summarizeMessage,
    type Binding,
    type FirmwareMessage,
    type Manifest,
    type PatchSummary,
  } from "../lib/protocol";
  import { DEFAULT_LAYOUT } from "../lib/pedal-layout";
  import { getBankCount, MAX_BANKS } from "../lib/bank-layout";
  import { ledColorFor } from "../lib/led-color";
  import {
    readSavedStageTheme, saveStageTheme, stageThemeToCssVars, type StageTheme,
  } from "../lib/stage-theme";
  import StageThemeEditor from "./StageThemeEditor.svelte";
  import StageBankPicker from "./StageBankPicker.svelte";
  import StageTuner from "./StageTuner.svelte";
  import StageMorph from "./StageMorph.svelte";
  import { readBankSelectionMode, saveBankSelectionMode, type BankSelectionMode } from "../lib/stage-behavior";
  import "../lib/stage-controls.css";

  type Props = {
    deviceInfo: { fw: string; device: string; bank: number; slot: number; profile?: string; stage_input?: boolean } | null;
    manifest: Manifest | null;
    device: Record<string, unknown> | null;
    connected: boolean;
    patches: PatchSummary[];
    onExit: () => void;
  };
  let { deviceInfo, manifest, device, connected, patches, onExit }: Props = $props();
  let bankCount = $derived(getBankCount(device));

  // Preset-navigation row (e.g. rig-select switches): a device-level
  // overlay, not a patch binding, so it never shows up in fullPatch.bindings
  // - mirrors firmware's _paint_preset_nav_leds (captain/app.py), which
  // paints these switches from device.preset_navigation, not the patch.
  // Without this a nav switch had no binding to read a label from and
  // rendered "-" forever (2026-08-14: reported as "bottom row shows no
  // rig names").
  type PresetNav = {
    switches?: Record<string, string | number>;
    bank_colors?: Record<string, string>;
  };
  let presetNav = $derived((device?.preset_navigation as PresetNav | undefined) ?? undefined);

  function navSlotFor(sw: string): number | null {
    const raw = presetNav?.switches?.[sw];
    if (raw === undefined) return null;
    const slot = Number(raw);
    return Number.isFinite(slot) ? slot : null;
  }

  /** The patch a nav switch targets IN THE CURRENT BANK, or null if the
   *  switch isn't mapped or that slot has no saved patch. Mirrors the
   *  firmware's available_slots gate (_paint_preset_nav_leds / bindings.py):
   *  a mapped switch pointing at an empty slot is fully inert on the real
   *  pedal (LED off, no navigation) - showing a "RIG N" placeholder here
   *  when nothing's there would claim a working switch that does nothing
   *  when pressed. Only a slot that actually holds a patch counts as bound. */
  function navPatchFor(sw: string): PatchSummary | null {
    const slot = navSlotFor(sw);
    if (slot === null || !deviceInfo) return null;
    return patches.find((p) => p.bank === deviceInfo.bank && p.slot === slot) ?? null;
  }

  // --- live state ---
  let context = $state<Record<string, unknown>>({});
  async function controlMorph(action: "position" | "trigger", percent?: number) {
    if (!connected || !deviceInfo?.profile || context.kemper_morph_ready !== "on"
        || typeof context.kemper_generation !== "number") throw new Error("Rig is not ready");
    await cmd.morphControl({ bank: deviceInfo.bank, slot: deviceInfo.slot,
      profile: deviceInfo.profile, generation: context.kemper_generation }, action, percent);
    await cmd.getContext();
  }
  let unsubFw: (() => void) | null = null;
  type KemperReconcile = {
    value: "on" | "off";
    timer: ReturnType<typeof setTimeout>;
  };
  // Each block needs an independent confirmation fence. A single shared
  // timer lets a second fast footswitch press cancel protection for the
  // first, so an already-queued CONTEXT can visibly bounce that first LED.
  const kemperReconciles = new Map<string, KemperReconcile>();

  // --- stage theme (per-section font/color/size, see stage-theme.ts) ---
  let stageTheme = $state<StageTheme>(readSavedStageTheme());
  let showThemeEditor = $state(false);
  let stageThemeVars = $derived(stageThemeToCssVars(stageTheme));
  // Short overlapping pieces bend the whole 288px beam around the frame.
  // A long straight element would rotate through, and be clipped by, corners.
  const borderBeam = Array.from({ length: 48 }, (_, index) => ({
    offset: (index + 0.5) * 6 - 144,
    opacity: Math.sin(Math.PI * (index + 0.5) / 48) ** 2,
  }));
  let borderSecondsPerPixel = $state(0);
  let stageActionHeight = $state<number | undefined>();
  let stageActionWidth = $state<number | undefined>();
  type StageHeaderGeometry = {
    top: number; left: number; width: number; height: number;
    actionTop: number; actionLeft: number; actionOffsetTop: number; actionOffsetRight: number;
  };
  let stageHeaderGeometry = $state<StageHeaderGeometry | undefined>();

  function measureStageHeader(node: HTMLElement) {
    // Dialog controls follow the real header, including portrait wrapping and
    // theme font changes. Measure only the Stage header, never its dialog copy.
    const action = node.querySelector<HTMLButtonElement>('[aria-label="Exit Stage"]');
    if (!action) return;
    const stage = node.parentElement;
    const view = node.ownerDocument.defaultView;
    let destroyed = false;
    let scheduled = false;
    const measure = () => {
      if (stage) {
        const width = Math.max(0, stage.clientWidth - 8);
        const height = Math.max(0, stage.clientHeight - 8);
        const radius = Math.max(0, Math.min(width / 2, height / 2,
          parseFloat(getComputedStyle(stage).borderTopLeftRadius) - 4));
        const perimeter = 2 * (width + height) + (2 * Math.PI - 8) * radius;
        if (perimeter > 0) borderSecondsPerPixel = 24 / perimeter;
      }
      const headerRect = node.getBoundingClientRect();
      const actionRect = action.getBoundingClientRect();
      if (actionRect.height > 0) stageActionHeight = actionRect.height;
      if (actionRect.width > 0) stageActionWidth = actionRect.width;
      if (headerRect.width <= 0 || headerRect.height <= 0) return;
      const next: StageHeaderGeometry = {
        top: headerRect.top, left: headerRect.left,
        width: headerRect.width, height: headerRect.height,
        actionTop: actionRect.top, actionLeft: actionRect.left,
        actionOffsetTop: actionRect.top - headerRect.top,
        actionOffsetRight: headerRect.right - actionRect.right,
      };
      if (!stageHeaderGeometry || (Object.keys(next) as Array<keyof StageHeaderGeometry>)
        .some((key) => stageHeaderGeometry?.[key] !== next[key])) stageHeaderGeometry = next;
    };
    const scheduleMeasure = () => {
      if (scheduled || destroyed) return;
      scheduled = true;
      void tick().then(() => {
        scheduled = false;
        if (!destroyed) measure();
      });
    };
    // A sibling can move the controls without resizing either the header or
    // the X button, so include the header's immediate layout children.
    const observer = new ResizeObserver(scheduleMeasure);
    observer.observe(node, { box: "border-box" });
    observer.observe(action, { box: "border-box" });
    for (const child of node.children) observer.observe(child, { box: "border-box" });
    if (stage) observer.observe(stage, { box: "border-box" });
    const mutations = new MutationObserver(scheduleMeasure);
    mutations.observe(node, { subtree: true, childList: true, characterData: true, attributes: true });
    if (stage) mutations.observe(stage, { attributes: true, attributeFilter: ["style", "class"] });
    view?.addEventListener("resize", scheduleMeasure);
    view?.addEventListener("scroll", scheduleMeasure, true);
    view?.visualViewport?.addEventListener("resize", scheduleMeasure);
    node.ownerDocument.fonts?.addEventListener("loadingdone", scheduleMeasure);
    scheduleMeasure();
    return { destroy() {
      destroyed = true;
      observer.disconnect();
      mutations.disconnect();
      view?.removeEventListener("resize", scheduleMeasure);
      view?.removeEventListener("scroll", scheduleMeasure, true);
      view?.visualViewport?.removeEventListener("resize", scheduleMeasure);
      node.ownerDocument.fonts?.removeEventListener("loadingdone", scheduleMeasure);
    } };
  }

  function synchronizePulses(node: HTMLElement) {
    if (typeof node.getAnimations !== "function") return;
    const timelineTime = node.ownerDocument.timeline?.currentTime;
    const origin = typeof timelineTime === "number" ? timelineTime : 0;
    const isPulse = (name: string) => /stage-(light-pulse|divider-wide-pulse)$/.test(name);
    let destroyed = false;
    const synchronize = () => {
      // One shared timeline origin includes CSS pseudo-elements and switches
      // activated later. The browser animates opacity; there is no JS frame loop.
      for (const animation of node.getAnimations({ subtree: true })) {
        if (isPulse((animation as CSSAnimation).animationName ?? "")) animation.startTime = origin;
      }
    };
    const started = (event: AnimationEvent) => {
      if (isPulse(event.animationName)) synchronize();
    };
    node.addEventListener("animationstart", started);
    // Include already-created animations as well as future activation and
    // reduced-motion changes, which create new CSS animations of their own.
    void tick().then(() => { if (!destroyed) synchronize(); });
    return { destroy() {
      destroyed = true;
      node.removeEventListener("animationstart", started);
    } };
  }
  let bankChangePending = $state(false);
  let bankChangeError = $state("");
  let showBankPicker = $state(false);
  let bankPickerLoading = $state(false);
  let bankPickerLoaded = $state(false);
  let bankPickerError = $state("");
  let bankPickerCurrent = $state<number | null>(null);
  let bankPickerProfile: string | undefined;
  let bankPickerSession = 0;
  let bankPickerSubscriptions: Array<() => void> = [];
  let bankSelectionMode = $state<BankSelectionMode>(readBankSelectionMode());
  type PreselectedBank = { bank: number; profile?: string; originBank: number; originSlot: number };
  let preselectedBank = $state<PreselectedBank | null>(null);
  const preselectionSubscriptions: Array<() => void> = [];

  function cancelPreselection() {
    if (!bankChangePending) {
      preselectedBank = null;
      bankChangeError = "";
    }
  }

  function changeBankSelectionMode(mode: BankSelectionMode) {
    if (bankChangePending) return;
    bankSelectionMode = mode;
    saveBankSelectionMode(mode);
    preselectedBank = null;
    bankPickerError = "";
  }

  async function watchPreselectionConnection() {
    for (const subscribe of [onDisconnected, onReconnecting]) {
      try {
        const unsubscribe = await subscribe(() => { preselectedBank = null; });
        if (_stopped) unsubscribe();
        else preselectionSubscriptions.push(unsubscribe);
      } catch { /* Reactive link changes still clear the preview. */ }
    }
  }
  let pendingSwitch = $state<string | null>(null);
  let switchActionError = $state("");
  let refreshedInventory = $state.raw<{
    source: PatchSummary[]; profile?: string; patches: PatchSummary[];
  } | null>(null);
  let bankInventory = $derived(refreshedInventory?.source === patches
    && refreshedInventory.profile === deviceInfo?.profile ? refreshedInventory.patches : patches);

  // Preserve configured rig-switch positions, then use remaining positions
  // for unmapped slots. This exposes all ten possible slots even on a generic
  // profile without preset navigation; live effect bindings are not executed.
  let preselectionSlots = $derived.by(() => {
    const slots = new Map<string, number>();
    const used = new Set<number>();
    if (!preselectedBank) return slots;
    const switches = DEFAULT_LAYOUT.flat();
    for (const sw of switches) {
      const slot = navSlotFor(sw);
      if (slot !== null && Number.isInteger(slot) && slot >= 1 && slot <= 10 && !used.has(slot)) {
        slots.set(sw, slot); used.add(slot);
      }
    }
    const available = Array.from({ length: 10 }, (_, index) => index + 1).filter(slot => !used.has(slot));
    for (const sw of switches) if (!slots.has(sw)) slots.set(sw, available.shift()!);
    return slots;
  });

  function preselectionPatchFor(sw: string): PatchSummary | null {
    if (!preselectedBank) return null;
    const bank = preselectedBank.bank;
    const slot = preselectionSlots.get(sw);
    return validPatches(bankInventory).find(p => p.bank === bank && p.slot === slot) ?? null;
  }

  function canSelectPreselectedRig(sw: string) {
    return connected && !!preselectedBank && preselectedBank.profile === deviceInfo?.profile
      && !bankChangePending && pendingSwitch === null && !showBankPicker && !!preselectionPatchFor(sw);
  }

  async function selectPreselectedRig(sw: string) {
    if (!canSelectPreselectedRig(sw)) return;
    const patch = preselectionPatchFor(sw)!;
    if (await changeBank({ bank: patch.bank, slot: patch.slot })) preselectedBank = null;
  }

  function validPatches(inventory: PatchSummary[], limit = bankCount) {
    return inventory.filter(p => Number.isInteger(p.bank) && p.bank >= 1 && p.bank <= limit
      && Number.isInteger(p.slot) && p.slot >= 1 && p.slot <= 10);
  }

  let bankOptions = $derived.by(() => {
    const slots = new Map<number, Set<number>>();
    for (const patch of validPatches(bankInventory)) {
      if (!slots.has(patch.bank)) slots.set(patch.bank, new Set());
      slots.get(patch.bank)!.add(patch.slot);
    }
    return [...slots].sort(([a], [b]) => a - b).map(([bank, entries]) => ({
      bank, count: entries.size, color: presetNav?.bank_colors?.[String(bank)],
    }));
  });

  function bankDestination(bank: number, current: { bank: number; slot: number }, inventory: PatchSummary[], limit = bankCount) {
    const slots = validPatches(inventory, limit).filter(p => p.bank === bank).map(p => p.slot).sort((a, b) => a - b);
    return slots.length ? { bank, slot: slots.includes(current.slot) ? current.slot : slots[0] } : null;
  }

  function dismissBankPicker(force = false) {
    if (bankChangePending && !force) return;
    ++bankPickerSession;
    showBankPicker = false;
    bankPickerLoading = false;
    bankPickerError = "";
    for (const unsubscribe of bankPickerSubscriptions) unsubscribe();
    bankPickerSubscriptions = [];
  }

  async function refreshBankPicker() {
    if (!showBankPicker || !connected || bankPickerLoading || bankChangePending) return;
    const session = bankPickerSession;
    bankPickerLoading = true;
    bankPickerLoaded = false;
    bankPickerError = "";
    const current = () => !_stopped && showBankPicker && connected
      && session === bankPickerSession && deviceInfo?.profile === bankPickerProfile;
    try {
      const info = await sendAndAwait<FirmwareMessage & { profile?: string }>({ type: "GET_DEVICE_INFO" });
      if (!current()) return;
      const inventory = await sendAndAwait<FirmwareMessage & { profile?: string }>({ type: "LIST_PATCHES" });
      if (!current()) return;
      if (info.type !== "DEVICE_INFO" || inventory.type !== "PATCH_LIST"
          || !info.current || !Array.isArray(inventory.patches)
          || !Number.isInteger(info.current.bank) || info.current.bank < 1 || info.current.bank > MAX_BANKS
          || !Number.isInteger(info.current.slot) || info.current.slot < 1 || info.current.slot > 10
          || (info.profile && inventory.profile && info.profile !== inventory.profile)
          || (info.profile && bankPickerProfile && info.profile !== bankPickerProfile)) {
        throw new Error("Invalid bank inventory");
      }
      refreshedInventory = { source: patches, profile: deviceInfo?.profile, patches: inventory.patches };
      bankPickerCurrent = info.current.bank;
      bankPickerLoaded = true;
    } catch {
      if (current()) bankPickerError = "Unable to load banks.";
    } finally {
      if (session === bankPickerSession) bankPickerLoading = false;
    }
  }

  async function openBankPicker() {
    if (_stopped || !connected || !deviceInfo || bankChangePending || pendingSwitch !== null || showBankPicker) return;
    const session = ++bankPickerSession;
    bankPickerProfile = deviceInfo.profile;
    bankPickerCurrent = deviceInfo.bank;
    bankChangeError = "";
    bankPickerError = "";
    bankPickerLoaded = false;
    showThemeEditor = false;
    showBankPicker = true;
    bankPickerLoading = true;
    try {
      for (const subscribe of [onDisconnected, onReconnecting]) {
        const unsubscribe = await subscribe(() => dismissBankPicker(true));
        if (_stopped || !showBankPicker || session !== bankPickerSession) { unsubscribe(); return; }
        bankPickerSubscriptions.push(unsubscribe);
      }
      bankPickerLoading = false;
      await refreshBankPicker();
    } catch {
      if (session === bankPickerSession && showBankPicker) {
        bankPickerLoading = false;
        bankPickerError = "Unable to load banks.";
      }
    }
  }

  async function selectBank(bank: number) {
    if (!showBankPicker || bankPickerLoading || bankChangePending || !connected || pendingSwitch !== null) return;
    if (bank === bankPickerCurrent) { preselectedBank = null; dismissBankPicker(); return; }
    const session = bankPickerSession;
    if (await changeBank({ bank })) {
      if (session === bankPickerSession) dismissBankPicker();
    }
  }

  let navigationPosition = $derived(deviceInfo && preselectedBank
    ? { bank: preselectedBank.bank, slot: deviceInfo.slot } : deviceInfo);

  function bankTarget(delta: -1 | 1, current: { bank: number; slot: number } | null = navigationPosition, inventory = bankInventory, limit = bankCount) {
    if (!current || !Number.isInteger(current.bank) || current.bank < 1 || current.bank > MAX_BANKS
        || !Number.isInteger(current.slot) || current.slot < 1 || current.slot > 10) return null;
    const valid = validPatches(inventory, limit);
    const banks = [...new Set(valid.map(p => p.bank))].sort((a, b) => a - b);
    if (!banks.length) return null;
    const index = banks.indexOf(current.bank);
    const bank = banks[index < 0 ? (delta > 0 ? 0 : banks.length - 1)
      : (index + delta + banks.length) % banks.length];
    if (bank === current.bank) return null;
    return bankDestination(bank, current, valid, limit);
  }

  let previousBank = $derived(bankTarget(-1));
  let nextBank = $derived(bankTarget(1));

  async function changeBank(selection: -1 | 1 | { bank: number; slot?: number }) {
    const direct = typeof selection !== "number";
    const exactRig = direct && selection.slot !== undefined;
    const fromPicker = direct && !exactRig;
    if (!connected || !deviceInfo || bankChangePending || pendingSwitch !== null
        || (!direct && !bankTarget(selection))) return false;
    bankChangePending = true;
    bankChangeError = "";
    bankPickerError = "";
    let cancelled = false;
    const initialProfile = deviceInfo.profile;
    const pickerSession = bankPickerSession;
    const initialPreselection = preselectedBank;
    let sent = false;
    const subscriptions: Array<() => void> = [];
    const fail = (message = "Bank change not confirmed.") => {
      if (!_stopped) {
        if (showBankPicker && fromPicker) bankPickerError = message;
        else bankChangeError = message;
      }
    };
    const canContinue = () => {
      const valid = !_stopped && connected && !cancelled
        && (!fromPicker || (showBankPicker && pickerSession === bankPickerSession))
        && (!exactRig || sent || (initialPreselection !== null && preselectedBank === initialPreselection))
        && !(initialProfile && deviceInfo?.profile && initialProfile !== deviceInfo.profile);
      if (!valid) fail();
      return valid;
    };
    try {
      // Observe transitions directly: a quick down/up pair may leave the
      // reactive connected prop true while old replies are still queued.
      subscriptions.push(await onDisconnected(() => { cancelled = true; }));
      subscriptions.push(await onReconnecting(() => { cancelled = true; }));
      if (!canContinue()) return;
      // Another editor or the physical pedal may have changed the inventory
      // or current slot. Resolve the destination from fresh firmware replies.
      const info = await sendAndAwait<FirmwareMessage & { profile?: string }>({ type: "GET_DEVICE_INFO" });
      if (!canContinue()) return;
      const inventory = await sendAndAwait<FirmwareMessage & { profile?: string }>({ type: "LIST_PATCHES" });
      if (!canContinue()) return;
      if (info.type !== "DEVICE_INFO" || inventory.type !== "PATCH_LIST"
          || !Array.isArray(inventory.patches) || !info.current) throw new Error("Invalid navigation state");
      // Legacy CP replies leave the active-list profile empty. Native replies
      // carry the real ID; never combine a position and list from two profiles.
      if ((info.profile && inventory.profile && info.profile !== inventory.profile)
          || (info.profile && deviceInfo?.profile && info.profile !== deviceInfo.profile)) {
        throw new Error("Navigation profile changed");
      }
      // A clean patch deleted by another editor may not emit an event. Keep
      // this confirmed list for button availability until the parent refreshes.
      refreshedInventory = { source: patches, profile: deviceInfo?.profile, patches: inventory.patches };
      // Another client may have changed the limit before GLOBAL reaches this UI.
      const limit = info.bank_count === undefined ? bankCount : getBankCount(info);
      const target = exactRig
        ? validPatches(inventory.patches, limit).find(p => p.bank === selection.bank && p.slot === selection.slot)
        : direct ? bankDestination(selection.bank, info.current, inventory.patches, limit)
        : bankTarget(selection, preselectedBank
          ? { bank: preselectedBank.bank, slot: info.current.slot } : info.current, inventory.patches, limit);
      if (!target) {
        if (exactRig) fail("Rig is no longer available.");
        else if (direct) fail("Bank is no longer available.");
        return false;
      }
      if (info.current.bank === target.bank && (!exactRig || info.current.slot === target.slot)) {
        preselectedBank = null;
        return true;
      }
      if (!exactRig && bankSelectionMode === "preselect") {
        preselectedBank = { bank: target.bank, profile: deviceInfo?.profile,
          originBank: deviceInfo!.bank, originSlot: deviceInfo!.slot };
        return true;
      }
      sent = true;
      const reply = await sendAndAwait({ type: "SWITCH_PATCH", bank: target.bank, slot: target.slot });
      if (reply.type !== "ACK") throw new Error("Unexpected navigation reply");
      if (!canContinue()) return;
      // Never paint an optimistic destination. This also recovers the shell's
      // current position if its unsolicited patch_switched event was lost.
      const confirmed = await sendAndAwait({ type: "GET_DEVICE_INFO" });
      if (!canContinue()) return;
      if (confirmed.type !== "DEVICE_INFO" || confirmed.current?.bank !== target.bank
          || confirmed.current?.slot !== target.slot) throw new Error("Navigation position not confirmed");
      await tick();
      return true;
    } catch {
      fail();
      return false;
    } finally {
      for (const unsubscribe of subscriptions) unsubscribe();
      bankChangePending = false;
    }
  }

  function canActivateSwitch(sw: string): boolean {
    return connected && deviceInfo?.stage_input === true && !!fullPatch
      && _patchLocation === `${deviceInfo.bank}/${deviceInfo.slot}`
      && _patchProfile === deviceInfo.profile
      && !bankChangePending && pendingSwitch === null && !showBankPicker
      && !preselectedBank
      && (!!bindingForSwitch(sw) || !!navPatchFor(sw));
  }

  async function activateSwitch(sw: string) {
    if (_stopped || !deviceInfo || !canActivateSwitch(sw)) return;
    const position = { bank: deviceInfo.bank, slot: deviceInfo.slot, profile: deviceInfo.profile };
    pendingSwitch = sw;
    switchActionError = "";
    let cancelled = false;
    const subscriptions: Array<() => void> = [];
    try {
      subscriptions.push(await onDisconnected(() => { cancelled = true; }));
      subscriptions.push(await onReconnecting(() => { cancelled = true; }));
      if (_stopped || cancelled || !connected || deviceInfo.bank !== position.bank
          || deviceInfo.slot !== position.slot || deviceInfo.profile !== position.profile) return;
      // One complete tap, guarded by the firmware's current patch/profile.
      // There is no remote key-down left held if the browser disconnects.
      const reply = await sendAndAwait({ type: "ACTIVATE_SWITCH", switch: sw, ...position });
      if (reply.type !== "ACK") throw new Error("Unexpected switch reply");
      if (_stopped || cancelled || !connected) return;
      // ACK means admitted. Only firmware events/context paint the new state.
      await cmd.getDeviceInfo();
      await pollContext();
    } catch {
      if (!_stopped) switchActionError = "Switch action not confirmed.";
    } finally {
      for (const unsubscribe of subscriptions) unsubscribe();
      pendingSwitch = null;
    }
  }

  // Reuse Screen's field colors while retaining Stage's responsive layout.
  // Kemper defaults call the title patch_name and the rig number kemper_rig;
  // layouts may also use their live/core aliases.
  let screenColors = $derived.by(() => {
    const tft = device?.tft;
    const layout = tft && typeof tft === "object"
      ? (tft as Record<string, unknown>).layout : undefined;
    const entries = Array.isArray(layout) ? layout : [];
    const compact = device?.tft_colors;
    const validColor = (color: unknown): string | undefined => {
      if (typeof color === "string" && /^#[0-9a-f]{6}$/i.test(color)) return color;
      if (typeof color === "number" && Number.isInteger(color)
          && color >= 0 && color <= 0xffffff) {
        return `#${color.toString(16).padStart(6, "0")}`;
      }
      return undefined;
    };
    const colorFor = (...fields: string[]): string | undefined => {
      for (const field of fields) {
        for (const entry of entries) {
          if (!entry || typeof entry !== "object" || entry.field !== field) continue;
          const color = validColor(entry.color);
          if (color) return color;
        }
      }
      // The kiosk receives only this tiny DEVICE_INFO projection, avoiding
      // an expensive GET_GLOBAL just to paint the four header fields.
      if (compact && typeof compact === "object" && !Array.isArray(compact)) {
        for (const field of fields) {
          const color = validColor((compact as Record<string, unknown>)[field]);
          if (color) return color;
        }
      }
      return undefined;
    };
    return {
      title: colorFor("patch_name", "kemper_rig_name"),
      bank: colorFor("bank", "kemper_bank"),
      rig: colorFor("kemper_rig_in_bank", "kemper_rig", "slot"),
      expression: colorFor("expression_mode"),
    };
  });

  function handleThemeChange(next: StageTheme) {
    stageTheme = next;
    saveStageTheme(next);
  }

  // Full patch (with bindings) fetched on mount and when deviceInfo changes
  let fullPatch = $state<{ name?: string; bindings?: Binding[] } | null>(null);
  let patchName = $derived(fullPatch?.name ?? (deviceInfo ? `${deviceInfo.bank}/${deviceInfo.slot}` : "-"));
  let bindings = $derived(fullPatch?.bindings ?? []);

  // Live latched state per switch (from binding_fired toggle_on / toggle_off)
  let latched = $state<Record<string, boolean>>({});
  // Rig identity carried by the most recent CONTEXT. It lets a patch change
  // distinguish a same-message fresh block snapshot from stale values that
  // belonged to the previous rig.
  let contextLocation = "";

  // 2-row x 5-column pedal layout
  let rows = $derived(DEFAULT_LAYOUT);

  // --- derived ---
  let rigName = $derived(
    (context.kemper_rig_name as string) || patchName || "-"
  );
  let bpm = $derived(context.kemper_bpm as number | undefined);
  let tunerOn = $derived(
    connected && (context.kemper_tuner === "on" || context.tuner === "on")
  );
  let tunerNote = $derived((context.kemper_tuner_note ?? context.tuner_note) as string | undefined);
  let tunerDeviance = $derived((context.kemper_tuner_deviance ?? context.tuner_deviance) as number | undefined);
  let tunerDismissed = $state(false);
  let showTuner = $derived(tunerOn && !tunerDismissed);
  $effect(() => { if (!tunerOn) tunerDismissed = false; });
  $effect(() => {
    if (showTuner) {
      showThemeEditor = false;
      if (showBankPicker) dismissBankPicker(true);
    }
  });
  let expressionMode = $derived(connected
    && (context.expression_mode === "VOL" || context.expression_mode === "WAH")
    ? context.expression_mode : "---");
  let screenLabels = $derived.by(() => {
    const tft = device?.tft;
    const layout = tft && typeof tft === "object"
      ? (tft as Record<string, unknown>).layout : undefined;
    const entries = Array.isArray(layout) ? layout : [];
    const compact = device?.tft_labels;
    const format = (fields: string[], fallbackField: string, fallbackPrefix: string): string => {
      let spec: Record<string, unknown> | undefined;
      for (const field of fields) {
        spec = entries.find(entry => entry && typeof entry === "object" && entry.field === field);
        if (spec) break;
      }
      if (!spec && compact && typeof compact === "object" && !Array.isArray(compact)) {
        for (const field of fields) {
          const saved = (compact as Record<string, unknown>)[field];
          if (saved && typeof saved === "object" && !Array.isArray(saved)) {
            spec = { ...saved, field };
            break;
          }
        }
      }
      const field = (spec?.field as string | undefined) ?? fallbackField;
      const value = preselectedBank && fallbackField === "bank" ? preselectedBank.bank
        : field in context ? context[field] : (field === "bank" ? deviceInfo?.bank
        : field === "slot" ? deviceInfo?.slot : undefined);
      // Match TFT's field formatting: unknown values do not display a
      // misleading number, and an explicitly empty prefix stays empty.
      if (value == null || value === "" || typeof value === "object") return "";
      const prefix = spec ? (typeof spec.prefix === "string" ? spec.prefix : "") : fallbackPrefix;
      const suffix = spec && typeof spec.suffix === "string" ? spec.suffix : "";
      return `${prefix}${value}${suffix}`;
    };
    const fallbackRigField = context.kemper_rig_in_bank != null ? "kemper_rig_in_bank" : "slot";
    return {
      bank: format(["bank", "kemper_bank"], "bank", "BANK "),
      rig: format(["kemper_rig_in_bank", "kemper_rig", "slot"], fallbackRigField, "RIG "),
    };
  });

  function displaySwitch(sw: string): string {
    if (sw === "up") return "UP";
    if (sw === "down") return "DOWN";
    return sw.toUpperCase();
  }

  function bindingForSwitch(sw: string): Binding | undefined {
    // The firmware's compiled switch table keeps the last configured binding.
    // Mirror that same action even if an imported patch contains duplicates.
    for (let index = bindings.length - 1; index >= 0; --index) {
      if (bindings[index].switch === sw) return bindings[index];
    }
    return undefined;
  }

  function effectLabel(b: Binding | undefined): string {
    if (!b) return "";
    if (b.label) return b.label;
    const keys = Object.keys(b.actions ?? {});
    const action = keys.length > 0 ? b.actions?.[keys[0]] : undefined;
    const msg = action?.messages?.[0];
    if (!msg) return displaySwitch(b.switch);
    try {
      const pluginId = (msg as Record<string,unknown>).plugin as string | undefined;
      const msgType = (msg as Record<string,unknown>).type as string;
      const schema = pluginId
        ? manifest?.plugins[pluginId]?.messages[msgType]
        : manifest?.core_messages[msgType];
      if (schema) return summarizeMessage(msg as Parameters<typeof summarizeMessage>[0], schema);
    } catch { /* fall through */ }
    return `${(msg as Record<string,unknown>).type ?? ""}`;
  }

  // `el` is the fixed-size clipping frame (overflow:hidden, never moves);
  // its only child is the ".stage__marquee-track" span that actually holds
  // the text and gets translated. Sliding the SAME element that also clips
  // itself doesn't reveal anything - the clip boundary moves with it - so
  // the frame/track have to be two different elements.
  //
  // `text` is only read to give Svelte a reactive trigger: the action never
  // reruns on its own when the frame's box is unchanged, but a new
  // label/rig name replacing the old one changes the track's content width
  // without resizing the frame - recheck whenever that display text
  // changes, not just when the frame itself resizes.
  function marquee(el: HTMLElement, text?: unknown) {
    const track = el.firstElementChild as HTMLElement | null;
    const check = () => {
      if (!track) return;
      const overflow = track.scrollWidth > el.clientWidth + 2;
      if (overflow) {
        const dx = -(track.scrollWidth - el.clientWidth);
        track.style.setProperty("--marquee-dx", `${dx}px`);
        track.classList.add("stage__marquee-active");
      } else {
        track.classList.remove("stage__marquee-active");
      }
    };
    check();
    const obs = new ResizeObserver(check);
    obs.observe(el);
    return { update: check, destroy() { obs.disconnect(); } };
  }

  function isLatchedOn(sw: string): boolean {
    const b = bindingForSwitch(sw);
    if (!b) return false;

    // Check Kemper block state from CONTEXT (authoritative, works for
    // changes made on the Kemper itself, not just Captain footswitches).
    const block = kemperBlock(b);
    if (block) {
      const key = "kemper_block_" + block;
      if (context[key] === "on") return true;
      if (context[key] === "off") return false;
    }

    // Fall back to binding_fired latch tracking.
    if (b.mode === "latched" || b.mode === "momentary") {
      return latched[sw] === true;
    }
    // Non-latched: show active when bound.
    return true;
  }

  /** Extract the Kemper block name (A/B/C/D/X/MOD/DLY/REV) from a binding,
   *  or null if the binding doesn't target a Kemper effect block. */
  function kemperBlock(b: Binding): string | null {
    for (const action of Object.values(b.actions ?? {})) {
      for (const msg of action.messages ?? []) {
        if ((msg as Record<string,unknown>).type === "kemper_effect_toggle") {
          return ((msg as Record<string,unknown>).slot as string) ?? null;
        }
      }
    }
    return null;
  }

  /** Return the Kemper block update performed by one concrete action. This
   *  drives immediate UI feedback from binding_fired while the Player's MIDI
   *  echo is still in flight; the next full CONTEXT remains authoritative. */
  function kemperUpdateForAction(b: Binding, actionName: string): { key: string; value: "on" | "off" } | null {
    const action = b.actions?.[actionName];
    for (const msg of action?.messages ?? []) {
      const record = msg as Record<string, unknown>;
      if (record.type === "kemper_effect_toggle" && typeof record.slot === "string") {
        const value = record.value === "on" || record.value === "off"
          ? record.value
          : actionName === "toggle_on" ? "on"
          : actionName === "toggle_off" ? "off"
          : null;
        if (value) return { key: `kemper_block_${record.slot}`, value };
      }
    }
    return null;
  }

  function scheduleKemperReconcile(key: string, value: "on" | "off") {
    const previous = kemperReconciles.get(key);
    if (previous) clearTimeout(previous.timer);
    const pending = {
      value,
      timer: setTimeout(() => {
        // A newer toggle for the same block supersedes this timeout.
        if (kemperReconciles.get(key) !== pending) return;
        kemperReconciles.delete(key);
        pollContext();
      }, 900),
    };
    kemperReconciles.set(key, pending);
  }

  function clearKemperReconciles() {
    for (const pending of kemperReconciles.values()) {
      clearTimeout(pending.timer);
    }
    kemperReconciles.clear();
  }

  /** Active state + LED colour for the ambient floor glow behind the grid
   *  (purely decorative echo of a switch's own LED colour - never a
   *  substitute for the border/background colour logic in the markup,
   *  which stays the source of truth for what's actually engaged). */
  function switchVisual(sw: string): { active: boolean; color: string | null } {
    if (preselectedBank) return { active: false, color: null };
    const b = bindingForSwitch(sw);
    const navPatch = b ? null : navPatchFor(sw);
    const navSlot = navPatch ? navSlotFor(sw) : null;
    const active = b ? isLatchedOn(sw) : (navSlot !== null && navSlot === deviceInfo?.slot);
    const color = b
      ? ledColorFor(b, true)
      : (navSlot !== null ? (presetNav?.bank_colors?.[String(deviceInfo?.bank)] ?? "#888888") : null);
    return { active, color };
  }

  // --- polling ---
  // Fetches the CURRENT context once (fast first paint on entering Stage,
  // before the firmware's own next proactive push). NOT re-run on a timer:
  // the firmware already pushes a fresh CONTEXT message on every change
  // (captain/app.py _push_context, throttled to 1 Hz, unconditional -
  // running regardless of which page the editor shows), so polling again
  // every 2 s here was pure redundant traffic on an already-busy data CDC
  // channel - competing with patch fetches, switch EVENT delivery and the
  // Kemper bridge's own USB-MIDI servicing for the same main-loop tick
  // budget. Removed 2026-08-15 after diagnostic logging showed the data
  // channel intermittently starved (responses arriving late/irregularly,
  // not lost outright) while this poll was firing on its own 2 s clock on
  // top of everything else already in flight.
  async function pollContext() {
    try { await cmd.getContext(); } catch { /* ignore */ }
  }

  async function fetchPatch(bank: number, slot: number) {
    try {
      // An explicit profile reads the saved file, not the active draft.
      // Native active reads tag their origin without changing that selector.
      await cmd.getPatch(bank, slot);
    } catch { /* ignore */ }
  }

  // --- lifecycle ---
  onMount(() => { void watchPreselectionConnection(); return () => _stop(); });
  onDestroy(() => { _stop(); });

  $effect(() => {
    if (preselectedBank && (!connected || deviceInfo?.profile !== preselectedBank.profile
        || preselectedBank.bank > bankCount
        || deviceInfo?.bank !== preselectedBank.originBank || deviceInfo?.slot !== preselectedBank.originSlot)) {
      preselectedBank = null;
    }
  });

  $effect(() => {
    if (showBankPicker && (!connected || deviceInfo?.profile !== bankPickerProfile)) {
      dismissBankPicker(true);
    }
  });

  $effect(() => {
    if (showBankPicker && deviceInfo) bankPickerCurrent = deviceInfo.bank;
  });

  // (Re)pull live state on every link transition into "connected". Covers
  // three cases the desktop app never hit because it only shows Stage
  // once already connected: (a) StageView mounted before the link was up
  // (the Pi kiosk, editor/src/kiosk), (b) a reconnect after a drop, and
  // (c) bank-step navigation changing deviceInfo. Without (a)/(b) the
  // subscriber was never attached / the first CONTEXT+PATCH never
  // re-fetched, so the grid sat on stale/empty state until the next
  // unsolicited firmware push happened to arrive.
  let _linkUp = false;
  $effect(() => {
    if (connected && !_linkUp) {
      _linkUp = true;
      _subscribe();
      pollContext();
    } else if (!connected) {
      _linkUp = false;
      // The same patch may have been edited while this browser was offline.
      // Keep input disabled until its bindings have been read on the new link.
      fullPatch = null;
      latched = {};
      clearKemperReconciles();
      // A reconnect needs a fresh confirmation, not the last pedal mode.
      if ("expression_mode" in context && context.expression_mode !== "") {
        context = { ...context, expression_mode: "" };
      }
    }
  });

  // Re-fetch the patch whenever it (bank-step nav) or the connection
  // changes. Reads both every run so Svelte tracks them as dependencies.
  let _patchLocation = "";
  let _patchProfile: string | undefined;
  let _announcedPatchLocation = "";
  $effect(() => {
    const bank = deviceInfo?.bank;
    const slot = deviceInfo?.slot;
    const profile = deviceInfo?.profile;
    const location = bank != null && slot != null ? `${bank}/${slot}` : "";
    if (location !== _patchLocation || profile !== _patchProfile) {
      const profileChanged = profile !== _patchProfile;
      clearKemperReconciles();
      if (_announcedPatchLocation && location !== _announcedPatchLocation) {
        _announcedPatchLocation = "";
      }
      _patchLocation = location;
      _patchProfile = profile;
      // Never render a new CONTEXT snapshot against the previous patch's
      // bindings while GET_PATCH for the new location is in flight.
      fullPatch = null;
      if (profileChanged || contextLocation !== location) context = {};
      latched = {};
      if (connected && location) pollContext();
    }
    if (connected && bank != null && slot != null) fetchPatch(bank, slot);
  });

  let _subscribing = false;
  let _stopped = false;
  async function _subscribe() {
    if (unsubFw || _subscribing) return;
    _subscribing = true;
    try {
      const unsub = await onFirmwareMessage((msg: FirmwareMessage) => {
        if (msg.type === "EVENT" && msg.event === "profile_switched" && showBankPicker) {
          dismissBankPicker(true);
        }
        if (msg.type === "EVENT" && (msg.event === "profile_switched" || msg.event === "patch_switched")) {
          preselectedBank = null;
        }
        if (msg.type === "CONTEXT" && msg.context) {
          const incoming = msg.context as Record<string, unknown>;
          const incomingBank = Number(incoming.bank);
          const incomingSlot = Number(incoming.slot);
          if (Number.isFinite(incomingBank) && Number.isFinite(incomingSlot)) {
            const incomingLocation = `${incomingBank}/${incomingSlot}`;
            // A full snapshot requested on the previous rig can complete
            // after patch_switched. Never apply its effects to newer bindings.
            if (_announcedPatchLocation && incomingLocation !== _announcedPatchLocation) return;
            contextLocation = incomingLocation;
          }
          const accepted = { ...incoming };
          const preservedOptimistic: Record<string, unknown> = {};
          for (const [key, pending] of kemperReconciles) {
            if (key in incoming && incoming[key] === pending.value) {
              clearTimeout(pending.timer);
              kemperReconciles.delete(key);
            } else {
              // Periodic/full snapshots queued before the footswitch event are
              // stale, and an unrelated full snapshot may omit this block.
              // Let unrelated fields through, but preserve every optimistic
              // effect independently until confirmation or its safety timeout.
              delete accepted[key];
              preservedOptimistic[key] = context[key] ?? pending.value;
            }
          }
          const isPartial = (msg as Record<string, unknown>).partial === true;
          context = isPartial
            ? { ...context, ...accepted }
            : { ...accepted, ...preservedOptimistic };
        } else if (msg.type === "PATCH"
            && (!deviceInfo?.stage_input || !deviceInfo.profile
              || (!msg.profile && msg.active_profile === deviceInfo.profile))
            && `${msg.bank}/${msg.slot}` === (_patchLocation || (deviceInfo
              ? `${deviceInfo.bank}/${deviceInfo.slot}` : ""))) {
          const p = (msg as unknown as { patch: { name?: string; bindings?: Binding[] } }).patch;
          fullPatch = p;
          // Location/profile changes already clear latched state above.
          // A metadata read of this same patch must preserve binding_fired
          // feedback that arrived just before its PATCH reply.
          const init: Record<string, boolean> = {};
          for (const b of p.bindings ?? []) {
            if (b.switch && (b.mode === "latched" || b.mode === "momentary")) {
              init[b.switch] = latched[b.switch] === true;
            }
          }
          latched = init;
        } else if (msg.type === "EVENT" && (msg as Record<string,unknown>).event === "patch_switched") {
          const ev = msg as Record<string, unknown>;
          const bank = Number(ev.bank);
          const slot = Number(ev.slot);
          if (Number.isFinite(bank) && Number.isFinite(slot)) {
            const location = `${bank}/${slot}`;
            // Invalidate synchronously: waiting for the parent prop update can
            // briefly map the old rig's Reverb onto BOOST, or erase a fast X
            // confirmation received before the reactive effect runs.
            _patchLocation = location;
            _announcedPatchLocation = _patchLocation;
            contextLocation = _patchLocation;
            clearKemperReconciles();
            context = {};
            fullPatch = null;
            latched = {};
            // A rig change does not guarantee that the Kemper will emit every
            // block state again. In particular, selecting the already-active
            // rig (or moving between rigs that share an ON block) can produce
            // no effect delta at all. We deliberately cleared the previous
            // rig's context above, so request one authoritative snapshot now;
            // otherwise that effect stays dark until an unrelated change.
            // The announced-location guard above rejects a snapshot that was
            // already in flight for the previous rig.
            pollContext();
          }
        } else if (msg.type === "EVENT" && (msg as Record<string,unknown>).event === "binding_fired") {
          const ev = msg as Record<string,unknown>;
          const sw = ev.switch as string;
          const action = ev.action as string;
          if (sw && (action === "toggle_on" || action === "toggle_off" || action === "press" || action === "release")) {
            latched = { ...latched, [sw]: action === "toggle_on" || action === "press" };
            const binding = bindingForSwitch(sw);
            const update = binding ? kemperUpdateForAction(binding, action) : null;
            if (update) {
              context = { ...context, [update.key]: update.value };
              scheduleKemperReconcile(update.key, update.value);
            }
          }
        }
      });
      if (_stopped) unsub();
      else unsubFw = unsub;
    } catch {
      /* ignore - the link-transition effect will retry */
    } finally {
      _subscribing = false;
    }
  }

  function _stop() {
    _stopped = true;
    dismissBankPicker(true);
    preselectedBank = null;
    for (const unsubscribe of preselectionSubscriptions.splice(0)) unsubscribe();
    if (unsubFw) { unsubFw(); unsubFw = null; }
    clearKemperReconciles();
  }
</script>

<div class="stage" class:stage--preselect={!!preselectedBank} style={stageThemeVars}
  style:--stage-beam-seconds-per-pixel={`${borderSecondsPerPixel}s`}
  style:--stage-action-height={stageActionHeight === undefined ? undefined : `${stageActionHeight}px`}
  style:--stage-action-width={stageActionWidth === undefined ? undefined : `${stageActionWidth}px`}
  style:--stage-header-top={stageHeaderGeometry === undefined ? undefined : `${stageHeaderGeometry.top}px`}
  style:--stage-header-left={stageHeaderGeometry === undefined ? undefined : `${stageHeaderGeometry.left}px`}
  style:--stage-header-width={stageHeaderGeometry === undefined ? undefined : `${stageHeaderGeometry.width}px`}
  style:--stage-header-height={stageHeaderGeometry === undefined ? undefined : `${stageHeaderGeometry.height}px`}
  style:--stage-action-top={stageHeaderGeometry === undefined ? undefined : `${stageHeaderGeometry.actionTop}px`}
  style:--stage-action-left={stageHeaderGeometry === undefined ? undefined : `${stageHeaderGeometry.actionLeft}px`}
  style:--stage-action-offset-top={stageHeaderGeometry === undefined ? undefined : `${stageHeaderGeometry.actionOffsetTop}px`}
  style:--stage-action-offset-right={stageHeaderGeometry === undefined ? undefined : `${stageHeaderGeometry.actionOffsetRight}px`} use:synchronizePulses>
  <span class="stage__border-pulse" aria-hidden="true">
    {#each borderBeam as segment}
      <span class="stage__border-segment" style:--beam-offset={segment.offset}
        style:--beam-opacity={segment.opacity}></span>
    {/each}
  </span>
  {#if showThemeEditor}
    <StageThemeEditor theme={stageTheme} onchange={handleThemeChange} onclose={() => (showThemeEditor = false)} />
  {/if}
  {#if showBankPicker}
    <StageBankPicker banks={bankPickerLoaded ? bankOptions : []} currentBank={bankPickerCurrent}
      loading={bankPickerLoading} pending={bankChangePending} error={bankPickerError}
      selectionMode={bankSelectionMode} onmodechange={changeBankSelectionMode}
      onselect={selectBank} onclose={() => dismissBankPicker()} onretry={refreshBankPicker} />
  {/if}
  {#if showTuner}
    <StageTuner note={tunerNote} deviance={tunerDeviance}
      onclose={() => (tunerDismissed = true)} />
  {/if}

  <!-- header: rig name + bank/rig + BPM + expression -->
  <div class="stage__header" use:measureStageHeader>
    <div class="stage__rig-name" style:color={screenColors.title} use:marquee={rigName}><span class="stage__marquee-track">{rigName}</span></div>
    <div class="stage__bank-controls" role="group" aria-label="Bank navigation" aria-busy={bankChangePending}>
      <button class="stage__bank-btn" aria-label="Next bank" title="Next bank"
              disabled={!connected || bankChangePending || pendingSwitch !== null || showBankPicker || !nextBank} onclick={() => changeBank(1)}>+</button>
      <button class="stage__bank-btn" aria-label="Previous bank" title="Previous bank"
              disabled={!connected || bankChangePending || pendingSwitch !== null || showBankPicker || !previousBank} onclick={() => changeBank(-1)}>−</button>
    </div>
    <div class="stage__meta">
      {#if deviceInfo}
        <button type="button" class="stage__bank-readout" aria-label="Choose bank" title="Choose bank"
          aria-haspopup="dialog" aria-expanded={showBankPicker}
          disabled={!connected || bankChangePending || pendingSwitch !== null} onclick={openBankPicker}>
          <span class="stage__bank" use:marquee={screenLabels.bank}><span class="stage__marquee-track"><span class="stage__bank-number" style:color={screenColors.bank}>{screenLabels.bank}</span></span></span>
        </button>
        <div class="stage__rig-readout">
          <span class="stage__rig" use:marquee={screenLabels.rig}><span class="stage__marquee-track"><span class="stage__rig-number" style:color={screenColors.rig}>{screenLabels.rig}</span></span></span>
        </div>
      {/if}
      {#if bpm}
        <span class="stage__bpm">{bpm} <small>BPM</small></span>
      {/if}
      {#if bankChangeError && !showBankPicker}
        <span class="stage__navigation-error" role="alert">{bankChangeError}</span>
      {/if}
      {#if switchActionError}
        <span class="stage__navigation-error" role="alert">{switchActionError}</span>
      {:else if connected && deviceInfo && deviceInfo.stage_input !== true}
        <span class="stage__navigation-error" role="status">Update Captain firmware to control switches from Stage.</span>
      {/if}
    </div>
    <span class="stage__expression" style:color={stageTheme.sections.expression?.color ?? screenColors.expression} aria-label={`Expression pedal: ${expressionMode}`}>
      <span class="stage__expression-label"><span>{expressionMode}</span></span>
    </span>
    <div class="stage__controls">
      <button class="stage__icon-btn stage__exit-btn stage-control-icon stage-control-icon--close" onclick={onExit} aria-label="Exit Stage">
        <svg viewBox="0 0 24 24" aria-hidden="true"><path d="m6 6 12 12M18 6 6 18" /></svg>
      </button>
      <button class="stage__icon-btn stage-control-icon" onclick={() => (showThemeEditor = !showThemeEditor)} aria-label="Stage appearance">
        <svg viewBox="0 0 24 24" aria-hidden="true"><path d="M4 7h16M4 17h16M8 4v6M16 14v6" /></svg>
      </button>
    </div>
  </div>

  {#if "kemper_morph_source" in context}
    <StageMorph {context} {connected}
      ready={connected && !!deviceInfo?.profile && context.kemper_morph_ready === "on"
        && !preselectedBank && !bankChangePending && pendingSwitch === null}
      identity={`${deviceInfo?.profile}/${deviceInfo?.bank}/${deviceInfo?.slot}/${context.kemper_generation}`}
      oncommand={controlMorph} />
  {/if}

  {#if preselectedBank}
    <div class="stage__preselection-bar">
      <span role="status">Bank {preselectedBank.bank} preselected · choose a rig
        <small>Playing bank {deviceInfo?.bank} · rig {deviceInfo?.slot}</small>
      </span>
      <button type="button" class="stage-control-button" aria-label="Cancel bank preselection" disabled={bankChangePending}
        onclick={cancelPreselection}>CANCEL</button>
    </div>
  {/if}

  <div class="stage__divider" aria-hidden="true"></div>

  <!-- 2-row x 5-column footswitch grid -->
  <div class="stage__pedal">
    <!-- ambient floor glow: echoes each engaged switch's own LED colour,
         purely decorative - never the source of truth for switch state -->
    <div class="stage__glow" aria-hidden="true">
      {#each rows as row}
        <div class="stage__glow-row">
          {#each row as sw}
            {@const v = switchVisual(sw)}
            <div class="stage__glow-spot"
                 style={v.color ? `background: radial-gradient(circle, ${v.color}59 0%, transparent 70%); opacity: ${v.active ? 1 : 0}` : "opacity: 0"}>
            </div>
          {/each}
        </div>
      {/each}
    </div>
    {#each rows as row, rowIndex}
      <div class="stage__pedal-row"
        class:stage__pedal-row--lower={rowIndex === rows.length - 1}>
        {#each row as sw}
          {@const b = preselectedBank ? null : bindingForSwitch(sw)}
          {@const navPatch = preselectedBank ? preselectionPatchFor(sw) : b ? null : navPatchFor(sw)}
          {@const navSlot = navPatch ? (preselectedBank ? preselectionSlots.get(sw)! : navSlotFor(sw)) : null}
          {@const active = !preselectedBank && (b ? isLatchedOn(sw) : (navSlot !== null && navSlot === deviceInfo?.slot))}
          <!-- Border and indicator follow the switch's assigned LED colour:
               bright with a full-panel colour tint when on, dim when off. Unbound
               switches get no coloured border at all. Preset-nav switches
               (device.preset_navigation, not a patch binding) use the
               configured bank colour the same way the firmware's physical
               LEDs do - see navPatchFor above. A switch mapped to a slot
               with no patch in THIS bank falls through to the unbound "-"
               look, same as the physical LED staying off. -->
          {@const offColor = b ? ledColorFor(b, false) : (navSlot !== null ? (presetNav?.bank_colors?.[String(preselectedBank?.bank ?? deviceInfo?.bank)] ?? "#888888") : null)}
          {@const onColor = b ? ledColorFor(b, true) : (navSlot !== null ? (presetNav?.bank_colors?.[String(preselectedBank?.bank ?? deviceInfo?.bank)] ?? "#888888") : null)}
          {@const label = b ? (effectLabel(b) || displaySwitch(sw)) : (navPatch ? navPatch.name : "-")}
          <button type="button" class="stage__switch"
               aria-label={preselectedBank ? `Rig ${preselectionSlots.get(sw)}: ${label}` : `Switch ${displaySwitch(sw)}: ${label}`}
               aria-pressed={active}
               aria-busy={pendingSwitch === sw || (!!preselectedBank && bankChangePending)}
               disabled={preselectedBank ? !canSelectPreselectedRig(sw) : !canActivateSwitch(sw)}
               onclick={() => preselectedBank ? selectPreselectedRig(sw) : activateSwitch(sw)}
               class:stage__switch--bound={!!b || navSlot !== null}
               class:stage__switch--active={active}
               style={onColor
                 ? `--switch-led: ${active ? onColor : offColor}; --switch-tint: ${onColor}55; border-color: ${active ? onColor : offColor}`
                 : ''}>
            <span class="stage__switch-label" use:marquee={label}><span class="stage__marquee-track">{label}</span></span>
            <span class="stage__switch-id">{preselectedBank ? `RIG ${preselectionSlots.get(sw)}` : displaySwitch(sw)}</span>
          </button>
        {/each}
      </div>
    {/each}
  </div>
</div>

<style>
  .stage {
    --stage-label-font-size: clamp(0.8rem, 3.2vw, 3rem);
    /* Stage is a dark instrument display in either editor shell theme.
       Saved section fonts/colours and inline TFT colours still win below. */
    --stage-display-bg: #080c10;
    --stage-display-panel: #10171e;
    --stage-display-edge: #42545f;
    --stage-display-text: #edf5f7;
    --stage-display-muted: #9eafb9;
    --stage-display-accent: #6fd99b;
    --stage-corner-radius: calc(clamp(28px, 4vw, 56px) * var(--stage-corner-scale, 0.75));
    --stage-switch-outer-radius: calc(clamp(16px, 3vw, 36px) * var(--stage-corner-scale, 0.75));
    display: flex; flex-direction: column;
    /* Kiosk/preview have no global sizing reset. Keep padding INSIDE 100dvh. */
    box-sizing: border-box;
    height: 100%; height: 100dvh;
    min-height: 0;
    overflow: hidden;
    border-radius: var(--stage-corner-radius);
    /* Keep the rounded cutouts black in every host, including the light editor. */
    box-shadow: 0 0 0 var(--stage-corner-radius) #000;
    /* Inset controls far enough to clear the rounded corners. */
    padding: max(clamp(8px, 1.2vw, 20px), calc(var(--stage-corner-radius) * 0.32));
    gap: clamp(6px, 1vw, 14px);
    font-family: var(--stage-font, "Inter", -apple-system, sans-serif);
    font-weight: 600;
    color: var(--stage-display-text);
    background: linear-gradient(135deg, #16212a 0%, var(--stage-display-bg) 38%, #0c1318 100%);
    font-variant-numeric: tabular-nums;
    user-select: none; -webkit-user-select: none;
    position: relative;
  }
  .stage::before {
    content: "";
    position: absolute; inset: 3px;
    border: 2px solid #657f8d;
    border-radius: max(0px, calc(var(--stage-corner-radius) - 3px));
    pointer-events: none;
  }
  .stage__border-pulse {
    position: absolute; inset: 0; z-index: 3;
    pointer-events: none;
    animation: stage-border-breathe 2.4s ease-in-out infinite;
  }
  .stage__border-segment {
    position: absolute; top: 0; left: 0;
    width: 8px; height: 4px; border-radius: 1px;
    pointer-events: none;
    background: #e0fff2;
    box-shadow: 0 0 5px 1px #a1edcf40;
    opacity: var(--beam-opacity);
    /* Each piece stays tangent to the same closed path, including while
       the beam straddles two sides. CSS owns the clock; no JS frame loop. */
    offset-path: inset(4px round max(0px, calc(var(--stage-corner-radius) - 4px)));
    offset-anchor: center;
    /* Phase is calculated only when layout changes. Plain percentage
       keyframes avoid per-frame length/percentage interpolation on the Pi. */
    animation: stage-border-orbit 24s linear infinite;
    animation-delay: calc(-24s - var(--beam-offset) * var(--stage-beam-seconds-per-pixel));
  }
  @keyframes stage-border-orbit {
    from { offset-distance: 0%; }
    to { offset-distance: 100%; }
  }
  @keyframes stage-border-breathe {
    0%, 100% { opacity: 0.6; }
    50% { opacity: 1; }
  }

  .stage__controls, .stage__bank-controls {
    display: flex; flex-direction: column; gap: clamp(2px, 0.4vh, 4px);
    flex: 0 0 auto;
    align-self: stretch;
    width: clamp(48px, 3vw, 64px);
  }
  .stage__icon-btn {
    width: 100%; min-height: 36px;
    flex: 1 1 0;
  }

  /* ----- header ----- */
  .stage__header {
    position: relative;
    flex: 0 0 auto;
    display: flex; align-items: center; justify-content: space-between;
    gap: clamp(4px, 1vw, 12px);
    flex-wrap: wrap;
    padding: 0;
  }
  .stage__divider {
    position: relative;
    flex: 0 0 6px;
    margin-block: -3px;
    pointer-events: none;
  }
  .stage--preselect::after {
    content: "";
    position: absolute; inset: 3px;
    border-radius: max(0px, calc(var(--stage-corner-radius) - 3px));
    z-index: 2;
    background: #caff0029;
    pointer-events: none;
    animation: stage-light-pulse 3s ease-in-out infinite;
  }
  .stage__preselection-bar {
    display: grid;
    grid-template-columns: minmax(0, 1fr) auto minmax(0, 1fr);
    align-items: center;
    gap: clamp(6px, 0.75vw, 10px);
    flex: 0 0 auto;
    min-width: 0;
    color: #d8ff79;
    font-size: clamp(12px, 1.2vw, 16px);
    line-height: 1.25;
  }
  .stage__preselection-bar > span { min-width: 0; overflow-wrap: anywhere; }
  .stage__preselection-bar small { display: block; color: var(--stage-display-muted); font-size: 0.85em; }
  .stage__preselection-bar button {
    grid-column: 2;
    justify-self: center;
    height: var(--stage-action-height);
    min-height: 0;
    min-width: 5.5em;
    padding: 0 1em;
  }
  @media (max-width: 48rem) {
    .stage__preselection-bar { grid-template-columns: minmax(0, 1fr); }
    .stage__preselection-bar > span { text-align: center; }
    .stage__preselection-bar button { grid-column: 1; }
  }
  @media (prefers-reduced-motion: reduce) {
    .stage--preselect::after { animation: none; opacity: 0.35; }
  }
  .stage__divider::before, .stage__divider::after {
    content: "";
    position: absolute; inset: 0;
  }
  .stage__divider::before {
    background:
      linear-gradient(90deg, #caff0000, #caff0014 15%, #caff0080 38%, #e5ff7a 50%, #caff0080 62%, #caff0014 85%, #caff0000) center / 100% 2px no-repeat,
      radial-gradient(ellipse at center, #caff0066, #caff0022 35%, #caff0000 72%);
    /* The centre stays visible while a second fixed gradient fills the line. */
    animation: stage-light-pulse 3s ease-in-out infinite;
  }
  .stage__divider::after {
    background:
      linear-gradient(90deg, #caff0000, #d8ff54 5%, #eeffa6 50%, #d8ff54 95%, #caff0000) center / 100% 2px no-repeat,
      linear-gradient(90deg, #caff0000, #caff0047 5%, #dfff7866 50%, #caff0047 95%, #caff0000);
    /* At the peak only the outer 5% fades. Both layers animate opacity only. */
    animation: stage-divider-wide-pulse 3s ease-in-out infinite;
  }
  @keyframes stage-divider-wide-pulse {
    0%, 100% { opacity: 0; }
    50% { opacity: 1; }
  }
  @keyframes stage-light-pulse {
    0%, 100% { opacity: 0.35; }
    50% { opacity: 1; }
  }
  /* Clipping frames for the marquee effect: fixed-size, never move. The
     ".stage__marquee-track" child inside each one holds the actual text and
     is what the `marquee` action translates - sliding the frame itself
     would move its own clip boundary with it and reveal nothing. */
  .stage__rig-name, .stage__bank, .stage__rig {
    max-width: 100%;
    overflow: hidden; text-overflow: clip;
    white-space: nowrap;
    position: relative;
    /* Flex items refuse to shrink below their content's natural width by
       default (the "min-width: auto" trap) - without this, BANK fighting
       BPM for room in the same flex line pushes BPM off-canvas instead of
       letting BANK's own clip/marquee frame absorb the squeeze. */
    min-width: 0;
  }
  .stage__bank, .stage__rig {
    font-size: calc(var(--stage-label-font-size) * var(--stage-bank-scale, 1));
    color: var(--stage-bank-color, #ffffff);
    font-family: var(--stage-bank-font, var(--stage-font, "Inter", -apple-system, sans-serif));
    letter-spacing: 0.02em;
    line-height: 1.2;
    flex: 1 1 auto;
  }
  .stage__rig-name {
    flex: 1 1 100%;
    font-size: calc(clamp(2.2rem, 10vw, 7rem) * var(--stage-rig-name-scale, 1));
    line-height: 1.12; letter-spacing: -0.035em;
    font-weight: 700;
    color: var(--stage-rig-name-color, #ffffff);
    font-family: var(--stage-rig-name-font, var(--stage-font, "Inter", -apple-system, sans-serif));
  }
  .stage__meta {
    display: flex; align-items: stretch; gap: clamp(4px, 0.7vw, 10px);
    align-self: stretch;
    flex-wrap: wrap;
    /* Grows to fill whatever's left of the header row (its own wrapped line
       in portrait, the remainder after the rig name in landscape), so the
       BPM auto-margin below has real space to push against and reaches the
       row's actual right edge instead of just the edge of a small
       bank/bpm/tuner cluster centered with everything else. min-width:0 is
       load-bearing here too, same trap as .stage__bank one level down: as a
       flex item of .stage__header it would otherwise refuse to shrink below
       BANK's huge intrinsic content width and overflow the header instead
       of being constrained to it. */
    flex: 1 1 0;
    min-width: 0;
  }
  .stage__bank-readout, .stage__rig-readout {
    display: flex; align-items: center;
    flex: 1 1 0;
    /* Keep a usable marquee frame; optional BPM/tuner wrap below on phones. */
    min-width: 30px;
    padding: clamp(6px, 1.2vh, 14px) clamp(8px, 1.2vw, 18px);
    border: 1px solid var(--stage-display-edge);
    border-radius: 3px;
    background: #090f14;
    box-shadow: inset 0 1px 3px #00000066;
  }
  .stage__bank-readout {
    /* Match the RIG div's content minimum: button defaults would subtract
       its padding from the 30 px marquee frame on narrow screens. */
    box-sizing: content-box;
    margin: 0;
    font: inherit;
    color: inherit;
    text-align: inherit;
    cursor: pointer;
    touch-action: manipulation;
    -webkit-tap-highlight-color: transparent;
  }
  .stage__bank-readout:disabled { cursor: default; }
  .stage__bank-readout:focus-visible { outline: 2px solid var(--stage-display-accent); outline-offset: -4px; }
  .stage__bank-readout:active:not(:disabled) { background: #172820; }
  .stage__bank-btn {
    box-sizing: border-box;
    min-width: 48px; min-height: 30px;
    display: flex; align-items: center; justify-content: center;
    flex: 1 1 0;
    padding: 0;
    border: 1px solid var(--stage-display-edge); border-radius: 3px;
    background: #111b23; color: var(--stage-display-text);
    font: 700 28px/1 ui-monospace, "Cascadia Code", Consolas, monospace;
    cursor: pointer;
    -webkit-tap-highlight-color: transparent;
  }
  .stage__bank-btn:active:not(:disabled) { background: #294236; }
  .stage__bank-btn:disabled { color: #edf5f759; cursor: default; }
  .stage__bank-btn:focus-visible { outline: 2px solid var(--stage-display-accent); outline-offset: 2px; }
  .stage__navigation-error {
    flex: 1 1 100%; min-width: 0;
    color: #ffb8a8; font-size: 12px; line-height: 1.3;
  }
  .stage__bpm {
    align-self: center;
    font-size: calc(clamp(1.1rem, 3.5vw, 2.5rem) * var(--stage-bpm-scale, 1));
    color: var(--stage-bpm-color, var(--stage-display-text));
    font-family: var(--stage-bpm-font, var(--stage-font, "Inter", -apple-system, sans-serif));
    margin-left: auto; /* docks BPM to the right edge */
  }
  .stage__bpm small { font-size: 0.45em; color: var(--stage-display-muted); letter-spacing: 0.08em; }

  /* ----- 2x5 pedal grid ----- */
  .stage__expression {
    position: relative;
    display: flex; align-items: center;
    flex: 0 0 auto;
    width: 6em; box-sizing: border-box;
    margin-left: auto;
    align-self: stretch;
    font-size: clamp(1rem, 3.5vw, 2.5rem);
    font-family: var(--stage-expression-font, var(--stage-font, "Inter", -apple-system, sans-serif));
    line-height: 1.1;
    color: var(--stage-display-text);
    white-space: nowrap;
    padding: 0.4em 0.5em;
    background: #090f14;
    border: 1px solid var(--stage-display-edge);
    border-radius: 3px;
  }
  /* Scale only the text inside a stable readout. Mode changes and large
     font preferences must not push the neighbouring Stage controls away. */
  .stage__expression-label {
    position: absolute; inset: 0;
    display: flex; align-items: center; justify-content: center;
    overflow: hidden;
    font-size: calc(1em * var(--stage-expression-scale, 2));
  }

  .stage__pedal {
    flex: 1 1 auto;
    min-height: 0;
    display: flex; flex-direction: column;
    gap: clamp(3px, 0.8vw, 8px);
    position: relative; z-index: 0; /* stacking context: keeps .stage__glow's
      negative z-index scoped here, above .stage's own flat background but
      below the switches painted on top of it */
  }
  /* Low-intensity, static light in the gutters follows actual LEDs. No
     blur filters, oversized layers or breathing animation on the Pi. */
  .stage__glow {
    position: absolute; inset: 0; z-index: -1;
    display: flex; flex-direction: column;
    gap: clamp(3px, 0.8vw, 8px);
    pointer-events: none;
    opacity: 0.18;
    overflow: hidden;
  }
  .stage__glow-row {
    flex: 1 1 0;
    display: flex;
    gap: clamp(3px, 0.8vw, 8px);
  }
  .stage__glow-spot {
    flex: 1 1 0;
    border-radius: 50%;
    opacity: 0;
    transition: opacity 0.15s ease;
  }
  .stage__pedal-row {
    flex: 1 1 0;
    min-height: 0;
    display: flex;
    gap: clamp(3px, 0.8vw, 8px);
  }
  .stage__switch {
    flex: 1 1 0;
    display: flex; flex-direction: column;
    align-items: center; justify-content: center;
    position: relative;
    gap: clamp(4px, 0.4vw, 8px);
    padding: clamp(6px, 1vw, 16px);
    padding-top: clamp(30px, 4vh, 52px);
    border-radius: 4px;
    background: linear-gradient(155deg, #19232c, var(--stage-display-panel) 45%, #0c1218);
    /* Unbound switches: neutral border, no colour. Bound switches get
       their LED colour via the inline style (see the grid markup). */
    border: 1px solid #26343e;
    min-width: 0;
    min-height: 0;
    box-sizing: border-box;
    font: inherit;
    color: inherit;
    text-align: inherit;
    touch-action: manipulation;
    -webkit-tap-highlight-color: transparent;
    cursor: pointer;
    box-shadow: inset 0 1px 0 #ffffff08, 0 2px 4px #00000030;
    transition: border-color 0.15s;
  }
  .stage__switch:disabled { cursor: default; }
  .stage__switch:focus-visible { outline: 2px solid var(--stage-display-accent); outline-offset: -4px; }
  .stage__switch::before {
    content: "";
    position: absolute; inset: 5px;
    border: 1px solid #ffffff06;
    border-radius: 1px;
    pointer-events: none;
  }
  .stage__pedal-row--lower .stage__switch:first-child {
    border-bottom-left-radius: var(--stage-switch-outer-radius);
  }
  .stage__pedal-row--lower .stage__switch:last-child {
    border-bottom-right-radius: var(--stage-switch-outer-radius);
  }
  .stage__pedal-row--lower .stage__switch:first-child::before {
    border-bottom-left-radius: max(0px, calc(var(--stage-switch-outer-radius) - 5px));
  }
  .stage__pedal-row--lower .stage__switch:last-child::before {
    border-bottom-right-radius: max(0px, calc(var(--stage-switch-outer-radius) - 5px));
  }
  .stage__switch::after {
    content: "";
    position: absolute;
    bottom: clamp(7px, 1.4vh, 16px); left: 28%; right: 28%;
    height: 4px;
    background: var(--switch-led, #34444e);
    opacity: 0.65;
    pointer-events: none;
  }
  .stage__switch--bound {
    border-color: #4c6270;
  }
  /* The active module is steady: its frame and indicator keep the user's
     exact LED colour, and its entire panel is tinted for clear on/off contrast. */
  .stage__switch--active {
    background:
      linear-gradient(160deg, #ffffff0d, transparent 70%),
      linear-gradient(var(--switch-tint, #6fd99b55), var(--switch-tint, #6fd99b55)),
      var(--stage-display-panel);
    box-shadow: inset 0 0 0 1px var(--switch-led), inset 0 1px 0 #ffffff0d;
  }
  .stage__switch--active::after {
    opacity: 1; height: 6px;
    box-shadow: 0 0 8px var(--switch-led);
    animation: stage-light-pulse 3s ease-in-out infinite;
  }

  .stage__switch-label {
    font-size: calc(var(--stage-label-font-size) * var(--stage-switch-label-scale, 1));
    color: var(--stage-switch-label-color, #ffffff);
    font-family: var(--stage-switch-label-font, var(--stage-font, "Inter", -apple-system, sans-serif));
    text-align: center;
    white-space: nowrap; overflow: hidden; text-overflow: clip;
    max-width: 100%; line-height: 1.15;
    font-weight: 700;
    position: relative;
  }
  /* Inline-block so `transform` applies to it (a plain inline element isn't
     transformable) - this is the piece that actually slides; its parent
     frame (.stage__rig-name / .stage__bank / .stage__switch-label above)
     stays put and clips it via overflow:hidden. */
  .stage__marquee-track {
    display: inline-block;
    white-space: nowrap;
  }
  /* :global - this class is only ever added imperatively by the `marquee`
     action (see <script>), never written in markup, so Svelte's scoped-CSS
     usage analysis can't see it applies here and silently drops the whole
     rule without :global (confirmed via a throwaway browser harness: the
     "unused selector" warning is not benign - the rule is genuinely absent
     from the shipped CSS otherwise). Compounded onto .stage__marquee-track,
     which IS statically visible in the template, so this still resolves
     normally rather than as a bare unscoped global selector. */
  .stage__marquee-track:global(.stage__marquee-active) {
    animation: stage-marquee 5s ease-in-out infinite;
  }
  @keyframes stage-marquee {
    0%, 15%   { transform: translateX(0); }
    40%, 60%  { transform: translateX(var(--marquee-dx, -50%)); }
    85%, 100% { transform: translateX(0); }
  }
  .stage__switch-id {
    position: absolute;
    top: clamp(8px, 1.2vh, 14px); left: clamp(8px, 1vw, 16px);
    font-size: calc(clamp(0.65rem, 2vw, 1.3rem) * var(--stage-switch-id-scale, 1));
    color: var(--stage-switch-id-color, var(--stage-display-muted));
    font-family: var(--stage-switch-id-font, var(--stage-font, "Inter", -apple-system, sans-serif));
    font-weight: 600; letter-spacing: 0.08em;
    line-height: 1.2;
  }
  @media (prefers-reduced-motion: reduce) {
    .stage__switch, .stage__glow-spot { transition: none; }
    .stage__marquee-track:global(.stage__marquee-active) { animation: none; }
    .stage__divider::before { animation: none; opacity: 0.85; }
    .stage__divider::after { animation: none; opacity: 0.7; }
    .stage__switch--active::after { animation: none; opacity: 1; }
  }
  @media (pointer: coarse) {
    .stage__icon-btn { min-height: 40px; }
    .stage__bank-btn { min-height: 40px; font-size: 32px; }
  }

  /* ===== LANDSCAPE: immersive full-screen ===== */
  @media (orientation: landscape) {
    .stage {
      --stage-label-font-size: min(8.5vh, 2.8vw, 4.5rem);
      padding: max(clamp(8px, 1.8vh, 18px), calc(var(--stage-corner-radius) * 0.32));
      gap: clamp(6px, 1.4vh, 12px);
    }
    .stage__header {
      flex: 0 0 auto;
      flex-direction: row;
      justify-content: space-between;
      align-items: center;
      flex-wrap: nowrap;
    }
    .stage__rig-name {
      /* A tall desktop window still has only one header row and five cards
         across. Bound type by both axes so ordinary saved names fit even
         when the editor's root font is enlarged; long names still scroll. */
      font-size: calc(min(12vh, 5.5vw, 7rem) * var(--stage-rig-name-scale, 1));
      line-height: 1.1;
      flex: 1.4 1 0;
    }
    .stage__meta {
      flex: 1.55 1 0;
    }
    .stage__bank-readout { flex-grow: 1.3; }
    .stage__bank, .stage__rig {
      color: var(--stage-bank-color, #ffffff);
      letter-spacing: 0.02em; line-height: 1.2;
    }
    .stage__bpm  { font-size: calc(min(5vh, 2.4vw, 3rem) * var(--stage-bpm-scale, 1)); }
    .stage__expression { font-size: min(5.5vh, 2.4vw, 3rem); }

    .stage__pedal {
      flex: 1 1 0;
      gap: clamp(6px, 1.8vh, 14px);
    }
    .stage__glow {
      gap: clamp(6px, 1.8vh, 14px);
    }
    .stage__glow-row {
      gap: clamp(6px, 1.8vh, 14px);
    }
    .stage__pedal-row {
      gap: clamp(6px, 1.8vh, 14px);
    }
    .stage__switch {
      padding: clamp(8px, 2vh, 20px);
      padding-top: clamp(24px, 4.8vh, 46px);
    }
    .stage__switch-label {
      color: var(--stage-switch-label-color, #ffffff);
      line-height: 1.1;
    }
    .stage__switch-id {
      font-size: calc(min(3.4vh, 1.2vw, 1.6rem) * var(--stage-switch-id-scale, 1));
    }
  }
</style>
