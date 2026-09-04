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
  import {
    readSavedStageTheme, saveStageTheme, stageThemeToCssVars, type StageTheme,
  } from "../lib/stage-theme";
  import StageThemeEditor from "./StageThemeEditor.svelte";

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

  // --- stage theme (per-section font/color/size, see stage-theme.ts) ---
  let stageTheme = $state<StageTheme>(readSavedStageTheme());
  let showThemeEditor = $state(false);
  let stageThemeVars = $derived(stageThemeToCssVars(stageTheme));

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
    const keys = Object.keys(b.actions ?? {});
    const action = keys.length > 0 ? b.actions?.[keys[0]] : undefined;
    const msg = action?.messages?.[0];
    if (!msg || (msg as Record<string,unknown>).type !== "kemper_effect_toggle") return null;
    return ((msg as Record<string,unknown>).slot as string) ?? null;
  }

  /** Active state + LED colour for the ambient floor glow behind the grid
   *  (purely decorative echo of a switch's own LED colour - never a
   *  substitute for the border/background colour logic in the markup,
   *  which stays the source of truth for what's actually engaged). */
  function switchVisual(sw: string): { active: boolean; color: string | null } {
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

  async function fetchPatch() {
    if (!deviceInfo) return;
    try {
      await cmd.getPatch(deviceInfo.bank, deviceInfo.slot);
    } catch { /* ignore */ }
  }

  // --- lifecycle ---
  onMount(() => () => _stop());
  onDestroy(() => { _stop(); });

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
      fetchPatch();
    } else if (!connected) {
      _linkUp = false;
    }
  });

  // Re-fetch the patch whenever it (bank-step nav) or the connection
  // changes. Reads both every run so Svelte tracks them as dependencies.
  $effect(() => {
    if (connected && deviceInfo) fetchPatch();
  });

  let _subscribing = false;
  async function _subscribe() {
    if (unsubFw || _subscribing) return;
    _subscribing = true;
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
    } catch {
      /* ignore - the link-transition effect will retry */
    } finally {
      _subscribing = false;
    }
  }

  function _stop() {
    if (unsubFw) { unsubFw(); unsubFw = null; }
  }
</script>

