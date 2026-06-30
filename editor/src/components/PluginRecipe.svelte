<script lang="ts">
  import { sendAndAwait, type PatchSummary, type PluginRecipeSchema } from "../lib/protocol";

  type Props = {
    schema: PluginRecipeSchema;
    patches: PatchSummary[];
  };
  let { schema, patches }: Props = $props();

  type Row = {
    bank: number; slot: number;
    captainName: string;
    presetLabel: string | null;
    bankMsb: number | null;
    pc: number | null;
    channel: number;
    loaded: boolean;
    err?: string;
  };

  let rows = $state<Row[]>([]);
  let busy = $state(false);

  /** Apply the plugin's pc_layout to a preset string and return the MIDI
   * indices, or null if the regex doesn't match or the formula is
   * unsafe. We only evaluate one arithmetic expression with named group
   * vars - `Function`-based eval is fine here because the formula comes
   * from the firmware-side plugin source, not from anywhere a user can
   * inject. We still sanity-check that the formula contains nothing but
   * group names, digits and a tiny operator set, just in case. */
  function computeIndices(preset: string): { bankMsb: number; pc: number } | null {
    const layout = schema.pc_layout;
    if (!layout) return null;
    const m = preset.match(new RegExp(layout.preset_regex));
    if (!m) return null;
    const env: Record<string, number> = {};
    for (let i = 0; i < layout.groups.length; i++) {
      const g = layout.groups[i];
      const raw = m[i + 1];
      const n = parseInt(raw, 10);
      if (!Number.isFinite(n)) return null;
      if (g.min !== undefined && n < g.min) return null;
      if (g.max !== undefined && n > g.max) return null;
      env[g.name] = n;
    }
    const formula = layout.index_formula;
    if (!/^[\sA-Za-z0-9_+\-*/().]+$/.test(formula)) return null;
    let idx: number;
    try {
      const args = Object.keys(env);
      const vals = args.map(k => env[k]);
      const fn = new Function(...args, `return (${formula});`) as (...a: number[]) => number;
      idx = fn(...vals);
    } catch {
      return null;
    }
    if (!Number.isFinite(idx) || idx < 0) return null;
    return {
      bankMsb: Math.floor(idx / layout.pc_max),
      pc: idx % layout.pc_max,
    };
  }

  function fillTemplate(tmpl: string | undefined, vars: Record<string, string | number>): string {
    if (!tmpl) return "";
    return tmpl.replace(/\{(\w+)\}/g, (_, k) => String(vars[k] ?? ""));
  }

  async function refresh() {
    if (busy) return;
    busy = true;
    try {
      const newRows: Row[] = [];
      for (const p of patches) {
        const row: Row = {
          bank: p.bank, slot: p.slot,
          captainName: p.name || "(unnamed)",
          presetLabel: null, bankMsb: null, pc: null,
          channel: schema.channel_default,
          loaded: false,
        };
        try {
          const resp: any = await sendAndAwait(
            { type: "GET_PATCH", bank: p.bank, slot: p.slot }, 3000);
          row.loaded = true;
          const messages: any[] = resp?.patch?.on_enter?.messages ?? [];
          const msg = messages.find(m => m?.type === schema.target_message_type);
          if (msg) {
            row.presetLabel = msg[schema.preset_field] ?? null;
            row.channel = msg[schema.channel_field] ?? schema.channel_default;
            if (typeof row.presetLabel === "string") {
              const idx = computeIndices(row.presetLabel);
              if (idx) { row.bankMsb = idx.bankMsb; row.pc = idx.pc; }
            }
          }
        } catch (e) {
          row.err = String(e);
        }
        newRows.push(row);
      }
      rows = newRows;
    } finally {
      busy = false;
    }
  }

  let lastFingerprint = $state("");
  $effect(() => {
    const fp = patches.map(p => `${p.bank}/${p.slot}/${p.name}`).join("|");
    if (fp !== lastFingerprint) {
      lastFingerprint = fp;
      refresh();
    }
  });

  function copyRecipe(r: Row) {
    if (r.presetLabel == null || r.pc == null) return;
    const lines = [
      `${schema.label} - preset ${r.presetLabel}:`,
      `  ${fillTemplate(schema.instructions, { preset: r.presetLabel })}`,
      ...(r.bankMsb && schema.pc_layout
        ? [`  Channel ${r.channel}  ${schema.pc_layout.bank_msb_label} = ${r.bankMsb}`]
        : []),
      ...(schema.pc_layout
        ? [`  Channel ${r.channel}  ${schema.pc_layout.pc_label} = ${r.pc}`]
        : []),
      schema.save_note ? `  ${schema.save_note}` : "",
    ].filter(Boolean);
    navigator.clipboard.writeText(lines.join("\n")).catch(() => {});
  }
</script>

