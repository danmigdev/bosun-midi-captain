<script lang="ts">
  import { untrack } from "svelte";
  import { cmd, type Manifest, type ExpressionConfig } from "../lib/protocol";
  import { pluginSectionsToShow } from "../lib/plugin-sections";
  import ExpressionPedals from "./ExpressionPedals.svelte";
  import ColorField from "./ColorField.svelte";

  type DeviceConfig = {
    device_name?: string;
    midi_channel?: number;
    long_press_ms?: number;
    double_tap_window_ms?: number;
    auto_momentary_on_hold?: boolean;
    auto_momentary_ms?: number;
    long_press_actions?: Record<string, Array<{ type: string; [k: string]: unknown }>>;
    tuner_exit_on_press?: boolean;
    preview?: { timeout_ms?: number; on_timeout?: "commit" | "cancel" };
    patch_link?: { implicit_by_position?: boolean; locked_slots?: number[] };
    autosave?: { enabled?: boolean; debounce_ms?: number };
    leds?: { brightness?: number; dim?: number };
    tft?: { brightness?: number; theme_color?: string; rotation?: number; rowstart?: number; colstart?: number };
    expression?: ExpressionConfig[];
    [k: string]: unknown;
  };

  type Props = { device: DeviceConfig | null; manifest?: Manifest | null; activeKind?: string; connected?: boolean };
  let { device, manifest = null, activeKind = "", connected = true }: Props = $props();

  /** A full expression-jack entry with sensible defaults (disabled, CC 11). */
  function defaultExpression(jack: number): ExpressionConfig {
    return {
      jack,
      enabled: false,
      invert: false,
      calibration: { min: 0, max: 65535 },
      curve: "linear",
      message: { type: "cc", channel: 1, cc: jack === 2 ? 4 : 11, value: 0 },
    };
  }

  /** Backfill any missing fields on a stored expression entry so the editor
   * always has a complete, mutable object to bind to. */
  function withExpressionDefaults(e: Partial<ExpressionConfig> & { jack: number }): ExpressionConfig {
    const d = defaultExpression(e.jack);
    return {
      jack: e.jack,
      enabled: e.enabled ?? d.enabled,
      invert: e.invert ?? d.invert,
      calibration: {
        min: e.calibration?.min ?? d.calibration.min,
        max: e.calibration?.max ?? d.calibration.max,
      },
      curve: e.curve ?? d.curve,
      message: e.message ?? d.message,
    };
  }

  // Fill in every section we render against so the template never has
  // to do non-null assertions on possibly-missing keys. The Svelte 5
  // template renders synchronously on prop change - initialising the
  // defaults via $effect was racing the render and silently throwing
  // a TypeError on `working.<section>!.<field>` accesses when the
  // backing section hadn't been seeded yet.
  function withDefaults(d: DeviceConfig | null): DeviceConfig {
    const w: DeviceConfig = d ? (structuredClone($state.snapshot(d) as DeviceConfig)) : {};
    if (w.midi_channel === undefined) w.midi_channel = 1;
    if (!w.autosave) w.autosave = { enabled: false, debounce_ms: 2000 };
    if (!w.leds) w.leds = { brightness: 64, dim: 64 };
    // Back-compat: migrate a legacy dim_percent (0-100) to dim (0-255).
    if (w.leds.dim == null) {
      const legacy = (w.leds as { dim_percent?: number }).dim_percent;
      w.leds.dim = legacy == null ? 64 : Math.round((legacy * 255) / 100);
    }
    if (!w.tft) w.tft = { brightness: 80, theme_color: "#00ff88", rotation: 180, rowstart: 80, colstart: 0 };
    // Expression jacks: two by default, each backfilled to a complete entry
    // so the editor can bind enable/invert/curve/message without null checks.
    if (!w.expression || w.expression.length === 0) {
      w.expression = [defaultExpression(1), defaultExpression(2)];
    } else {
      w.expression = w.expression.map(e => withExpressionDefaults(e));
    }
    if (!w.long_press_actions) w.long_press_actions = {};
    if (w.tuner_exit_on_press === undefined) w.tuner_exit_on_press = true;
    if (!w.preview) w.preview = { timeout_ms: 1500, on_timeout: "commit" };
    if (!w.patch_link) w.patch_link = {};
    return w;
  }

  // Plugin config sections to show: the active profile's plugin, or any
  // plugin whose config block is already present in this device.json (so an
  // imported profile shows its section even before activeKind resolves).
  // See lib/plugin-sections.ts.
  let pluginConfigs = $derived(pluginSectionsToShow(manifest, activeKind, device as Record<string, unknown> | null));

  /** Lazy-write a value into working[sectionKey][name]. Used by every
   * field's onchange handler so the section dict is created on the fly
   * the first time the user toggles a field. Doing this in an onchange
   * (event handler) keeps mutations out of the render path - Svelte 5
   * throws `state_unsafe_mutation` if you write to $state during a
   * $derived or template expression. */
  function writeField(sectionKey: string, name: string, value: unknown) {
    let section = working[sectionKey] as Record<string, unknown> | undefined;
    if (!section) {
      working[sectionKey] = {};
      section = working[sectionKey] as Record<string, unknown>;
    }
    section[name] = value;
  }

  function readField(sectionKey: string, name: string, fallback: unknown): unknown {
    const section = working[sectionKey] as Record<string, unknown> | undefined;
    const v = section?.[name];
    return v === undefined ? fallback : v;
  }

  // Initial copy lifted from `device` via untrack so Svelte doesn't
  // flag this as state_referenced_locally - the $effect below is the
  // source of truth for upstream changes and reseeds working.
  let working = $state<DeviceConfig>(untrack(() => withDefaults(device)));
  let lastFingerprint = $state<string>("");
  let saving = $state(false);
  let savedAt = $state<string>("");
  let saveErr = $state<string>("");

  $effect(() => {
    if (device) {
      const fp = JSON.stringify(device);
      if (fp !== lastFingerprint) {
        working = withDefaults(device);
        lastFingerprint = fp;
      }
    }
  });

  async function save() {
    saving = true; saveErr = "";
    try {
      await cmd.putGlobal(working as Record<string, unknown>);
      savedAt = new Date().toLocaleTimeString();
      // Pull the freshly persisted version back.
      await cmd.getGlobal();
    } catch (e) {
      saveErr = String(e);
    } finally {
      saving = false;
    }
  }

  function ensure<T extends object, K extends keyof T>(o: T, k: K, def: T[K]): T[K] {
    if (o[k] === undefined) o[k] = def;
    return o[k]!;
  }

  const ROTATIONS = [0, 90, 180, 270];
  const SWITCH_NAMES = ["1","2","3","4","up","A","B","C","D","down"];

  // Bank navigation lives in long_press_actions as captain_bank_step
  // messages. The selects below read the current mapping reactively
  // and write back through _setBankStepSwitch, which rebuilds the
  // dict to preserve any other long-press actions present in the JSON.
  function _findBankStepSwitch(delta: number): string {
    const lpa = working.long_press_actions ?? {};
    for (const [sw, msgs] of Object.entries(lpa)) {
      if ((msgs ?? []).some(m => m.type === "captain_bank_step" && (m as { delta?: number }).delta === delta)) {
        return sw;
      }
    }
    return "";
  }
  let bankUpSwitch = $derived(_findBankStepSwitch(1));
  let bankDownSwitch = $derived(_findBankStepSwitch(-1));

  function _setBankStepSwitch(delta: number, newSwitch: string) {
    const lpa: Record<string, Array<{ type: string; [k: string]: unknown }>> = {};
    for (const [sw, msgs] of Object.entries(working.long_press_actions ?? {})) {
      const filtered = (msgs ?? []).filter(
        m => !(m.type === "captain_bank_step" && (m as { delta?: number }).delta === delta)
      );
      if (filtered.length > 0) lpa[sw] = filtered;
    }
    if (newSwitch) {
      lpa[newSwitch] = [
        ...(lpa[newSwitch] ?? []),
        { type: "captain_bank_step", delta },
      ];
    }
    working.long_press_actions = lpa;
  }

  function onBankUpChange(e: Event) {
    _setBankStepSwitch(1, (e.target as HTMLSelectElement).value);
  }
  function onBankDownChange(e: Event) {
    _setBankStepSwitch(-1, (e.target as HTMLSelectElement).value);
  }
