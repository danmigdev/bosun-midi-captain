/// <reference types="vitest" />
import { fileURLToPath } from "node:url";
import { defineConfig } from "vite";
import { svelte } from "@sveltejs/vite-plugin-svelte";

const host = process.env.TAURI_DEV_HOST;

export default defineConfig({
  plugins: [svelte()],
  clearScreen: false,
  server: {
    port: 1420,
    strictPort: true,
    host: host || false,
    hmr: host ? { protocol: "ws", host, port: 1421 } : undefined,
    watch: { ignored: ["**/src-tauri/**"] },
  },
  test: {
    // jsdom gives Svelte components a window/document to render into.
    // Tests live alongside source under src/**/*.test.ts(?x).
    environment: "jsdom",
    globals: true,
    include: ["src/**/*.test.{ts,tsx,js}", "tests/**/*.test.{ts,tsx,js}"],
    setupFiles: ["./tests/setup.ts"],
    // svelte 5.5x+ exposes the bare "svelte" entry via only `worker` and
    // `browser` conditions (default = index-server). Vitest's test server
    // runs with `resolve.conditions = ["node"]` (its Vite-5 compatibility
    // path hardcodes node conditions), so bare "svelte" resolves to the
    // SERVER build and @testing-library/svelte's `mount` throws "not
    // available on the server". The alias pins the bare specifier to the
    // client entry (the same file the app's browser build resolves via the
    // `browser` condition), making component tests mountable. Test-only
    // alias: the app's own build is untouched.
    alias: [
      {
        find: /^svelte$/,
        replacement: fileURLToPath(new URL("./node_modules/svelte/src/index-client.js", import.meta.url)),
      },
    ],
    // Tauri's @tauri-apps/api/core ships ESM but expects a browser-like
    // global object; jsdom provides it, but the api also probes for
    // window.__TAURI_INTERNALS__ which is absent in tests. The setup
    // file shims it (see tests/setup.ts).
  },
});
