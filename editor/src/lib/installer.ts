import { invoke } from "@tauri-apps/api/core";

export interface DeviceState {
  bootloader_drive: string | null;
  circuitpy_drive: string | null;
  has_captain_firmware: boolean;
  captain_version: string | null;
  /** CircuitPython version on the CIRCUITPY drive (from boot_out.txt), and
   * whether it's compatible with the bundled firmware. `circuitpython_ok`
   * is true when the version can't be read (don't block on uncertainty). */
  circuitpython_version: string | null;
  circuitpython_ok: boolean;
  /** A pedal-class USB serial device is plugged in (CircuitPython / RP2
   * bootloader VID), whether or not it runs bosun. Combined with "not
   * connected" + no captain firmware, this flags an unflashed pedal. */
  usb_pedal_present: boolean;
}

export async function detectPedal(): Promise<DeviceState> {
  return invoke<DeviceState>("detect_pedal");
}

/** Native picker for a firmware source: a folder (`zip=false`) or a `.zip`
 * (`zip=true`). Resolves to the chosen path, or null if cancelled. */
export async function pickFirmwareSource(zip: boolean): Promise<string | null> {
  return invoke<string | null>("pick_firmware_source", { zip });
}

/** Resolve a picked folder/zip to a firmware root dir (extracting a zip to
 * temp if needed). Rejects if the selection has no firmware. */
export async function prepareFirmwareSource(source: string): Promise<string> {
  return invoke<string>("prepare_firmware_source", { source });
}
