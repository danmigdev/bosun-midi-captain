import {
  FirmwareCommandTimeoutError,
  sendAndAwait,
  type FirmwareMessage,
  type ProfileInfo,
} from "./protocol";

function transientReadError(error: unknown): boolean {
  if (error instanceof FirmwareCommandTimeoutError) return true;
  const message = error instanceof Error ? error.message : String(error);
  return /^(?:error:\s*)?(?:background_busy|request_timeout)$/.test(message.trim());
}

async function read<T extends FirmwareMessage = FirmwareMessage>(type: string, timeout: number): Promise<T> {
  try {
    return await sendAndAwait<T>({ type }, timeout);
  } catch (error) {
    if (!transientReadError(error)) throw error;
    // These requests only read state. Allow one retry after the current
    // firmware response has had an opportunity to finish draining.
    await new Promise<void>(resolve => setTimeout(resolve, 250));
  }
  return sendAndAwait<T>({ type }, timeout);
}

/** Load one network session without overlapping large firmware responses.
 * sendAndAwait also delivers every reply through the normal firmware bus,
 * so App's existing subscribers populate device, manifest and patch state.
 */
export async function readNetworkBootstrap(): Promise<{ profiles: ProfileInfo[]; active: string }> {
  await read("GET_DEVICE_INFO", 8000);
  const profiles = await read<Extract<FirmwareMessage, { type: "PROFILE_LIST" }>>("LIST_PROFILES", 8000);
  await read("GET_MANIFEST", 15000);
  if (profiles.profiles.some(profile => profile.active)) {
    await read("LIST_PATCHES", 10000);
    await read("GET_DIRTY", 8000);
    await read("GET_MIDI_LEARN", 8000);
    await read("GET_GLOBAL", 10000);
  }
  return { profiles: profiles.profiles, active: profiles.active };
}
