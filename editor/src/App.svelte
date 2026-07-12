<script lang="ts">
  import { onMount, onDestroy } from "svelte";
  import PatchEditor from "./components/PatchEditor.svelte";
  import MidiLearn, { type PatchCapture } from "./components/MidiLearn.svelte";
  import Installer from "./components/Installer.svelte";
  import FirmwarePushOverlay from "./components/FirmwarePushOverlay.svelte";
  import MaintenancePanel from "./components/MaintenancePanel.svelte";
  import MidiMonitor from "./components/MidiMonitor.svelte";
  import Dashboard from "./components/Dashboard.svelte";
  import PatchesGrid from "./components/PatchesGrid.svelte";
  import Onboarding from "./components/Onboarding.svelte";
  import { detectPedal } from "./lib/installer";
  import { readSavedTheme, saveTheme, type Theme } from "./lib/theme";
  import {
    readSavedScale, saveScale, clampScale,
    DEFAULT_SCALE, STEP,
  } from "./lib/ui-scale";
  import { type LinkConfig, resolveLinkedPatches, applyLockToggle } from "./lib/patch-links";
  import PatchActions from "./components/PatchActions.svelte";
  import Settings from "./components/Settings.svelte";
  import PluginRecipe from "./components/PluginRecipe.svelte";
  import ProfilePicker from "./components/ProfilePicker.svelte";
  import TftLayout from "./components/TftLayout.svelte";
  import QuickSetup from "./components/QuickSetup.svelte";
  import PedalSimulator from "./components/PedalSimulator.svelte";
  import DeviceImport from "./components/DeviceImport.svelte";
  import SetlistView from "./components/SetlistView.svelte";
  import type { SetlistItem } from "./lib/setlists";
  import {
    autoConnect,
    cmd,
    connect,
    disconnect,
    fallbackManifest,
    isConnected,
    listPorts,
    midiBridgeStart,
    midiBridgeStop,
    midiBridgeStatus,
    type BridgeStatus,
    onDisconnected,
    onFirmwareMessage,
    patchIdOf,
    type FirmwareMessage,
    type Manifest,
    type MidiInCapturedEvent,
    type MidiLearnTable,
    type Binding,
    type Patch,
    type PatchSummary,
    type PortInfo,
  } from "./lib/protocol";

  /** Built-in pages plus any plugin recipe ids contributed by the
   * firmware manifest at runtime. We type as `string` so a new plugin
   * can introduce its own page id without an editor code change. */
  type Page = string;
  let page = $state<Page>("home");
  // Which tab is showing inside the Editor page. "Quick setup" is a mode of the
  // editor (it acts on the open patch), not a separate nav destination.
  let editorTab = $state<"switches" | "quicksetup" | "simulate">("switches");

  let ports = $state<PortInfo[]>([]);
  let selectedPort = $state<string | null>(null);
  let connected = $state(false);
  let connectedPortName = $state<string>("");
  let manualMode = $state(false);
  let busy = $state(false);
  let error = $state<string>("");
  // Surfaced firmware errors. Routed through the toast system: when the
  // pedal reports an ERROR (rejected message, exception, etc.) the user
  // sees a transient banner instead of having it disappear silently
  // into the log.
  function flashFirmwareError(detail: string) {
    showToast("error", detail);
  }

  // Toast notifications. Top-right floating banner, auto-dismissed.
  // Child components fire `window.dispatchEvent("bosun-toast", {detail: {level, message}})`
  // and we render it here so any page (Maintenance export, future
  // flows) gets a consistent confirmation/error surface.
  type ToastLevel = "ok" | "error" | "info";
  let toast = $state<{ message: string; level: ToastLevel } | null>(null);
  let _toastTimer: ReturnType<typeof setTimeout> | undefined;
  function showToast(level: ToastLevel, message: string) {
    toast = { message, level };
    if (_toastTimer !== undefined) clearTimeout(_toastTimer);
    _toastTimer = setTimeout(() => { toast = null; }, level === "error" ? 8000 : 5000);
  }

  let showInstaller = $state(false);
  // True when the installer was auto-opened because an unflashed pedal was
  // detected (vs. the user clicking "Install firmware" themselves). Drives the
  // installer's confirmation gate.
  let installerAutoPrompt = $state(false);
  // Latch so a dismissed auto-prompt ("Not now") doesn't immediately reopen.
  // Reset when the pedal is unplugged, so a replug prompts again.
  let installDismissed = $state(false);
  let unflashedPollHandle: ReturnType<typeof setInterval> | null = null;
  // Guards for pollForUnflashedPedal. `_pollInFlight` stops overlapping probes
  // (auto_connect can take longer than the poll interval) from fighting over
  // the serial port. `_stockSerialMisses` debounces the ambiguous serial-only
  // case: a freshly-plugged Bosun pedal that is still booting / whose data CDC
  // port hasn't enumerated yet fails the first probe even though it IS flashed,
  // so we require several consecutive misses before claiming "no firmware".
  let _pollInFlight = false;
  let _stockSerialMisses = 0;
  const STOCK_SERIAL_RETRIES_BEFORE_PROMPT = 3;
  // Triggered by the topbar "Update firmware" button. Streams the bundled
  // firmware tree to the connected pedal via PUT_FILE - no MSC drive
  // dance required. The MSC-drive Installer modal is only used for the
  // fresh-install case from the welcome screen.
  // false = closed; true = push the bundled firmware; a string = push from
  // that resolved firmware-root path (user-picked folder or extracted zip).
  let showFirmwarePush = $state<string | boolean>(false);
  // First-launch wizard. Shown until the user explicitly dismisses it
  // (the flag is persisted in localStorage so we don't pester returning
  // users). The wizard is also useful as a "reset" if the user clears
  // their local storage and reopens the editor.
  let showOnboarding = $state<boolean>(
    (() => { try { return localStorage.getItem("BOSUN_ONBOARDED") !== "1"; } catch { return false; } })()
  );

  // Theme. Applied to <html> via data-theme so all CSS variables below
  // resolve to the right palette. The $effect both seeds the attribute
  // on first paint and persists every subsequent toggle - so
  // toggleTheme only needs to mutate the state.
  let theme = $state<Theme>(readSavedTheme());
  $effect(() => { saveTheme(theme); });
  function toggleTheme() {
    theme = theme === "dark" ? "light" : "dark";
  }

  // Toggle a slot column's lock (linked across banks). Writes the new
  // patch_link into device.json via PUT_GLOBAL and updates the local copy
  // optimistically so the padlock flips immediately in the grid/editor.
  async function toggleSlotLock(slot: number) {
    if (!globalDevice) return;
    // Pause the connection watchdog: PUT_GLOBAL makes the firmware write
    // device.json to flash, which briefly stalls the USB CDC; without this
    // the watchdog could mis-read that stall as a lost connection.
    busy = true;
    try {
      // applyLockToggle only returns the new device after the firmware has
      // persisted it - so we commit to globalDevice (and the padlock UI)
      // ONLY on success. A failed write never leaves the lock showing a
      // state the pedal didn't save.
      globalDevice = await applyLockToggle(globalDevice, patches, slot, async (d) => { await cmd.putGlobal(d); });
    } catch (e) {
      const msg = String(e);
      if (msg.toLowerCase().includes("not connected")) {
        // The live link is gone - route through the normal recovery so the
        // user gets the "click Connect" prompt instead of a dead-end toast.
        window.dispatchEvent(new CustomEvent("rust-disconnected", { detail: "lock toggle: not connected" }));
      } else {
        showToast("error", "Lock change failed: " + msg);
      }
    } finally {
      busy = false;
    }
  }

  // UI scale. Ctrl+/Ctrl-/Ctrl+0 bumps/shrinks/resets the editor font
  // size by setting html.style.fontSize. Persisted across launches.
  let uiScale = $state<number>(readSavedScale());
  $effect(() => { saveScale(uiScale); });
  function bumpScale(delta: number)  { uiScale = clampScale(uiScale + delta); }
  function resetScale()              { uiScale = DEFAULT_SCALE; }
  // Window-level keyboard shortcuts. Ctrl/Cmd + matches the browser
  // zoom convention (also "=" because "+" requires Shift on most
  // layouts). We use preventDefault so the WebView2 doesn't try to
  // apply its own zoom on top.
  $effect(() => {
    const onKey = (e: KeyboardEvent) => {
      if (!(e.ctrlKey || e.metaKey)) return;
      if (e.key === "+" || e.key === "=") { bumpScale(STEP);  e.preventDefault(); }
      else if (e.key === "-")             { bumpScale(-STEP); e.preventDefault(); }
      else if (e.key === "0")             { resetScale();     e.preventDefault(); }
    };
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  });

  // Firmware update check - fetched on app startup + manual "Check"
  // button. No polling.
  let updateStatus = $state<import("./lib/firmware-update").UpdateStatus>({ kind: "idle" });
  // Version of the firmware bundled with this editor (what the "Update
  // firmware" push actually installs). Fetched once - it never changes at
  // runtime. This is the offline update signal: the editor can always flash
  // its bundled tree, so if the pedal is older we can offer the update even
  // when GitHub is unreachable or the repo has no release yet.
  let bundledVersion = $state<string>("");
  async function checkForFirmwareUpdate() {
    updateStatus = { kind: "checking" };
    try {
      const { fetchLatestRelease, fetchBundledVersion } = await import("./lib/firmware-update");
      if (!bundledVersion) bundledVersion = await fetchBundledVersion();
      const latest = await fetchLatestRelease();
      updateStatus = {
        kind: "ok",
        installed: deviceInfo?.fw ?? "",
        latest,
        updateAvailable: false,           // recomputed reactively in $derived below
      };
    } catch (e) {
      updateStatus = { kind: "error", message: String(e) };
    }
  }
  const cmpVer = (a: string, b: string): number => {
    const s = (v: string) => v.split("-")[0].split(".").map(n => parseInt(n, 10) || 0);
    const pa = s(a), pb = s(b);
    for (let i = 0; i < 3; i++) { if ((pa[i] || 0) !== (pb[i] || 0)) return (pa[i] || 0) - (pb[i] || 0); }
    return 0;
  };
  // The version we can install right now is the bundled one. An update is
  // available when it is newer than what the pedal reports - this works with
  // no network. (A GitHub release newer than the bundled tree is surfaced as
  // an informational note below, since installing it would need a newer
  // editor build.)
  let updateAvailable = $derived.by<boolean>(() => {
    if (!deviceInfo?.fw || !bundledVersion) return false;
    return cmpVer(bundledVersion, deviceInfo.fw) > 0;
  });
  // A published release newer than what we bundle: a hint to ship a new
  // editor, not something the bundled-firmware push can install.
  let onlineNewer = $derived.by<string>(() => {
    if (updateStatus.kind !== "ok" || !updateStatus.latest?.version || !bundledVersion) return "";
    return cmpVer(updateStatus.latest.version, bundledVersion) > 0 ? updateStatus.latest.version : "";
  });
  // One-shot check on mount (fire-and-forget).
  $effect(() => { void checkForFirmwareUpdate(); });

  let deviceInfo = $state<{ fw: string; device: string; bank: number; slot: number; profile?: string } | null>(null);
  // Active profile's plugin kind (e.g. "ampero_ii_stage" or "kemper_player").
  // Drives which plugin-specific UI sections are shown.
  let activeKind = $state<string>("");
  let activeProfile = $state<import("./lib/protocol").ProfileInfo | null>(null);
  let hasActiveProfile = $derived<boolean>(!!deviceInfo?.profile);
  let patches = $state<PatchSummary[]>([]);
  let dirtyIds = $state<Array<{ bank: number; slot: number }>>([]);
  let learning = $state(false);
  let manifest = $state<Manifest | null>(null);
  let currentPatch = $state<{ bank: number; slot: number; patch: Patch } | null>(null);
  // Auto-retry state for GET_MANIFEST. The firmware occasionally drops a
  // request when it arrives mid-boot or while it's still flushing the
  // previous response - the editor stays stuck on "Loading plugin
  // manifest..." forever. We periodically re-send while connected and
  // give up after MANIFEST_MAX_RETRIES with a user-visible error.
  let manifestRetries = $state(0);
  let manifestGaveUp = $state(false);
  // True when `manifest` holds the core-only fallback (built after GET_MANIFEST
  // exhausted its retries) rather than a real firmware manifest. Drives the
  // "Plugin features unavailable" banner and lets us tell a genuine manifest
  // from the stand-in so a later real MANIFEST response can replace it.
  let manifestFallbackActive = $state(false);
  const MANIFEST_MAX_RETRIES = 5;
  const MANIFEST_RETRY_MS = 3000;
  let _manifestTimer: ReturnType<typeof setTimeout> | undefined;

  let globalDevice = $state<Record<string, unknown> | null>(null);
  let midiLearnTable = $state<MidiLearnTable>({ pc_to_patch: [] });
  let captures = $state<PatchCapture[]>([]);
  const _bankMsbCache = new Map<string, number>();
  const CAPTURE_CAP = 20;

  type LogEntry = { ts: number; raw: FirmwareMessage };
  let log = $state<LogEntry[]>([]);
  const LOG_CAP = 200;

  let unsubMsg: (() => void) | null = null;
  let unsubDisc: (() => void) | null = null;

  onMount(async () => {
    unsubMsg  = await onFirmwareMessage(handleMessage);
    unsubDisc = await onDisconnected(async () => {
      connected = false; learning = false; deviceInfo = null;
      manifest = null; currentPatch = null;
      manifestRetries = 0; manifestGaveUp = false; manifestFallbackActive = false;
      // Clean up Rust state too - the reader thread exits on disconnect
      // but the SerialHandle stays in state.serial. Explicit disconnect
      // releases it so the next autoConnect goes through cleanly.
      try { await disconnect(); } catch {}
    });
    connected = await isConnected();
    if (connected) {
      // Re-fetch the world (after HMR, after a fresh window, after Tauri
      // restart that kept the serial handle alive somehow).
      await refetchAll();
    }
    await refreshPorts();

    // Auto-connect on startup: if nothing is connected yet, silently probe
    // for the pedal and attach if found. Stays quiet when no pedal is plugged
    // in (no error toast) - the user can still connect manually. The watchdog
    // starts automatically via the `connected` $effect below.
    if (!connected && !manualMode) {
      // Show the "Connecting…" indicator while autoConnect runs. The Rust
      // command is async (off the UI thread) so the window stays responsive.
      busy = true;
      try {
        connectedPortName = await autoConnect();
        connected = true;
        await refetchAll();
      } catch { /* no pedal present - stay disconnected */ }
      finally { busy = false; }
    }

    // Watch for a pedal that's plugged in but isn't running bosun (factory /
    // stock firmware hides its drive, so the only hint is the USB device). When
    // we spot one, auto-open the installer with a confirmation gate. Runs only
    // while disconnected and the installer is closed.
    await pollForUnflashedPedal();
    unflashedPollHandle = setInterval(pollForUnflashedPedal, 2500);

    // ProfilePicker fires this after sending SWITCH_PROFILE. The firmware
    // is about to reboot - we wait, then reconnect from scratch and
    // re-fetch all state for the new profile.
    let _switchingProfile = false;
    window.addEventListener("profile-switched", async () => {
      // Re-entry guard: ignore stacked events (rapid clicks, retry loops).
      if (_switchingProfile) return;
      _switchingProfile = true;
      busy = true; error = "";
      // Clear UI immediately so user sees the transition happening
      deviceInfo = null; manifest = null; currentPatch = null;
      patches = []; dirtyIds = []; midiLearnTable = { pc_to_patch: [] };
      globalDevice = null; activeKind = "";
      manifestRetries = 0; manifestGaveUp = false; manifestFallbackActive = false;
      try { await disconnect(); } catch {}
      connected = false;
      try {
        // Firmware boot is ~3-4s. We don't know exactly when it's ready,
        // so retry with backoff instead of a single fixed wait.
        const ok = await tryReconnect(15_000);
        if (ok) {
          await refetchAll();
        } else {
          error = "profile switch: firmware didn't come back online in time";
        }
      } finally {
        busy = false;
        _switchingProfile = false;
      }
    });

    // Generic toast notifications. Any component can dispatch this
    // event to surface a transient confirmation / error banner.
    window.addEventListener("bosun-toast", (e: Event) => {
      const detail = (e as CustomEvent<{ level: ToastLevel; message: string }>).detail;
      if (detail) showToast(detail.level, detail.message);
    });

    // Track concurrent "connecting" operations from any source
    // (waitForReboot after a profile switch / reboot / firmware push,
    // any future auto-reconnect path). Counter-based so overlapping
    // ops don't end the indicator prematurely. When the count hits 0
    // we clear busy.
    let connectingDepth = 0;
    window.addEventListener("bosun-connecting", (e: Event) => {
      const detail = (e as CustomEvent<{ active: boolean }>).detail;
      if (!detail) return;
      connectingDepth = Math.max(0, connectingDepth + (detail.active ? 1 : -1));
      busy = connectingDepth > 0 || busy;
      if (connectingDepth === 0 && !_switchingProfile) busy = false;
    });

    // Fired by waitForReboot() after it has re-established the Rust
    // side connection following a self-issued reboot (export across
    // profiles, import-as-new-profile, manual reboot, firmware push).
    // The disconnect event already flipped `connected` to false; here
    // we flip it back and refetch the world so the user lands in a
    // working state instead of a stale "disconnected" UI.
    window.addEventListener("connection-resynced", async () => {
      try {
        connected = await isConnected();
        if (connected) {
          deviceInfo = null; manifest = null; currentPatch = null;
          patches = []; dirtyIds = []; midiLearnTable = { pc_to_patch: [] };
          globalDevice = null; activeKind = "";
          manifestRetries = 0; manifestGaveUp = false; manifestFallbackActive = false;
          await refetchAll();
        }
      } catch (e) { error = String(e); }
    });

    // Maintenance page "Update firmware" buttons ask us to open the OTA
    // push overlay (kept in App so it can survive a page switch). A
    // `detail.source` string targets a user-picked firmware folder/zip;
    // absent → push the bundled firmware.
    window.addEventListener("bosun-open-firmware-push", (e: Event) => {
      const src = (e as CustomEvent<{ source?: string }>).detail?.source;
      showFirmwarePush = src || true;
    });

    // Rust-level disconnect (firmware rebooted via OTA / manual reboot /
    // crash). USB CDC re-enumerates as the same COM port so the OS
    // doesn't fire a disconnect, but `send_command` fails with "not
    // connected" once the reader thread has bailed. Sync UI state and
    // surface a recoverable error message - user clicks Connect again.
    window.addEventListener("rust-disconnected", async () => {
      if (!connected) return;
      connected = false;
      deviceInfo = null; manifest = null; currentPatch = null;
      patches = []; dirtyIds = []; midiLearnTable = { pc_to_patch: [] };
      globalDevice = null; activeKind = "";
      manifestRetries = 0; manifestGaveUp = false; manifestFallbackActive = false;
      // Defensively release the Rust-side handle so a future Connect
      // doesn't bounce against a stale "already connected" state.
      try { await disconnect(); } catch {}
      error = "Lost connection to firmware - click Connect to re-attach.";
    });
  });

  /** Try to (re)connect within `budgetMs`, with exponential-ish backoff.
      Returns true on success, false if the budget expired. Used after a
      firmware reboot when we don't know exactly when the data port is
      back. Each autoConnect attempt does its own probe-PING so a
      premature attempt fails fast - no harm in retrying. */
  async function tryReconnect(budgetMs: number): Promise<boolean> {
    const deadline = Date.now() + budgetMs;
    let waitMs = 1000;
    // First wait - give the firmware a head start so the very first
    // attempt has a chance instead of burning through immediate retries.
    await new Promise(r => setTimeout(r, 2000));
    while (Date.now() < deadline) {
      // Defensive: stale Rust handle could refuse autoConnect with
      // "already connected". Force-disconnect before each attempt.
      try { await disconnect(); } catch {}
      try {
        connectedPortName = await autoConnect();
        connected = true;
        await new Promise(r => setTimeout(r, 300));
        return true;
      } catch {}
      await new Promise(r => setTimeout(r, waitMs));
      waitMs = Math.min(waitMs + 500, 3000);
    }
    return false;
  }

  async function refetchAll() {
    try {
      await cmd.getDeviceInfo();
      // The plugin manifest is GLOBAL (not per-profile): it lists the available
      // target devices/kinds and their fields. Fetch it even with no profile -
      // otherwise profile creation only offers "generic" and plugin-specific UI
      // (Settings sections, TFT fields like kemper_rig) never appears.
      await cmd.getManifest();
      // Resolve the active profile. A freshly-installed pedal has none, and the
      // PER-PROFILE queries below (patches/global/dirty/midi-learn) answer
      // "not_found" until one exists - so skip them when there's no profile.
      let hasProfile = false;
      try {
        const r = await cmd.listProfiles();
        const act = r.profiles.find(p => p.active) ?? null;
        activeKind = act?.kind ?? "";
        activeProfile = act;
        hasProfile = !!act;
      } catch { activeKind = ""; activeProfile = null; }
      if (hasProfile) {
        await cmd.listPatches();
        await cmd.getDirty();
        await cmd.getMidiLearn();
        await cmd.getGlobal();
      }
      // Successful re-sync - any prior "lost connection" / "reconnect failed"
      // error is now stale.
      error = "";
    } catch (e) { error = String(e); }
  }

  // ------------------- watchdog -------------------
  // Catch state desyncs the rust-disconnected event might miss: if the
  // UI thinks we're connected but the Rust backend reports otherwise
  // (reader thread died without firing the event, OS-level CDC stall,
  // etc.), surface it as rust-disconnected so the UI follows reality.
  let _watchdog: ReturnType<typeof setInterval> | undefined;
  let _watchdogFails = 0;
  function startWatchdog() {
    stopWatchdog();
    _watchdogFails = 0;
    _watchdog = setInterval(async () => {
      if (!connected || busy) return;
      try {
        const ok = await isConnected();
        if (ok) { _watchdogFails = 0; return; }
        // Two consecutive negative reads before declaring death - avoids
        // false-positive disconnects during a transient race (e.g. mid
        // reconnect, mid-reboot detection).
        _watchdogFails++;
        if (_watchdogFails >= 2) {
          _watchdogFails = 0;
          window.dispatchEvent(new CustomEvent("rust-disconnected", { detail: "watchdog: backend reports not connected" }));
        }
      } catch { _watchdogFails = 0; }
    }, 3000);
  }
  function stopWatchdog() {
    if (_watchdog !== undefined) { clearInterval(_watchdog); _watchdog = undefined; }
  }
  $effect(() => {
    if (connected) startWatchdog();
    else           stopWatchdog();
  });

  // Manifest retry watchdog. Triggers once when we're connected but no
  // manifest has arrived yet. Each tick re-sends GET_MANIFEST until either
  // it lands (handler clears the timer) or we hit MANIFEST_MAX_RETRIES.
  $effect(() => {
    // The manifest is global (works with no profile), so retry it regardless -
    // it's needed to populate plugin kinds/fields even before a profile exists.
    if (!connected || manifest || manifestGaveUp) {
      if (_manifestTimer !== undefined) { clearTimeout(_manifestTimer); _manifestTimer = undefined; }
      return;
    }
    if (_manifestTimer !== undefined) return;   // already scheduled
    _manifestTimer = setTimeout(function tick() {
      if (manifest || !connected) {
        _manifestTimer = undefined; return;
      }
      if (manifestRetries >= MANIFEST_MAX_RETRIES) {
        manifestGaveUp = true;
        // Install the core-only fallback so the editor stays usable (Patches,
        // Editor, on_enter/on_exit) with core MIDI messages. Plugin-specific
        // messages and Settings sections are absent until a real MANIFEST
        // arrives; a non-blocking banner tells the user. We never overwrite a
        // real manifest with the fallback (guarded by !manifest above).
        manifest = fallbackManifest();
        manifestFallbackActive = true;
        _manifestTimer = undefined;
        return;
      }
      manifestRetries += 1;
      cmd.getManifest().catch(() => {});
      _manifestTimer = setTimeout(tick, MANIFEST_RETRY_MS);
    }, MANIFEST_RETRY_MS);
  });

  function retryManifest() {
    manifestRetries = 0;
    manifestGaveUp = false;
    // Drop the core-only fallback so the retry watchdog re-arms and a real
    // MANIFEST can replace it (the effect gate keys off `manifest` being null).
    if (manifestFallbackActive) { manifest = null; manifestFallbackActive = false; }
    cmd.getManifest().catch(() => {});
  }

  // A connected pedal with no profile (typically right after a fresh install)
  // needs the create-profile flow even for a returning user who dismissed
  // onboarding before. Open it once; re-arm after a profile exists so a future
  // virgin pedal prompts again. Avoids the dead-end "firmware: not_found" state.
  let _onboardingForced = false;
  $effect(() => {
    if (connected && deviceInfo && !hasActiveProfile && !showOnboarding && !_onboardingForced) {
      _onboardingForced = true;
      showOnboarding = true;
    }
    if (hasActiveProfile) _onboardingForced = false;
  });

  onDestroy(() => {
    unsubMsg?.(); unsubDisc?.(); stopWatchdog();
    if (_manifestTimer !== undefined) clearTimeout(_manifestTimer);
    if (_toastTimer !== undefined) clearTimeout(_toastTimer);
    if (unflashedPollHandle) clearInterval(unflashedPollHandle);
  });

  // The installer finished copying the firmware. A running bosun hides its
  // drive, so the wizard can't confirm success - we close it and connect to the
  // pedal once it has rebooted (the self-heal hard reset brings the data port
  // up a few seconds later).
  async function handleInstalled() {
    showInstaller = false;
    installerAutoPrompt = false;
    installDismissed = false;
    busy = true;
    error = "";
    try {
      const ok = await tryReconnect(20_000);
      if (ok) {
        await refetchAll();
        showToast("ok", "Firmware installed. Pedal connected.");
      } else {
        error = "Firmware installed, but the pedal didn't reconnect in time - click Connect.";
      }
    } catch (e) {
      error = "Firmware installed; reconnect failed: " + String(e);
    } finally {
      busy = false;
    }
  }

  // Detect a pedal that needs bosun installed and offer to install it.
  // Confirmation-gated, never silent. Covers all install-needing states:
  //   - in the RP2040 bootloader (RPI-RP2 mass storage, no serial port)
  //   - a CIRCUITPY drive with no captain firmware, or the wrong CircuitPython
  //   - a stock pedal running firmware (serial USB VID, drive hidden)
  // A healthy-but-unattached bosun is told apart from a stock pedal by trying a
  // real protocol connect first: bosun ACKs and attaches (no prompt); a stock
  // pedal doesn't (prompt).
  async function pollForUnflashedPedal() {
    if (connected || showInstaller || busy || manualMode) return;
    // Don't let a new probe start while the previous one is still running:
    // auto_connect can take several seconds, longer than the poll interval,
    // and overlapping opens contend for the same serial port (spurious fails).
    if (_pollInFlight) return;
    _pollInFlight = true;
    try {
      let dev;
      try { dev = await detectPedal(); } catch { return; }

      const inBootloader = !!dev.bootloader_drive;
      const cpNeedsFirmware = !!dev.circuitpy_drive && !dev.has_captain_firmware;
      const cpWrongVersion = !!dev.circuitpy_drive && !dev.circuitpython_ok;
      // Stock firmware running: only a serial device, no install drive exposed.
      const stockSerial =
        dev.usb_pedal_present && !dev.bootloader_drive && !dev.circuitpy_drive;

      const needsInstall = inBootloader || cpNeedsFirmware || cpWrongVersion || stockSerial;
      // Nothing install-worthy present: re-arm the dismissal latch AND clear the
      // serial-miss debounce so a later detection (or a replug) starts fresh.
      if (!needsInstall) { installDismissed = false; _stockSerialMisses = 0; return; }
      // Only the serial-only case is ambiguous; the drive/bootloader states are
      // definite, so they never accumulate (or wait on) the miss counter.
      if (!stockSerial) _stockSerialMisses = 0;
      if (installDismissed) return;

      // For the serial-only case, a healthy Bosun ACKs - attach silently and
      // skip the prompt. But a freshly-plugged pedal needs a few seconds to
      // boot and bring its data CDC up, so a single failed probe is NOT proof
      // of missing firmware: retry quietly across ticks and only prompt after
      // several consecutive misses. Drives/bootloader can't be probed this way.
      if (stockSerial) {
        try {
          connectedPortName = await autoConnect();
          connected = true;
          _stockSerialMisses = 0;
          await refetchAll();
          return;
        } catch { /* no bosun protocol yet - maybe still booting */ }
        if (connected || showInstaller) return;
        _stockSerialMisses += 1;
        if (_stockSerialMisses < STOCK_SERIAL_RETRIES_BEFORE_PROMPT) return;
      }

      installerAutoPrompt = true;
      showInstaller = true;
    } finally {
      _pollInFlight = false;
    }
  }

  async function refreshPorts() {
    error = "";
    try {
      ports = await listPorts();
      if (ports.length && !selectedPort) selectedPort = ports[0].name;
    } catch (e) { error = String(e); }
  }

  async function doConnect() {
    busy = true; error = "";
    try {
      // Defensive: if a prior stale handle is somehow still around (e.g.
      // closed the window mid-operation and reopened), clear it first so
      // the connect attempt isn't refused with "already connected".
      try { await disconnect(); } catch {}
      if (manualMode) {
        if (!selectedPort) throw new Error("pick a port first");
        await connect(selectedPort);
        connectedPortName = selectedPort;
      } else {
        connectedPortName = await autoConnect();
      }
      connected = true;
      await refetchAll();
    } catch (e) { error = String(e); }
    finally { busy = false; }
  }

  async function doDisconnect() {
    busy = true;
    try {
      await disconnect();
      // Drop the MIDI relay too - the pedal is going away.
      await stopBridge();
      connected = false; deviceInfo = null; manifest = null;
      currentPatch = null; learning = false; captures = [];
      patches = []; dirtyIds = []; midiLearnTable = { pc_to_patch: [] };
      globalDevice = null; activeKind = ""; error = "";
      _bankMsbCache.clear();
    } catch (e) { error = String(e); }
    finally { busy = false; }
  }

  async function toggleLearn() {
    try {
      if (learning) await cmd.stopLearn();
      else          await cmd.startLearn();
      learning = !learning;
      // Capture only works if the source's MIDI actually reaches the pedal. For
      // a USB-only Kemper Player that means the PC must relay it - so when the
      // user starts learning, bring the in-editor bridge up automatically (best
      // effort: a missing Kemper/pedal MIDI port just leaves it off with a hint).
      if (learning && !bridge.active) void startBridge(true);
    } catch (e) { error = String(e); }
  }

  // ---- in-editor MIDI bridge (Kemper Player <-> pedal) ----
  // Relays USB-MIDI both ways so MIDI Learn capture and the bidirectional sync
  // work without running tools/midi_bridge.py by hand.
  let bridge = $state<BridgeStatus>({ active: false, kemper_port: null, pedal_port: null });
  async function refreshBridge() {
    try { bridge = await midiBridgeStatus(); } catch { /* leave as-is */ }
  }
  async function startBridge(auto = false) {
    try {
      bridge = await midiBridgeStart();
      showToast("ok", `MIDI bridge on: ${bridge.kemper_port} <-> ${bridge.pedal_port}`);
    } catch (e) {
      // On an auto-start (learn began) a missing device is expected for users
      // not on a USB Kemper - keep it quiet-ish but still tell them why capture
      // may stay empty. A manual click always surfaces the reason.
      const msg = String(e);
      showToast(auto ? "info" : "error", `MIDI bridge not started: ${msg}`);
      await refreshBridge();
    }
  }
  async function stopBridge() {
    try { await midiBridgeStop(); } catch { /* ignore */ }
    await refreshBridge();
  }
  // Reflect the real backend state whenever the Learn page opens.
  $effect(() => { if (page === "learn" && connected) void refreshBridge(); });

  async function updateMidiLearn(table: MidiLearnTable) {
    midiLearnTable = table;
    await cmd.putMidiLearn(table);
  }
  function clearCapture(idx: number) { captures = captures.filter((_, i) => i !== idx); }
  function clearAllCaptures() { captures = []; }

  // Click a placeholder cell in the patches grid to materialize a
  // blank patch at that (bank, slot) and drop straight into the
  // editor. The patch is created with sensible defaults; the user
  // can edit name, color, bindings from there.
  async function createBlankPatch(bank: number, slot: number) {
    const { defaultLedFor } = await import("./lib/switch-colors");
    const SWITCH_ORDER = ["1","2","3","4","up","A","B","C","D","down"];
    const blank: Patch = {
      name: `Patch ${String(bank).padStart(2,"0")}/${String(slot).padStart(2,"0")}`,
      tft_color: "#00ff88",
      bindings: SWITCH_ORDER.map(sw => ({
        switch: sw,
        mode: "tap",
        label: "",
        led: { on: defaultLedFor(sw) },
        actions: { press: { messages: [] } },
      })),
    };
    try {
      await cmd.putPatch(bank, slot, blank);
      await cmd.listPatches();
      openPatchInEditor(bank, slot);
    } catch (e) {
      try {
        window.dispatchEvent(new CustomEvent("bosun-toast", {
          detail: { level: "error", message: "Couldn't create patch: " + String(e) },
        }));
      } catch {}
    }
  }

  function openPatchInEditor(bank: number, slot: number) {
    // Only ask the firmware to switch when this isn't already the
    // live patch. SWITCH_PATCH runs switches.reset_all() on the pedal
    // which zeroes every latched LED, so a redundant "switch to the
    // patch I'm already on" was making the user see "LED off after
    // opening the patch" before they did anything else.
    if (!deviceInfo || deviceInfo.bank !== bank || deviceInfo.slot !== slot) {
      cmd.switchPatch(bank, slot).catch(() => {});
    }
    // Set the target location FIRST so the incoming PATCH response is
    // recognised as "matches current" by the handleMessage filter. We
    // seed with a placeholder patch shape - the response fills it in
    // a moment later.
    const placeholder = patches.find(p => p.bank === bank && p.slot === slot);
    currentPatch = {
      bank, slot,
      patch: { name: placeholder?.name ?? "", bindings: [] },
    };
    cmd.getPatch(bank, slot).catch(() => {});
    page = "editor";
  }

  // When the editor page is visible without a loaded patch, fetch the
  // device's current patch (covers reload, navigation-without-click).
  let fetchedAt = $state<string>("");
  $effect(() => {
    if (page === "editor" && deviceInfo && !currentPatch) {
      const key = `${deviceInfo.bank}/${deviceInfo.slot}`;
      if (key !== fetchedAt) {
        fetchedAt = key;
        cmd.getPatch(deviceInfo.bank, deviceInfo.slot).catch(() => {});
      }
    }
  });

  // Lazy-fetch device config the first time Settings opens.
  let settingsRequested = $state(false);
  $effect(() => {
    if (page === "settings" && connected && !globalDevice && !settingsRequested) {
      settingsRequested = true;
      cmd.getGlobal().catch(() => {});
    }
  });

  // Expose app state + cmd helpers to window so the CDP debug script can poke.
  $effect(() => {
    (window as unknown as { __app: unknown }).__app = {
      page, connected, connectedPortName,
      deviceInfo, manifest, currentPatch, patches, dirtyIds,
      globalDevice, midiLearnTable, captures, learning,
      logCount: log.length,
      lastLog: log.length ? log[log.length - 1] : null,
      error,
    };
  });
  (window as unknown as { __cmd: unknown }).__cmd = cmd;
  import("@tauri-apps/api/core").then(m => {
    (window as unknown as { __invoke: unknown }).__invoke = m.invoke;
  });

  // Mirror every received firmware-message line for the CDP debug tool.
  let rawRx = $state<string[]>([]);
  let rawPayloads = $state<Array<{len:number; head:string; tail:string; valid:boolean}>>([]);
  onMount(() => {
    let unsub: (() => void) | undefined;
    let unsubRaw: (() => void) | undefined;
    // Svelte's onMount return type is `() => void | Promise<never>`. The
    // cleanup function must be returned synchronously, so we kick off
    // the async setup in an IIFE and stash the unsubscribers as they
    // arrive. The cleanup tolerates the early-unmount race.
    (async () => {
      const { onFirmwareRawLine } = await import("./lib/protocol");
      unsubRaw = await onFirmwareRawLine((p) => {
        let valid = false;
        let err = "";
        try { JSON.parse(p); valid = true; } catch (x) { err = String(x); }
        const entry: any = { len: p.length, head: p.slice(0, 140), tail: p.slice(-80), valid, err, full: p };
        const next = [...rawPayloads, entry];
        if (next.length > 200) next.shift();
        rawPayloads = next;
      });
      unsub = await onFirmwareMessage((m) => {
        const s = JSON.stringify(m);
        const next = [...rawRx, s];
        if (next.length > 100) next.shift();
        rawRx = next;
      });
    })();
    return () => { unsub?.(); unsubRaw?.(); };
  });
  $effect(() => {
    (window as unknown as { __rawRx: string[]; __rawPayloads: unknown[] }).__rawRx = rawRx;
    (window as unknown as { __rawRx: string[]; __rawPayloads: unknown[] }).__rawPayloads = rawPayloads;
  });

  function handleMessage(msg: FirmwareMessage) {
    if (!isLogNoise(msg)) pushLog(msg);
    switch (msg.type) {
      case "DEVICE_INFO":
        deviceInfo = {
          fw: msg.fw, device: msg.device,
          bank: msg.current.bank, slot: msg.current.slot,
          profile: (msg as { profile?: string }).profile ?? "",
        };
        // NOTE: do NOT auto-fetch the current Captain patch on connect.
        // currentPatch stays null until the user explicitly opens one
        // from the Patches list - that's what enables Clone/Delete and
        // tells the user "this is what those buttons will act on".
        break;
      case "MANIFEST":
        manifest = { core_messages: msg.core_messages, plugins: msg.plugins };
        manifestRetries = 0;
        manifestGaveUp = false;
        manifestFallbackActive = false;
        if (_manifestTimer !== undefined) { clearTimeout(_manifestTimer); _manifestTimer = undefined; }
        break;
      case "GLOBAL":
        globalDevice = msg.device;
        break;
      case "PATCH":
        // Only adopt the response when it matches the patch the user
        // is currently viewing. Background GET_PATCH calls fired by
        // link-maintenance helpers (removeLink's partner fetch, etc.)
        // would otherwise hijack the editor and snap the user to the
        // wrong patch - making it look like an edit "got reverted".
        if (!currentPatch
            || (msg.bank === currentPatch.bank && msg.slot === currentPatch.slot)) {
          currentPatch = { bank: msg.bank, slot: msg.slot, patch: msg.patch };
        }
        break;
      case "PATCH_LIST":
        patches = msg.patches;
        break;
      case "DIRTY":
        dirtyIds = msg.patches;
        break;
      case "MIDI_LEARN":
        midiLearnTable = msg.table?.pc_to_patch ? msg.table : { pc_to_patch: [] };
        break;
      case "ERROR": {
        const err = (msg as { error?: string }).error || "unknown";
        const of  = (msg as { of?: string }).of;
        const detail = (msg as { detail?: string }).detail;
        // A profile-less (freshly-installed) pedal answers "not_found" to
        // manifest/patch queries. That's expected, not a fault - don't toast it.
        if (err === "not_found" && !deviceInfo?.profile) { break; }
        flashFirmwareError(
          `firmware: ${err}${of ? ` (handling ${of})` : ""}${detail ? ` - ${detail}` : ""}`,
        );
        break;
      }
      case "EVENT":
        if (msg.event === "patch_switched") {
          const bank = msg.bank as number, slot = msg.slot as number;
          if (deviceInfo) deviceInfo = { ...deviceInfo, bank, slot };
          cmd.getPatch(bank, slot).catch(() => {});
        } else if (msg.event === "dirty_state_changed") {
          dirtyIds = (msg.patches as Array<{ bank: number; slot: number }>) ?? [];
        } else if (msg.event === "discarded" || msg.event === "saved") {
          // Firmware just reverted (or persisted) some patches: pull the
          // canonical version back so the editor's working copy snaps
          // to it instead of keeping the stale unsaved label/color/...
          // Without this, hitting Discard cleared the dirty flag but
          // the patch editor still showed the rejected edits.
          const list = (msg.patches as Array<{ bank: number; slot: number }>) ?? [];
          const cur = currentPatch;
          if (cur && list.some(p => p.bank === cur.bank && p.slot === cur.slot)) {
            cmd.getPatch(cur.bank, cur.slot).catch(() => {});
            cmd.listPatches().catch(() => {});
          }
        } else if (msg.event === "midi_in_captured") {
          handleCapture(msg as unknown as MidiInCapturedEvent);
        }
        break;
    }
  }

  function handleCapture(msg: MidiInCapturedEvent) {
    const port = msg.port, channel = msg.channel, kind = msg.kind;
    const data = msg.data ?? [];
    const key = `${port}:${channel}`;
    if (kind === "cc" && data[0] === 0) { _bankMsbCache.set(key, data[1] ?? 0); return; }
    if (kind === "pc" && data.length > 0) {
      const capture: PatchCapture = {
        port, channel,
        bank_msb: _bankMsbCache.get(key) ?? 0,
        pc: data[0], ts: Date.now(),
      };
      const next = [capture, ...captures];
      if (next.length > CAPTURE_CAP) next.length = CAPTURE_CAP;
      captures = next;
    }
  }

  function pushLog(raw: FirmwareMessage) {
    const next = [...log, { ts: Date.now(), raw }];
    if (next.length > LOG_CAP) next.splice(0, next.length - LOG_CAP);
    log = next;
  }

  /** Quiet the log: probe ACKs, bad_json from leading-newline drain. */
  function isLogNoise(msg: FirmwareMessage): boolean {
    const id = (msg as { id?: string }).id;
    if (id && id.startsWith("probe-")) return true;
    if (msg.type === "ERROR" && (msg as { error?: string }).error === "bad_json") return true;
    return false;
  }
  function fmtTs(ms: number) {
    const d = new Date(ms);
    return `${String(d.getHours()).padStart(2,"0")}:${String(d.getMinutes()).padStart(2,"0")}:${String(d.getSeconds()).padStart(2,"0")}.${String(d.getMilliseconds()).padStart(3,"0")}`;
  }
  function summarize(m: FirmwareMessage): string {
    if (m.type === "EVENT") {
      const rest = Object.entries(m).filter(([k]) => k !== "type" && k !== "event")
        .map(([k, v]) => `${k}=${typeof v === "object" ? JSON.stringify(v) : v}`).join(" ");
      return `EVENT ${m.event}  ${rest}`;
    }
    return JSON.stringify(m);
  }

  // Built-in nav items + any plugin recipes injected dynamically from
  // the manifest. A nav item can declare a `kind` to be shown only when
  // the matching plugin is the active profile; plugin recipe items
  // inherit their kind from the plugin id.
  // The 10 physical switches, in firmware order. Passed to QuickSetup so the
  // user can assign recipe roles to switches.
  const SWITCH_NAMES = ["1","2","3","4","up","A","B","C","D","down"];

  // Apply a recipe's generated bindings to the currently-open patch. Each
  // binding is written straight to the firmware, then we re-read the patch and
  // refresh the list so the editor and grid reflect the new switches.
  function applyRecipeBindings(bindings: Binding[]) {
    if (!currentPatch) return;
    const { bank, slot } = currentPatch;
    for (const b of bindings) cmd.putBinding(bank, slot, b);
    cmd.getPatch(bank, slot);
    cmd.listPatches();
    // Jump back to the switch list so the user sees the bindings just written.
    editorTab = "switches";
    window.dispatchEvent(new CustomEvent("bosun-toast", {
      detail: { level: "ok", message: `Applied ${bindings.length} binding${bindings.length === 1 ? "" : "s"}` },
    }));
  }

  // Persist a setlist onto the pedal. The ordered (bank, slot) list lives under
  // device.json's "setlist" key; the firmware's captain_setlist_step walks it.
  // We merge into the current globalDevice (never clobbering other keys) and
  // push it with PUT_GLOBAL, updating the local copy optimistically so the
  // "on pedal" badge flips once the write lands.
  async function sendSetlistToPedal(payload: { name: string; items: SetlistItem[] }) {
    if (!globalDevice) return;
    const next = { ...globalDevice, setlist: { name: payload.name, items: payload.items } };
    busy = true;
    try {
      await cmd.putGlobal(next);
      globalDevice = next;
      window.dispatchEvent(new CustomEvent("bosun-toast", {
        detail: { level: "ok", message: `Setlist "${payload.name}" sent (${payload.items.length} patch${payload.items.length === 1 ? "" : "es"})` },
      }));
    } catch (e) {
      const msg = String(e);
      if (msg.toLowerCase().includes("not connected")) {
        window.dispatchEvent(new CustomEvent("rust-disconnected", { detail: "setlist send: not connected" }));
      } else {
        showToast("error", "Setlist send failed: " + msg);
      }
    } finally {
      busy = false;
    }
  }

  // Sidebar items carry a `group` so the nav can render hierarchically instead
  // of one flat list. Home stands alone above the groups. "Quick setup" is NOT
  // here: it lives inside the Editor as a tab (it only makes sense on the patch
  // you have open), see `editorTab` below.
  type NavItem = { id: Page; label: string; icon: string; kind?: string; group: NavGroup };
  type NavGroup = "" | "build" | "device" | "system";
  const CORE_NAV: NavItem[] = [
    { id: "home",     label: "Home",         icon: "⌂", group: "" },
    { id: "patches",  label: "Patches",      icon: "▣", group: "build" },
    { id: "editor",   label: "Editor",       icon: "✎", group: "build" },
    { id: "setlist",  label: "Setlist",      icon: "≣", group: "build" },
    { id: "tft",      label: "Screen layout", icon: "▭", group: "device" },
    { id: "learn",    label: "MIDI Learn",   icon: "↻", group: "device" },
    { id: "settings", label: "Settings",     icon: "⚙", group: "device" },
    { id: "maint",    label: "Maintenance",  icon: "⊕", group: "system" },
    { id: "monitor",  label: "MIDI Monitor", icon: "∿", group: "system" },
    { id: "log",      label: "Log",          icon: "≡", group: "system" },
  ];
  // Plugin recipe pages (e.g. Ampero auto-follow setup) are device setup, so
  // they join the Device group. Active-kind filter unchanged.
  let visibleNav = $derived.by<NavItem[]>(() => {
    const recipes: NavItem[] = [];
    if (manifest) {
      for (const [pluginId, plug] of Object.entries(manifest.plugins)) {
        const r = plug.recipe_schema;
        if (r) {
          recipes.push({ id: r.id, label: r.label, icon: r.icon ?? "♪", kind: pluginId, group: "device" });
        }
      }
    }
    return [...CORE_NAV, ...recipes].filter(item => !item.kind || item.kind === activeKind);
  });
  // Bucket the visible items into their sections for rendering, in a fixed
  // group order, dropping any empty group. Order within a group follows
  // insertion order (so plugin recipes land after Settings in Device).
  let navGroups = $derived.by<Array<{ label: string; items: NavItem[] }>>(() => {
    const order: Array<{ key: NavGroup; label: string }> = [
      { key: "",       label: "" },
      { key: "build",  label: "Build" },
      { key: "device", label: "Device" },
      { key: "system", label: "System" },
    ];
    return order
      .map(g => ({ label: g.label, items: visibleNav.filter(n => n.group === g.key) }))
      .filter(g => g.items.length > 0);
  });
  // If the user was on a now-hidden page after a profile switch, bounce
  // them to Patches so they don't see a blank content area.
  $effect(() => {
    if (visibleNav.length && !visibleNav.some(n => n.id === page)) page = "home";
  });

