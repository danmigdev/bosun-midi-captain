// Platform detection helpers. Used to adapt the UI (hide desktop-only
// features on Android) and gate Tauri invoke() calls that only exist on
// one platform.

/** True when running inside the Tauri Android WebView. */
export const IS_ANDROID: boolean = (() => {
  try {
    if (typeof navigator !== "undefined") {
      return /android/i.test(navigator.userAgent)
          || /android/i.test(navigator.platform ?? "");
    }
  } catch { /* SSR / test environment */ }
  return false;
})();
