import { describe, it, expect } from "vitest";
import { MODE_HELP, PARAM_HELP, helpForMessageType } from "./help-text";
import { ACTION_KEYS_BY_MODE, type BindingMode } from "./protocol";

describe("MODE_HELP", () => {
  it("has an entry for every binding mode", () => {
    const modes = Object.keys(ACTION_KEYS_BY_MODE) as BindingMode[];
    expect(modes).toHaveLength(5);
    for (const mode of modes) {
      expect(MODE_HELP[mode], `${mode} help`).toBeTruthy();
      expect(typeof MODE_HELP[mode]).toBe("string");
    }
  });
});

describe("PARAM_HELP", () => {
  it("has entries for the common param names", () => {
    for (const name of ["channel", "cc", "value", "scope"]) {
      expect(PARAM_HELP[name], `${name} help`).toBeTruthy();
      expect(typeof PARAM_HELP[name]).toBe("string");
    }
  });
});

describe("helpForMessageType", () => {
  it("returns a string for known message types", () => {
    expect(typeof helpForMessageType("cc")).toBe("string");
    expect(typeof helpForMessageType("captain_preview_step")).toBe("string");
  });

  it("returns undefined for unknown message types", () => {
    expect(helpForMessageType("nonsense")).toBeUndefined();
  });
});