<div class="stage" style={stageThemeVars}>
  <!-- controls: transparent, top-right, appear on tap -->
  <div class="stage__controls">
    <button class="stage__icon-btn" onclick={() => (showThemeEditor = !showThemeEditor)} aria-label="Stage appearance">⚙</button>
    <button class="stage__icon-btn" onclick={onExit} aria-label="Exit Stage">✕</button>
  </div>

  {#if showThemeEditor}
    <StageThemeEditor theme={stageTheme} onchange={handleThemeChange} onclose={() => (showThemeEditor = false)} />
  {/if}

  <!-- header: rig name + bank/rig + BPM + tuner -->
  <div class="stage__header">
    <div class="stage__rig-name" use:marquee={rigName}><span class="stage__marquee-track">{rigName}</span></div>
    <div class="stage__meta">
      {#if deviceInfo}
        <span class="stage__bank" use:marquee={`${deviceInfo.bank}/${deviceInfo.slot}`}><span class="stage__marquee-track">BANK {deviceInfo.bank} · RIG {deviceInfo.slot}</span></span>
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
          {@const label = b ? (effectLabel(b) || displaySwitch(sw)) : (navPatch ? navPatch.name : "-")}
          <div class="stage__switch"
               class:stage__switch--bound={!!b || navSlot !== null}
               class:stage__switch--active={active}
               style={onColor ? (active
                   ? `border-color: ${onColor}; background: ${onColor}66; box-shadow: 0 0 16px ${onColor}50`
                   : `border-color: ${offColor}`) : ''}>
            <span class="stage__switch-label" use:marquee={label}><span class="stage__marquee-track">{label}</span></span>
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
    font-family: var(--stage-font, "Inter", -apple-system, sans-serif);
    font-weight: 600;
    color: var(--text);
    background: var(--bg);
    user-select: none; -webkit-user-select: none;
    position: relative;
  }

  .stage__controls {
    position: absolute; top: clamp(0.3rem, 1vw, 0.6rem); right: clamp(0.3rem, 1vw, 0.6rem);
    z-index: 10;
    display: flex; gap: clamp(0.3rem, 1vw, 0.6rem);
    opacity: 0; transition: opacity 0.3s;
  }
  .stage:hover .stage__controls, .stage:active .stage__controls { opacity: 0.9; }
  .stage__icon-btn {
    /* Circle inverted vs the active theme: near-white on dark,
       near-black on light -- always stands out against the stage. */
    background: var(--text); border: none;
    color: var(--bg); font-size: clamp(1rem, 3vw, 1.8rem);
    width: clamp(2rem, 5vw, 3rem); height: clamp(2rem, 5vw, 3rem);
    border-radius: 50%;
    display: flex; align-items: center; justify-content: center;
    cursor: pointer;
    -webkit-tap-highlight-color: transparent;
  }
  .stage__icon-btn:active { opacity: 0.6; }

  /* ----- header ----- */
  .stage__header {
    flex: 0 0 auto;
    display: flex; align-items: baseline; justify-content: center;
    gap: clamp(0.6rem, 2vw, 1.5rem);
    flex-wrap: wrap;
    padding: 0 clamp(3rem, 8vw, 5rem); /* avoid overlap with exit ✕ on both sides */
  }
  /* Clipping frames for the marquee effect: fixed-size, never move. The
     ".stage__marquee-track" child inside each one holds the actual text and
     is what the `marquee` action translates - sliding the frame itself
     would move its own clip boundary with it and reveal nothing. */
  .stage__rig-name, .stage__bank {
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
  .stage__bank {
    font-size: calc(clamp(3.6rem, 14vw, 8rem) * var(--stage-bank-scale, 1));
    color: var(--stage-bank-color, #ffffff);
    font-family: var(--stage-bank-font, var(--stage-font, "Inter", -apple-system, sans-serif));
    text-transform: uppercase;
    letter-spacing: 0.04em;
    line-height: 1.1;
  }
  .stage__rig-name {
    font-size: calc(clamp(3.6rem, 14vw, 8rem) * var(--stage-rig-name-scale, 1));
    line-height: 1.1; letter-spacing: -0.02em;
    color: var(--stage-rig-name-color, #ffffff);
    font-family: var(--stage-rig-name-font, var(--stage-font, "Inter", -apple-system, sans-serif));
  }
  .stage__meta {
    display: flex; align-items: baseline; gap: clamp(0.5rem, 2vw, 1.2rem);
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
    flex: 1 1 auto;
    min-width: 0;
  }
  .stage__bpm {
    font-size: calc(clamp(1.5rem, 6vw, 3rem) * var(--stage-bpm-scale, 1));
    color: var(--stage-bpm-color, var(--text));
    font-family: var(--stage-bpm-font, var(--stage-font, "Inter", -apple-system, sans-serif));
    margin-left: auto; /* docks BPM (and tuner after it) to the right edge */
  }
  .stage__bpm small { font-size: 0.55em; color: var(--text-muted); }
  .stage__tuner {
    font-size: calc(clamp(1.5rem, 6vw, 3rem) * var(--stage-tuner-scale, 1));
    color: var(--stage-tuner-color, #4ade80);
    font-family: var(--stage-tuner-font, var(--stage-font, "Inter", -apple-system, sans-serif));
  }

  /* ----- 2x5 pedal grid ----- */
  .stage__pedal {
    flex: 1 1 auto;
    display: flex; flex-direction: column;
    gap: clamp(3px, 0.8vw, 8px);
    position: relative; z-index: 0; /* stacking context: keeps .stage__glow's
      negative z-index scoped here, above .stage's own flat background but
      below the switches painted on top of it */
  }
  /* Ambient glow layer: one blurred, oversized spot per grid cell, aligned
     with the real switches above it via the identical flex geometry. Colour
     and visibility are set inline per spot (see switchVisual in <script>);
     this layer never carries functional meaning on its own. */
  .stage__glow {
    position: absolute; inset: 0; z-index: -1;
    display: flex; flex-direction: column;
    gap: clamp(3px, 0.8vw, 8px);
    pointer-events: none;
    filter: blur(clamp(20px, 4vw, 48px));
  }
  .stage__glow-row {
    flex: 1 1 0;
    display: flex;
    gap: clamp(3px, 0.8vw, 8px);
  }
  .stage__glow-spot {
    flex: 1 1 0;
    border-radius: 50%;
    transform: scale(1.6);
    opacity: 0;
    transition: opacity 0.5s ease;
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
  /* Gentle breathing pulse on an engaged switch - a live "this is on" cue
     that never touches border/background colour, so the LED-accurate
     colour logic above stays the single source of truth for state. */
  .stage__switch--active {
    animation: stage-switch-pulse 2.6s ease-in-out infinite;
  }
  @keyframes stage-switch-pulse {
    0%, 100% { transform: scale(1); filter: brightness(1); }
    50%      { transform: scale(1.02); filter: brightness(1.12); }
  }
  @media (prefers-reduced-motion: reduce) {
    .stage__switch--active { animation: none; }
    .stage__glow-spot { transition: none; }
    .stage__marquee-track:global(.stage__marquee-active) { animation: none; }
  }

  .stage__switch-label {
    font-size: calc(clamp(1.5rem, 6.6vw, 3rem) * var(--stage-switch-label-scale, 1));
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
    font-size: calc(clamp(1.5rem, 6.6vw, 3rem) * var(--stage-switch-id-scale, 1));
    color: var(--stage-switch-id-color, var(--text-dim));
    font-family: var(--stage-switch-id-font, var(--stage-font, "Inter", -apple-system, sans-serif));
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
      font-size: calc(clamp(2.6rem, 12vh, 7rem) * var(--stage-rig-name-scale, 1));
      line-height: 1;
    }
    .stage__meta {
      font-size: clamp(1rem, 4vh, 2.5rem);
      gap: clamp(0.5rem, 2vh, 1.5rem);
    }
    .stage__bank {
      font-size: calc(clamp(2.6rem, 12vh, 7rem) * var(--stage-bank-scale, 1));
      color: var(--stage-bank-color, #ffffff);
      text-transform: uppercase; letter-spacing: 0.04em; line-height: 1.1;
    }
    .stage__bpm  { font-size: calc(clamp(1.2rem, 5vh, 3rem) * var(--stage-bpm-scale, 1)); }
    .stage__tuner { font-size: calc(clamp(1.2rem, 5vh, 3rem) * var(--stage-tuner-scale, 1)); }

    .stage__pedal {
      flex: 1 1 0;
      gap: clamp(2px, 1.2vh, 8px);
    }
    .stage__glow {
      gap: clamp(2px, 1.2vh, 8px);
      filter: blur(clamp(16px, 4vh, 40px));
    }
    .stage__glow-row {
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
      font-size: calc(clamp(1.8rem, 9vh, 4.5rem) * var(--stage-switch-label-scale, 1));
      color: var(--stage-switch-label-color, #ffffff);
      line-height: 1.1;
    }
    .stage__switch-id {
      font-size: calc(clamp(1.8rem, 9vh, 4.5rem) * var(--stage-switch-id-scale, 1));
    }
  }
</style>
