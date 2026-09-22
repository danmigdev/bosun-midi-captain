import { sendAndAwait } from "./protocol";

/** Do not discard live patch edits or retry an ambiguous reboot request. */
export async function enterBootloader(): Promise<void> {
  const dirty = await sendAndAwait({ type: "GET_DIRTY" });
  if (dirty.type !== "DIRTY" || !Array.isArray(dirty.patches)) {
    throw new Error("Could not check unsaved patch edits. Bootloader request cancelled.");
  }
  if (dirty.patches.length) {
    throw new Error("Save or discard unsaved patch edits before entering the bootloader.");
  }
  try {
    const reply = await sendAndAwait({ type: "REBOOT", mode: "bootloader" });
    if (reply.type !== "ACK") throw new Error("Unexpected response");
  } catch {
    throw new Error("Bootloader request was not acknowledged. Check for the RPI-RP2 drive before trying again.");
  }
}