<div class="recipe">
  <header class="pageHead">
    <h2>{schema.label}</h2>
    <button onclick={refresh} disabled={busy}>{busy ? "Loading…" : "Refresh"}</button>
  </header>

  {#if schema.hint}
    <p class="hint">{schema.hint}</p>
  {/if}

  {#if rows.length === 0}
    <p class="muted">No patches yet. Create some in the Patches tab.</p>
  {/if}

  <div class="cards">
    {#each rows as r}
      <div class="card" class:warn={!r.presetLabel} class:err={r.err}>
        <div class="head">
          <span class="loc">{String(r.bank).padStart(2,"0")}/{String(r.slot).padStart(2,"0")}</span>
          <span class="name">{r.captainName}</span>
          {#if r.presetLabel}
            <span class="arrow">→</span>
            <span class="ampero">{r.presetLabel}</span>
          {:else if r.loaded}
            <span class="badge no-ref">no {schema.target_message_type} in on_enter</span>
          {/if}
          <span class="grow"></span>
          {#if r.presetLabel && r.pc !== null}
            <button class="copy" onclick={() => copyRecipe(r)}>Copy</button>
          {/if}
        </div>

        {#if r.err}
          <p class="errmsg">Couldn't load patch: {r.err}</p>
        {:else if !r.presetLabel && r.loaded}
          <p class="warnmsg">{schema.missing_message ?? "No matching message in on_enter."}</p>
        {:else if r.presetLabel && schema.pc_layout && r.pc !== null}
          <div class="recipe-body">
            <p>{fillTemplate(schema.instructions, { preset: r.presetLabel })}</p>
            <table>
              <thead><tr><th>Message</th><th>Channel</th><th>Command</th><th>Data</th></tr></thead>
              <tbody>
                {#if r.bankMsb}
                  <tr>
                    <td>1</td>
                    <td>{r.channel}</td>
                    <td>{schema.pc_layout.bank_msb_label}</td>
                    <td>{r.bankMsb}</td>
                  </tr>
                {/if}
                <tr>
                  <td>{r.bankMsb ? "2" : "1"}</td>
                  <td>{r.channel}</td>
                  <td><strong>{schema.pc_layout.pc_label}</strong></td>
                  <td><strong>{r.pc}</strong></td>
                </tr>
              </tbody>
            </table>
            {#if schema.save_note}
              <p class="save">{schema.save_note}</p>
            {/if}
          </div>
        {/if}
      </div>
    {/each}
  </div>
</div>

<style>
  .recipe { padding-bottom: 2rem; }
  .pageHead { display: flex; align-items: center; gap: 0.75rem; margin: 0 0 1rem; }
  .pageHead h2 { margin: 0; font-size: 1.05rem; font-weight: 600; color: var(--text); flex: 1; }
  .pageHead button {
    background: var(--bg-hover); color: var(--text); border: 1px solid var(--border-strong);
    padding: 0.35rem 0.7rem; border-radius: 4px; cursor: pointer; font-size: 0.8rem;
  }
  .pageHead button:disabled { opacity: 0.5; }
  .hint { color: var(--text-muted); font-size: 0.85rem; margin: 0 0 1rem; max-width: 720px; }
  .muted { color: var(--text-muted); }
  .cards { display: flex; flex-direction: column; gap: 0.65rem; }
  .card {
    background: var(--bg-card); border: 1px solid var(--border); border-radius: 6px;
    padding: 0.85rem 1rem;
  }
  .card.warn { border-color: rgba(217,155,111,0.35); }
  .card.err  { border-color: rgba(239,155,155,0.35); }
  .head { display: flex; align-items: center; gap: 0.6rem; margin-bottom: 0.5rem; }
  .head .loc {
    font-family: ui-monospace, Consolas, monospace; font-size: 0.78rem;
    color: var(--text-muted); background: var(--bg); padding: 0.15rem 0.45rem; border-radius: 3px;
  }
  .head .name { color: var(--text); font-weight: 600; }
  .head .arrow { color: var(--text-dim); }
  .head .ampero {
    font-family: ui-monospace, Consolas, monospace; color: var(--accent);
    background: var(--accent-bg); padding: 0.15rem 0.45rem; border-radius: 3px;
  }
  .head .grow { flex: 1; }
  .head .copy {
    background: var(--accent-bg); color: var(--accent); border: 1px solid var(--accent-border);
    padding: 0.3rem 0.7rem; border-radius: 3px; cursor: pointer; font-size: 0.78rem;
  }
  .head .copy:hover { background: var(--accent-hover-bg); }
  .head .badge.no-ref {
    background: rgba(217,155,111,0.10); color: var(--warn); padding: 0.15rem 0.5rem;
    border-radius: 3px; font-size: 0.7rem;
  }
  .recipe-body p { font-size: 0.85rem; color: var(--text); margin: 0 0 0.5rem; }
  .recipe-body p.save { color: var(--text-muted); font-style: italic; font-size: 0.78rem; margin-top: 0.5rem; }
  table { border-collapse: collapse; width: 100%; max-width: 540px; margin: 0.3rem 0; }
  th, td { border: 1px solid var(--border); padding: 0.4rem 0.6rem; text-align: left; font-size: 0.82rem; }
  th { background: var(--bg-hover); color: var(--text-muted); font-weight: 500; }
  td { color: var(--text); }
  td strong { color: var(--accent); }
  .errmsg { color: var(--err); font-size: 0.85rem; }
  .warnmsg { color: var(--warn); font-size: 0.82rem; }
</style>
