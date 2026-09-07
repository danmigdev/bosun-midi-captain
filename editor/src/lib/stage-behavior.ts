// Stage preferences belong to the current browser/editor, not the Captain.
export type BankSelectionMode = "immediate" | "preselect";

const STORAGE_KEY = "BOSUN_STAGE_BANK_SELECTION";

export function readBankSelectionMode(): BankSelectionMode {
  try {
    return localStorage.getItem(STORAGE_KEY) === "preselect" ? "preselect" : "immediate";
  } catch {
    return "immediate";
  }
}

export function saveBankSelectionMode(mode: BankSelectionMode): void {
  try {
    localStorage.setItem(STORAGE_KEY, mode === "preselect" ? "preselect" : "immediate");
  } catch {
    // Stage remains usable when browser storage is unavailable or full.
  }
}
