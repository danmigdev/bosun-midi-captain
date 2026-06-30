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
  assets_present: boolean;
  asset_problems: string[];
  /** A pedal-class USB serial device is plugged in (CircuitPython / RP2
   * bootloader VID), whether or not it runs bosun. Combined with "not
   * connected" + no captain firmware, this flags an unflashed pedal. */
  usb_pedal_present: boolean;
}

export async function detectPedal(): Promise<DeviceState> {
  return invoke<DeviceState>("detect_pedal");
}

export async function flashCircuitPython(target: string): Promise<void> {
  await invoke("flash_circuitpython", { target });
}

export async function installFirmware(target: string): Promise<string[]> {
  return invoke<string[]>("install_firmware", { target });
}

/** Ask the pedal to reboot into the RP2040 bootloader (RPI-RP2) over its
 * REPL, so the user skips the physical hold-footswitch dance. Best effort;
 * resolves with the ports it wrote to, rejects if none were writable. */
export async function rebootToBootloader(): Promise<string> {
  return invoke<string>("reboot_to_bootloader");
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

export type InstallPhase =
  | "detecting"
  | "assets_missing"
  | "no_device"
  | "bootloader"
  | "circuitpy_wrong_cp"
  | "circuitpy_no_firmware"
  | "installed";

export function phaseOf(state: DeviceState | null): InstallPhase {
  if (!state) return "detecting";
  if (!state.assets_present) return "assets_missing";
  if (state.bootloader_drive) return "bootloader";
  // An incompatible CircuitPython must be reflashed before anything else -
  // takes priority over both the install and "installed" paths, since
  // firmware copied onto the wrong CircuitPython just crashes on boot.
  if (state.circuitpy_drive && !state.circuitpython_ok) return "circuitpy_wrong_cp";
  if (state.circuitpy_drive && !state.has_captain_firmware) return "circuitpy_no_firmware";
  if (state.circuitpy_drive && state.has_captain_firmware) return "installed";
  return "no_device";
}
