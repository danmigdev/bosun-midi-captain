import { invoke } from "@tauri-apps/api/core";

export interface DeviceState {
  bootloader_drive: string | null;
  /** A CIRCUITPY drive. Bosun exposes no drive, so this always means
   * stock (factory) or other foreign firmware. */
  circuitpy_drive: string | null;
  /** A pedal-class USB serial device is plugged in (MIDI Captain / RP2
   * bootloader VID), whether or not it runs bosun. Combined with "not
   * connected", this flags an unflashed pedal. */
  usb_pedal_present: boolean;
}

export async function detectPedal(): Promise<DeviceState> {
  return invoke<DeviceState>("detect_pedal");
}
