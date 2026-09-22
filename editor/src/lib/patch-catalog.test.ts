import { expect, it, vi } from 'vitest';
import { collectPatchCatalog } from './patch-catalog';
import type { FirmwareMessage } from './protocol';
const entries = Array.from({ length: 625 }, (_, i) => ({ bank: Math.floor(i / 5) + 1, slot: i % 5 + 1, name: 'x'.repeat(128) }));
const message = (value: object) => value as FirmwareMessage;
it('assembles 625 patches in bounded pages', async () => {
  const read = vi.fn(async ({ offset, limit }) => message({ type: 'PATCH_LIST', profile: 'head', offset, revision: 9,
    total: entries.length, patches: entries.slice(offset, offset + limit), next_offset: offset + limit < entries.length ? offset + limit : -1 }));
  const result = await collectPatchCatalog(read);
  expect(result.paginated).toBe(true);
  expect(result.message).toMatchObject({ patches: entries });
  expect(result.message).not.toHaveProperty('offset');
  expect(read).toHaveBeenCalledTimes(10);
});
it('accepts a legacy complete response', async () => {
  const result = await collectPatchCatalog(async () => message({ type: 'PATCH_LIST', patches: entries.slice(0, 2) }));
  expect(result.paginated).toBe(false);
});
it.each(['duplicate', 'truncated', 'loop', 'oversized'])('rejects a %s inventory', async fault => {
  await expect(collectPatchCatalog(async () => message({ type: 'PATCH_LIST', offset: 0, revision: 1,
    total: fault === 'oversized' ? 626 : 2,
    patches: fault === 'duplicate' ? [entries[0], entries[0]] : [entries[0]],
    next_offset: fault === 'loop' ? 0 : -1 }))).rejects.toThrow();
});
it('restarts when a mutation occurs between pages', async () => {
  let calls = 0;
  const read = vi.fn(async ({ offset }) => {
    calls++;
    return message({ type: 'PATCH_LIST', offset, profile: 'head', revision: calls === 1 ? 1 : 2,
      total: 2, patches: [entries[Number(offset)]], next_offset: offset === 0 ? 1 : -1 });
  });
  expect((await collectPatchCatalog(read)).message).toMatchObject({ patches: entries.slice(0, 2), revision: 2 });
  expect(read).toHaveBeenCalledTimes(4);
});
