<script lang="ts">
  import { untrack } from "svelte";
  import {
    ACTION_KEYS_BY_MODE,
    cmd,
    debouncedPutBinding,
    defaultMessageFromSchema,
    flattenManifest,
    sendAndAwait,
    summarizeMessage,
    type Binding,
    type BindingMode,
    type FlattenedSchema,
    type Manifest,
    type MessageSchema,
    type MidiMessage,
    type ParamSchema,
    type Patch,
  } from "../lib/protocol";
  import { defaultLedFor } from "../lib/switch-colors";
  import { resolveLinkedPatches, isSlotLocked, retargetOnEnterBank, type LinkConfig } from "../lib/patch-links";

  type Props = {
    bank: number;
    slot: number;
    patch: Patch;
    manifest: Manifest;
    activeKind?: string;
    /** All patches on the device, used to populate the "Linked to" picker.
     *  Without it we'd have to round-trip to LIST_PATCHES every render. */
    allPatches?: import("../lib/protocol").PatchSummary[];
    /** Global link config (device.patch_link). Drives the auto-propagation
     *  on every edit: every binding write and every patch meta write also
     *  fires the same payload at every resolved linked target. */
    linkConfig?: LinkConfig;
    /** Toggle this slot's column lock (linked across banks). */
    onToggleLock?: (slot: number) => void;
  };

  let { bank, slot, patch, manifest, activeKind = "", allPatches = [], linkConfig, onToggleLock }: Props = $props();

  /** Is this patch's slot column locked across banks? */
  let slotLocked = $derived(isSlotLocked(slot, linkConfig, allPatches));

  /** Resolve link targets at this exact moment. Used by the propagation
   *  helpers below. Recomputed on every call so changes to working.linked_to
   *  / settings / patches list are reflected immediately. */
  function linkTargets(): Array<{ bank: number; slot: number }> {
    return resolveLinkedPatches({ bank, slot }, working, allPatches, linkConfig);
  }

  function propagateBinding(b: Binding) {
    for (const t of linkTargets()) {
      cmd.putBinding(t.bank, t.slot, b);
    }
  }

  function propagatePatch() {
    // Lock-driven propagation: copy this patch's payload onto every patch in
    // the same (locked) slot across banks. structuredClone keeps nested
    // bindings/actions independent so later edits don't bleed in.
    // retargetOnEnterBank fixes the mirrored copy's on_enter to address the
    // target bank (else a bank change loads the right patch but tells the
    // device to go to the source bank).
    for (const t of linkTargets()) {
      const clone = structuredClone($state.snapshot(working) as Patch);
      retargetOnEnterBank(clone, t.bank);
      cmd.putPatch(t.bank, t.slot, clone);
    }
  }

  /** Single entry point for "write the working patch to the firmware".
   *  Always writes the source and propagates to linked targets in the
   *  same call so we can't accidentally land in a state where the
   *  linked patches drift apart from the master.
   *
   *  Also refreshes App.svelte's view of this patch by firing a
   *  GET_PATCH. Without this, App's currentPatch.patch stays at the
   *  pre-edit snapshot - so if the user navigates away and comes back
   *  via the sidebar Editor button (which does NOT go through
   *  openPatchInEditor), PatchEditor remounts on stale data and the
   *  edit (linked_to, name, on_enter, ...) disappears from the UI even
   *  though the firmware has it. The PATCH response that comes back
   *  re-syncs our local working copy via the $effect below. */
  function persistPatch() {
    cmd.putPatch(bank, slot, working);
    propagatePatch();
    cmd.getPatch(bank, slot).catch(() => {});
  }

  // Filtered manifest: drop plugins that don't match the active profile's
  // kind so the message-type picker only offers core + relevant plugin
  // messages. Empty activeKind means we haven't loaded the profile yet
  // (allow everything to avoid showing a bare picker).
  let filteredManifest = $derived.by<Manifest>(() => {
    if (!activeKind) return manifest;
    const plugins: Manifest["plugins"] = {};
    for (const [id, plug] of Object.entries(manifest.plugins)) {
      if (id === activeKind) plugins[id] = plug;
    }
    return { core_messages: manifest.core_messages, plugins };
  });

  // Local mutable working copy. Re-syncs from the upstream `patch` prop
  // whenever App.svelte reassigns currentPatch (i.e. the prop reference
  // changes). The previous version used a `${bank}/${slot}/${name}`
  // string key to decide when to re-sync, which silently skipped the
  // re-sync whenever a PATCH response arrived with the same name as
  // the placeholder seeded by openPatchInEditor - so freshly-saved
  // linked_to (and bindings) were not adopted into `working`, and the
  // editor header showed "0 explicit links" right after a link.
  //
  // Reference equality is the right discriminator: App.svelte always
  // builds a NEW object when it adopts a PATCH response (or seeds a
  // placeholder, or switches to a different patch). User edits live
  // on `working` and never mutate the prop, so a re-sync only fires
  // when the App-level truth was actually refreshed - which is
  // precisely when we want to adopt it.
  let working = $state<Patch>(untrack(() => structuredClone($state.snapshot(patch) as Patch)));
  let lastPatchRef = $state<Patch | null>(null);
  $effect(() => {
    if (patch !== lastPatchRef) {
      working = structuredClone($state.snapshot(patch) as Patch);
      lastPatchRef = patch;
    }
  });

  let expanded = $state<Set<string>>(new Set());
  let allTypes = $derived<FlattenedSchema[]>(flattenManifest(filteredManifest));
  let typesByName = $derived<Record<string, FlattenedSchema>>(
    Object.fromEntries(allTypes.map(t => [t.type, t]))
  );

  function toggle(sw: string) {
    // Expansion is purely a UI affordance: no binding is created just by
    // looking at a switch. The body renders an empty state with a
    // "Create binding" button for unbound switches; binding actually
    // appears (and the LED lights) only when the user opts in.
    const next = new Set(expanded);
    if (next.has(sw)) next.delete(sw); else next.add(sw);
    expanded = next;
  }

  function createBinding(sw: string) {
    if (working.bindings.find(b => b.switch === sw)) return;
    working.bindings.push({
      switch: sw,
      mode: "tap",
      label: "",
      // Per-switch default color so the LED is visible from the start
      // (and stays the same color across patches when that switch is
      // used). User can override via the LED color picker.
      led: { on: defaultLedFor(sw) },
      actions: { press: { messages: [] } },
    });
    persistPatch();
  }

  function unbind(sw: string) {
    const idx = working.bindings.findIndex(b => b.switch === sw);
    if (idx < 0) return;
    working.bindings.splice(idx, 1);
    persistPatch();
  }

  function bindingFor(sw: string): Binding | null {
    return working.bindings.find(b => b.switch === sw) ?? null;
  }

  function commit(b: Binding, debounceKey?: string) {
    if (debounceKey) debouncedPutBinding(bank, slot, b, debounceKey);
    else cmd.putBinding(bank, slot, b);
    // Auto-propagation: every binding edit also lands on every linked
    // target. We don't debounce the propagation itself - the editor
    // already debounces the source write for keystrokes, and by the
    // time commit() runs we're past that. For each target the firmware
    // does a put_binding (no reboot, just disk + LED refresh).
    propagateBinding(b);
  }

  function changeMode(b: Binding, mode: BindingMode) {
    b.mode = mode;
    for (const key of ACTION_KEYS_BY_MODE[mode]) {
      if (!b.actions[key]) b.actions[key] = { messages: [] };
    }
    commit(b);
  }

  function addMessage(b: Binding, actionKey: string, msgType: string) {
    const schema = typesByName[msgType];
    if (!schema) return;
    if (!b.actions[actionKey]) b.actions[actionKey] = { messages: [] };
    b.actions[actionKey].messages.push(defaultMessageFromSchema(msgType, schema));
    commit(b);
  }

  function removeMessage(b: Binding, actionKey: string, idx: number) {
    b.actions[actionKey]?.messages.splice(idx, 1);
    commit(b);
  }

  function changeMessageType(b: Binding, actionKey: string, idx: number, newType: string) {
    const schema = typesByName[newType];
    if (!schema) return;
    b.actions[actionKey].messages[idx] = defaultMessageFromSchema(newType, schema);
    commit(b);
  }

  /** Hide a param if its `if` condition isn't satisfied by sibling values. */
  function paramVisible(param: ParamSchema, msg: MidiMessage): boolean {
    if (!param.if) return true;
    for (const [k, v] of Object.entries(param.if)) {
      if (msg[k] !== v) return false;
    }
    return true;
  }

  /** Coerce input string values back to the schema's type. */
  function coerce(param: ParamSchema, raw: string | number | boolean): unknown {
    if (param.type === "int")  return typeof raw === "number" ? raw : parseInt(String(raw), 10) || 0;
    if (param.type === "bool") return Boolean(raw);
    if (param.type === "enum" && param.values && typeof param.values[0] === "number") {
      return typeof raw === "number" ? raw : parseInt(String(raw), 10);
    }
    return raw;
  }

  // ---------- on_enter (patch-load macro) ----------

  function ensureOnEnter() {
    if (!working.on_enter) working.on_enter = { messages: [] };
    return working.on_enter;
  }

  function addOnEnterMessage(msgType: string) {
    const schema = typesByName[msgType];
    if (!schema) return;
    ensureOnEnter().messages.push(defaultMessageFromSchema(msgType, schema));
    persistPatch();
  }

  function removeOnEnterMessage(idx: number) {
    if (!working.on_enter) return;
    working.on_enter.messages.splice(idx, 1);
    persistPatch();
  }

  function changeOnEnterMessageType(idx: number, newType: string) {
    const schema = typesByName[newType];
    if (!schema) return;
    ensureOnEnter().messages[idx] = defaultMessageFromSchema(newType, schema);
    persistPatch();
  }

  let onEnterExpanded = $state(false);

  // How many OTHER banks this (locked) slot links to. Drives the editor's
  // "edits propagate to N" hint and the page-header Save/Discard counts.
  let implicitCount = $derived.by<number>(() => {
    if (!isSlotLocked(slot, linkConfig, allPatches)) return 0;
    return allPatches.filter(p => p.slot === slot && p.bank !== bank).length;
  });

  // When the patch name (or color) changes, also refresh the patches list
  // so the Patches tab shows the new name without a manual click. Debounced
  // because the name is updated on every keystroke.
  let _listRefreshDebounce: ReturnType<typeof setTimeout> | null = null;
  function patchMetaChanged() {
    persistPatch();
    if (_listRefreshDebounce) clearTimeout(_listRefreshDebounce);
    _listRefreshDebounce = setTimeout(() => {
      cmd.listPatches();
      _listRefreshDebounce = null;
    }, 400);
  }

  // All switches in the firmware order
  const SWITCH_ORDER = ["1","2","3","4","up","A","B","C","D","down"];

  function summarize(b: Binding): string {
    return `${b.mode}${b.auto_momentary === false ? " (no auto-mom)" : ""}`;
  }

  const MODES: BindingMode[] = ["tap","latched","momentary","long_press_alt","double_tap"];
