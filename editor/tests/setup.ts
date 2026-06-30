// Global test setup: registers jest-dom matchers and shims the Tauri
// runtime so modules that import @tauri-apps/api/core (protocol.ts,
// firmware-push.ts, ...) don't crash on load in a jsdom environment.

import "@testing-library/jest-dom/vitest";
import { vi, afterEach } from "vitest";

// Tauri's invoke() probes window.__TAURI_INTERNALS__ on call. Provide
// a default that throws so any *unintended* IPC during a test surfaces
// loudly; tests that need to exercise an invoke path should mock it
// via vi.mock("@tauri-apps/api/core") explicitly.
type TauriInternals = {
  invoke: (...args: unknown[]) => unknown;
  transformCallback: (cb: unknown) => number;
  ipc: (...args: unknown[]) => void;
};
(globalThis as unknown as { __TAURI_INTERNALS__: TauriInternals }).__TAURI_INTERNALS__ = {
  invoke: () => {
    throw new Error("Unexpected Tauri invoke() call in test - mock @tauri-apps/api/core if intentional");
  },
  transformCallback: () => 0,
  ipc: () => {},
};

// Stub localStorage for modules that persist preferences (theme,
// ui-scale, etc). jsdom provides one, but reset between tests so a
// flag set by test A doesn't bleed into test B.
afterEach(() => {
  try { localStorage.clear(); } catch {}
  vi.restoreAllMocks();
});
