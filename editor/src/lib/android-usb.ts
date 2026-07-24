// Android USB host permission handling.
//
// On Android, the app must request permission to access a USB device before
// opening a serial connection. tauri-plugin-serialplugin handles this
// internally via the Android USB Host API, but the frontend may need to
// surface the permission dialog state.
//
// For the MVP, the plugin's automatic permission prompt on first open()
// is sufficient. This module provides helpers for future enhancements
// (device attach/detach events, permission state tracking).

import { IS_ANDROID } from "./platform";

/** True if the platform may need USB permission before serial access. */
export function needsUsbPermission(): boolean {
  return IS_ANDROID;
}

/**
 * Listen for USB device attach events. On Android, the OS may broadcast
 * an intent when a USB device is plugged in. Returns an unsubscribe
 * function (no-op on non-Android platforms).
 */
export function onUsbDeviceAttached(cb: (deviceName: string) => void): () => void {
  if (!IS_ANDROID) return () => {};
  // TODO Phase 2: wire up Tauri Android intent listener.
  // For now, the user manually taps Connect after plugging in the pedal.
  const _ = cb;
  return () => {};
}

/**
 * Listen for USB device detach events. Returns an unsubscribe function.
 */
export function onUsbDeviceDetached(cb: (deviceName: string) => void): () => void {
  if (!IS_ANDROID) return () => {};
  // TODO Phase 2: wire up Tauri Android intent listener.
  const _ = cb;
  return () => {};
}
