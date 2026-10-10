import {
  FirmwareCommandTimeoutError,
  sendAndAwait,
  type FirmwareMessage,
  type ProfileInfo,
} from "./protocol";

function isReadTimeout(error: unknown): boolean {
  if (error instanceof FirmwareCommandTimeoutError) return true;
  const message = error instanceof Error ? error.message : String(error);
  return /^(?:error:\s*)?request_timeout$/.test(message.trim());
}

/** One read, retried once after a timeout within a two-read budget. */
async function read<T extends FirmwareMessage = FirmwareMessage>(type: string, timeout: number): Promise<T> {
  const deadline = Date.now() + 2 * timeout;
  let retried = false;
  while (true) {
    try {
      return await sendAndAwait<T>({ type }, Math.min(timeout, deadline - Date.now()));
    } catch (error) {
      if (retried || !isReadTimeout(error)) throw error;
      retried = true;
      const remaining = deadline - Date.now();
      if (remaining <= 0) throw error;
      await new Promise<void>(resolve => setTimeout(resolve, Math.min(250, remaining)));
      if (Date.now() >= deadline) throw error;
    }
  }
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
    await read("GET_GLOBAL", 10000);
  }
  return { profiles: profiles.profiles, active: profiles.active };
}
