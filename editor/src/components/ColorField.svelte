<script lang="ts">
  // Reusable color picker: a row of ten easily-clickable base colors plus the
  // native OS picker for any custom shade. Used everywhere the editor edits a
  // color (TFT label + theme, switch LEDs, profile chips) so the base palette
  // is consistent app-wide.
  //
  // Works with both wiring styles found in the app:
  //   - two-way:  <ColorField bind:value={x} />              (+ optional onchange)
  //   - one-way:  <ColorField value={x} onchange={hex => ...} />  (writeback in
  //               the handler; used where a raw bind would push a default back
  //               into the model on mere render, e.g. the latched LED-off color)
  type Props = {
    value?: string;
    title?: string;
    onchange?: (hex: string) => void;
  };
  let { value = $bindable("#ffffff"), title = "", onchange }: Props = $props();

  // Ten base colors: white, the six primaries/secondaries, purple, magenta and
  // black (LED off). Vivid and distinct on the black TFT and the RGB LEDs.
  const BASE_COLORS = [
    "#ffffff", "#ff0000", "#ff7f00", "#ffff00", "#00ff00",
    "#00ffff", "#0000ff", "#ff00ff", "#8000ff", "#000000",
  ];

  const norm = (c: string) => (c ?? "").toLowerCase();

  function pick(hex: string) {
    value = hex;
    onchange?.(hex);
  }
</script>

<div class="colorfield">
  <div class="swatches" role="group" aria-label="Base colors">
    {#each BASE_COLORS as c (c)}
      <button
        type="button"
        class="swatch"
        class:sel={norm(value) === c}
        style="background:{c};"
        title={c}
        aria-label={c}
        aria-pressed={norm(value) === c}
        onclick={() => pick(c)}
      ></button>
    {/each}
  </div>
  <input
    type="color"
    value={value}
    {title}
    aria-label="Custom color"
    oninput={(e) => pick((e.target as HTMLInputElement).value)}
  />
</div>

<style>
  .colorfield { display: inline-flex; align-items: center; gap: 0.4rem; flex-wrap: wrap; }
  .swatches { display: inline-flex; gap: 3px; flex-wrap: wrap; }
  .swatch {
    width: 18px; height: 18px; padding: 0; border-radius: 4px; cursor: pointer;
    border: 1px solid var(--border-strong);
    /* Inner ring so pure white/black swatches stay visible on either theme. */
    box-shadow: inset 0 0 0 1px rgba(128, 128, 128, 0.35);
  }
  .swatch.sel { outline: 2px solid var(--accent); outline-offset: 1px; }
  .colorfield input[type="color"] {
    padding: 0; width: 34px; height: 26px; cursor: pointer;
    background: var(--bg); border: 1px solid var(--border-strong); border-radius: 4px;
  }
</style>
