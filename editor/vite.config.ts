/// <reference types="vitest" />
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
    // Tauri's @tauri-apps/api/core ships ESM but expects a browser-like
    // global object; jsdom provides it, but the api also probes for
    // window.__TAURI_INTERNALS__ which is absent in tests. The setup
    // file shims it (see tests/setup.ts).
  },
});
