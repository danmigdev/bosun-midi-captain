/** DEVICE_INFO fields that identify the firmware family and its update path. */
export interface FirmwareIdentity {
  fw: string;
  native_experimental?: boolean;
  firmware_ota?: boolean;
  reboot_modes?: string[];
}

export function supportsBootloader(info: FirmwareIdentity | null | undefined): boolean {
  return isNativeFirmware(info) && info?.reboot_modes?.includes("bootloader") === true;
}

export function isNativeFirmware(info: FirmwareIdentity | null | undefined): boolean {
  return info?.native_experimental === true || /(?:^|[-+.])native(?:$|[-+.])/i.test(info?.fw ?? "");
}

/** Older CircuitPython releases predate capability flags, so retain their OTA
 * support. Native versions belong to a separate release line and use UF2. */
export function supportsFirmwareFileOta(info: FirmwareIdentity | null | undefined): boolean {
  return !!info?.fw?.trim() && !isNativeFirmware(info) && info.firmware_ota !== false;
}

export class FirmwareOtaUnavailableError extends Error {
  constructor() {
    super("This device does not support CircuitPython firmware updates.");
    this.name = "FirmwareOtaUnavailableError";
  }
}

export function assertFirmwareFileOta(info: FirmwareIdentity | null | undefined): void {
  if (!supportsFirmwareFileOta(info)) throw new FirmwareOtaUnavailableError();
}
