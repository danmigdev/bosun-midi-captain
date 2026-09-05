import { describe, it, expect, beforeEach } from "vitest";

import {
  MIN_SECTION_SCALE, MAX_SECTION_SCALE,
  clampSectionScale, readSavedStageTheme, saveStageTheme,
  resetSection, resetAllStageTheme, stageThemeToCssVars,
  type StageTheme,
} from "./stage-theme";

beforeEach(() => {
  localStorage.clear();
});

describe("clampSectionScale", () => {
  it("clamps below MIN to MIN", () => {
    expect(clampSectionScale(0.1)).toBe(MIN_SECTION_SCALE);
    expect(clampSectionScale(-5)).toBe(MIN_SECTION_SCALE);
  });

  it("clamps above MAX to MAX", () => {
    expect(clampSectionScale(99)).toBe(MAX_SECTION_SCALE);
  });

  it("rounds to 2 decimals to avoid floating-point drift", () => {
    expect(clampSectionScale(1.0500000000000003)).toBe(1.05);
  });

  it("falls back to 1 for non-finite input", () => {
    expect(clampSectionScale(NaN)).toBe(1);
    expect(clampSectionScale(Infinity)).toBe(1);
  });
});

describe("readSavedStageTheme", () => {
  it("returns an empty theme when nothing is stored", () => {
    expect(readSavedStageTheme()).toEqual({ version: 1, sections: {} });
  });

  it("returns an empty theme for malformed JSON", () => {
    localStorage.setItem("BOSUN_STAGE_THEME", "not json");
    expect(readSavedStageTheme()).toEqual({ version: 1, sections: {} });
  });

  it("drops unknown section keys and non-object section values", () => {
    localStorage.setItem("BOSUN_STAGE_THEME", JSON.stringify({
      sections: { rigName: { color: "#ff0000" }, notASection: { color: "#00ff00" }, bank: "nope" },
    }));
    expect(readSavedStageTheme()).toEqual({
      version: 1,
      sections: { rigName: { color: "#ff0000" } },
    });
  });

  it("clamps an out-of-range stored scale", () => {
    localStorage.setItem("BOSUN_STAGE_THEME", JSON.stringify({
      sections: { tuner: { scale: 99 } },
    }));
    expect(readSavedStageTheme().sections.tuner?.scale).toBe(MAX_SECTION_SCALE);
  });

  it("round-trips a full theme via saveStageTheme", () => {
    const theme: StageTheme = {
      version: 1,
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
    version: 1,
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
    expect(resetAllStageTheme()).toEqual({ version: 1, sections: {} });
  });
});

describe("stageThemeToCssVars", () => {
  it("produces an empty string for an empty theme", () => {
    expect(stageThemeToCssVars({ version: 1, sections: {} })).toBe("");
  });

  it("emits only the fields that are set", () => {
    const css = stageThemeToCssVars({
      version: 1,
      sections: { switchLabel: { color: "#123456" } },
    });
    expect(css).toBe("--stage-switch-label-color: #123456");
  });

  it("maps every section to its kebab-case CSS key", () => {
    const css = stageThemeToCssVars({
      version: 1,
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
      "--stage-rig-name-scale: 1.1",
      "--stage-bank-font: monospace",
      "--stage-bpm-color: #b",
      "--stage-tuner-color: #c",
      "--stage-switch-label-color: #d",
      "--stage-switch-id-color: #e",
    ].join("; "));
  });
});
