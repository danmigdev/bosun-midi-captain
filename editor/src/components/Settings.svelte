<script lang="ts">
  import { untrack } from "svelte";
  import { cmd, type Manifest } from "../lib/protocol";
  import { pluginSectionsToShow } from "../lib/plugin-sections";

  type DeviceConfig = {
    device_name?: string;
    midi_channel?: number;
    long_press_ms?: number;
    double_tap_window_ms?: number;
    auto_momentary_on_hold?: boolean;
    auto_momentary_ms?: number;
    long_press_actions?: Record<string, Array<{ type: string; [k: string]: unknown }>>;
    patch_link?: { implicit_by_position?: boolean; locked_slots?: number[] };
    autosave?: { enabled?: boolean; debounce_ms?: number };
    leds?: { brightness?: number };
    tft?: { brightness?: number; theme_color?: string; rotation?: number; rowstart?: number; colstart?: number };
    expression?: Array<{ jack: number; calibration?: { min?: number; max?: number } }>;
    [k: string]: unknown;
  };

  type Props = { device: DeviceConfig | null; manifest?: Manifest | null; activeKind?: string };
  let { device, manifest = null, activeKind = "" }: Props = $props();

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
    if (!w.leds) w.leds = { brightness: 64 };
    if (!w.tft) w.tft = { brightness: 80, theme_color: "#00ff88", rotation: 180, rowstart: 80, colstart: 0 };
    if (!w.expression) w.expression = [
      { jack: 1, calibration: { min: 0, max: 1023 } },
      { jack: 2, calibration: { min: 0, max: 1023 } },
    ];
    if (!w.long_press_actions) w.long_press_actions = {};
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
    </section>

    <section class="block">
      <h3>Display</h3>
      <div class="grid">
        <label>Brightness (0–100) <input type="number" min="0" max="100" bind:value={working.tft!.brightness} /></label>
        <label>Theme color <input type="color" bind:value={working.tft!.theme_color} /></label>
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
      {#each working.expression ?? [] as exp, i (i)}
        <div class="exp">
          <span class="jack">EXP {exp.jack}</span>
          <label>min <input type="number" min="0" max="65535" bind:value={exp.calibration!.min} /></label>
          <label>max <input type="number" min="0" max="65535" bind:value={exp.calibration!.max} /></label>
        </div>
      {/each}
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
      <span class="hint small">Most settings apply live. Display + LED changes need a reboot.</span>
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
  input[type="color"] { padding: 0; width: 50px; height: 30px; cursor: pointer; }
  input[type="checkbox"] { width: auto; }
  .exp { display: flex; align-items: end; gap: 0.6rem; padding: 0.4rem 0; }
  .exp .jack { background: var(--bg-hover); color: var(--warn-text); padding: 0.25rem 0.5rem; border-radius: 3px; font-family: ui-monospace, Consolas, monospace; font-size: 0.8rem; min-width: 4rem; text-align: center; }
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
