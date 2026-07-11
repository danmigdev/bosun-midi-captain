<script lang="ts">
  import {
    summarizeMessage,
    type Binding,
    type Manifest,
    type MessageSchema,
    type MidiMessage,
  } from "../lib/protocol";
  import { RECIPES, type Recipe, type RecipeCtx } from "../lib/recipes";

  type Props = {
    switches: string[];
    manifest: Manifest;
    activeKind: string;
    existing?: Binding[];
    onApply: (bindings: Binding[]) => void;
  };
  let { switches, manifest, activeKind, existing = [], onApply }: Props = $props();

  let ctx = $derived<RecipeCtx>({ activeKind, manifest });

  // Recipes offered for the current context.
  let visibleRecipes = $derived(RECIPES.filter((r) => r.available(ctx)));

  // Which recipe card is expanded (by id), and the role->switch assignment
  // per recipe. Assignments are kept keyed by recipe id so switching cards
  // doesn't lose a partial selection.
  let expandedId = $state<string | null>(null);
  let assignments = $state<Record<string, Record<string, string>>>({});

  // Pure read: returns the current assignment for a recipe, or an empty object
  // when none exists yet. It must NOT mutate `assignments` - this runs inside a
  // {@const} during render, and writing to reactive state there trips Svelte's
  // unsafe-mutation guard and aborts the whole page branch from mounting.
  // The entry is created lazily by setRole when the user actually picks a switch.
  function assignFor(id: string): Record<string, string> {
    return assignments[id] ?? {};
  }

  function toggle(id: string) {
    expandedId = expandedId === id ? null : id;
  }

  function setRole(recipeId: string, roleKey: string, value: string) {
    const a = { ...assignFor(recipeId) };
    if (value) a[roleKey] = value;
    else delete a[roleKey];
    assignments = { ...assignments, [recipeId]: a };
  }

  /** Set of switch names that already carry a binding, for the warning marker. */
  let usedSwitches = $derived(new Set(existing.map((b) => b.switch)));

  /** Look up a message's schema (core or active plugin) so summarizeMessage
   * can render its template rather than falling back to raw JSON. */
  function schemaFor(type: string): MessageSchema | undefined {
    return (
      manifest.core_messages?.[type] ??
      manifest.plugins?.[activeKind]?.messages?.[type]
    );
  }

  function summarize(msg: MidiMessage): string {
    return summarizeMessage(msg, schemaFor(msg.type));
  }

  /** Are all required roles for this recipe assigned? */
  function complete(recipe: Recipe): boolean {
    const a = assignFor(recipe.id);
    return recipe.roles.every((role) => role.optional || !!a[role.key]);
  }

  /** Flatten the recipe's would-be bindings into rows of (switch, summary)
   * for the live preview. */
  function previewRows(recipe: Recipe): Array<{ sw: string; label: string; text: string }> {
    let bindings: Binding[] = [];
    try {
      bindings = recipe.build(assignFor(recipe.id), ctx);
    } catch {
      return [];
    }
    const rows: Array<{ sw: string; label: string; text: string }> = [];
    for (const b of bindings) {
      for (const action of Object.values(b.actions)) {
        for (const msg of action.messages) {
          rows.push({ sw: b.switch, label: b.label ?? "", text: summarize(msg) });
        }
      }
    }
    return rows;
  }

  function apply(recipe: Recipe) {
    if (!complete(recipe)) return;
    onApply(recipe.build(assignFor(recipe.id), ctx));
  }
</script>

