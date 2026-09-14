import { invoke } from "@tauri-apps/api/core";
import { IS_ANDROID } from "./platform";

export type UsbUpdateJob = {
  id: string;
  phase: "preflight" | "bootloader" | "backup" | "writing" | "rebooting" | "restoring" | "done" | "restored" | "failed" | "recovery-required";
  message: string;
  percent: number;
  port: string;
  previous_version: string;
  version: string;
  backup_path: string;
  identity: { serial: string; bus: string; ports: number[] };
};
export function usbUpdateTerminal(job: UsbUpdateJob | null): boolean {
  return !!job && ["done", "restored", "failed"].includes(job.phase);
}
export function usbUpdateRunning(job: UsbUpdateJob | null): boolean {
  return !!job && !usbUpdateTerminal(job) && job.phase !== "recovery-required";
}
export function usbUpdateStatus(): Promise<UsbUpdateJob | null> {
  return IS_ANDROID ? Promise.resolve(null) : invoke("usb_update_status");
}
export function startUsbUpdate(port: string): Promise<UsbUpdateJob> {
  if (IS_ANDROID || !port || port.startsWith("tcp://")) return Promise.reject(new Error("Select a direct Desktop USB connection"));
  return invoke("usb_update_start", { port });
}
export function recoverUsbUpdate(): Promise<void> {
  if (IS_ANDROID) return Promise.reject(new Error("USB firmware recovery requires Bosun Desktop"));
  return invoke("usb_update_recover");
}
