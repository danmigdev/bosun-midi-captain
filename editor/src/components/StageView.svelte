<script lang="ts">
  import { onMount, onDestroy, tick } from "svelte";
  import {
    cmd,
    onFirmwareMessage,
    summarizeMessage,
    type Binding,
    type FirmwareMessage,
    type Manifest,
    type PatchSummary,
  } from "../lib/protocol";
  import { DEFAULT_LAYOUT } from "../lib/pedal-layout";
  import { ledColorFor } from "../lib/led-color";

  type Props = {
    deviceInfo: { fw: string; device: string; bank: number; slot: number; profile?: string } | null;
    manifest: Manifest | null;
    device: Record<string, unknown> | null;
    connected: boolean;
    patches: PatchSummary[];
    onExit: () => void;
  };
  let { deviceInfo, manifest, device, connected, patches, onExit }: Props = $props();

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
  let unsubFw: (() => void) | null = null;

  // Full patch (with bindings) fetched on mount and when deviceInfo changes
  let fullPatch = $state<{ name?: string; bindings?: Binding[] } | null>(null);
  let patchName = $derived(fullPatch?.name ?? (deviceInfo ? `${deviceInfo.bank}/${deviceInfo.slot}` : "-"));
  let bindings = $derived(fullPatch?.bindings ?? []);

  // Live latched state per switch (from binding_fired toggle_on / toggle_off)
  let latched = $state<Record<string, boolean>>({});

  // 2-row x 5-column pedal layout
  let rows = $derived(DEFAULT_LAYOUT);

  // --- derived ---
  let rigName = $derived(
    (context.kemper_rig_name as string) || patchName || "-"
  );
  let bpm = $derived(context.kemper_bpm as number | undefined);
  let tunerOn = $derived(
    context.kemper_tuner === "on" || context.tuner === "on"
  );
  let tunerNote = $derived(context.kemper_tuner_note as string | undefined);
  let tunerDeviance = $derived(context.kemper_tuner_deviance as number | undefined);

  function displaySwitch(sw: string): string {
    if (sw === "up") return "UP";
    if (sw === "down") return "DOWN";
    return sw.toUpperCase();
  }

  function bindingForSwitch(sw: string): Binding | undefined {
    return bindings.find((b: Binding) => b.switch === sw);
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

  function marquee(el: HTMLElement) {
    const check = () => {
      const overflow = el.scrollWidth > el.clientWidth + 2;
      if (overflow) {
        const dx = -(el.scrollWidth - el.clientWidth);
        el.style.setProperty("--marquee-dx", `${dx}px`);
        el.classList.add("stage__switch-label--scroll");
      } else {
        el.classList.remove("stage__switch-label--scroll");
      }
    };
    check();
    const obs = new ResizeObserver(check);
    obs.observe(el);
    return { destroy() { obs.disconnect(); } };
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
    const keys = Object.keys(b.actions ?? {});
    const action = keys.length > 0 ? b.actions?.[keys[0]] : undefined;
    const msg = action?.messages?.[0];
    if (!msg || (msg as Record<string,unknown>).type !== "kemper_effect_toggle") return null;
    return ((msg as Record<string,unknown>).slot as string) ?? null;
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

  async function fetchPatch() {
    if (!deviceInfo) return;
    try {
      await cmd.getPatch(deviceInfo.bank, deviceInfo.slot);
    } catch { /* ignore */ }
  }

  // --- lifecycle ---
  onMount(() => { _start(); return () => _stop(); });
  onDestroy(() => { _stop(); });

  $effect(() => {
    if (connected && deviceInfo) fetchPatch();
  });

  async function _start() {
    if (!connected) return;
    pollContext(); fetchPatch();
    try {
      const unsub = await onFirmwareMessage((msg: FirmwareMessage) => {
        if (msg.type === "CONTEXT" && msg.context) {
          context = msg.context as Record<string, unknown>;
        } else if (msg.type === "PATCH" && deviceInfo
            && msg.bank === deviceInfo.bank && msg.slot === deviceInfo.slot) {
          const p = (msg as unknown as { patch: { name?: string; bindings?: Binding[] } }).patch;
          fullPatch = p;
          // Reset latched state: all switches start OFF on patch load.
          // binding_fired events will update individual switches as they fire.
          const init: Record<string, boolean> = {};
          for (const b of p.bindings ?? []) {
            if (b.switch && (b.mode === "latched" || b.mode === "momentary")) {
              init[b.switch] = false;
            }
          }
          latched = init;
        } else if (msg.type === "EVENT" && (msg as Record<string,unknown>).event === "binding_fired") {
          const ev = msg as Record<string,unknown>;
          const sw = ev.switch as string;
          const action = ev.action as string;
          if (sw && (action === "toggle_on" || action === "toggle_off" || action === "press")) {
            latched = { ...latched, [sw]: action !== "toggle_off" };
          }
        }
      });
      unsubFw = unsub;
    } catch { /* ignore */ }
  }

  function _stop() {
    if (unsubFw) { unsubFw(); unsubFw = null; }
  }
</script>

<div class="stage">
  <!-- exit button: transparent, top-right, appears on tap -->
  <button class="stage__exit" onclick={onExit} aria-label="Exit Stage">✕</button>

  <!-- header: rig name + bank/rig + BPM + tuner -->
  <div class="stage__header">
    <div class="stage__rig-name">{rigName}</div>
    <div class="stage__meta">
      {#if deviceInfo}
        <span class="stage__bank">BANK {deviceInfo.bank} · RIG {deviceInfo.slot}</span>
      {/if}
      {#if bpm}
        <span class="stage__bpm">{bpm} <small>BPM</small></span>
      {/if}
      {#if tunerOn}
        <span class="stage__tuner">
          {tunerNote ?? "--"} {tunerDeviance != null ? (tunerDeviance < 8000 ? "♭" : tunerDeviance > 8400 ? "♯" : "●") : ""}
        </span>
      {/if}
    </div>
  </div>

  <!-- 2-row x 5-column footswitch grid -->
  <div class="stage__pedal">
    {#each rows as row}
      <div class="stage__pedal-row">
        {#each row as sw}
          {@const b = bindingForSwitch(sw)}
          {@const navPatch = b ? null : navPatchFor(sw)}
          {@const navSlot = navPatch ? navSlotFor(sw) : null}
          {@const active = b ? isLatchedOn(sw) : (navSlot !== null && navSlot === deviceInfo?.slot)}
          <!-- Border follows the switch's assigned LED colour: bright +
               glowing background when latched on, dim when off. Unbound
               switches get no coloured border at all. Preset-nav switches
               (device.preset_navigation, not a patch binding) use the
               configured bank colour the same way the firmware's physical
               LEDs do - see navPatchFor above. A switch mapped to a slot
               with no patch in THIS bank falls through to the unbound "-"
               look, same as the physical LED staying off. -->
          {@const offColor = b ? ledColorFor(b, false) : (navSlot !== null ? (presetNav?.bank_colors?.[String(deviceInfo?.bank)] ?? "#888888") : null)}
          {@const onColor = b ? ledColorFor(b, true) : (navSlot !== null ? (presetNav?.bank_colors?.[String(deviceInfo?.bank)] ?? "#888888") : null)}
          <div class="stage__switch"
               class:stage__switch--bound={!!b || navSlot !== null}
               class:stage__switch--active={active}
               style={onColor ? (active
                   ? `border-color: ${onColor}; background: ${onColor}66; box-shadow: 0 0 16px ${onColor}50`
                   : `border-color: ${offColor}`) : ''}>
            <span class="stage__switch-label" use:marquee>{b ? (effectLabel(b) || displaySwitch(sw)) : (navPatch ? navPatch.name : "-")}</span>
            <span class="stage__switch-id">{displaySwitch(sw)}</span>
          </div>
        {/each}
      </div>
    {/each}
  </div>
</div>

<style>
  .stage {
    display: flex; flex-direction: column;
    height: 100%; height: 100dvh;
    padding: clamp(0.3rem, 1vw, 1rem);
    gap: clamp(0.3rem, 1vw, 0.8rem);
    font-family: "Inter", -apple-system, sans-serif;
    font-weight: 600;
    color: var(--text);
    background: var(--bg);
    user-select: none; -webkit-user-select: none;
    position: relative;
  }

  .stage__exit {
    position: absolute; top: clamp(0.3rem, 1vw, 0.6rem); right: clamp(0.3rem, 1vw, 0.6rem);
    z-index: 10;
    /* Circle inverted vs the active theme: near-white on dark,
       near-black on light -- always stands out against the stage. */
    background: var(--text); border: none;
    color: var(--bg); font-size: clamp(1rem, 3vw, 1.8rem);
    width: clamp(2rem, 5vw, 3rem); height: clamp(2rem, 5vw, 3rem);
    border-radius: 50%;
    display: flex; align-items: center; justify-content: center;
    cursor: pointer; opacity: 0; transition: opacity 0.3s;
    -webkit-tap-highlight-color: transparent;
  }
  .stage:hover .stage__exit, .stage:active .stage__exit { opacity: 0.9; }
  .stage__exit:active { opacity: 0.6; }

  /* ----- header ----- */
  .stage__header {
    flex: 0 0 auto;
    display: flex; align-items: baseline; justify-content: center;
    gap: clamp(0.6rem, 2vw, 1.5rem);
    flex-wrap: wrap;
    padding: 0 clamp(3rem, 8vw, 5rem); /* avoid overlap with exit ✕ on both sides */
  }
  .stage__bank {
    font-size: clamp(3.6rem, 14vw, 8rem);
    color: #ffffff;
    text-transform: uppercase;
    letter-spacing: 0.04em;
    line-height: 1.1;
    max-width: 100%;
    overflow: hidden; text-overflow: ellipsis;
    white-space: nowrap;
  }
  .stage__rig-name {
    font-size: clamp(3.6rem, 14vw, 8rem);
    line-height: 1.1; letter-spacing: -0.02em;
    color: #ffffff;
  }
  .stage__meta {
    display: flex; align-items: baseline; gap: clamp(0.5rem, 2vw, 1.2rem);
    flex-wrap: wrap;
  }
  .stage__bank {
    font-size: clamp(3.6rem, 14vw, 8rem);
    color: #ffffff;
    text-transform: uppercase;
    letter-spacing: 0.04em;
    line-height: 1.1;
  }
  .stage__bpm {
    font-size: clamp(1.5rem, 6vw, 3rem);
    color: var(--text);
  }
  .stage__bpm small { font-size: 0.55em; color: var(--text-muted); }
  .stage__tuner {
    font-size: clamp(1.5rem, 6vw, 3rem);
    color: #4ade80;
  }

  /* ----- 2x5 pedal grid ----- */
  .stage__pedal {
    flex: 1 1 auto;
    display: flex; flex-direction: column;
    gap: clamp(3px, 0.8vw, 8px);
  }
  .stage__pedal-row {
    flex: 1 1 0;
    display: flex;
    gap: clamp(3px, 0.8vw, 8px);
  }
  .stage__switch {
    flex: 1 1 0;
    display: flex; flex-direction: column;
    align-items: center; justify-content: center;
    gap: clamp(1px, 0.2vw, 4px);
    padding: clamp(2px, 0.3vw, 6px);
    border-radius: clamp(4px, 0.6vw, 10px);
    background: var(--bg-card);
    /* Unbound switches: neutral border, no colour. Bound switches get
       their LED colour via the inline style (see the grid markup). */
    border: 4px solid transparent;
    min-width: 0;
    transition: border-color 0.2s, background 0.2s;
  }
  .stage__switch--bound {
    border-color: rgba(255, 255, 255, 0.12);
  }

  .stage__switch-label {
    font-size: clamp(1.5rem, 6.6vw, 3rem);
    color: #ffffff;
    text-align: center;
    white-space: nowrap; overflow: hidden; text-overflow: ellipsis;
    max-width: 100%; line-height: 1.15;
    font-weight: 700;
    position: relative;
  }
  .stage__switch-label--scroll {
    text-overflow: clip;
    animation: stage-marquee 5s ease-in-out infinite;
  }
  @keyframes stage-marquee {
    0%, 15%   { transform: translateX(0); }
    40%, 60%  { transform: translateX(var(--marquee-dx, -50%)); }
    85%, 100% { transform: translateX(0); }
  }
  .stage__switch-id {
    font-size: clamp(1.5rem, 6.6vw, 3rem);
    color: var(--text-dim);
    font-weight: 600;
  }

  /* ===== LANDSCAPE: immersive full-screen ===== */
  @media (orientation: landscape) {
    .stage {
      padding: clamp(2px, 1vh, 8px);
      gap: clamp(2px, 0.8vh, 6px);
    }
    .stage__header {
      flex: 0 0 auto;
      flex-direction: row;
      justify-content: space-between;
      align-items: baseline;
      padding: 0 clamp(0.5rem, 2vh, 1rem);
    }
    .stage__rig-name {
      font-size: clamp(2.6rem, 12vh, 7rem);
      line-height: 1;
    }
    .stage__meta {
      font-size: clamp(1rem, 4vh, 2.5rem);
      gap: clamp(0.5rem, 2vh, 1.5rem);
    }
    .stage__bank { font-size: clamp(2.6rem, 12vh, 7rem); color: #ffffff; text-transform: uppercase; letter-spacing: 0.04em; line-height: 1.1; }
    .stage__bpm  { font-size: clamp(1.2rem, 5vh, 3rem); }
    .stage__tuner { font-size: clamp(1.2rem, 5vh, 3rem); }

    .stage__pedal {
      flex: 1 1 0;
      gap: clamp(2px, 1.2vh, 8px);
    }
    .stage__pedal-row {
      gap: clamp(2px, 1.2vh, 8px);
    }
    .stage__switch {
      border-radius: clamp(4px, 1.2vh, 12px);
      border: 4px solid transparent;
      background: var(--bg-card);
      padding: clamp(1px, 0.5vh, 4px);
      gap: clamp(0px, 0.3vh, 3px);
    }
    .stage__switch--bound {
      border-color: rgba(255, 255, 255, 0.12);
    }
    .stage__switch-label {
      font-size: clamp(1.8rem, 9vh, 4.5rem);
      color: #ffffff;
      line-height: 1.1;
    }
    .stage__switch-id {
      font-size: clamp(1.8rem, 9vh, 4.5rem);
    }
  }
</style>