<div class="quicksetup">
  {#if visibleRecipes.length === 0}
    <p class="muted">No guided setups available for this profile.</p>
  {/if}

  {#each visibleRecipes as recipe (recipe.id)}
    {@const open = expandedId === recipe.id}
    {@const assign = assignFor(recipe.id)}
    <section class="card" class:open>
      <button class="cardhead" onclick={() => toggle(recipe.id)} type="button">
        {#if recipe.icon}<span class="icon">{recipe.icon}</span>{/if}
        <span class="titles">
          <span class="label">{recipe.label}</span>
          <span class="desc">{recipe.description}</span>
        </span>
        <span class="chevron">{open ? "▾" : "▸"}</span>
      </button>

      {#if open}
        <div class="body">
          <div class="grid">
            {#each recipe.roles as role (role.key)}
              {@const chosen = assign[role.key] ?? ""}
              {@const clash = chosen !== "" && usedSwitches.has(chosen)}
              <label>
                <span class="rolelabel">
                  {role.label}
                  {#if role.optional}<span class="opt">(optional)</span>{/if}
                  {#if clash}<span class="warn" title="This switch already has a binding">⚠ in use</span>{/if}
                </span>
                <select
                  value={chosen}
                  onchange={(e) => setRole(recipe.id, role.key, e.currentTarget.value)}
                >
                  <option value="">- none -</option>
                  {#each switches as s}
                    <option value={s}>{s}{usedSwitches.has(s) ? " (in use)" : ""}</option>
                  {/each}
                </select>
                {#if role.hint}<span class="hint small">{role.hint}</span>{/if}
              </label>
            {/each}
          </div>

          {#if previewRows(recipe).length > 0}
            {@const rows = previewRows(recipe)}
            <div class="preview">
              <div class="previewhead">Preview</div>
              <ul>
                {#each rows as row}
                  <li>
                    <span class="sw">{row.sw}</span>
                    {#if row.label}<span class="rowlabel">{row.label}</span>{/if}
                    <span class="msg">{row.text}</span>
                  </li>
                {/each}
              </ul>
            </div>
          {/if}

          <div class="actions">
            <button
              class="primary"
              type="button"
              disabled={!complete(recipe)}
              onclick={() => apply(recipe)}
            >
              Apply
            </button>
            {#if !complete(recipe)}
              <span class="hint small">Assign all required switches to enable Apply.</span>
            {/if}
          </div>
        </div>
      {/if}
    </section>
  {/each}
</div>

<style>
  .quicksetup { display: flex; flex-direction: column; gap: 0.6rem; }
  .card {
    background: var(--bg-card);
    border: 1px solid var(--border);
    border-radius: 6px;
    overflow: hidden;
  }
  .card.open { border-color: var(--border-strong); }
  .cardhead {
    display: flex;
    align-items: center;
    gap: 0.7rem;
    width: 100%;
    text-align: left;
    background: transparent;
    border: none;
    color: var(--text);
    padding: 0.8rem 1rem;
    cursor: pointer;
    font: inherit;
  }
  .cardhead:hover { background: var(--bg-elevated); }
  .icon { font-size: 1.1rem; line-height: 1; }
  .titles { display: flex; flex-direction: column; gap: 0.15rem; flex: 1; min-width: 0; }
  .label { font-weight: 600; font-size: 0.9rem; color: var(--accent); }
  .desc { font-size: 0.78rem; color: var(--text-muted); }
  .chevron { color: var(--text-muted); font-size: 0.85rem; }

  .body {
    padding: 0.4rem 1rem 0.9rem;
    border-top: 1px solid var(--border);
    display: flex;
    flex-direction: column;
    gap: 0.7rem;
  }
  .grid {
    display: grid;
    grid-template-columns: repeat(auto-fit, minmax(200px, 1fr));
    gap: 0.6rem;
    margin-top: 0.5rem;
  }
  label {
    display: flex;
    flex-direction: column;
    gap: 0.2rem;
    font-size: 0.75rem;
    color: var(--text-muted);
  }
  .rolelabel { display: flex; align-items: center; gap: 0.4rem; }
  .opt { color: var(--text-muted); font-weight: 400; }
  .warn { color: var(--err); font-size: 0.72rem; }
  select {
    background: var(--bg);
    color: var(--text);
    border: 1px solid var(--border-strong);
    padding: 0.35rem 0.5rem;
    border-radius: 3px;
    font-size: 0.85rem;
  }

  .preview {
    background: var(--bg);
    border: 1px solid var(--border);
    border-radius: 4px;
    padding: 0.5rem 0.7rem;
  }
  .previewhead {
    color: var(--accent);
    font-size: 0.7rem;
    text-transform: uppercase;
    letter-spacing: 0.05em;
    font-weight: 600;
    margin-bottom: 0.35rem;
  }
  .preview ul { list-style: none; margin: 0; padding: 0; display: flex; flex-direction: column; gap: 0.25rem; }
  .preview li { display: flex; align-items: center; gap: 0.5rem; font-size: 0.8rem; }
  .sw {
    display: inline-block;
    min-width: 2.2rem;
    text-align: center;
    background: var(--accent-bg);
    color: var(--accent);
    border: 1px solid var(--accent-border);
    border-radius: 3px;
    padding: 0.05rem 0.3rem;
    font-size: 0.72rem;
    font-weight: 600;
  }
  .rowlabel { color: var(--text-muted); font-size: 0.75rem; }
  .msg { color: var(--text); font-family: ui-monospace, monospace; font-size: 0.78rem; }

  .actions { display: flex; align-items: center; gap: 0.75rem; }
  .primary {
    background: var(--accent-bg);
    color: var(--accent);
    border: 1px solid var(--accent-border);
    padding: 0.45rem 1.2rem;
    border-radius: 4px;
    font-weight: 600;
    cursor: pointer;
    font-size: 0.85rem;
  }
  .primary:hover:not(:disabled) { background: var(--accent-hover-bg); }
  .primary:disabled { opacity: 0.45; cursor: not-allowed; }
  .muted { color: var(--text-muted); }
  .hint { color: var(--text-muted); font-size: 0.8rem; margin: 0; }
  .hint.small { font-size: 0.72rem; }
</style>
