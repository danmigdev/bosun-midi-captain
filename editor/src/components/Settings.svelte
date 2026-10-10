<script lang="ts">
  import BrowseProgramMap from "./BrowseProgramMap.svelte";
  import KemperIdentity from "./KemperIdentity.svelte";
  import { untrack } from "svelte";
  import { cmd, type Manifest, type ExpressionConfig } from "../lib/protocol";
  import { pluginSectionsToShow } from "../lib/plugin-sections";
  import { getBankCount, getRigsPerBank, MAX_BANKS, MAX_RIGS_PER_BANK } from "../lib/bank-layout";
  import { CAPTAIN_10, expressionJacks, hasSwitch, missingSwitches, type HardwareLayout } from "../lib/hardware";
  import ExpressionPedals from "./ExpressionPedals.svelte";

  type DeviceConfig = {
    device_name?: string;
    midi_channel?: number;
    rigs_per_bank?: number;
    bank_count?: number;
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
    preset_navigation?: { switches?: Record<string, number>; bank_colors?: Record<string, string> };
    tft?: { brightness?: number; rotation?: number };
    expression?: ExpressionConfig[];
    [k: string]: unknown;
  };

  type Props = {
    device: DeviceConfig | null; manifest?: Manifest | null; activeKind?: string; connected?: boolean;
    /** Connected model; omitted means the 10-switch Captain. */
    hardware?: HardwareLayout;
  };
  let { device, manifest = null, activeKind = "", connected = true, hardware = CAPTAIN_10 }: Props = $props();

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
    w.rigs_per_bank = getRigsPerBank(w);
    w.bank_count = getBankCount(w);
    if (!w.autosave) w.autosave = { enabled: false, debounce_ms: 2000 };
    if (!w.leds) w.leds = { brightness: 64, dim: 64 };
    if (w.leds.dim == null) w.leds.dim = 64;
    if (!w.tft) w.tft = { brightness: 80, rotation: 180 };
    // Expression jacks: one default per jack this model has (none on a
    // pedal without jacks), each backfilled to a complete entry so the
    // editor can bind enable/invert/curve/message without null checks.
    // Entries for jacks the pedal lacks (a config copied from another
    // model) are kept verbatim until the user removes them.
    const present = expressionJacks(hardware);
    if (!w.expression || w.expression.length === 0) {
      if (present.length > 0) w.expression = present.map(jack => defaultExpression(jack));
    } else {
      w.expression = w.expression.map(e => present.includes(e.jack) ? withExpressionDefaults(e) : e);
    }
    if (!w.long_press_actions) w.long_press_actions = {};
    if (!w.preset_navigation) w.preset_navigation = {};
    if (!w.preset_navigation.switches) w.preset_navigation.switches = {};
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
  // flag this as state_referenced_locally - the effect below is the
  // source of truth for upstream changes and reseeds working.
  let working = $state<DeviceConfig>(untrack(() => withDefaults(device)));
  let lastFingerprint = $state<string>("");
  let saving = $state(false);
  let savedAt = $state<string>("");
  let saveErr = $state<string>("");

  // Pre-effect: reseed before the DOM updates, so the template never renders
  // one pass of a working copy seeded for another model (ExpressionPedals
  // would get un-backfilled entries for jacks the new model has).
  $effect.pre(() => {
    if (device) {
      // The jack count is part of the fingerprint: expression defaults are
      // seeded per model, so hardware reported after the config arrived (or
      // a pedal swap) reseeds instead of keeping the other model's jacks.
      const fp = `${hardware.expression_jacks}:${JSON.stringify(device)}`;
      if (fp !== lastFingerprint) {
        working = withDefaults(device);
        lastFingerprint = fp;
      }
    }
  });

  async function save() {
    if (!bankCountValid) return;
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

  const ROTATIONS = [0, 90, 180, 270];
  // The pickers follow the connected model. Keys saved for switches or jacks
  // it lacks (a config copied from another model) are never dropped: they
  // are listed under "Not on this pedal" until the user removes them.
  let switchNames = $derived(hardware.switches);
  let jacks = $derived(expressionJacks(hardware));
  let jackSummary = $derived(hardware.expression_jacks === 1 ? "One expression jack."
    : hardware.expression_jacks === 2 ? "Two expression jacks." : `${hardware.expression_jacks} expression jacks.`);
  type OrphanRow = { key: string; id: string; detail: string; remove: () => void };
  let bankCountValid = $derived(typeof working.bank_count === "number" && Number.isInteger(working.bank_count)
    && working.bank_count >= 1 && working.bank_count <= MAX_BANKS);

  // Bank navigation lives in long_press_actions as captain_bank_step
  // messages. The selects below read the current mapping reactively
  // and write back through _setBankStepSwitch, which rebuilds the
  // dict to preserve any other long-press actions present in the JSON.
  function _findBankStepSwitch(delta: number): string {
    const lpa = working.long_press_actions ?? {};
    // A switch this pedal has wins over one it lacks: only that one fires.
    let missing = "";
    for (const [sw, msgs] of Object.entries(lpa)) {
      if ((msgs ?? []).some(m => m.type === "captain_bank_step" && (m as { delta?: number }).delta === delta)) {
        if (hasSwitch(hardware, sw)) return sw;
        if (!missing) missing = sw;
      }
    }
    return missing;
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

  /** A stored bank switch this pedal lacks still gets its own option, so the
   * select shows the real mapping and changing it moves the action. */
  function isMissingSwitch(sw: string): boolean {
    return sw !== "" && !hasSwitch(hardware, sw);
  }

  function longPressSummary(msgs: unknown): string {
    if (!Array.isArray(msgs) || msgs.length === 0) return "no actions";
    return msgs.map((m: { type?: string; delta?: unknown }) => {
      if (m?.type !== "captain_bank_step") return String(m?.type ?? "unknown");
      return m.delta === 1 ? "Bank up" : m.delta === -1 ? "Bank down" : `Bank step ${m.delta}`;
    }).join(", ");
  }
  function removeLongPress(sw: string) {
    const lpa = { ...(working.long_press_actions ?? {}) };
    delete lpa[sw];
    working.long_press_actions = lpa;
  }
  let orphanLongPress = $derived<OrphanRow[]>(
    missingSwitches(hardware, Object.keys(working.long_press_actions ?? {})).map(sw => ({
      key: sw, id: sw, detail: longPressSummary(working.long_press_actions?.[sw]),
      remove: () => removeLongPress(sw),
    })),
  );

  // Preset-navigation row: device.preset_navigation.switches maps a switch
  // name to the slot (within the CURRENT bank) it jumps to. It's a
  // device-wide overlay, not a patch binding - the firmware only applies it
  // to a switch when the active patch declares no binding of its own for
  // that switch (per-patch bindings always win), so the same switch can act
  // as a plain effect toggle in one patch and a rig-select in another.
  function navSlotValue(sw: string): string {
    const v = working.preset_navigation?.switches?.[sw];
    return v === undefined ? "" : String(v);
  }
  function setNavSlot(sw: string, raw: string) {
    const switches = { ...(working.preset_navigation?.switches ?? {}) };
    const n = raw.trim() === "" ? NaN : Number(raw);
    if (!Number.isFinite(n) || n <= 0) {
      delete switches[sw];
    } else {
      switches[sw] = n;
    }
    working.preset_navigation = { ...(working.preset_navigation ?? {}), switches };
  }
  let orphanNav = $derived<OrphanRow[]>(
    missingSwitches(hardware, Object.keys(working.preset_navigation?.switches ?? {})).map(sw => ({
      key: sw, id: sw.toUpperCase(), detail: `slot ${navSlotValue(sw)}`,
      remove: () => setNavSlot(sw, ""),
    })),
  );

  function expressionSummary(e: Partial<ExpressionConfig>): string {
    const state = e.enabled ? "enabled" : "disabled";
    const m = e.message;
    if (!m) return state;
    return `${state}, ${m.type === "cc" ? `CC ${m.cc ?? "?"}` : m.type}`;
  }
  // Rows keep the index into working.expression so a removal is exact even
  // when an imported config repeats a jack number.
  let orphanExpression = $derived<OrphanRow[]>(
    (working.expression ?? []).flatMap((e, index) => jacks.includes(e.jack) ? [] : [{
      key: String(index), id: `EXP ${e.jack ?? "?"}`, detail: expressionSummary(e),
      remove: () => { working.expression = (working.expression ?? []).filter((_, i) => i !== index); },
    }]),
  );
</script>

{#snippet notOnThisPedal(label: string, note: string, rows: OrphanRow[])}
  <div class="orphans" role="group" aria-label={label}>
    <p class="hint small"><strong>Not on this pedal.</strong> {note}</p>
    <ul>
      {#each rows as row (row.key)}
        <li>
          <span class="orphanid">{row.id}</span>
          <span class="orphandetail">{row.detail}</span>
          <button type="button" onclick={row.remove} aria-label="Remove {row.id}">Remove</button>
        </li>
      {/each}
    </ul>
  </div>
{/snippet}

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
      <h3>Banks</h3>
      <label>Number of banks
        <input type="number" min="1" max={MAX_BANKS} step="1" bind:value={working.bank_count} />
      </label>
      <p class="hint">
        Navigate banks 1 through this number, skipping empty banks and wrapping
        at the ends. For example, 2 cycles between banks 1 and 2. Higher banks
        stay available in the editor. Physical bank navigation requires the
        Captain firmware that supports this setting.
      </p>
      {#if !bankCountValid}<p class="err" role="alert">Enter a whole number from 1 to {MAX_BANKS}.</p>{/if}
      <label>Rigs per bank
        <select bind:value={working.rigs_per_bank}>
          {#each Array(MAX_RIGS_PER_BANK) as _, index}
            <option value={index + 1}>{index + 1}</option>
          {/each}
        </select>
      </label>
      <p class="hint">
        Saved separately for this profile. Use 5 for Kemper Player, or match
        your device's bank layout. New patches fill these slots before starting
        the next bank. Existing patches and their MIDI commands stay unchanged.
      </p>
    </section>

    <section class="block">
      <h3>Global long-press</h3>
      <p class="hint">Bank navigation triggered by holding a switch. Applied on every patch - a patch can override by declaring its own long_press action on that switch.</p>
      <div class="grid">
        <label>Bank up: hold this switch
          <select value={bankUpSwitch} onchange={onBankUpChange}>
            <option value="">- none -</option>
            {#each switchNames as s}<option value={s}>{s}</option>{/each}
            {#if isMissingSwitch(bankUpSwitch)}<option value={bankUpSwitch}>{bankUpSwitch} (not on this pedal)</option>{/if}
          </select>
        </label>
        <label>Bank down: hold this switch
          <select value={bankDownSwitch} onchange={onBankDownChange}>
            <option value="">- none -</option>
            {#each switchNames as s}<option value={s}>{s}</option>{/each}
            {#if isMissingSwitch(bankDownSwitch)}<option value={bankDownSwitch}>{bankDownSwitch} (not on this pedal)</option>{/if}
          </select>
        </label>
      </div>
      {#if orphanLongPress.length > 0}
        {@render notOnThisPedal("Long-press actions not on this pedal",
          `Long-press actions saved for switches the ${hardware.name} does not have (for example from a backup of another model). They never fire here and are kept until you remove them.`,
          orphanLongPress)}
      {/if}
    </section>

    <section class="block">
      <h3>Preset navigation row</h3>
      <p class="hint">
        Dedicate switches to jump straight to a rig slot within the current
        bank: pressing loads that slot, its LED shows the bank colour (bright
        on the active slot, dimmed on the others), and Stage view shows that
        slot's patch name instead of "-". Applies only when the current
        patch leaves that switch unbound - a patch that binds the switch to
        something else always takes priority. Leave blank for a normal,
        patch-bound switch.
      </p>
      <div class="grid">
        {#each switchNames as sw}
          <label>{sw.toUpperCase()}
            <input type="number" min="1" placeholder="unmapped"
                   value={navSlotValue(sw)}
                   oninput={(e) => setNavSlot(sw, e.currentTarget.value)} />
          </label>
        {/each}
      </div>
      {#if orphanNav.length > 0}
        {@render notOnThisPedal("Preset navigation not on this pedal",
          `Slots saved for switches the ${hardware.name} does not have (for example from a backup of another model). They never fire here and are kept until you remove them.`,
          orphanNav)}
      {/if}
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
        <label>Rotation
          <select bind:value={working.tft!.rotation}>
            {#each ROTATIONS as r}<option value={r}>{r}°</option>{/each}
          </select>
        </label>
      </div>
    </section>

    <!-- Hidden on a pedal without jacks unless a copied config left entries
         to remove; ExpressionPedals (which polls STATS) only mounts with jacks. -->
    {#if jacks.length > 0 || orphanExpression.length > 0}
    <section class="block">
      <h3>Expression pedals</h3>
      {#if jacks.length > 0}
        <p class="hint">
          {jackSummary} Enable a jack, then move the pedal and use
          Capture min / Capture max to calibrate its travel. Each jack sends a
          continuous MIDI message (CC, or a plugin control) with the live
          0-127 position.
        </p>
        <ExpressionPedals bind:expression={working.expression!} {jacks} {manifest} {activeKind} device={working} {connected} />
      {/if}
      {#if orphanExpression.length > 0}
        {@render notOnThisPedal("Expression pedals not on this pedal",
          `Settings saved for expression jacks the ${hardware.name} does not have (for example from a backup of another model). They are ignored here and kept until you remove them.`,
          orphanExpression)}
      {/if}
    </section>
    {/if}

    {#each pluginConfigs as cfg (cfg.key)}
      {@const fields = Object.entries(cfg.fields)}
      {#if fields.length > 0}
      <section class="block">
        <h3>{cfg.label}</h3>
        {#if activeKind === "kemper_head" && cfg.key === "kemper"}<KemperIdentity {connected} />{/if}
        {#if activeKind === "kemper_head" && cfg.key === "kemper" && readField(cfg.key, "mode", "performance") === "browse"}
          <BrowseProgramMap value={readField(cfg.key, "browse_program_map", {})} onChange={value => writeField(cfg.key, "browse_program_map", value)} />
        {/if}
        {#if cfg.hint}<p class="hint">{cfg.hint}</p>{/if}
        <div class="grid">
          {#each fields as [name, field] (name)}
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
      <button class="primary" onclick={save} disabled={saving || !bankCountValid}>
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
  .orphans { margin-top: 0.7rem; padding: 0.5rem 0.7rem; border: 1px dashed var(--border-strong); border-radius: 4px; }
  .orphans .hint strong { color: var(--warn-text); }
  .orphans ul { list-style: none; margin: 0; padding: 0; display: flex; flex-direction: column; gap: 0.3rem; }
  .orphans li { display: flex; align-items: center; gap: 0.6rem; font-size: 0.8rem; }
  .orphanid { font-family: ui-monospace, Consolas, monospace; color: var(--warn-text); min-width: 3.5rem; }
  .orphandetail { flex: 1; color: var(--text-muted); }
  .orphans button {
    background: var(--bg-hover); color: var(--err); border: 1px solid var(--border-strong);
    padding: 0.2rem 0.6rem; border-radius: 3px; cursor: pointer; font-size: 0.75rem;
  }

  /* ---------- mobile ---------- */
  @media (max-width: 767px) {
    .form { padding: 0.5rem; }
    .block { padding: 0.5rem; margin-bottom: 0.5rem; }
    .block h3 { font-size: 0.9rem; }
    .grid { grid-template-columns: 1fr; gap: 0.4rem; }
    .grid input, .grid select, .form input, .form select {
      width: 100%;
      font-size: 0.9rem;
      padding: 0.45rem 0.5rem;
      min-height: 44px;
    }
    .grid input[type="checkbox"] { width: auto; min-height: auto; }
    .cb { flex-wrap: wrap; gap: 0.35rem; }
    .orphans button { min-height: 44px; }
    .saverow { flex-direction: column; gap: 0.35rem; }
    .saverow button { width: 100%; min-height: 44px; }
  }
</style>
