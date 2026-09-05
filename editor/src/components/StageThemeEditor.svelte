<script lang="ts">
  // Overlay panel for customizing Stage View's per-section fonts/colors/
  // sizes. Opened from the gear icon in StageView.svelte; edits apply live
  // (StageView re-derives its CSS vars from `theme` on every change) and
  // are persisted by the caller via saveStageTheme.
  import ColorField from "./ColorField.svelte";
  import {
    STAGE_SECTIONS,
    FONT_STACKS,
    MIN_SECTION_SCALE,
    MAX_SECTION_SCALE,
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

  const SECTION_LABELS: Record<StageSection, string> = {
    rigName: "Rig name",
    bank: "Bank / Rig",
    bpm: "BPM",
    tuner: "Tuner",
    switchLabel: "Switch label",
    switchId: "Switch ID",
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
  <div class="theme-panel__header">
    <h2>Stage appearance</h2>
    <button type="button" class="theme-panel__close" onclick={onclose} aria-label="Close appearance panel">✕</button>
  </div>

  <label class="theme-panel__global">
    <span>Default font</span>
    <select value={theme.fontFamily ?? ""} onchange={(e) => setGlobalFont((e.target as HTMLSelectElement).value)}>
      <option value="">Inter (default)</option>
      {#each Object.entries(FONT_STACKS) as [name, stack]}
        <option value={stack}>{name}</option>
      {/each}
    </select>
  </label>

  <div class="theme-panel__sections">
    {#each STAGE_SECTIONS as section (section)}
      {@const s = theme.sections[section] ?? {}}
      <div class="theme-panel__row">
        <span class="theme-panel__label">{SECTION_LABELS[section]}</span>

        <select
          class="theme-panel__font"
          value={s.fontFamily ?? ""}
          onchange={(e) => {
            const v = (e.target as HTMLSelectElement).value;
            if (v) updateSection(section, { fontFamily: v });
            else clearSectionFont(section);
          }}
        >
          <option value="">Default</option>
          {#each Object.entries(FONT_STACKS) as [name, stack]}
            <option value={stack}>{name}</option>
          {/each}
        </select>

        <ColorField
          value={s.color ?? DEFAULT_SECTION_COLOR[section]}
          title={`${SECTION_LABELS[section]} color`}
          onchange={(hex) => updateSection(section, { color: hex })}
        />

        <input
          type="range"
          class="theme-panel__scale"
          min={MIN_SECTION_SCALE}
          max={MAX_SECTION_SCALE}
          step="0.05"
          value={s.scale ?? 1}
          oninput={(e) => updateSection(section, { scale: clampSectionScale(parseFloat((e.target as HTMLInputElement).value)) })}
          aria-label={`${SECTION_LABELS[section]} size`}
        />
        <span class="theme-panel__scale-value">{Math.round((s.scale ?? 1) * 100)}%</span>

        <button type="button" class="theme-panel__reset" onclick={() => onchange(resetSection(theme, section))}>Reset</button>
      </div>
    {/each}
  </div>

  <div class="theme-panel__footer">
    <button type="button" class="theme-panel__reset-all" onclick={() => onchange(resetAllStageTheme())}>Reset all</button>
  </div>
</div>

<style>
  .theme-panel {
    position: absolute; inset: 0; z-index: 20;
    background: var(--overlay-bg);
    display: flex; align-items: center; justify-content: center;
    padding: 1rem;
    font-family: "Inter", -apple-system, sans-serif;
  }
  .theme-panel__header, .theme-panel__global, .theme-panel__sections, .theme-panel__footer {
    background: var(--bg-elevated);
  }
  .theme-panel > * {
    width: 100%; max-width: 640px;
  }
  .theme-panel {
    flex-direction: column;
    gap: 0;
  }
  .theme-panel__header {
    display: flex; align-items: center; justify-content: space-between;
    padding: 0.9rem 1.1rem;
    border: 1px solid var(--border); border-bottom: none;
    border-radius: 10px 10px 0 0;
  }
  .theme-panel__header h2 { margin: 0; font-size: 1rem; color: var(--text); }
  .theme-panel__close {
    background: none; border: none; color: var(--text-muted); cursor: pointer;
    font-size: 1.1rem; line-height: 1; padding: 0.2rem 0.4rem;
  }
  .theme-panel__close:hover { color: var(--text); }

  .theme-panel__global {
    display: flex; align-items: center; justify-content: space-between;
    padding: 0.7rem 1.1rem;
    border: 1px solid var(--border); border-top: none;
    color: var(--text-soft); font-size: 0.85rem;
  }

  .theme-panel__sections {
    border: 1px solid var(--border); border-top: none;
    max-height: 55vh; overflow-y: auto;
  }
  .theme-panel__row {
    display: flex; align-items: center; gap: 0.6rem;
    padding: 0.6rem 1.1rem;
    border-top: 1px solid var(--border);
  }
  .theme-panel__row:first-child { border-top: none; }
  .theme-panel__label {
    flex: 0 0 7rem;
    color: var(--text); font-size: 0.85rem; font-weight: 600;
  }
  .theme-panel__font {
    flex: 1 1 8rem; min-width: 0;
    background: var(--bg-input); color: var(--text);
    border: 1px solid var(--border-strong); border-radius: 6px;
    padding: 0.3rem 0.4rem; font-size: 0.8rem;
  }
  .theme-panel__scale {
    flex: 1 1 6rem; min-width: 3rem;
  }
  .theme-panel__scale-value {
    flex: 0 0 3rem; text-align: right;
    color: var(--text-muted); font-size: 0.78rem;
  }
  .theme-panel__reset {
    flex: 0 0 auto;
    background: none; border: 1px solid var(--border-strong); border-radius: 6px;
    color: var(--text-muted); cursor: pointer;
    padding: 0.25rem 0.55rem; font-size: 0.75rem;
  }
  .theme-panel__reset:hover { color: var(--text); border-color: var(--border-stronger); }

  .theme-panel__footer {
    display: flex; justify-content: flex-end;
    padding: 0.7rem 1.1rem;
    border: 1px solid var(--border); border-top: none;
    border-radius: 0 0 10px 10px;
  }
  .theme-panel__reset-all {
    background: var(--warn-bg); border: 1px solid var(--warn); color: var(--warn-text);
    border-radius: 6px; cursor: pointer; padding: 0.35rem 0.8rem; font-size: 0.8rem;
  }
</style>
