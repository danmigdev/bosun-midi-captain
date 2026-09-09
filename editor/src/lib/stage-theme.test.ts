import { describe, it, expect, beforeEach } from "vitest";

import {
  MIN_SECTION_SCALE, MAX_SECTION_SCALE, DEFAULT_SECTION_SCALE,
  clampSectionScale, readSavedStageTheme, saveStageTheme,
  resetSection, resetAllStageTheme, stageThemeToCssVars,
  type StageTheme,
} from "./stage-theme";

beforeEach(() => {
  localStorage.clear();
});

describe("clampSectionScale", () => {
  it("clamps below MIN to MIN", () => {
    expect(clampSectionScale(0.01)).toBe(MIN_SECTION_SCALE);
    expect(clampSectionScale(-5)).toBe(MIN_SECTION_SCALE);
  });

  it("clamps above MAX to MAX", () => {
    expect(clampSectionScale(99)).toBe(MAX_SECTION_SCALE);
  });

  it("rounds to 2 decimals to avoid floating-point drift", () => {
    expect(clampSectionScale(1.0500000000000003)).toBe(1.05);
  });

  it("accepts the full 10% to 500% range", () => {
    expect(clampSectionScale(0.1)).toBe(0.1);
    expect(clampSectionScale(5)).toBe(5);
  });

  it("falls back to the 100% default for non-finite input", () => {
    expect(DEFAULT_SECTION_SCALE).toBe(1);
    expect(clampSectionScale(NaN)).toBe(1);
    expect(clampSectionScale(Infinity)).toBe(1);
  });
});

describe("readSavedStageTheme", () => {
  it("returns an empty theme when nothing is stored", () => {
    expect(readSavedStageTheme()).toEqual({ version: 2, sections: {} });
  });

  it("returns an empty theme for malformed JSON", () => {
    localStorage.setItem("BOSUN_STAGE_THEME", "not json");
    expect(readSavedStageTheme()).toEqual({ version: 2, sections: {} });
  });

  it("drops unknown section keys and non-object section values", () => {
    localStorage.setItem("BOSUN_STAGE_THEME", JSON.stringify({
      sections: { rigName: { color: "#ff0000" }, notASection: { color: "#00ff00" }, bank: "nope" },
    }));
    expect(readSavedStageTheme()).toEqual({
      version: 2,
      sections: { rigName: { color: "#ff0000" } },
    });
  });

  it("clamps an out-of-range stored scale", () => {
    localStorage.setItem("BOSUN_STAGE_THEME", JSON.stringify({
      sections: { tuner: { scale: 99 } },
    }));
    expect(readSavedStageTheme().sections.tuner?.scale).toBe(MAX_SECTION_SCALE);
  });

  it("preserves saved scales across the expanded range", () => {
    saveStageTheme({ version: 2, sections: { rigName: { scale: 0.1 }, bank: { scale: 5 } } });
    expect(readSavedStageTheme().sections).toEqual({ rigName: { scale: 0.1 }, bank: { scale: 5 } });
  });

  it("converts old saved percentages without changing their rendered sizes or migrating twice", () => {
    localStorage.setItem("BOSUN_STAGE_THEME", JSON.stringify({
      version: 1,
      fontFamily: "Georgia, serif",
      sections: {
        rigName: { scale: 2, color: "#abcdef" },
        bank: { scale: 1.2 },
        switchLabel: { scale: 0.5 },
        switchId: { fontFamily: "monospace" },
      },
    }));
    const migrated = readSavedStageTheme();
    expect(migrated).toEqual({
      version: 2,
      fontFamily: "Georgia, serif",
      sections: {
        rigName: { scale: 1, color: "#abcdef" },
        bank: { scale: 0.6 },
        switchLabel: { scale: 0.25 },
        switchId: { fontFamily: "monospace" },
      },
    });
    const css = stageThemeToCssVars(migrated);
    expect(css).toContain("--stage-rig-name-scale: 2");
    expect(css).toContain("--stage-bank-scale: 1.2");
    expect(css).toContain("--stage-switch-label-scale: 0.5");
    saveStageTheme(migrated);
    expect(readSavedStageTheme()).toEqual(migrated);
  });

  it("round-trips a full theme via saveStageTheme", () => {
    const theme: StageTheme = {
      version: 2,
      fontFamily: "Georgia, serif",
      sections: {
        rigName: { color: "#ff0000", scale: 1.2 },
        switchId: { fontFamily: "monospace" },
      },
    };
    saveStageTheme(theme);
    expect(readSavedStageTheme()).toEqual(theme);
  });
});