</script>

<div class="app">
  <header class="topbar">
    <div class="brand">
      <svg class="logo" viewBox="0 0 24 24" aria-hidden="true">
        <!-- stylised anchor - nod to "bosun" / boatswain -->
        <circle cx="12" cy="5" r="2.4" fill="none" stroke="currentColor" stroke-width="1.6"/>
        <path d="M12 7.5 V18.5 M7 12 H17 M5 16 Q12 22 19 16" fill="none" stroke="currentColor" stroke-width="1.6" stroke-linecap="round"/>
      </svg>
      <span class="wordmark">Bosun</span>
    </div>

    <!-- Connection pill: at-a-glance state, replaces deciphering top-button labels.
         Three states: connected (green), busy/connecting (pulsing amber),
         disconnected (grey). busy covers explicit Connect clicks AND the
         reconnect dance after a SWITCH_PROFILE / REBOOT. -->
    <span class="connpill"
          class:on={connected && !busy}
          class:busy
          title={busy ? "Connecting…" : connected ? `Connected on ${connectedPortName}` : "No pedal connected"}>
      <span class="dot"></span>
      {busy ? "Connecting…" : connected ? "Connected" : "Disconnected"}
    </span>

    <div class="grow"></div>

    {#if connected}
      <ProfilePicker {manifest} />

      <!-- Firmware status: only meaningful when we're actually talking
           to a pedal. Disconnected → no claim, no button. -->
      {#if updateStatus.kind === "checking"}
        <span class="fwstatus muted">Checking for updates…</span>
      {:else if updateAvailable}
        <button class="topbtn primary"
                onclick={() => showFirmwarePush = true}
                title={onlineNewer
                  ? `Installs the bundled firmware v${bundledVersion}. A newer release (v${onlineNewer}) is available online - update the editor to ship it.`
                  : `Installs the bundled firmware v${bundledVersion}`}>
          Update firmware ({deviceInfo?.fw ?? "?"} -> {bundledVersion})
        </button>
      {:else if updateStatus.kind === "error"}
        <button class="topbtn ghost" onclick={checkForFirmwareUpdate} title={updateStatus.message}>Update check failed - retry</button>
      {:else if deviceInfo?.fw}
        <span class="fwstatus muted" title="Firmware up to date">v{deviceInfo.fw} ✓</span>
      {/if}
    {/if}

    <div class="uiscale" role="group" aria-label="UI scale">
      <button class="scalebtn" onclick={() => bumpScale(-STEP)}
              title="Shrink UI (Ctrl -)" aria-label="Shrink UI">A−</button>
      <button class="scalebtn"
              onclick={resetScale}
              ondblclick={resetScale}
              title="Reset UI scale (Ctrl 0) - current {Math.round(uiScale * 100)}%"
              aria-label="Reset UI scale">{Math.round(uiScale * 100)}%</button>
      <button class="scalebtn" onclick={() => bumpScale(STEP)}
              title="Enlarge UI (Ctrl +)" aria-label="Enlarge UI">A+</button>
    </div>

    <button class="themetoggle" onclick={toggleTheme}
            title={theme === "dark" ? "Switch to light theme" : "Switch to dark theme"}
            aria-label="Toggle theme">
      {#if theme === "dark"}
        <!-- sun -->
        <svg viewBox="0 0 24 24" aria-hidden="true">
          <circle cx="12" cy="12" r="4" fill="none" stroke="currentColor" stroke-width="1.7"/>
          <path d="M12 3 V5 M12 19 V21 M3 12 H5 M19 12 H21 M5.5 5.5 L7 7 M17 17 L18.5 18.5 M5.5 18.5 L7 17 M17 7 L18.5 5.5"
                stroke="currentColor" stroke-width="1.7" stroke-linecap="round" fill="none"/>
        </svg>
      {:else}
        <!-- moon -->
        <svg viewBox="0 0 24 24" aria-hidden="true">
          <path d="M20 14.5 A8 8 0 1 1 9.5 4 A6.5 6.5 0 0 0 20 14.5 Z"
                fill="none" stroke="currentColor" stroke-width="1.7" stroke-linejoin="round"/>
        </svg>
      {/if}
    </button>
  </header>

  {#if !connected}
    <main class="welcome">
      <div class="card">
        <h1>Connect your pedal</h1>
        <p class="hint">Plug the MIDI Captain via USB, then click below.</p>
        <button class="big" onclick={doConnect} disabled={busy}>
          {busy ? "Connecting…" : "Connect"}
        </button>
        <details class="advanced" inert={busy}>
          <summary>Advanced</summary>
          <label class="adv">
            <input type="checkbox" bind:checked={manualMode} disabled={busy} />
            manual port pick
          </label>
          {#if manualMode}
            <div class="manual">
              <button onclick={refreshPorts} disabled={busy}>Refresh</button>
              <select bind:value={selectedPort} disabled={busy}>
                {#each ports as p}<option value={p.name}>{p.name} - {p.kind}</option>{/each}
              </select>
            </div>
          {/if}
          <p class="hint small">
            Use the manual picker only if Connect auto-detect picks the wrong port
            (e.g. when other CDC devices are plugged in).
          </p>
        </details>
        {#if error}<p class="err">{error}</p>{/if}

        <!-- Fresh pedal with no firmware? The installer walks you through
             putting the Pico in bootloader mode and flashing CircuitPython +
             the bosun firmware. Surfaced as a quiet secondary link so it
             doesn't fight with the primary Connect CTA. Always shown - the
             install wizard itself detects what the pedal actually needs.
             Blocked while connecting so it can't open the installer (which
             would tear down the in-flight serial connection). -->
        <hr class="divider" />
        <p class="install-link">
          New pedal - never flashed before?
          <button class="linkbtn" onclick={() => showInstaller = true} disabled={busy}>Install firmware →</button>
        </p>
      </div>
    </main>
  {:else}
    {#if manifestFallbackActive}
      <div class="fallback-banner" role="status">
        <span class="fallback-banner__icon" aria-hidden="true">⚠</span>
        <span class="fallback-banner__msg">Plugin features unavailable - core MIDI only</span>
        <button class="fallback-banner__retry" onclick={retryManifest}>Retry</button>
      </div>
    {/if}
    <div class="shell">
      <nav class="sidebar">
        {#each navGroups as grp}
          {#if grp.label}
            <div class="navgroup-label">{grp.label}</div>
          {/if}
          {#each grp.items as item}
            <button class="navitem" class:active={page === item.id}
                    onclick={() => page = item.id}>
              <span class="icon">{item.icon}</span>
              <span class="lbl">{item.label}</span>
              {#if item.id === "patches" && dirtyIds.length > 0}
                <span class="badge-dot" title="unsaved">{dirtyIds.length}</span>
              {/if}
              {#if item.id === "learn" && learning}
                <span class="pulse-dot" title="learning"></span>
              {/if}
            </button>
          {/each}
        {/each}
        <div class="grow"></div>
        <button class="navitem ghost" onclick={doDisconnect} disabled={busy}>
          <span class="icon">⏻</span>
          <span class="lbl">Disconnect</span>
        </button>
      </nav>

      <main class="content">
        {#if page === "home"}
          <Dashboard
            {connected} {deviceInfo} {activeProfile} {activeKind}
            {manifest} {patches} {connectedPortName}
            onNavigate={(p) => page = p} />

        {:else if page === "patches"}
          <header class="pageHead">
            <h2>Patches</h2>
            <PatchActions {patches} currentPatchEnvelope={currentPatch} />
          </header>
          <PatchesGrid
            {patches}
            deviceInfo={deviceInfo ? { bank: deviceInfo.bank, slot: deviceInfo.slot } : null}
            {dirtyIds}
            linkConfig={(globalDevice?.patch_link as LinkConfig | undefined)}
            onToggleLock={(s) => { void toggleSlotLock(s); }}
            onOpen={openPatchInEditor}
            onCreate={(b, s) => { void createBlankPatch(b, s); }}
          />

          <div class="row toolbar">
            <button onclick={() => cmd.saveNow()} disabled={dirtyIds.length === 0}>
              Save{dirtyIds.length ? ` (${dirtyIds.length})` : ""}
            </button>
            <button onclick={() => cmd.discard()} disabled={dirtyIds.length === 0}>Discard</button>
            <button onclick={() => cmd.listPatches()}>Refresh list</button>
          </div>

        {:else if page === "editor"}
          {@const linkConfig = (globalDevice?.patch_link as LinkConfig | undefined)}
          {@const linkedTargets = currentPatch
            ? resolveLinkedPatches(
                { bank: currentPatch.bank, slot: currentPatch.slot },
                currentPatch.patch,
                patches,
                linkConfig,
              )
            : []}
          {@const linkedGroup = currentPatch
            ? [{ bank: currentPatch.bank, slot: currentPatch.slot }, ...linkedTargets]
            : []}
          {@const curDirty = currentPatch
            ? dirtyIds.some(d => d.bank === currentPatch!.bank && d.slot === currentPatch!.slot)
            : false}
          {@const groupDirty = linkedGroup.some(p =>
            dirtyIds.some(d => d.bank === p.bank && d.slot === p.slot))}
          {@const orderedPatches = patches.slice().sort((a, b) => a.bank - b.bank || a.slot - b.slot)}
          {@const curIdx = currentPatch
            ? orderedPatches.findIndex(p => p.bank === currentPatch!.bank && p.slot === currentPatch!.slot)
            : -1}
          {@const prevPatch = curIdx > 0 ? orderedPatches[curIdx - 1] : null}
          {@const nextPatch = curIdx >= 0 && curIdx < orderedPatches.length - 1 ? orderedPatches[curIdx + 1] : null}
          <header class="pageHead">
            <h2>
              {#if currentPatch}
                <span class="navgroup">
                  <button class="navarrow"
                          disabled={!prevPatch}
                          title={prevPatch ? `Previous: ${patchIdOf(prevPatch.bank, prevPatch.slot)} ${prevPatch.name || ""}` : "No previous patch"}
                          onclick={() => prevPatch && openPatchInEditor(prevPatch.bank, prevPatch.slot)}
                          aria-label="Previous patch">‹</button>
                  <button class="navarrow"
                          disabled={!nextPatch}
                          title={nextPatch ? `Next: ${patchIdOf(nextPatch.bank, nextPatch.slot)} ${nextPatch.name || ""}` : "No next patch"}
                          onclick={() => nextPatch && openPatchInEditor(nextPatch.bank, nextPatch.slot)}
                          aria-label="Next patch">›</button>
                </span>
                Editing {patchIdOf(currentPatch.bank, currentPatch.slot)} · {currentPatch.patch.name || "(unnamed)"}
                {#if curDirty}<span class="dirty-tag" title="unsaved">●</span>{/if}
              {:else}
                Editor
              {/if}
            </h2>
            {#if currentPatch}
              <button class="primary"
                      disabled={!groupDirty}
                      onclick={() => linkedGroup.forEach(p => cmd.saveNow(p.bank, p.slot))}
                      title={linkedGroup.length > 1
                        ? `Persist this patch + ${linkedGroup.length - 1} linked target(s) to disk`
                        : "Persist this patch to disk on the pedal"}>
                Save{linkedGroup.length > 1 ? ` (${linkedGroup.length})` : ""}
              </button>
              <button disabled={!groupDirty}
                      onclick={() => linkedGroup.forEach(p => cmd.discard(p.bank, p.slot))}
                      title={linkedGroup.length > 1
                        ? `Discard unsaved changes on this patch and ${linkedGroup.length - 1} linked target(s)`
                        : "Throw away unsaved changes and reload this patch from disk"}>
                Discard{linkedGroup.length > 1 ? ` (${linkedGroup.length})` : ""}
              </button>
            {/if}
          </header>
          {#if currentPatch && manifest}
            <div class="editor-tabs" role="tablist" aria-label="Editor mode">
              <button role="tab" class="etab" class:active={editorTab === "switches"}
                      aria-selected={editorTab === "switches"}
                      onclick={() => editorTab = "switches"}>Switches</button>
              <button role="tab" class="etab" class:active={editorTab === "quicksetup"}
                      aria-selected={editorTab === "quicksetup"}
                      onclick={() => editorTab = "quicksetup"}>Quick setup</button>
              <button role="tab" class="etab" class:active={editorTab === "simulate"}
                      aria-selected={editorTab === "simulate"}
                      onclick={() => editorTab = "simulate"}>Simulate</button>
            </div>
            {#if editorTab === "switches"}
              {#if activeKind === "kemper_player"}
                <!-- Kemper-only: pull the current rig's real name off the
                     device and offer it as this patch's name (single-rig, see
                     the component's note on why bulk scanning is not done). -->
                <DeviceImport
                  envelope={currentPatch}
                  {connected}
                  onApplied={(b, s) => { cmd.getPatch(b, s).catch(() => {}); cmd.listPatches().catch(() => {}); }}
                />
              {/if}
              <PatchEditor bank={currentPatch.bank} slot={currentPatch.slot}
                           patch={currentPatch.patch} {manifest} {activeKind}
                           allPatches={patches} {linkConfig}
                           onToggleLock={(s) => { void toggleSlotLock(s); }} />
            {:else if editorTab === "quicksetup"}
              <p class="muted" style="margin:0 0 0.75rem">
                Guided setups for this patch: pick which switches to use and the bindings are written for you - no need to know the MIDI messages. Applying jumps you back to Switches to see the result.
              </p>
              <QuickSetup
                switches={SWITCH_NAMES}
                {manifest}
                {activeKind}
                existing={currentPatch.patch.bindings}
                onApply={applyRecipeBindings}
              />
            {:else}
              <PedalSimulator
                bindings={currentPatch.patch.bindings}
                device={globalDevice}
                {connected}
              />
            {/if}
          {:else if !hasActiveProfile}
            <div class="manifest-error">
              <p>No profile on this pedal yet.</p>
              <p class="muted">
                A freshly-installed pedal has no patches until you create a profile
                for your gear (e.g. Kemper Player). Create one to get started.
              </p>
              <div class="row toolbar">
                <button class="primary" onclick={() => showOnboarding = true}>Create your first profile</button>
              </div>
            </div>
          {:else if !manifest && manifestGaveUp}
            <div class="manifest-error">
              <p>Plugin manifest didn't arrive after {MANIFEST_MAX_RETRIES} retries.</p>
              <p class="muted">
                The connected port acknowledged the PING but never returned a MANIFEST.
                Most common cause: the firmware on the pedal is older than this editor
                and has a known bug truncating large responses silently. Re-flash the
                firmware bundled with this editor to fix it. If a retry now succeeds
                you can ignore the re-flash.
              </p>
              <div class="row toolbar">
                <button class="primary" onclick={retryManifest}>Retry</button>
                <button onclick={() => showFirmwarePush = true}>Re-flash firmware</button>
                <button onclick={async () => { try { await disconnect(); } catch {} window.dispatchEvent(new CustomEvent("rust-disconnected", { detail: "manual" })); }}>Disconnect</button>
              </div>
            </div>
          {:else if !manifest}
            <p class="muted">
              Loading plugin manifest{manifestRetries > 0 ? ` (retry ${manifestRetries}/${MANIFEST_MAX_RETRIES})` : ""}…
            </p>
          {:else if deviceInfo}
            <p class="muted">Loading patch {deviceInfo.bank}/{deviceInfo.slot}…</p>
          {:else}
            <p class="muted">Pick a patch from the Patches tab to edit.</p>
          {/if}

        {:else if page === "setlist"}
          <header class="pageHead">
            <h2>Setlist</h2>
          </header>
          <p class="muted" style="margin:0 0 0.75rem">
            Build a gig in song order, then send it to the pedal. On stage, a "Setlist next / previous" switch (set it up in Quick setup) walks the list one song at a time, wherever those patches live in the grid. It never moves your patches.
          </p>
          <SetlistView
            {patches}
            deviceSetlist={(globalDevice?.setlist as { name?: string; items: SetlistItem[] } | undefined) ?? null}
            onSend={(payload) => { void sendSetlistToPedal(payload); }}
          />

        {:else if page === "learn"}
          <header class="pageHead">
            <h2>MIDI Learn</h2>
            <button class="primary" onclick={toggleLearn}>
              {learning ? "Stop learn" : "Start learn"}
            </button>
          </header>
          {#if deviceInfo}
            <MidiLearn
              {learning}
              table={midiLearnTable}
              {captures}
              {patches}
              currentBank={deviceInfo.bank}
              currentSlot={deviceInfo.slot}
              {bridge}
              onStartBridge={() => startBridge(false)}
              onStopBridge={stopBridge}
              onUpdate={updateMidiLearn}
              onClearCapture={clearCapture}
              onClearAllCaptures={clearAllCaptures}
            />
          {/if}

        {:else if manifest && Object.values(manifest.plugins).some(p => p.recipe_schema?.id === page)}
          {#each Object.values(manifest.plugins) as plug}
            {#if plug.recipe_schema?.id === page}
              <PluginRecipe schema={plug.recipe_schema} {patches} />
            {/if}
          {/each}

        {:else if page === "tft"}
          <TftLayout device={globalDevice} {manifest} {activeKind} />

        {:else if page === "settings"}
          <header class="pageHead">
            <h2>Settings</h2>
            <button onclick={() => cmd.getGlobal()}>Reload</button>
          </header>
          <Settings device={globalDevice} {manifest} {activeKind} {connected} />

        {:else if page === "maint"}
          <header class="pageHead">
            <h2>Maintenance</h2>
          </header>
          <MaintenancePanel {connected} {activeProfile} />

        {:else if page === "monitor"}
          <header class="pageHead">
            <h2>MIDI Monitor</h2>
          </header>
          <p class="muted" style="margin:0 0 0.75rem">
            Live view of every MIDI message the pedal sends and receives, decoded
            newest-first. The stream runs only while this page is open, so it
            never adds traffic during normal play. Handy for debugging bindings,
            MIDI Learn, and device sync.
          </p>
          <MidiMonitor {connected} />

        {:else if page === "log"}
          <header class="pageHead">
            <h2>Event log</h2>
            <button onclick={() => log = []}>Clear</button>
          </header>
          <div class="logbox">
            {#if log.length === 0}
              <div class="empty-state empty-state--inline">
                <div class="empty-state__title">Log is quiet</div>
                <p class="empty-state__hint">
                  Protocol messages will appear here as the pedal sends them.
                  Useful when you're debugging bindings or watching the
                  bidirectional sync to a supported device (Kemper, Ampero, ...).
                </p>
              </div>
            {:else}
              {#each log.slice().reverse() as e}
                <div class="logline">
                  <span class="ts">{fmtTs(e.ts)}</span>
                  <span class="msg">{summarize(e.raw)}</span>
                </div>
              {/each}
            {/if}
          </div>
        {/if}
      </main>
    </div>

    <!-- Footer removed: connection state lives in the topbar pill,
         transient errors flow through the toast system. The welcome
         screen still surfaces `error` inline when disconnected (a
         top-right toast would be ambiguous when the user is staring
         at the welcome card). -->
  {/if}

  {#if showInstaller}
    <Installer
      requireConfirm={installerAutoPrompt}
      onInstalled={handleInstalled}
      onClose={() => {
        showInstaller = false;
        // If the user dismissed an auto-prompt, don't reopen until replug.
        if (installerAutoPrompt) installDismissed = true;
        installerAutoPrompt = false;
      }} />
  {/if}
  {#if showFirmwarePush}
    <FirmwarePushOverlay
      source={typeof showFirmwarePush === "string" ? showFirmwarePush : undefined}
      onClose={() => showFirmwarePush = false} />
  {/if}

  {#if showOnboarding}
    <Onboarding
      {connected}
      hasActiveProfile={!!activeProfile}
      {manifest}
      onClose={() => showOnboarding = false} />
  {/if}

  {#if toast}
    <div class="toast toast--{toast.level}" role="status" aria-live="polite">
      <span class="toast__icon" aria-hidden="true">
        {#if toast.level === "ok"}{'✓'}
        {:else if toast.level === "error"}{'⚠'}
        {:else}{'ℹ'}{/if}
      </span>
      <span class="toast__msg">{toast.message}</span>
      <button class="toast__close" onclick={() => toast = null} aria-label="Dismiss">{'×'}</button>
    </div>
  {/if}
</div>

<style>
  /* ---------- design tokens ---------- */
  :global(:root), :global(:root[data-theme="dark"]) {
    --bg:           #14161b;
    --bg-elevated:  #181b21;
    --bg-card:      #1c1f26;
    --bg-input:     #14161b;
    --bg-hover:     #22262e;
    --bg-hover-strong: #2a2f38;
    --bg-active:    #1f2e26;
    --topbar-1:     #1d212a;
    --topbar-2:     #1a1d24;
    --border:       #2a2e36;
    --border-strong:#3a414c;
    --border-stronger:#4a515c;
    --text:         #e4e6eb;
    --text-muted:   #9aa1ad;
    --text-dim:     #6a7280;
    --text-soft:    #c6cad2;
    --accent:       #6fd99b;
    --accent-bg:    #1c2924;
    --accent-border:#2d4c3a;
    --accent-hover-bg:     #243a30;
    --accent-hover-border: #3d6650;
    --accent-glow:  rgba(111, 217, 155, 0.18);
    --warn:         #d99b6f;
    --warn-bg:      rgba(217,155,111,0.18);
    --warn-text:    #f4cd7a;
    --err:          #ef9b9b;
    --err-bg:       #2a1a1a;
    --err-border:   #5a3030;
    --overlay-bg:   rgba(0,0,0,0.55);
    --shadow-card:  0 1px 0 rgba(255,255,255,0.02) inset;
    --shadow-card-strong: 0 4px 24px rgba(0,0,0,0.25);
    --shadow-modal: 0 24px 80px rgba(0,0,0,0.6),
                    0 0 0 1px rgba(255,255,255,0.02) inset;
    --welcome-gradient: radial-gradient(ellipse 60% 80% at 50% 30%, rgba(111, 217, 155, 0.06) 0%, transparent 60%);
  }
  :global(:root[data-theme="light"]) {
    --bg:           #f3f5f8;
    --bg-elevated:  #ffffff;
    --bg-card:      #ffffff;
    --bg-input:     #ffffff;
    --bg-hover:     #e9ecf1;
    --bg-hover-strong: #dde2e9;
    --bg-active:    #e0f2e8;
    --topbar-1:     #ffffff;
    --topbar-2:     #f3f5f8;
    --border:       #d9dde2;
    --border-strong:#bfc4ca;
    --border-stronger:#a8aeb6;
    --text:         #1c1f26;
    --text-muted:   #5a6068;
    --text-dim:     #8a92a0;
    --text-soft:    #3a414c;
    --accent:       #2f8f54;
    --accent-bg:    #def0e5;
    --accent-border:#b3deb8;
    --accent-hover-bg:     #c5e8d2;
    --accent-hover-border: #90c8a3;
    --accent-glow:  rgba(47, 143, 84, 0.22);
    --warn:         #b86b30;
    --warn-bg:      rgba(184,107,48,0.14);
    --warn-text:    #8a4a18;
    --err:          #c8423f;
    --err-bg:       #fbe5e4;
    --err-border:   #f0b8b6;
    --overlay-bg:   rgba(15,17,22,0.35);
    --shadow-card:  0 1px 0 rgba(0,0,0,0.02) inset, 0 1px 2px rgba(15,17,22,0.04);
    --shadow-card-strong: 0 4px 16px rgba(15,17,22,0.08);
    --shadow-modal: 0 24px 60px rgba(15,17,22,0.18),
                    0 0 0 1px rgba(15,17,22,0.04) inset;
    --welcome-gradient: radial-gradient(ellipse 60% 80% at 50% 30%, rgba(47, 143, 84, 0.05) 0%, transparent 60%);
  }

  :global(body) { background: var(--bg); color: var(--text); margin: 0; overflow: hidden; }
  :global(html, body, #app) { height: 100vh; }

  /* Global button baseline. Components keep their own styles when they
     declare a class (.topbtn, .scalebtn, .themetoggle, .navitem, ...);
     anonymous <button> elements - the "+ add", "Save", "Cancel" kind
     scattered across modals and the patch editor - all share this look
     so the UI stops feeling patchworked. Modifier classes layer on top:
       .primary  -> accent
       .danger   -> err
   */
  :global(button) {
    font: inherit;
    cursor: pointer;
    background: var(--bg-hover);
    border: 1px solid var(--border-strong);
    color: var(--text-soft);
    padding: 0.38rem 0.8rem;
    border-radius: 5px;
    font-size: 0.82rem;
    font-weight: 500;
    transition: background 0.12s ease, border-color 0.12s ease, color 0.12s ease;
  }
  :global(button:hover:not(:disabled)) {
    background: var(--bg-hover-strong);
    border-color: var(--border-stronger);
    color: var(--text);
  }
  :global(button:disabled) { opacity: 0.4; cursor: not-allowed; }
  :global(button.primary) {
    background: var(--accent-bg);
    border-color: var(--accent-border);
    color: var(--accent);
    font-weight: 600;
  }
  :global(button.primary:hover:not(:disabled)) {
    background: var(--accent-hover-bg);
    border-color: var(--accent-hover-border);
    color: var(--accent);
  }
  :global(button.danger) {
    background: var(--err-bg);
    border-color: var(--err-border);
    color: var(--err);
  }
  :global(button.danger:hover:not(:disabled)) {
    background: var(--err-bg);
    border-color: var(--err);
    color: var(--err);
  }

  .app {
    display: flex; flex-direction: column; height: 100vh;
    font-family: "Inter", -apple-system, BlinkMacSystemFont, "Segoe UI", "Helvetica Neue", Arial, sans-serif;
    font-feature-settings: "ss01", "cv11";
    -webkit-font-smoothing: antialiased; -moz-osx-font-smoothing: grayscale;
    text-rendering: optimizeLegibility;
  }

  /* ---------- top bar ---------- */
  .topbar {
    display: flex; align-items: center; padding: 0.65rem 1.15rem;
    background: linear-gradient(180deg, var(--topbar-1) 0%, var(--topbar-2) 100%);
    border-bottom: 1px solid var(--border);
    gap: 0.85rem; flex-shrink: 0;
  }
  .brand {
    display: flex; align-items: center; gap: 0.55rem;
    user-select: none;
  }
  .brand .logo {
    width: 22px; height: 22px;
    color: var(--accent);
    flex-shrink: 0;
  }
  .brand .wordmark {
    font-weight: 700;
    font-size: 1.02rem;
    letter-spacing: 0.02em;
    color: var(--text);
  }
  .connpill {
    display: inline-flex; align-items: center; gap: 0.4rem;
    background: var(--bg-hover); border: 1px solid var(--border-strong);
    color: var(--text-muted);
    padding: 0.22rem 0.55rem 0.22rem 0.5rem;
    border-radius: 999px;
    font-size: 0.72rem; font-weight: 500;
    letter-spacing: 0.02em;
    margin-left: 0.25rem;
  }
  .connpill .dot {
    width: 7px; height: 7px; border-radius: 50%;
    background: var(--text-dim);
    box-shadow: 0 0 0 0 transparent;
    transition: all 0.2s ease;
  }
  .connpill.on { color: var(--accent); border-color: var(--accent-border); background: var(--accent-bg); }
  .connpill.on .dot {
    background: var(--accent);
    box-shadow: 0 0 0 3px var(--accent-glow);
  }
  .connpill.busy { color: var(--warn); border-color: var(--warn); background: var(--warn-bg); }
  .connpill.busy .dot {
    background: var(--warn);
    animation: connpill-pulse 1.1s ease-in-out infinite;
  }
  @keyframes connpill-pulse {
    0%, 100% { opacity: 0.45; transform: scale(0.85); box-shadow: 0 0 0 0 transparent; }
    50%      { opacity: 1;   transform: scale(1.15);  box-shadow: 0 0 0 4px rgba(217,155,111,0.22); }
  }
  .grow { flex: 1; }
  .topbtn {
    background: var(--bg-hover); border: 1px solid var(--border-strong); color: var(--text-soft);
    padding: 0.38rem 0.75rem; border-radius: 5px; cursor: pointer;
    font-size: 0.8rem; font-weight: 500;
    transition: all 0.15s ease;
  }
  .topbtn:hover { background: var(--bg-hover-strong); border-color: var(--border-stronger); }
  .topbtn.primary {
    background: var(--accent-bg); border-color: var(--accent-border); color: var(--accent); font-weight: 600;
  }
  .topbtn.primary:hover { background: var(--accent-hover-bg); border-color: var(--accent-hover-border); }
  .topbtn.ghost {
    background: transparent; border-color: var(--err-border); color: var(--err);
  }
  .topbtn.ghost:hover { background: var(--err-bg); }
  .fwstatus {
    font-size: 0.74rem;
    color: var(--accent);
    background: var(--accent-bg);
    padding: 0.28rem 0.55rem;
    border-radius: 4px;
    align-self: center;
    font-variant-numeric: tabular-nums;
  }
  .fwstatus.muted { color: var(--text-muted); background: transparent; }

  .themetoggle {
    display: inline-flex; align-items: center; justify-content: center;
    width: 30px; height: 30px;
    background: transparent; border: 1px solid var(--border);
    color: var(--text-muted); border-radius: 5px; cursor: pointer;
    padding: 0; margin-left: 0.25rem;
    transition: background 0.15s ease, color 0.15s ease, border-color 0.15s ease;
  }
  .themetoggle:hover { background: var(--bg-hover); color: var(--text); border-color: var(--border-strong); }
  .themetoggle svg { width: 16px; height: 16px; }

  .uiscale {
    display: inline-flex; align-items: center;
    margin-left: 0.25rem;
    border: 1px solid var(--border); border-radius: 5px;
    overflow: hidden;
    background: transparent;
  }
  .uiscale .scalebtn {
    background: transparent; border: 0; color: var(--text-muted);
    font-size: 0.74rem; font-weight: 500;
    height: 30px; min-width: 30px; padding: 0 0.45rem;
    cursor: pointer;
    transition: background 0.12s ease, color 0.12s ease;
    font-variant-numeric: tabular-nums;
  }
  .uiscale .scalebtn:hover { background: var(--bg-hover); color: var(--text); }
  .uiscale .scalebtn + .scalebtn { border-left: 1px solid var(--border); }

  /* ---------- welcome ---------- */
  .welcome {
    flex: 1; display: flex; align-items: center; justify-content: center;
    padding: 2rem;
    background: var(--welcome-gradient), var(--bg);
  }
  .card {
    background: var(--bg-card); border: 1px solid var(--border); border-radius: 10px;
    padding: 2.25rem 2.5rem 1.75rem; max-width: 480px; width: 100%;
    text-align: center;
    box-shadow: var(--shadow-card-strong);
  }
  .card h1 {
    margin: 0 0 0.5rem; font-size: 1.45rem; font-weight: 600;
    color: var(--text); letter-spacing: -0.01em;
  }
  .card .hint { color: var(--text-muted); margin: 0 0 1.75rem; font-size: 0.9rem; line-height: 1.5; }
  .card .hint.small { font-size: 0.78rem; margin-top: 1rem; line-height: 1.4; }
  .big {
    background: var(--accent-bg); color: var(--accent); border: 1px solid var(--accent-border);
    font-weight: 600; font-size: 1rem; padding: 0.85rem 2.5rem;
    border-radius: 7px; cursor: pointer; min-width: 240px;
    transition: all 0.15s ease;
  }
  .big:hover:not(:disabled) { background: var(--accent-hover-bg); border-color: var(--accent-hover-border); }
  .big:disabled { opacity: 0.5; cursor: not-allowed; }
  .divider {
    border: 0; height: 1px;
    background: linear-gradient(90deg, transparent 0%, var(--border) 50%, transparent 100%);
    margin: 1.5rem 0 1rem;
  }
  .install-link {
    color: var(--text-dim); font-size: 0.78rem; margin: 0; line-height: 1.5;
  }
  .linkbtn {
    background: none; border: none; color: var(--text-muted); cursor: pointer;
    font-size: inherit; font-family: inherit; padding: 0;
    text-decoration: underline; text-underline-offset: 2px;
    transition: color 0.15s ease;
  }
  .linkbtn:hover:not(:disabled) { color: var(--accent); }
  .linkbtn:disabled { opacity: 0.4; cursor: not-allowed; text-decoration: none; }
  .adv { display: inline-flex; align-items: center; gap: 0.3rem; color: var(--text-muted); font-size: 0.8rem; margin-top: 0.6rem; cursor: pointer; }
  .manual { display: flex; gap: 0.4rem; margin-top: 0.5rem; justify-content: center; }
  details.advanced {
    margin-top: 1.5rem; text-align: left;
    border-top: 1px solid var(--border); padding-top: 0.6rem;
  }
  details.advanced > summary {
    cursor: pointer; color: var(--text-dim); font-size: 0.75rem;
    text-transform: uppercase; letter-spacing: 0.06em;
    padding: 0.2rem 0; list-style: none;
  }
  details.advanced > summary::-webkit-details-marker { display: none; }
  details.advanced > summary::before { content: "▸ "; font-size: 0.7rem; }
  details.advanced[open] > summary::before { content: "▾ "; }
  details.advanced > summary:hover { color: var(--text-muted); }
  .manual button, .manual select { background: var(--bg-hover); border: 1px solid var(--border-strong); color: var(--text-soft); padding: 0.3rem 0.5rem; border-radius: 3px; font-size: 0.8rem; }

  /* ---------- main shell ---------- */
  .shell { flex: 1; display: flex; min-height: 0; }
  .sidebar {
    width: 190px; flex-shrink: 0;
    background: var(--bg-elevated); border-right: 1px solid var(--border);
    display: flex; flex-direction: column; padding: 0.55rem 0.45rem;
    gap: 0.12rem;
  }
  .navitem {
    position: relative;
    display: flex; align-items: center; gap: 0.65rem; width: 100%;
    background: transparent; border: none; color: var(--text-muted);
    padding: 0.55rem 0.75rem 0.55rem 0.85rem; border-radius: 5px;
    text-align: left; font-size: 0.88rem; cursor: pointer;
    transition: background 0.12s ease, color 0.12s ease;
  }
  .navitem:hover { background: var(--bg-hover); color: var(--text); }
  .navitem.active {
    background: var(--accent-bg); color: var(--accent); font-weight: 600;
  }
  .navitem.active::before {
    content: "";
    position: absolute; left: 0; top: 18%; bottom: 18%;
    width: 3px; border-radius: 0 2px 2px 0;
    background: var(--accent);
  }
  .navitem .icon { width: 1.1rem; text-align: center; font-size: 0.95rem; opacity: 0.7; }
  .navitem.active .icon { opacity: 1; }
  .navitem .lbl { flex: 1; }
  .navitem.ghost {
    color: var(--text-dim); margin-top: 0.65rem;
    border-top: 1px solid var(--border); padding-top: 0.8rem; border-radius: 0;
  }
  .navitem.ghost:hover { color: var(--err); background: transparent; }
  .navitem .badge-dot {
    background: var(--warn); color: var(--bg-card); font-size: 0.65rem; font-weight: 600;
    padding: 0.05rem 0.4rem; border-radius: 999px; min-width: 1rem; text-align: center;
  }
  .pulse-dot {
    width: 8px; height: 8px; border-radius: 50%; background: var(--warn-text);
    animation: pulse 1.5s ease-in-out infinite;
  }
  @keyframes pulse { 50% { opacity: 0.35; } }
  /* Section header separating the sidebar groups (Build / Device / System). */
  .navgroup-label {
    font-size: 0.66rem; font-weight: 600; letter-spacing: 0.07em;
    text-transform: uppercase; color: var(--text-dim);
    padding: 0.2rem 0.85rem; margin-top: 0.7rem;
  }
  .navgroup-label:first-child { margin-top: 0.2rem; }

  /* ---------- content area ---------- */
  .content {
    flex: 1; min-width: 0;
    overflow: auto; padding: 1rem 1.25rem;
    background: var(--bg);
  }
  .pageHead {
    display: flex; align-items: center; gap: 0.75rem;
    margin: 0 0 1rem;
  }
  .pageHead h2 {
    margin: 0; font-size: 1.05rem; font-weight: 600; color: var(--text); flex: 1;
    display: flex; align-items: center; gap: 0.55rem;
  }
  .pageHead h2 .navgroup {
    display: inline-flex; gap: 0.2rem;
    margin-right: 0.2rem;
  }
  .pageHead h2 .navarrow {
    background: transparent;
    border: 1px solid var(--border);
    color: var(--text-muted);
    font-size: 1.1rem; line-height: 1;
    padding: 0.1rem 0.5rem 0.15rem;
    border-radius: 4px;
    font-weight: 600;
  }
  .pageHead h2 .navarrow:hover:not(:disabled) {
    background: var(--bg-hover);
    color: var(--text);
    border-color: var(--border-strong);
  }
  .pageHead h2 .navarrow:disabled {
    opacity: 0.3;
    cursor: default;
  }
  /* .pageHead buttons inherit the global :global(button) baseline. */

  /* Editor Switches / Quick setup tab strip. */
  .editor-tabs {
    display: flex; gap: 0.25rem;
    border-bottom: 1px solid var(--border);
    margin: 0 0 1rem;
  }
  .etab {
    background: transparent; border: none;
    color: var(--text-muted); font-size: 0.88rem; font-weight: 500;
    padding: 0.5rem 0.9rem; cursor: pointer;
    border-bottom: 2px solid transparent; margin-bottom: -1px;
    transition: color 0.12s ease, border-color 0.12s ease;
  }
  .etab:hover { color: var(--text); }
  .etab.active {
    color: var(--accent); font-weight: 600;
    border-bottom-color: var(--accent);
  }

  /* Patches grid tile-level styles live in components/PatchesGrid.svelte.
     Only the dirty-tag indicator (reused in the editor pageHead) stays
     here as a shared visual token. */
  .dirty-tag { color: var(--warn); }

  .empty-state {
    display: flex; flex-direction: column; align-items: center;
    text-align: center; padding: 3.5rem 1rem;
    max-width: 460px; margin: 1.5rem auto 0;
  }
  .empty-state--inline { padding: 1.6rem 1rem; margin: 0 auto; }
  .empty-state__title {
    font-size: 1.05rem; font-weight: 600; color: var(--text);
    margin-bottom: 0.45rem;
    letter-spacing: 0.005em;
  }
  .empty-state__hint {
    margin: 0; color: var(--text-muted); font-size: 0.85rem;
    line-height: 1.55;
  }

  .toast {
    position: fixed; top: 3.4rem; right: 1rem;
    z-index: 200;
    display: flex; align-items: center; gap: 0.55rem;
    min-width: 240px; max-width: 420px;
    padding: 0.7rem 0.85rem;
    background: var(--bg-card); border: 1px solid var(--border);
    border-radius: 6px;
    box-shadow: var(--shadow-modal);
    font-size: 0.85rem; color: var(--text); line-height: 1.4;
    animation: toast-in 0.2s cubic-bezier(0.16, 1, 0.3, 1);
  }
  .toast--ok    { border-left: 3px solid var(--accent); }
  .toast--error { border-left: 3px solid var(--err); }
  .toast--info  { border-left: 3px solid var(--text-muted); }
  .toast__icon {
    flex-shrink: 0;
    width: 1.2rem; height: 1.2rem;
    display: inline-flex; align-items: center; justify-content: center;
    font-weight: 600; font-size: 0.95rem;
  }
  .toast--ok    .toast__icon { color: var(--accent); }
  .toast--error .toast__icon { color: var(--err); }
  .toast--info  .toast__icon { color: var(--text-muted); }
  .toast__msg { flex: 1; word-break: break-word; }
  .toast__close {
    background: transparent; border: 0; cursor: pointer;
    color: var(--text-dim); font-size: 1.1rem; line-height: 1;
    padding: 0 0.15rem;
  }
  .toast__close:hover { color: var(--text); }
  @keyframes toast-in {
    from { opacity: 0; transform: translateY(-8px); }
    to   { opacity: 1; transform: translateY(0); }
  }

  /* Non-blocking banner shown when only the core-only fallback manifest is
     active (the plugin manifest never arrived). Sits under the topbar, above
     the shell, so it doesn't take over the page like .manifest-error did. */
  .fallback-banner {
    display: flex; align-items: center; gap: 0.55rem;
    padding: 0.5rem 1.15rem;
    background: var(--warn-bg); border-bottom: 1px solid var(--warn);
    color: var(--warn-text); font-size: 0.82rem;
    flex-shrink: 0;
  }
  .fallback-banner__icon { font-weight: 600; }
  .fallback-banner__msg { flex: 1; }
  .fallback-banner__retry {
    background: transparent; border: 1px solid var(--warn);
    color: var(--warn-text); padding: 0.2rem 0.6rem; border-radius: 4px;
    font-size: 0.76rem;
  }
  .fallback-banner__retry:hover { background: rgba(217,155,111,0.12); }

  .manifest-error {
    max-width: 640px;
    background: var(--bg-card); border: 1px solid var(--err-border);
    border-left: 3px solid var(--err);
    border-radius: 4px; padding: 0.85rem 1rem;
  }
  .manifest-error p { margin: 0 0 0.55rem; line-height: 1.5; color: var(--text); }
  .manifest-error p.muted { color: var(--text-muted); font-size: 0.85rem; }
  .manifest-error .toolbar { margin-top: 0.4rem; }

  .toolbar { display: flex; gap: 0.7rem; flex-wrap: wrap; margin-top: 1rem; }

  /* ---------- log ---------- */
  .logbox {
    background: var(--bg-card); border: 1px solid var(--border); border-radius: 4px;
    padding: 0.5rem; font-family: ui-monospace, Consolas, monospace;
    font-size: 0.75rem; line-height: 1.45;
    max-height: calc(100vh - 200px); overflow: auto;
  }
  .logline { display: flex; gap: 0.5rem; padding: 0.05rem 0; }
  .ts { color: var(--text-dim); flex-shrink: 0; }
  .msg { color: var(--text-soft); word-break: break-all; }

  .muted { color: #9aa1ad; }
  .err { color: #ef9b9b; font-size: 0.85rem; }
</style>
