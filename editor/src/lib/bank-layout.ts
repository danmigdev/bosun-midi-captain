/** Profile-local layout. Existing patches remain available in the editor even
 * when a smaller bank or slot layout is configured. */
export const DEFAULT_RIGS_PER_BANK = 5;
export const MAX_RIGS_PER_BANK = 10;
export const MAX_BANKS = 125;

export function getBankCount(device: { bank_count?: unknown } | null | undefined): number {
  const value = device?.bank_count;
  return typeof value === "number" && Number.isInteger(value)
    && value >= 1 && value <= MAX_BANKS ? value : MAX_BANKS;
}

export function getRigsPerBank(device: { rigs_per_bank?: unknown } | null | undefined): number {
  const value = device?.rigs_per_bank;
  return typeof value === "number" && Number.isInteger(value)
    && value >= 1 && value <= MAX_RIGS_PER_BANK ? value : DEFAULT_RIGS_PER_BANK;
}

export function nextFreePatch(
  patches: ReadonlyArray<{ bank: number; slot: number }>, rigsPerBank: number, bankCount = MAX_BANKS,
): { bank: number; slot: number } | null {
  const count = getRigsPerBank({ rigs_per_bank: rigsPerBank });
  const used = new Set(patches.map(p => `${p.bank}/${p.slot}`));
  const banks = getBankCount({ bank_count: bankCount });
  for (let bank = 1; bank <= banks; bank++) {
    for (let slot = 1; slot <= count; slot++) {
      if (!used.has(`${bank}/${slot}`)) return { bank, slot };
    }
  }
  return null;
}
