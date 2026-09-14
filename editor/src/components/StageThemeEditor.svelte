<script lang="ts">
  // Overlay panel for customizing Stage View's per-section fonts/colors/
  // sizes. Opened from the gear icon in StageView.svelte; edits apply live
  // (StageView re-derives its CSS vars from `theme` on every change) and
  // are persisted by the caller via saveStageTheme.
  import ColorField from "./ColorField.svelte";
  import StageSelect from "./StageSelect.svelte";
  import {
    STAGE_SECTIONS,
    FONT_STACKS,
    MIN_SECTION_SCALE,
    MAX_SECTION_SCALE,
    DEFAULT_SECTION_SCALE,
    MIN_CORNER_SCALE,
    MAX_CORNER_SCALE,
    DEFAULT_CORNER_SCALE,
    clampCornerScale,
    resetStageCorners,
    clampSectionScale,
    resetSection,
    resetAllStageTheme,
    type StageSection,
    type StageTheme,
  } from "../lib/stage-theme";

  type Props = {
    theme: StageTheme;
    onchange: (theme: StageTheme) => void;
    onclose: () => void;
  };
  let { theme, onchange, onclose }: Props = $props();

  const fontOptions = Object.entries(FONT_STACKS).map(([label, value]) => ({ label, value }));
  const globalFontOptions = [{ value: "", label: "Inter (default)" }, ...fontOptions];
  const sectionFontOptions = [{ value: "", label: "Default" }, ...fontOptions];

  const SECTION_LABELS: Record<StageSection, string> = {
    rigName: "Rig name",
    bank: "Bank / Rig",
    bpm: "BPM",
    tuner: "Tuner",
    switchLabel: "Switch label",
    switchId: "Switch ID",
    expression: "VOL / WAH",
  };

  // Starting point shown in the color picker when a section has no
  // override yet - matches StageView's current hardcoded defaults. Purely
  // cosmetic: "Reset" removes the override outright, reverting to the
  // theme-adaptive var(--text)/var(--text-dim) or hardcoded default.
  const DEFAULT_SECTION_COLOR: Record<StageSection, string> = {
    rigName: "#ffffff",
    bank: "#ffffff",
    bpm: "#e4e6eb",
    tuner: "#4ade80",
    switchLabel: "#ffffff",
    switchId: "#6a7280",
    expression: "#ffffff",
  };

  function updateSection(section: StageSection, patch: Partial<{ fontFamily: string; color: string; scale: number }>) {
    const current = theme.sections[section] ?? {};
    onchange({ ...theme, sections: { ...theme.sections, [section]: { ...current, ...patch } } });
  }

  function clearSectionFont(section: StageSection) {
    const current = { ...(theme.sections[section] ?? {}) };
    delete current.fontFamily;
    onchange({ ...theme, sections: { ...theme.sections, [section]: current } });
  }

  function setGlobalFont(value: string) {
    onchange({ ...theme, fontFamily: value || undefined });
  }
</script>