describe("resetSection / resetAllStageTheme", () => {
  const theme: StageTheme = {
    version: 2,
    fontFamily: "Georgia, serif",
    sections: {
      rigName: { color: "#ff0000" },
      tuner: { scale: 1.5 },
    },
  };

  it("resetSection removes only the targeted section", () => {
    const next = resetSection(theme, "rigName");
    expect(next.sections.rigName).toBeUndefined();
    expect(next.sections.tuner).toEqual({ scale: 1.5 });
    expect(next.fontFamily).toBe("Georgia, serif");
  });

  it("resetAllStageTheme clears everything", () => {
    expect(resetAllStageTheme()).toEqual({ version: 2, sections: {} });
  });
});

describe("stageThemeToCssVars", () => {
  it("applies percentages relative to twice the original base size", () => {
    expect(stageThemeToCssVars({ version: 2, sections: {
      rigName: { scale: 0.1 }, bank: { scale: 1 }, switchLabel: { scale: 5 },
    } })).toContain("--stage-rig-name-scale: 0.2; --stage-bank-scale: 2");
    expect(stageThemeToCssVars({ version: 2, sections: { switchLabel: { scale: 5 } } }))
      .toContain("--stage-switch-label-scale: 10");
  });

  it("renders the old 200% size at the new 100% on a fresh or reset theme", () => {
    const defaults = [
      "--stage-rig-name-scale: 2", "--stage-bank-scale: 2", "--stage-bpm-scale: 2",
      "--stage-tuner-scale: 2", "--stage-switch-label-scale: 2", "--stage-switch-id-scale: 2", "--stage-expression-scale: 2",
    ].join("; ");
    expect(stageThemeToCssVars(readSavedStageTheme())).toBe(defaults);
    expect(stageThemeToCssVars(resetAllStageTheme())).toBe(defaults);
  });

  it("adds a color override without changing default sizes", () => {
    const css = stageThemeToCssVars({
      version: 2,
      sections: { switchLabel: { color: "#123456" } },
    });
    expect(css).toContain("--stage-switch-label-color: #123456; --stage-switch-label-scale: 2");
    expect(css).not.toContain("-font:");
  });

  it("maps every section to its kebab-case CSS key", () => {
    const css = stageThemeToCssVars({
      version: 2,
      fontFamily: "Georgia, serif",
      sections: {
        rigName: { color: "#a", scale: 1.1 },
        bank: { fontFamily: "monospace" },
        bpm: { color: "#b" },
        tuner: { color: "#c" },
        switchLabel: { color: "#d" },
        switchId: { color: "#e" },
      },
    });
    expect(css).toBe([
      "--stage-font: Georgia, serif",
      "--stage-rig-name-color: #a",
      "--stage-rig-name-scale: 2.2",
      "--stage-bank-font: monospace",
      "--stage-bank-scale: 2",
      "--stage-bpm-color: #b",
      "--stage-bpm-scale: 2",
      "--stage-tuner-color: #c",
      "--stage-tuner-scale: 2",
      "--stage-switch-label-color: #d",
      "--stage-switch-label-scale: 2",
      "--stage-switch-id-color: #e",
      "--stage-switch-id-scale: 2",
      "--stage-expression-scale: 2",
    ].join("; "));
  });
});
