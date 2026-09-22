import type { FirmwareMessage } from './protocol';

type Request = { type: string; [key: string]: unknown };

/** Assemble a consistent inventory before exposing it to a view or backup. */
export async function collectPatchCatalog(
  request: (message: Request) => Promise<FirmwareMessage>,
  base: Request = { type: 'LIST_PATCHES' },
): Promise<{ message: FirmwareMessage; paginated: boolean }> {
  for (let attempt = 0; attempt < 3; attempt++) {
    const patches: unknown[] = [];
    const keys = new Set<string>();
    let revision: unknown;
    let profile: unknown;
    let total: unknown;
    for (;;) {
      const offset = patches.length;
      const message = await request({ ...base, id: undefined, limit: 64, offset });
      const page = message as unknown as Record<string, unknown>;
      if (page.type !== 'PATCH_LIST' || !Array.isArray(page.patches))
        throw new Error('Invalid patch inventory');
      // CircuitPython and older native firmware return the complete list.
      if (page.offset === undefined) {
        if (offset !== 0) throw new Error('Patch pagination disappeared');
        return { message, paginated: false };
      }
      if (offset === 0) { revision = page.revision; profile = page.profile; total = page.total; }
      else if (revision !== page.revision || profile !== page.profile || total !== page.total) break;
      if (page.offset !== offset || !Number.isInteger(total) || Number(total) < 0 || Number(total) > 625 ||
          !Number.isInteger(revision) || page.patches.length > 64 || offset + page.patches.length > Number(total))
        throw new Error('Invalid patch page');
      for (const item of page.patches) {
        if (!item || typeof item !== 'object') throw new Error('Invalid patch entry');
        const { bank, slot } = item as { bank: unknown; slot: unknown };
        const key = `${bank}/${slot}`;
        if (!Number.isInteger(bank) || Number(bank) < 1 || Number(bank) > 125 ||
            !Number.isInteger(slot) || Number(slot) < 1 || Number(slot) > 10 || keys.has(key))
          throw new Error('Invalid or duplicate patch coordinates');
        keys.add(key);
        patches.push(item);
      }
      if (page.next_offset === -1 && patches.length === total) {
        const { offset: _offset, next_offset: _next, ...complete } = page;
        return { message: { ...complete, patches } as unknown as FirmwareMessage, paginated: true };
      }
      if (page.next_offset !== patches.length || patches.length <= offset || patches.length >= Number(total))
        throw new Error('Incomplete patch inventory');
    }
  }
  throw new Error('Patch inventory changed repeatedly; retry when edits have settled');
}
