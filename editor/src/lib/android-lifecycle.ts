// Android lifecycle management.
//
// On Android, the app can be backgrounded at any time. The serial connection
// must survive or be gracefully re-established. This module provides:
//
// 1. Visibility change detection (app backgrounded / foregrounded)
// 2. State persistence (save before suspend, restore on resume)
// 3. Auto-reconnect after returning to foreground
// 4. Back-button navigation (pop page instead of closing app)
//
// The Android WebView fires 'visibilitychange' when the app goes to
// background (visibilityState = "hidden") and returns ("visible").
// Tauri window blur/focus events provide an additional signal.

import { IS_ANDROID } from "./platform";

export type LifecycleState = "active" | "inactive" | "background";

type LifecycleCallback = (state: LifecycleState) => void;
type BackButtonCallback = () => boolean; // return true = handled, don't close

const _lifecycleListeners = new Set<LifecycleCallback>();
const _backButtonListeners = new Set<BackButtonCallback>();
let _currentState: LifecycleState = "active";
let _installed = false;

/** Current lifecycle state. */
export function getLifecycleState(): LifecycleState {
  return _currentState;
}

/** Register a callback for lifecycle state changes. Returns an unsubscribe function. */
export function onLifecycleChange(cb: LifecycleCallback): () => void {
  _lifecycleListeners.add(cb);
  _install();
  return () => { _lifecycleListeners.delete(cb); };
}

/** Register a back-button handler. First handler returning true wins.
 *  Returns an unsubscribe function. */
export function onBackButton(cb: BackButtonCallback): () => void {
  _backButtonListeners.add(cb);
  _install();
  return () => { _backButtonListeners.delete(cb); };
}

function _install(): void {
  if (_installed || !IS_ANDROID) return;
  _installed = true;

  // ---- visibility change (background / foreground) ----
  document.addEventListener("visibilitychange", () => {
    const hidden = document.visibilityState === "hidden";
    const next: LifecycleState = hidden ? "background" : "active";
    if (next !== _currentState) {
      _currentState = next;
      for (const cb of _lifecycleListeners) {
        try { cb(_currentState); } catch {}
      }
    }
  });

  // ---- back button via popstate ----
  // Android back button fires popstate in the WebView. We push a
  // dummy state on mount so the first back-press triggers popstate
  // instead of closing the app. Handlers can consume the event.
  window.history.pushState({ __bosun_guard: true }, "", "");
  window.addEventListener("popstate", (e) => {
    // If a handler consumed the back press, push a new guard so the
    // NEXT back press doesn't close the app either.
    let consumed = false;
    for (const cb of _backButtonListeners) {
      try { if (cb()) { consumed = true; break; } } catch {}
    }
    if (consumed) {
      window.history.pushState({ __bosun_guard: true }, "", "");
      e.preventDefault();
    }
    // If not consumed, the default popstate behavior closes the app.
  });

  // ---- Tauri window blur (additional signal) ----
  try {
    if (typeof window !== "undefined" && "__TAURI_INTERNALS__" in window) {
      import("@tauri-apps/api/window").then(({ getCurrentWindow }) => {
        const win = getCurrentWindow();
        win.listen("tauri://blur", () => {
          if (_currentState === "active") {
            _currentState = "inactive";
            for (const cb of _lifecycleListeners) {
              try { cb(_currentState); } catch {}
            }
          }
        });
        win.listen("tauri://focus", () => {
          if (_currentState !== "active") {
            _currentState = "active";
            for (const cb of _lifecycleListeners) {
              try { cb(_currentState); } catch {}
            }
          }
        });
      }).catch(() => {});
    }
  } catch {}
}

/**
 * Save critical editor state to localStorage so it survives an Android
 * process kill. Called on background / beforeunload.
 */
export function saveSessionState(state: Record<string, unknown>): void {
  if (!IS_ANDROID) return;
  try {
    localStorage.setItem("BOSUN_SESSION", JSON.stringify({
      ...state,
      _ts: Date.now(),
    }));
  } catch {}
}

/**
 * Restore editor state saved by saveSessionState(). Returns null if
 * nothing was saved or the saved state is older than `maxAgeMs`.
 */
export function restoreSessionState(maxAgeMs = 300_000): Record<string, unknown> | null {
  if (!IS_ANDROID) return null;
  try {
    const raw = localStorage.getItem("BOSUN_SESSION");
    if (!raw) return null;
    const state = JSON.parse(raw);
    if (Date.now() - (state._ts ?? 0) > maxAgeMs) {
      localStorage.removeItem("BOSUN_SESSION");
      return null;
    }
    return state;
  } catch { return null; }
}
