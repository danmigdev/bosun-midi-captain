import {
  FirmwareCommandTimeoutError,
  sendAndAwait,
  type FirmwareMessage,
  type ProfileInfo,
} from "./protocol";

function transientReadError(error: unknown): "busy" | "timeout" | null {
  if (error instanceof FirmwareCommandTimeoutError) return "timeout";
  const message = error instanceof Error ? error.message : String(error);
  const match = /^(?:error:\s*)?(background_busy|request_timeout)$/.exec(message.trim());
  return match ? (match[1] === "background_busy" ? "busy" : "timeout") : null;
}

async function read<T extends FirmwareMessage = FirmwareMessage>(type: string, timeout: number): Promise<T> {
  const deadline = Date.now() + 2 * timeout;
  let timeoutRetries = 0;
  let busyWait = 250;
  while (true) {
    try {
      return await sendAndAwait<T>({ type }, Math.min(timeout, deadline - Date.now()));
    } catch (error) {
      const transient = transientReadError(error);
      if (!transient) throw error;
      if (transient === "timeout") {
        if (timeoutRetries >= 1) throw error;
        timeoutRetries += 1;
      }
      // Other Stage/editor clients can own Captain's response stream for
      // several seconds. A busy response rejects this read before it starts;
      // wait for that stream to drain, keeping the original two-read budget.
      const remaining = deadline - Date.now();
      if (remaining <= 0) throw error;
      const delay = transient === "busy" ? busyWait : 250;
      if (transient === "busy") busyWait = Math.min(busyWait * 2, 1000);
      await new Promise<void>(resolve => setTimeout(resolve, Math.min(delay, remaining)));
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
    await read("GET_MIDI_LEARN", 8000);
    await read("GET_GLOBAL", 10000);
  }
  return { profiles: profiles.profiles, active: profiles.active };
}