<div class="theme-panel" role="dialog" aria-label="Stage appearance">
  <div class="theme-panel__sheet">
  <div class="theme-panel__header">
    <h2>Stage appearance</h2>
    <button type="button" class="theme-panel__close stage-control-icon stage-control-icon--dialog stage-control-icon--close" onclick={onclose} aria-label="Close appearance panel">
      <svg viewBox="0 0 24 24" aria-hidden="true"><path d="m6 6 12 12M18 6 6 18" /></svg>
    </button>
  </div>

  <div class="theme-panel__body">
  <div class="theme-panel__global">
    <span>Default font</span>
    <div class="theme-panel__font-control">
      <StageSelect id="stage-font-default" label="Default font"
        value={theme.fontFamily ?? ""} options={globalFontOptions} onchange={setGlobalFont} />
    </div>
  </div>

  <div class="theme-panel__row theme-panel__corners" role="group" aria-label="Rounded corners">
    <div class="theme-panel__row-header">
      <h3 class="theme-panel__label">Rounded corners</h3>
      <button type="button" class="theme-panel__reset stage-control-button" aria-label="Reset corners"
        onclick={() => onchange(resetStageCorners(theme))}>Reset</button>
    </div>
    <p class="theme-panel__hint">Screen, bottom outer switch corners and top-right X. 0%: square. Default: 75%.</p>
    <label class="theme-panel__field">
      <span class="theme-panel__scale-label">Corners <span class="theme-panel__scale-value">{Math.round((theme.corners ?? DEFAULT_CORNER_SCALE) * 100)}%</span></span>
      <input type="range" class="theme-panel__scale"
        min={MIN_CORNER_SCALE} max={MAX_CORNER_SCALE} step="0.05"
        value={theme.corners ?? DEFAULT_CORNER_SCALE}
        aria-label="Corners"
        oninput={(e) => onchange({ ...theme,
          corners: clampCornerScale(parseFloat((e.target as HTMLInputElement).value)),
        })} />
    </label>
  </div>

  <div class="theme-panel__sections">
    {#each STAGE_SECTIONS as section (section)}
      {@const s = theme.sections[section] ?? {}}
      <div class="theme-panel__row" role="group" aria-label={`${SECTION_LABELS[section]} appearance`}>
        <div class="theme-panel__row-header">
          <h3 class="theme-panel__label">{SECTION_LABELS[section]}</h3>
          <button type="button" class="theme-panel__reset stage-control-button" onclick={() => onchange(resetSection(theme, section))}>Reset</button>
        </div>

        <div class="theme-panel__field">
        <span>Font</span>
        <div class="theme-panel__font-control">
          <StageSelect id={`stage-font-${section}`} label={`${SECTION_LABELS[section]} font`}
            value={s.fontFamily ?? ""} options={sectionFontOptions}
            onchange={(value) => {
              if (value) updateSection(section, { fontFamily: value });
              else clearSectionFont(section);
            }} />
        </div>
        </div>

        <div class="theme-panel__color">
        <span class="theme-panel__field-label">Color</span>
        <ColorField
          value={s.color ?? DEFAULT_SECTION_COLOR[section]}
          title={`${SECTION_LABELS[section]} color`}
          onchange={(hex) => updateSection(section, { color: hex })}
        />
        </div>

        <label class="theme-panel__field">
        <span class="theme-panel__scale-label">Size <span class="theme-panel__scale-value">{Math.round((s.scale ?? DEFAULT_SECTION_SCALE) * 100)}%</span></span>
        <input
          type="range"
          class="theme-panel__scale"
          min={MIN_SECTION_SCALE}
          max={MAX_SECTION_SCALE}
          step="0.05"
          value={s.scale ?? DEFAULT_SECTION_SCALE}
          oninput={(e) => updateSection(section, { scale: clampSectionScale(parseFloat((e.target as HTMLInputElement).value)) })}
          aria-label={`${SECTION_LABELS[section]} size`}
        />
        </label>
      </div>
    {/each}
  </div>
  </div>

  <div class="theme-panel__footer">
    <button type="button" class="theme-panel__reset-all stage-control-button" onclick={() => onchange(resetAllStageTheme())}>Reset all</button>
  </div>
  </div>
</div>

<style>
  .theme-panel {
    position: absolute; inset: 0; z-index: 20;
    background: var(--overlay-bg, rgba(0, 0, 0, 0.55));
    display: flex; align-items: center; justify-content: center;
    padding: clamp(0.5rem, 2vw, 1.5rem);
    box-sizing: border-box; overflow: hidden;
    font-family: var(--stage-control-font, "Inter", -apple-system, sans-serif);
  }
  .theme-panel__sheet {
    display: flex; flex-direction: column;
    width: 100%; max-width: 960px; max-height: 100%; min-height: 0;
    overflow: hidden;
    background: var(--bg-elevated, #181b21);
    border: 1px solid var(--stage-control-border, #344752); border-radius: var(--stage-control-radius, 3px);
    box-shadow: 0 12px 48px #0005;
  }
  .theme-panel__header {
    display: flex; align-items: center; justify-content: space-between;
    gap: 1rem; flex: 0 0 auto;
    padding: 0.75rem 1rem;
    border-bottom: 1px solid var(--border, #2a2e36);
  }
  .theme-panel__header h2 { margin: 0; font-size: 1.15rem; color: var(--text, #e4e6eb); }
  .theme-panel__body {
    min-height: 0; overflow-y: auto; overscroll-behavior: contain;
    padding: 1rem; scrollbar-gutter: stable;
  }
  .theme-panel__global {
    display: grid; gap: 0.5rem; margin-bottom: 1.25rem;
    color: var(--text, #e4e6eb); font-size: 1rem; font-weight: 600;
  }
  .theme-panel__sections {
    display: grid; grid-template-columns: repeat(2, minmax(0, 1fr)); gap: 1rem;
  }
  .theme-panel__corners { margin-bottom: 1rem; }
  .theme-panel__hint {
    margin: 0; color: var(--text-soft, #c6cad2); font-size: 0.9rem;
  }
  .theme-panel__row {
    display: grid; align-content: start; gap: 1rem; min-width: 0;
    padding: 1rem;
    border: 1px solid var(--stage-control-border, #344752); border-radius: var(--stage-control-radius, 3px);
    background: var(--bg, #14161b);
  }
  .theme-panel__row-header {
    display: flex; align-items: center; justify-content: space-between; gap: 1rem;
  }
  .theme-panel__label {
    margin: 0; color: var(--text, #e4e6eb); font-size: 1rem; font-weight: 600;
  }
  .theme-panel__field, .theme-panel__color {
    display: grid; gap: 0.5rem; min-width: 0;
    color: var(--text-soft, #c6cad2); font-size: 0.9rem;
  }
  .theme-panel__font-control {
    min-width: 0;
  }
  .theme-panel__scale-label {
    display: flex; align-items: center; justify-content: space-between; gap: 0.75rem;
  }
  .theme-panel__scale-value {
    flex-shrink: 0; color: var(--text, #e4e6eb); font-size: 1rem; font-variant-numeric: tabular-nums;
  }
  .theme-panel__scale {
    appearance: none; width: 100%; min-width: 0; height: 48px; margin: 0;
    background: transparent; cursor: pointer; accent-color: var(--accent, #6fd99b);
  }
  .theme-panel__scale::-webkit-slider-runnable-track {
    height: 8px; border-radius: 4px; background: var(--border-strong, #3a414c);
  }
  .theme-panel__scale::-webkit-slider-thumb {
    appearance: none; width: 32px; height: 32px; margin-top: -12px;
    border: 2px solid var(--text, #e4e6eb); border-radius: 50%; background: var(--accent, #6fd99b);
  }
  .theme-panel__scale::-moz-range-track {
    height: 8px; border-radius: 4px; background: var(--border-strong, #3a414c);
  }
  .theme-panel__scale::-moz-range-thumb {
    width: 28px; height: 28px; border: 2px solid var(--text, #e4e6eb);
    border-radius: 50%; background: var(--accent, #6fd99b);
  }
  .theme-panel__color :global(.colorfield) {
    display: grid; grid-template-columns: minmax(0, 1fr) 48px;
    align-items: start; gap: 0.75rem; min-width: 0;
  }
  .theme-panel__color :global(.swatches) {
    display: grid; grid-template-columns: repeat(auto-fit, minmax(48px, 1fr)); gap: 8px;
  }
  .theme-panel__color :global(.swatch) {
    width: 100%; min-width: 48px; height: 48px; border-radius: var(--stage-control-radius, 3px);
    border-color: var(--stage-control-border, #344752);
  }
  .theme-panel__color :global(.swatch.sel) {
    outline-color: var(--accent, #6fd99b);
  }
  .theme-panel__color :global(input[type="color"]) {
    width: 48px; height: 48px; padding: 3px; box-sizing: border-box; border-radius: var(--stage-control-radius, 3px);
    background: var(--stage-control-background, #111b23); border-color: var(--stage-control-border, #344752);
  }
  .theme-panel :global(button), .theme-panel input {
    touch-action: manipulation;
  }
  .theme-panel input:focus-visible, .theme-panel__color :global(input:focus-visible) {
    outline: 2px solid var(--stage-control-accent, #baff3f); outline-offset: -4px;
  }
  .theme-panel__reset {
    flex: 0 0 auto; min-width: 4.5em;
  }
  .theme-panel__footer {
    display: flex; justify-content: flex-end; flex: 0 0 auto;
    padding: 0.75rem 1rem; border-top: 1px solid var(--border, #2a2e36);
  }
  .theme-panel__reset-all {
    min-width: 6.25em;
  }
  @media (max-width: 680px) {
    .theme-panel__sections { grid-template-columns: minmax(0, 1fr); }
  }
  @media (max-width: 420px), (max-height: 420px) {
    .theme-panel__header, .theme-panel__footer { padding: 0.5rem 0.75rem; }
    .theme-panel__body { padding: 0.75rem; }
    .theme-panel__row { padding: 0.75rem; }
  }
</style>
