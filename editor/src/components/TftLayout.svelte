<script lang="ts">
  import { onMount } from "svelte";
  import { cmd, type Manifest } from "../lib/protocol";

  type LayoutEntry = {
    field?: string;
    text?: string;
    x: number;
    y: number;
    size: number;
    color: string;
    prefix?: string;
    suffix?: string;
    halign?: "left" | "center" | "right";
    valign?: "top" | "center" | "bottom";
    font?: string;
    // Marquee: when the text is wider than the screen, scroll it horizontally
    // instead of clipping. `scroll_speed` is px/second (default 40 on device).
    scroll?: boolean;
    scroll_speed?: number;
  };

  const HALIGN: Array<LayoutEntry["halign"]> = ["left", "center", "right"];
  const VALIGN: Array<LayoutEntry["valign"]> = ["top", "center", "bottom"];

  type Props = {
    device: Record<string, unknown> | null;
    manifest: Manifest | null;
    activeKind?: string;
  };
  let { device, manifest, activeKind = "" }: Props = $props();

  // Working copy of the layout. Initialized from device.tft.layout and
  // pushed back via cmd.putGlobal on Save.
  let layout = $state<LayoutEntry[]>([]);
  let saving = $state(false);
  let savedAt = $state<string>("");
  let err = $state<string>("");

  let lastFingerprint = $state("");
  $effect(() => {
    if (!device) return;
    const cur = (device.tft as any)?.layout ?? [];
    const fp = JSON.stringify(cur);
    if (fp !== lastFingerprint) {
      lastFingerprint = fp;
      layout = structuredClone($state.snapshot(cur)) as LayoutEntry[];
    }
  });

  // Fields available: always-on core fields + any plugin TFT_FIELDS.
  // The `source` is shown in the dropdown so the user can immediately see
  // whether a field is universal (core) or specific to a target device.
  type FieldOpt = { id: string; label: string; source: string };
  const CORE_FIELDS: FieldOpt[] = [
    { id: "patch_name", label: "Patch name",   source: "core" },
    { id: "bank",       label: "Captain bank", source: "core" },
    { id: "slot",       label: "Captain slot", source: "core" },
  ];
  let pluginFields = $derived.by<FieldOpt[]>(() => {
    if (!manifest) return [];
    const out: FieldOpt[] = [];
    for (const [id, plug] of Object.entries(manifest.plugins)) {
      // Only surface fields for the plugin matching the active profile.
      if (activeKind && id !== activeKind) continue;
      const pluginLabel = (plug as any).label ?? "plugin";
      const fields = (plug as any).tft_fields ?? {};
      for (const [fid, spec] of Object.entries(fields)) {
        out.push({
          id: fid,
          label: (spec as any).label ?? fid,
          source: pluginLabel,
        });
      }
    }
    return out;
  });
  let allFields = $derived<FieldOpt[]>([...CORE_FIELDS, ...pluginFields]);

  function addEntry() {
    layout = [...layout, {
      field: "patch_name",
      halign: "center", valign: "top",
      x: 0, y: 20, size: 2,
      color: "#ffffff",
      prefix: "", suffix: "",
      font: "system",
    }];
  }

  function removeEntry(i: number) {
    layout = layout.filter((_, idx) => idx !== i);
  }

  // ---- vertical pack / distribute helpers ----
  const TFT_HEIGHT = 240;
  const LINE_HEIGHT_BASE = 12;   // terminalio.FONT at scale 1; close enough for BDF too

  function labelHeight(e: LayoutEntry): number {
    return LINE_HEIGHT_BASE * Math.max(1, e.size ?? 1);
  }

  function packTop() {
    let y = 0;
    layout = layout.map(e => {
      const h = labelHeight(e);
      const next = { ...e, valign: "top" as const, y };
      y += h;
      return next;
    });
  }

  function packBottom() {
    const next: LayoutEntry[] = new Array(layout.length);
    let yBottom = TFT_HEIGHT;
    for (let i = layout.length - 1; i >= 0; i--) {
      const e = layout[i];
      const h = labelHeight(e);
      yBottom -= h;
      next[i] = { ...e, valign: "top" as const, y: yBottom };
    }
    layout = next;
  }

  function distributeEvenly() {
    const N = layout.length;
    if (N === 0) return;
    const heights = layout.map(labelHeight);
    const total = heights.reduce((s, h) => s + h, 0);
    // When labels collectively fill or overflow the screen, distribute by
    // packing them top-to-bottom with no gaps (and no overlap). Otherwise
    // spread the remaining space evenly into N+1 gaps.
    if (total >= TFT_HEIGHT) {
      let y = 0;
      layout = layout.map((e, i) => {
        const next = { ...e, valign: "top" as const, y };
        y += heights[i];
        return next;
      });
      return;
    }
    const gap = (TFT_HEIGHT - total) / (N + 1);
    let y = gap;
    layout = layout.map((e, i) => {
      const next = { ...e, valign: "top" as const, y: Math.round(y) };
      y += heights[i] + gap;
      return next;
    });
  }

  async function save() {
    if (!device) return;
    saving = true; err = "";
    try {
      const next = JSON.parse(JSON.stringify(device));
      next.tft = next.tft ?? {};
      next.tft.layout = JSON.parse(JSON.stringify(layout));
      await cmd.putGlobal(next);
      savedAt = new Date().toLocaleTimeString();
      await cmd.getGlobal();
    } catch (e) { err = String(e); }
    finally { saving = false; }
  }

  let availableFonts = $state<string[]>([]);
  onMount(async () => {
    try {
      const fr = await cmd.listFonts();
      availableFonts = fr.fonts ?? [];
    } catch {}
  });

  function resetToDefaults() {
    if (!manifest) return;
    // Prefer the plugin matching the active profile's kind. If no match,
    // fall back to the first plugin that ships a default layout.
    if (activeKind && manifest.plugins[activeKind]) {
      const def = (manifest.plugins[activeKind] as any).default_layout;
      if (def && def.length) {
        layout = JSON.parse(JSON.stringify(def));
        return;
      }
    }
    for (const plug of Object.values(manifest.plugins)) {
      const def = (plug as any).default_layout;
      if (def && def.length) {
        layout = JSON.parse(JSON.stringify(def));
        return;
      }
    }
  }

  // Preview pixel mapping: 240×240 source → preview canvas size.
  const PREV_W = 240;
  const PREV_H = 240;
  // Sample values used in the preview when context is empty. Core fields
  // (patch_name, bank, slot) stay hardcoded since they're owned by the
  // captain; plugin fields are pulled from each plugin's TFT_FIELDS map
  // in the manifest, where each entry can optionally declare `sample`.
  // Falls back to the field name in uppercase when no sample is set.
  let PREVIEW_CTX = $derived.by<Record<string, string | number>>(() => {
    const out: Record<string, string | number> = {
      patch_name: "Heavy",
      bank: 1,
      slot: 4,
    };
    if (manifest) {
      for (const [, plug] of Object.entries(manifest.plugins)) {
        const fields = (plug as { tft_fields?: Record<string, { sample?: unknown; label?: string }> }).tft_fields;
        if (!fields) continue;
        for (const [name, spec] of Object.entries(fields)) {
          if (spec.sample !== undefined) {
            out[name] = spec.sample as string | number;
          } else if (!(name in out)) {
            out[name] = name.toUpperCase();
          }
        }
      }
    }
    return out;
  });
  function previewText(e: LayoutEntry): string {
    const pfx = e.prefix ?? "";
    const sfx = e.suffix ?? "";
    if (e.field) {
      const v = PREVIEW_CTX[e.field] ?? "";
      return pfx + String(v) + sfx;
    }
    return pfx + (e.text ?? "") + sfx;
  }

  // ---- marquee preview ----
  // Mirrors the firmware (display.py): left inset, bounce with a dwell at each
  // end, px/second travel. Keyed by entry index; a positive span means the
  // label overflows and is actively scrolling, which the layout below uses to
  // re-anchor it to the left (like the device does).
  const PREV_MARGIN = 6;
  const SCROLL_DEFAULT_SPEED = 40;
  const SCROLL_PAUSE_MS = 800;
  let marqueeSpan = $state<Record<number, number>>({});

  // `size` isn't used directly but is included so the action re-measures when
  // the font scale (and thus the text width) changes.
  type MarqueeParams = { enabled: boolean; speed: number; text: string; size: number; idx: number };

  function marquee(node: HTMLElement, params: MarqueeParams) {
    let anim: Animation | null = null;
    function stop() { if (anim) { anim.cancel(); anim = null; } }
    function apply(p: MarqueeParams) {
      stop();
      node.style.transform = "translateX(0)";
      if (!p.enabled) { marqueeSpan[p.idx] = 0; return; }
      // Measure after layout so scrollWidth reflects the rendered text.
      requestAnimationFrame(() => {
        const preview = node.parentElement?.parentElement; // inner -> label -> preview
        const avail = (preview ? preview.clientWidth : PREV_W) - 2 * PREV_MARGIN;
        const span = Math.max(0, Math.ceil(node.scrollWidth - avail));
        marqueeSpan[p.idx] = span;
        if (span <= 0) return;
        const speed = p.speed > 0 ? p.speed : SCROLL_DEFAULT_SPEED;
        const travel = (span / speed) * 1000;
        const dur = 2 * (SCROLL_PAUSE_MS + travel);
        anim = node.animate(
          [
            { transform: "translateX(0)", offset: 0 },
            { transform: "translateX(0)", offset: SCROLL_PAUSE_MS / dur },
            { transform: `translateX(${-span}px)`, offset: (SCROLL_PAUSE_MS + travel) / dur },
            { transform: `translateX(${-span}px)`, offset: (2 * SCROLL_PAUSE_MS + travel) / dur },
            { transform: "translateX(0)", offset: 1 },
          ],
          { duration: dur, iterations: Infinity, easing: "linear" },
        );
      });
    }
    apply(params);
    return {
      update(p: MarqueeParams) { apply(p); },
      destroy() { stop(); },
    };
  }