</script>

{#if !device}
  <p class="muted">Loading device config…</p>
{:else}
  <div class="form">

    <section class="block">
      <h3>Switch behavior</h3>
      <div class="grid">
        <label>long_press (ms) <input type="number" min="100" max="3000" bind:value={working.long_press_ms} /></label>
        <label>double_tap window (ms) <input type="number" min="100" max="1000" bind:value={working.double_tap_window_ms} /></label>
        <label class="cb">
          <input type="checkbox" bind:checked={working.auto_momentary_on_hold} />
          auto-momentary on hold (latched only)
        </label>
        <label>auto-momentary threshold (ms)
          <input type="number" min="100" max="3000" bind:value={working.auto_momentary_ms} />
        </label>
      </div>
    </section>

    <section class="block">
      <h3>MIDI</h3>
      <label>Channel (1–16)
        <input type="number" min="1" max="16" bind:value={working.midi_channel} />
      </label>
      <p class="hint small">
        The channel the pedal uses to talk to and listen from your device (e.g.
        the Kemper Player). It's device-wide, not tied to a plugin - match your
        device's MIDI channel.
      </p>
    </section>

    <section class="block">
      <h3>Global long-press</h3>
      <p class="hint">Bank navigation triggered by holding a switch. Applied on every patch - a patch can override by declaring its own long_press action on that switch.</p>
      <div class="grid">
        <label>Bank up: hold this switch
          <select value={bankUpSwitch} onchange={onBankUpChange}>
            <option value="">- none -</option>
            {#each SWITCH_NAMES as s}<option value={s}>{s}</option>{/each}
          </select>
        </label>
        <label>Bank down: hold this switch
          <select value={bankDownSwitch} onchange={onBankDownChange}>
            <option value="">- none -</option>
            {#each SWITCH_NAMES as s}<option value={s}>{s}</option>{/each}
          </select>
        </label>
      </div>
    </section>

    <section class="block">
      <h3>Preset preview</h3>
      <p class="hint">
        Browse patches on the screen without loading them, then jump to the one
        you pick - no MIDI fires for the patches you scroll past. Bind
        <code>Preview Step</code> to a switch to scroll, and
        <code>Preview Commit</code> / <code>Preview Cancel</code> to confirm or
        back out (in the patch editor's message list).
      </p>
      <div class="grid">
        <label>Auto-resolve after (ms)
          <input type="number" min="0" max="10000" bind:value={working.preview!.timeout_ms} />
        </label>
        <label>When it auto-resolves
          <select bind:value={working.preview!.on_timeout}>
            <option value="commit">Load the previewed patch</option>
            <option value="cancel">Return to current patch</option>
          </select>
        </label>
      </div>
      <p class="hint small">
        If you stop scrolling for this long, the preview resolves on its own.
      </p>
    </section>

    <section class="block">
      <h3>Tuner</h3>
      <label class="cb">
        <input type="checkbox" bind:checked={working.tuner_exit_on_press} />
        Exit the tuner on the next footswitch press
      </label>
      <p class="hint small">
        When the tuner screen is up, the next stomp dismisses it and still
        performs that switch's action in one press.
      </p>
    </section>

    <section class="block">
      <h3>Persistence</h3>
      <div class="grid">
        <label class="cb">
          <input type="checkbox" bind:checked={working.autosave!.enabled} />
          autosave changes to flash
        </label>
        <label>debounce (ms)
          <input type="number" min="0" max="60000" bind:value={working.autosave!.debounce_ms} />
        </label>
      </div>
      <p class="hint small">
        Autosave only works in performance mode (USB drive disabled). In
        editing mode it silently no-ops.
      </p>
    </section>

    <section class="block">
      <h3>LEDs</h3>
      <label>Brightness (0–255)
        <input type="number" min="0" max="255" bind:value={working.leds!.brightness} />
      </label>
      <label>Off (dimmed) LED brightness (0–255)
        <input type="number" min="0" max="255" bind:value={working.leds!.dim} />
      </label>
      <p class="hint">
        How bright a latched switch's LED is when it is OFF, on the same 0–255 scale
        as Brightness above. It scales the ON colour: 255 = as bright as ON, 0 = off.
        64 is the default; lower it for a fainter off state (more contrast between on
        and off). Applies live.
      </p>
    </section>

    <section class="block">
      <h3>Display</h3>
      <div class="grid">
        <label>Brightness (0–100) <input type="number" min="0" max="100" bind:value={working.tft!.brightness} /></label>
        <label>Theme color <ColorField bind:value={working.tft!.theme_color} /></label>
        <label>Rotation
          <select bind:value={working.tft!.rotation}>
            {#each ROTATIONS as r}<option value={r}>{r}°</option>{/each}
          </select>
        </label>
        <label>rowstart <input type="number" min="0" max="320" bind:value={working.tft!.rowstart} /></label>
        <label>colstart <input type="number" min="0" max="320" bind:value={working.tft!.colstart} /></label>
      </div>
    </section>

    <section class="block">
      <h3>Expression pedals</h3>
      <p class="hint">
        Two expression jacks. Enable a jack, then move the pedal and use
        Capture min / Capture max to calibrate its travel. Each jack sends a
        continuous MIDI message (CC, or a plugin control) with the live
        0-127 position.
      </p>
      <ExpressionPedals bind:expression={working.expression!} {manifest} {connected} />
    </section>

    {#each pluginConfigs as cfg (cfg.key)}
      {@const visibleFields = Object.entries(cfg.fields).filter(([, f]) => !f.hidden)}
      {#if visibleFields.length > 0}
      <section class="block">
        <h3>{cfg.label}</h3>
        {#if cfg.hint}<p class="hint">{cfg.hint}</p>{/if}
        <div class="grid">
          {#each visibleFields as [name, field] (name)}
            {#if field.type === "bool"}
              <label class="cb">
                <input type="checkbox"
                       checked={readField(cfg.key, name, field.default ?? false) as boolean}
                       onchange={(e) => writeField(cfg.key, name, e.currentTarget.checked)} />
                {field.label ?? name}
              </label>
            {:else if field.type === "int"}
              <label>{field.label ?? name}
                <input type="number"
                       min={field.min ?? undefined}
                       max={field.max ?? undefined}
                       value={readField(cfg.key, name, field.default ?? 0) as number}
                       onchange={(e) => writeField(cfg.key, name, Number(e.currentTarget.value))} />
              </label>
            {:else if field.type === "enum" && field.values}
              <label>{field.label ?? name}
                <select value={readField(cfg.key, name, field.default ?? "") as string}
                        onchange={(e) => writeField(cfg.key, name, e.currentTarget.value)}>
                  {#each field.values as v}<option value={v}>{v}</option>{/each}
                </select>
              </label>
            {:else}
              <label>{field.label ?? name}
                <input type="text"
                       value={readField(cfg.key, name, field.default ?? "") as string}
                       onchange={(e) => writeField(cfg.key, name, e.currentTarget.value)} />
              </label>
            {/if}
          {/each}
        </div>
      </section>
      {/if}
    {/each}

    <footer class="saverow">
      <button class="primary" onclick={save} disabled={saving}>
        {saving ? "Saving…" : "Save settings"}
      </button>
      {#if savedAt}<span class="ok">saved at {savedAt}</span>{/if}
      {#if saveErr}<span class="err">{saveErr}</span>{/if}
      <span class="grow"></span>
      <span class="hint small">Most settings apply live. Display changes need a reboot.</span>
    </footer>
  </div>
{/if}

<style>
  .form { display: flex; flex-direction: column; gap: 0; padding-bottom: 4rem; }
  .block { background: var(--bg-card); border: 1px solid var(--border); border-radius: 6px; padding: 0.85rem 1rem; margin-bottom: 0.85rem; }
  h3 { color: var(--accent); margin: 0 0 0.6rem; font-size: 0.8rem; text-transform: uppercase; letter-spacing: 0.05em; font-weight: 600; }
  .grid { display: grid; grid-template-columns: repeat(auto-fit, minmax(220px, 1fr)); gap: 0.6rem; }
  label { display: flex; flex-direction: column; gap: 0.2rem; font-size: 0.75rem; color: var(--text-muted); }
  label.cb { flex-direction: row; align-items: center; gap: 0.4rem; color: var(--text); font-size: 0.85rem; }
  input, select { background: var(--bg); color: var(--text); border: 1px solid var(--border-strong); padding: 0.35rem 0.5rem; border-radius: 3px; font-size: 0.85rem; }
  input[type="checkbox"] { width: auto; }
  .saverow {
    position: sticky; bottom: 0;
    background: var(--bg-elevated); border-top: 1px solid var(--border);
    padding: 0.7rem 1rem; margin: 0 -1.25rem -1rem;
    display: flex; align-items: center; gap: 0.75rem;
  }
  .saverow button.primary { background: var(--accent-bg); color: var(--accent); border: 1px solid var(--accent-border); padding: 0.5rem 1.2rem; border-radius: 4px; font-weight: 600; cursor: pointer; font-size: 0.85rem; }
  .saverow button.primary:hover:not(:disabled) { background: var(--accent-hover-bg); }
  .saverow button.primary:disabled { opacity: 0.45; cursor: not-allowed; }
  .saverow .ok { color: var(--accent); font-size: 0.8rem; }
  .saverow .err { color: var(--err); font-size: 0.8rem; }
  .saverow .grow { flex: 1; }
  .muted { color: var(--text-muted); }
  .hint { color: var(--text-muted); font-size: 0.8rem; margin: 0.2rem 0 0.4rem; }
  .hint.small { font-size: 0.75rem; }
</style>
