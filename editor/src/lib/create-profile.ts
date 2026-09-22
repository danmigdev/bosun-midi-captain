import { cmd, sendAndAwait } from './protocol';
import { readKemperSetup, type KemperSetup } from './setup-guide';

export async function createConfiguredProfile(id: string, name: string, kind: string, color: string,
  setup: KemperSetup = readKemperSetup()): Promise<void> {
  await cmd.createProfile(id, name, kind, color);
  if (kind !== 'kemper_head') return;
  try {
    const reply = await sendAndAwait({ type: 'GET_GLOBAL', profile: id });
    if (reply.type !== 'GLOBAL' || !reply.device || typeof reply.device !== 'object' ||
        (reply as { profile?: string }).profile !== id) throw new Error('Profile identity could not be verified');
    await sendAndAwait({ type: 'PUT_GLOBAL', profile: id, device: {
      ...reply.device, kemper: { generation: setup.generation, mode: setup.mode },
    } });
  } catch (error) {
    throw new Error(`Profile "${name}" was created, but its PROFILER settings could not be saved. Open its settings and select ${setup.generation} / ${setup.mode}. ${String(error)}`);
  }
}