</script>

<div class="tftlayout">
  <header class="pageHead">
    <h2>Screen layout</h2>
    <div class="packgroup" title="Re-position all labels vertically">
      <button onclick={packTop}    title="Pack labels at the top">↑ Top</button>
      <button onclick={distributeEvenly} title="Distribute labels evenly across the screen">⇅ Even</button>
      <button onclick={packBottom} title="Pack labels at the bottom">↓ Bottom</button>
    </div>
    <button onclick={resetToDefaults}>Reset to plugin default</button>
    <button class="primary" onclick={save} disabled={saving}>
      {saving ? "Saving…" : "Save"}
    </button>
    {#if savedAt}<span class="ok">saved at {savedAt}</span>{/if}
    {#if err}<span class="err">{err}</span>{/if}
  </header>

  <p class="hint">
    Each entry below is a label drawn on the 240×240 TFT. <code>field</code>
    pulls a live value (patch name, bank, scene…); <code>text</code> is a
    static literal. <code>prefix</code>/<code>suffix</code> wrap the value
    (e.g. prefix <code>BANK </code>).
  </p>

  <div class="cols">
    <div class="entries">
      {#each layout as e, i (i)}
        <div class="entry">
          <div class="row">
            <label>Field
              <select bind:value={e.field}>
                <option value="">- literal text -</option>
                {#each allFields as f}
                  <option value={f.id}>{f.source} · {f.label}</option>
                {/each}
              </select>
            </label>
            {#if !e.field}
              <label>Text<input bind:value={e.text} /></label>
            {/if}
            <label>Prefix<input bind:value={e.prefix} size="6" /></label>
            <label>Suffix<input bind:value={e.suffix} size="6" /></label>
          </div>
          <div class="row">
            <label>H-align
              <select bind:value={e.halign}>
                {#each HALIGN as a}<option value={a}>{a}</option>{/each}
              </select>
            </label>
            <label>V-align
              <select bind:value={e.valign}>
                {#each VALIGN as a}<option value={a}>{a}</option>{/each}
              </select>
            </label>
            <label>X offset<input type="number" min="-240" max="240" bind:value={e.x} /></label>
            <label>Y offset<input type="number" min="-240" max="240" bind:value={e.y} /></label>
            <label>Size<input type="number" min="1" max="6" bind:value={e.size} /></label>
            <label>Color<input type="color" bind:value={e.color} /></label>
            <label>Font
              <select value={e.font ?? "system"}
                      onchange={(ev) => e.font = (ev.target as HTMLSelectElement).value}>
                <option value="system">system (terminalio)</option>
                {#each availableFonts as f}<option value={f}>{f}</option>{/each}
              </select>
            </label>
            <label class="chk" title="Scroll the text horizontally when it is too wide for the screen">Scroll
              <input type="checkbox" bind:checked={e.scroll} />
            </label>
            {#if e.scroll}
              <label title="Scroll speed in pixels per second">Speed
                <input type="number" min="5" max="200" placeholder="40"
                       value={e.scroll_speed ?? ""}
                       oninput={(ev) => {
                         const v = (ev.target as HTMLInputElement).value;
                         e.scroll_speed = v === "" ? undefined : Number(v);
                       }} />
              </label>
            {/if}
            <div class="grow"></div>
            <button class="tiny danger" onclick={() => removeEntry(i)} title="Remove">×</button>
          </div>
        </div>
      {/each}
      <button class="addbtn" onclick={addEntry}>+ Add label</button>
    </div>

    <div class="previewWrap">
      <h3>Preview (240×240)</h3>
      <div class="preview" style="width: {PREV_W}px; height: {PREV_H}px;">
        {#each layout as e, i (i)}
          {@const scrolling = !!e.scroll && (marqueeSpan[i] ?? 0) > 0}
          {@const h = e.halign ?? "left"}
          {@const v = e.valign ?? "top"}
          {@const baseX = scrolling ? PREV_MARGIN : (h === "center" ? PREV_W/2 : (h === "right" ? PREV_W : 0))}
          {@const baseY = v === "center" ? PREV_H/2 : (v === "bottom" ? PREV_H : 0)}
          {@const tx = scrolling ? "0" : (h === "center" ? "-50%" : (h === "right" ? "-100%" : "0"))}
          {@const ty = v === "center" ? "-50%" : (v === "bottom" ? "-100%" : "0")}
          {@const boxH = (e.size ?? 1) * 12}
          {@const offX = scrolling ? 0 : e.x}
          <div class="prevlabel"
               style="left:{baseX + offX}px; top:{baseY + e.y}px;
                      transform: translate({tx}, {ty});
                      color:{e.color}; font-size:{e.size * 9}px;
                      height:{boxH}px; line-height:{boxH}px;">
            <span class="marqueeInner"
                  use:marquee={{ enabled: !!e.scroll, speed: e.scroll_speed ?? SCROLL_DEFAULT_SPEED,
                                 text: previewText(e), size: e.size, idx: i }}>{previewText(e)}</span>
          </div>
        {/each}
      </div>
      <p class="prevhint">Preview uses sample data - patch name "Heavy",
        Ampero preset "P01-4", scene 3.</p>
    </div>
  </div>
</div>

<style>
  .tftlayout { padding-bottom: 2rem; }
  .pageHead { display: flex; align-items: center; gap: 0.5rem; margin: 0 0 0.5rem; }
  .pageHead h2 { margin: 0; flex: 1; font-size: 1.05rem; font-weight: 600; }
  .pageHead button {
    background: var(--bg-hover); color: var(--text); border: 1px solid var(--border-strong);
    padding: 0.35rem 0.7rem; border-radius: 4px; cursor: pointer; font-size: 0.78rem;
  }
  .pageHead button.primary { background: var(--accent-bg); color: var(--accent); border-color: var(--accent-border); font-weight: 600; }
  .packgroup { display: flex; gap: 0.25rem; padding-right: 0.4rem; border-right: 1px solid var(--border); margin-right: 0.4rem; }
  .packgroup button { font-size: 0.75rem; padding: 0.32rem 0.55rem; }
  .pageHead .ok  { color: var(--accent); font-size: 0.8rem; }
  .pageHead .err { color: var(--err); font-size: 0.8rem; }
  .hint { color: var(--text-muted); font-size: 0.82rem; margin: 0 0 1rem; max-width: 800px; }
  .hint code { background: var(--bg); padding: 0.05rem 0.35rem; border-radius: 3px; color: var(--warn-text); }

  .cols { display: flex; gap: 1.5rem; align-items: flex-start; }
  .entries { flex: 1; display: flex; flex-direction: column; gap: 0.45rem; }

  .entry { background: var(--bg-card); border: 1px solid var(--border); border-radius: 4px; padding: 0.5rem 0.7rem; }
  .row { display: flex; gap: 0.55rem; align-items: end; margin-bottom: 0.4rem; flex-wrap: wrap; }
  .row:last-child { margin-bottom: 0; }
  label { display: flex; flex-direction: column; gap: 0.15rem; font-size: 0.7rem; color: var(--text-muted); }
  label.chk { align-items: center; }
  label.chk input[type="checkbox"] { width: 16px; height: 16px; margin-top: 0.15rem; }
  input, select { background: var(--bg); color: var(--text); border: 1px solid var(--border-strong);
                  padding: 0.3rem 0.45rem; border-radius: 3px; font-size: 0.82rem; }
  input[type="number"] { width: 4.5rem; }
  input[type="color"]  { padding: 0; width: 40px; height: 28px; }
  .grow { flex: 1; }
  button.tiny { background: var(--bg-hover); color: var(--text); border: 1px solid var(--border-strong);
                padding: 0.2rem 0.5rem; border-radius: 3px; cursor: pointer; font-size: 0.78rem; }
  button.tiny:disabled { opacity: 0.35; cursor: not-allowed; }
  button.tiny.danger { background: rgba(239,155,155,0.08); color: var(--err); border-color: rgba(239,155,155,0.35); }

  .addbtn { background: var(--accent-bg); color: var(--accent); border: 1px solid var(--accent-border);
            padding: 0.4rem; border-radius: 4px; cursor: pointer; font-size: 0.82rem; }
  .addbtn:hover { background: var(--accent-hover-bg); }

  .previewWrap { flex-shrink: 0; }
  .previewWrap h3 { color: var(--accent); font-size: 0.72rem; text-transform: uppercase; letter-spacing: 0.06em; margin: 0 0 0.4rem; }
  .preview {
    background: #000; border: 1px solid var(--border); border-radius: 4px;
    position: relative; font-family: ui-monospace, Consolas, monospace; overflow: hidden;
  }
  .prevlabel {
    position: absolute; white-space: nowrap; line-height: 1; font-weight: 500;
    /* Dashed outline so the user can see each label's bounding box and spot
       overlaps. Outline doesn't affect layout, so positioning stays exact. */
    outline: 1px dashed rgba(255, 255, 255, 0.25);
    outline-offset: 0;
  }
  .marqueeInner { display: inline-block; white-space: nowrap; will-change: transform; }
  .prevhint { color: var(--text-dim); font-size: 0.72rem; margin: 0.4rem 0 0; max-width: 240px; }
</style>
