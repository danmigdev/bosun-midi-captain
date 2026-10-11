import { beforeEach, expect, it, vi } from 'vitest';
const mocks = vi.hoisted(() => ({ create: vi.fn(), request: vi.fn() }));
vi.mock('./protocol', () => ({ cmd: { createProfile: mocks.create }, sendAndAwait: mocks.request }));
import { createConfiguredProfile, profileIdFromName } from './create-profile';
beforeEach(() => { vi.resetAllMocks(); localStorage.clear(); });
it('creates PROFILER settings on the new profile without changing another profile', async () => {
  mocks.request.mockResolvedValueOnce({ type: 'GLOBAL', profile: 'stage', device: { midi_channel: 3, custom: 42 } });
  await createConfiguredProfile('stage', 'Stage', 'kemper_head', '#123456',
    { target: 'stage', generation: 'MK2', mode: 'browse', connection: 'din' });
  expect(mocks.request).toHaveBeenLastCalledWith({ type: 'PUT_GLOBAL', profile: 'stage', device: {
    midi_channel: 3, custom: 42, kemper: { generation: 'MK2', mode: 'browse' },
  } });
});
it('stops when the response belongs to another profile', async () => {
  mocks.request.mockResolvedValue({ type: 'GLOBAL', profile: 'other', device: {} });
  await expect(createConfiguredProfile('stage', 'Stage', 'kemper_head', '')).rejects.toThrow('was created');
  expect(mocks.request).toHaveBeenCalledTimes(1);
});
it('does not create again or write settings when creation fails', async () => {
  mocks.create.mockRejectedValue(new Error('already exists'));
  await expect(createConfiguredProfile('stage', 'Stage', 'kemper_head', '')).rejects.toThrow('already exists');
  expect(mocks.request).not.toHaveBeenCalled();
});
it('derives a valid profile id from the whole name', () => {
  expect(profileIdFromName('Live rig')).toBe('live-rig');
  expect(profileIdFromName('  Kemper / Stage #2 ')).toBe('kemper-stage-2');
  expect(profileIdFromName('Città')).toBe('citt');
  expect(profileIdFromName('')).toBe('');
});
it('keeps the id within the firmware limit without a trailing dash', () => {
  const id = profileIdFromName('A very long profile name for the Sunday service band');
  expect(id.length).toBeLessThanOrEqual(32);
  expect(id).toBe('a-very-long-profile-name-for-the');
  expect(profileIdFromName('abcdefghij abcdefghij abcdefghij x')).toBe('abcdefghij-abcdefghij-abcdefghij');
  expect(profileIdFromName('abcdefghij abcdefghij abcdefghi xyz')).toBe('abcdefghij-abcdefghij-abcdefghi');
});