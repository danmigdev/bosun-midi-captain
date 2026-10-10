import { describe, it, expect } from "vitest";
import { MODE_HELP } from "./help-text";
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