</script>

<section class="patch">
  <header class="patchhead">
    <label class="patchname">
      Patch name
      <input bind:value={working.name}
             oninput={patchMetaChanged} />
    </label>
    <label class="patchcolor" title="Color shown on the pedal's screen (TFT) for this patch">
      Screen color
      <input type="color" bind:value={working.tft_color}
             onchange={patchMetaChanged} />
    </label>
  </header>

  <!-- on_enter macro: messages that fire automatically when this patch loads -->
  <section class="on-enter">
    <button class="onhead" onclick={() => onEnterExpanded = !onEnterExpanded}>
      <span class="chevron">{onEnterExpanded ? "▾" : "▸"}</span>
      <span class="title">On enter</span>
      <span class="hint">{working.on_enter?.messages?.length ?? 0} message{(working.on_enter?.messages?.length ?? 0) === 1 ? "" : "s"} fire when this patch loads</span>
    </button>
    {#if onEnterExpanded}
      <div class="onbody">
        {#each (working.on_enter?.messages ?? []) as msg, mi}
          <div class="msg">
            <select value={msg.type}
                    onchange={(e) => changeOnEnterMessageType(mi, (e.target as HTMLSelectElement).value)}>
              {#each allTypes as t}
                <option value={t.type}>{t.source} · {t.label}</option>
              {/each}
            </select>
            {#if typesByName[msg.type]}
              {#each Object.entries(typesByName[msg.type].params) as [pname, param]}
                {#if paramVisible(param, msg)}
                  <label class="param">
                    <span>{param.label ?? pname}</span>
                    {#if param.type === "int"}
                      <input type="number" min={param.min} max={param.max}
                             value={msg[pname] as number}
                             oninput={(e) => {
                               msg[pname] = coerce(param, (e.target as HTMLInputElement).valueAsNumber);
                               persistPatch();
                             }} />
                    {:else if param.type === "enum"}
                      <select value={String(msg[pname])}
                              onchange={(e) => {
                                msg[pname] = coerce(param, (e.target as HTMLSelectElement).value);
                                persistPatch();
                              }}>
                        {#each param.values ?? [] as v}
                          <option value={String(v)}>{v}</option>
                        {/each}
                      </select>
                    {:else if param.type === "bool"}
                      <input type="checkbox"
                             checked={Boolean(msg[pname])}
                             onchange={(e) => {
                               msg[pname] = (e.target as HTMLInputElement).checked;
                               persistPatch();
                             }} />
                    {:else}
                      <input value={String(msg[pname] ?? "")}
                             oninput={(e) => {
                               msg[pname] = (e.target as HTMLInputElement).value;
                               persistPatch();
                             }} />
                    {/if}
                  </label>
                {/if}
              {/each}
            {/if}
            <button class="rm" onclick={() => removeOnEnterMessage(mi)} title="Remove">×</button>
          </div>
        {/each}
        <div class="addmsg">
          <select id="oe-add">
            {#each allTypes as t}
              <option value={t.type}>{t.source} · {t.label}</option>
            {/each}
          </select>
          <button onclick={(e) => {
            const sel = (e.currentTarget as HTMLElement).previousElementSibling as HTMLSelectElement;
            addOnEnterMessage(sel.value);
          }}>+ add message</button>
        </div>
      </div>
    {/if}
  </section>

  <!-- Cross-bank lock. When closed, this switch is linked across every bank:
       editing this patch propagates to the same slot in all banks. Same
       padlock as the Patches grid column header. -->
  <section class="links">
    <div class="lockrow">
      <button class="lockbtn" class:locked={slotLocked}
              onclick={() => onToggleLock?.(slot)}
              aria-pressed={slotLocked}
              title={slotLocked
                ? "Locked across banks - edits propagate to this slot in every bank. Click to unlock."
                : "Lock across banks so edits propagate to this slot in every bank."}>
        <svg class="lockicon" viewBox="0 0 24 24" aria-hidden="true">
          <rect x="5" y="11" width="14" height="9" rx="2" />
          {#if slotLocked}<path d="M8 11 V8 a4 4 0 0 1 8 0 V11" />{:else}<path d="M8 11 V8 a4 4 0 0 1 8 0" />{/if}
        </svg>
        <span>{slotLocked ? "Locked across banks" : "Lock across banks"}</span>
      </button>
      {#if slotLocked && implicitCount > 0}
        <span class="hint small">edits propagate to {implicitCount} other bank{implicitCount === 1 ? "" : "s"}</span>
      {/if}
    </div>
  </section>

  <ul class="bindings">
    {#each SWITCH_ORDER as sw}
      {@const b = bindingFor(sw)}
      <li class:expanded={expanded.has(sw)}>
        <button class="bindinghead" onclick={() => toggle(sw)}>
          <span class="sw">{sw}</span>
          {#if b}
            <span class="mode">{summarize(b)}</span>
            <span class="label">{b.label ?? ""}</span>
            <span class="swatch" style:background={b.led?.on ?? "#000"}></span>
            {#if b.mode === "latched"}
              <span class="swatch off" style:background={b.led?.off ?? "#000"}></span>
            {/if}
          {:else}
            <span class="empty">unbound</span>
          {/if}
          <span class="chevron">{expanded.has(sw) ? "▾" : "▸"}</span>
        </button>

        {#if expanded.has(sw) && !b}
          <div class="bindingbody emptybody">
            <button onclick={() => createBinding(sw)}>+ Create binding</button>
          </div>
        {/if}
        {#if expanded.has(sw) && b}
          <div class="bindingbody">
            <div class="row">
              <label>
                Mode
                <select bind:value={b.mode}
                        onchange={() => changeMode(b, b.mode)}>
                  {#each MODES as m}<option value={m}>{m}</option>{/each}
                </select>
              </label>
              <label>
                Label
                <input bind:value={b.label}
                       oninput={() => commit(b, `lbl.${bank}.${slot}.${sw}`)} />
              </label>
              <label>
                LED on
                <input type="color" value={b.led?.on ?? "#000000"}
                       onchange={(e) => {
                         if (!b.led) b.led = { on: "#000000" };
                         b.led.on = (e.target as HTMLInputElement).value;
                         commit(b);
                       }} />
              </label>
              {#if b.mode === "latched"}
                <label>
                  LED off
                  <input type="color" value={b.led?.off ?? "#000000"}
                         onchange={(e) => {
                           // One-way binding + onchange-only writeback so
                           // that merely rendering the row doesn't push
                           // a default "#000000" into b.led.off. Without
                           // this, Svelte's two-way bind reads back the
                           // color input's default and the latched-off
                           // LED renders BLACK instead of the dim
                           // fallback derived from led.on.
                           if (!b.led) b.led = { on: "#000000" };
                           b.led.off = (e.target as HTMLInputElement).value;
                           commit(b);
                         }} />
                </label>
                <label class="toggle">
                  <input type="checkbox"
                         checked={b.auto_momentary !== false}
                         onchange={(e) => { b.auto_momentary = (e.target as HTMLInputElement).checked; commit(b); }} />
                  auto-momentary on hold
                </label>
              {/if}
            </div>

            {#each ACTION_KEYS_BY_MODE[b.mode] as actionKey}
              <fieldset class="action">
                <legend>{actionKey}</legend>
                {#each (b.actions[actionKey]?.messages ?? []) as msg, mi}
                  <div class="msg">
                    <select value={msg.type}
                            onchange={(e) => changeMessageType(b, actionKey, mi, (e.target as HTMLSelectElement).value)}>
                      {#each allTypes as t}
                        <option value={t.type}>{t.source} · {t.label}</option>
                      {/each}
                    </select>
                    {#if typesByName[msg.type]}
                      {#each Object.entries(typesByName[msg.type].params) as [pname, param]}
                        {#if paramVisible(param, msg)}
                          <label class="param">
                            <span>{param.label ?? pname}</span>
                            {#if param.type === "int"}
                              <input type="number" min={param.min} max={param.max}
                                     value={msg[pname] as number}
                                     oninput={(e) => {
                                       msg[pname] = coerce(param, (e.target as HTMLInputElement).valueAsNumber);
                                       commit(b, `m.${bank}.${slot}.${sw}.${actionKey}.${mi}.${pname}`);
                                     }} />
                            {:else if param.type === "enum"}
                              <select value={String(msg[pname])}
                                      onchange={(e) => {
                                        msg[pname] = coerce(param, (e.target as HTMLSelectElement).value);
                                        commit(b);
                                      }}>
                                {#each param.values ?? [] as v}
                                  <option value={String(v)}>{v}</option>
                                {/each}
                              </select>
                            {:else if param.type === "bool"}
                              <input type="checkbox"
                                     checked={Boolean(msg[pname])}
                                     onchange={(e) => {
                                       msg[pname] = (e.target as HTMLInputElement).checked;
                                       commit(b);
                                     }} />
                            {:else}
                              <input value={String(msg[pname] ?? "")}
                                     oninput={(e) => {
                                       msg[pname] = (e.target as HTMLInputElement).value;
                                       commit(b, `m.${bank}.${slot}.${sw}.${actionKey}.${mi}.${pname}`);
                                     }} />
                            {/if}
                          </label>
                        {/if}
                      {/each}
                    {/if}
                    <button class="rm" onclick={() => removeMessage(b, actionKey, mi)} title="Remove message">×</button>
                  </div>
                {/each}
                <div class="addmsg">
                  <select id={`add-${sw}-${actionKey}`}>
                    {#each allTypes as t}
                      <option value={t.type}>{t.source} · {t.label}</option>
                    {/each}
                  </select>
                  <button onclick={(e) => {
                    const sel = (e.currentTarget as HTMLElement).previousElementSibling as HTMLSelectElement;
                    addMessage(b, actionKey, sel.value);
                  }}>+ add message</button>
                </div>
              </fieldset>
            {/each}

            <div class="bindingfoot">
              <button class="unbind"
                      onclick={() => unbind(sw)}
                      title="Remove this binding entirely. The LED turns off and the switch becomes inert until you create a new binding.">
                Unbind switch {sw}
              </button>
            </div>
          </div>
        {/if}
      </li>
    {/each}
  </ul>
</section>

<style>
  .patch { background: var(--bg-card); border: 1px solid var(--border); border-radius: 6px; padding: 0.75rem; }
  .patchhead { display: flex; gap: 1rem; align-items: end; margin-bottom: 0.75rem; }
  .patchhead label { display: flex; flex-direction: column; gap: 0.25rem; font-size: 0.75rem; color: var(--text-muted); }
  .patchhead input { background: var(--bg); color: var(--text); border: 1px solid var(--border-strong); padding: 0.3rem 0.5rem; border-radius: 4px; }
  .patchhead input[type="color"] { padding: 0; width: 40px; height: 28px; cursor: pointer; }
  .on-enter, .links { background: var(--bg-hover); border-radius: 4px; margin-bottom: 0.6rem; }
  .lockrow { display: flex; align-items: center; gap: 0.6rem; flex-wrap: wrap; padding: 0.5rem 0.6rem; }
  .lockbtn {
    display: inline-flex; align-items: center; gap: 0.4rem;
    background: var(--bg-card); border: 1px solid var(--border);
    border-radius: 999px; padding: 0.3rem 0.7rem; cursor: pointer;
    color: var(--text-muted); font-size: 0.78rem; font-family: inherit;
    transition: border-color 0.12s ease, color 0.12s ease;
  }
  .lockbtn:hover { border-color: var(--border-strong); color: var(--text); }
  .lockbtn.locked { border-color: var(--accent-border); color: var(--accent); background: var(--accent-bg); }
  .lockicon { width: 14px; height: 14px; display: block; }
  .lockicon rect, .lockicon path {
    fill: none; stroke: currentColor; stroke-width: 1.7;
    stroke-linecap: round; stroke-linejoin: round;
  }
  .lockbtn.locked .lockicon rect { fill: var(--accent); }
  .onhead {
    display: flex; align-items: center; gap: 0.6rem; width: 100%;
    padding: 0.45rem 0.65rem; background: transparent; border: none;
    color: var(--text); cursor: pointer; text-align: left; font-size: 0.85rem;
  }
  .onhead:hover { background: var(--bg-hover); }
  .onhead .title { font-weight: 600; color: var(--accent); }
  .onhead .hint { color: var(--text-muted); font-size: 0.78rem; }
  .onhead .chevron { color: var(--text-dim); font-size: 0.7rem; width: 1rem; text-align: center; }
  .onbody { padding: 0.5rem 0.75rem 0.75rem; border-top: 1px solid var(--border); }
  ul.bindings { list-style: none; padding: 0; margin: 0; display: flex; flex-direction: column; gap: 0.25rem; }
  ul.bindings li { background: var(--bg-hover); border-radius: 4px; }
  ul.bindings li.expanded { background: var(--bg-hover); }
  .bindinghead { display: flex; align-items: center; gap: 0.6rem; width: 100%; padding: 0.45rem 0.65rem; background: transparent; border: none; color: var(--text); cursor: pointer; text-align: left; font-size: 0.85rem; }
  .bindinghead:hover { background: var(--bg-hover); }
  .sw { font-family: ui-monospace, Consolas, monospace; min-width: 2.5rem; color: var(--text); font-weight: 600; }
  .mode { color: var(--text-muted); font-size: 0.75rem; min-width: 7rem; }
  .label { flex: 1; color: var(--text); }
  .empty { flex: 1; color: var(--text-dim); font-style: italic; }
  .swatch { display: inline-block; width: 14px; height: 14px; border-radius: 50%; border: 1px solid var(--border-strong); }
  .swatch.off { opacity: 0.7; }
  .chevron { color: var(--text-dim); font-size: 0.7rem; width: 1rem; text-align: right; }
  .bindingbody { padding: 0.5rem 0.75rem 0.75rem; border-top: 1px solid var(--border); }
  .bindingbody.emptybody { display: flex; align-items: center; justify-content: flex-end; }
  .bindingfoot { display: flex; justify-content: flex-end; margin-top: 0.5rem; }
  .bindingfoot .unbind {
    background: transparent; border: 1px solid var(--err-border); color: var(--err);
    padding: 0.3rem 0.65rem; border-radius: 4px; cursor: pointer; font-size: 0.78rem;
  }
  .bindingfoot .unbind:hover { background: var(--err-bg); }
  .row { display: flex; gap: 0.75rem; align-items: end; flex-wrap: wrap; margin-bottom: 0.75rem; }
  .row label { display: flex; flex-direction: column; gap: 0.2rem; font-size: 0.7rem; color: var(--text-muted); }
  .row input, .row select, .msg input, .msg select, .addmsg select {
    background: var(--bg); color: var(--text); border: 1px solid var(--border-strong); padding: 0.25rem 0.4rem; border-radius: 3px; font-size: 0.8rem;
  }
  .row input[type="color"] { padding: 0; width: 36px; height: 26px; cursor: pointer; }
  .toggle { flex-direction: row !important; align-items: center; gap: 0.4rem !important; color: var(--text) !important; }
  fieldset.action { border: 1px solid var(--border); border-radius: 4px; padding: 0.5rem 0.6rem; margin-bottom: 0.5rem; }
  fieldset.action legend { color: var(--accent); padding: 0 0.4rem; font-size: 0.75rem; }
  .msg { display: flex; gap: 0.4rem; align-items: end; flex-wrap: wrap; margin-bottom: 0.35rem; padding: 0.35rem; background: var(--bg-elevated); border-radius: 3px; }
  .param { display: flex; flex-direction: column; gap: 0.15rem; font-size: 0.7rem; color: var(--text-muted); }
  .param span { white-space: nowrap; }
  .rm { background: rgba(239,155,155,0.08); border-color: rgba(239,155,155,0.35); color: var(--err); padding: 0.2rem 0.5rem; cursor: pointer; border-radius: 3px; }
  .addmsg { display: flex; gap: 0.4rem; margin-top: 0.3rem; }
  .addmsg button { background: var(--accent-bg); color: var(--accent); border: 1px solid var(--accent-border); padding: 0.25rem 0.5rem; cursor: pointer; border-radius: 3px; font-size: 0.8rem; }
  .addmsg button:hover { background: var(--accent-hover-bg); }
</style>
