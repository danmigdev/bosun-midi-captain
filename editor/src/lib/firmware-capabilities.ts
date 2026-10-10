/** DEVICE_INFO fields that identify the firmware and its maintenance options. */
export interface FirmwareIdentity {
  fw: string;
  reboot_modes?: string[];
}

/** True when the firmware accepts REBOOT into its USB bootloader. A reply
 * without `reboot_modes` offers no bootloader entry. */
export function supportsBootloader(info: FirmwareIdentity | null | undefined): boolean {
  return info?.reboot_modes?.includes("bootloader") === true;
}
