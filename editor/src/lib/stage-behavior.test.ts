import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { readBankSelectionMode, saveBankSelectionMode, type BankSelectionMode } from "./stage-behavior";

const KEY = "BOSUN_STAGE_BANK_SELECTION";

beforeEach(() => localStorage.clear());
afterEach(() => vi.restoreAllMocks());

describe("Stage bank selection preference", () => {
  it("keeps immediate navigation for an existing installation without a preference", () => {
    expect(readBankSelectionMode()).toBe("immediate");
    expect(localStorage.getItem(KEY)).toBeNull();
  });

  it("persists the opted-in preselection and a later return to immediate navigation", () => {
    saveBankSelectionMode("preselect");
    expect(localStorage.getItem(KEY)).toBe("preselect");
    expect(readBankSelectionMode()).toBe("preselect");
    saveBankSelectionMode("immediate");
    expect(localStorage.getItem(KEY)).toBe("immediate");
    expect(readBankSelectionMode()).toBe("immediate");
  });

  it.each(["", "future-mode", '"preselect"', "PRESELECT"])("ignores an unsupported saved value %j", value => {
    localStorage.setItem(KEY, value);
    expect(readBankSelectionMode()).toBe("immediate");
  });

  it("falls back safely when browser storage cannot be read", () => {
    vi.spyOn(Storage.prototype, "getItem").mockImplementation(() => { throw new Error("Storage blocked"); });
    expect(readBankSelectionMode()).toBe("immediate");
  });

  it("does not interrupt Stage if saving is blocked or quota is exhausted", () => {
    vi.spyOn(Storage.prototype, "setItem").mockImplementation(() => { throw new Error("Storage full"); });
    expect(() => saveBankSelectionMode("preselect")).not.toThrow();
  });

  it("does not persist an unsupported value received at runtime", () => {
    saveBankSelectionMode("future-mode" as BankSelectionMode);
    expect(localStorage.getItem(KEY)).toBe("immediate");
  });
});
