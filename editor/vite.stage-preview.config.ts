// Dev-only config: previews StageView in a plain browser tab, without a
// Tauri host or physical pedal/Kemper. Run with:
//   npx vite --config vite.stage-preview.config.ts
// then open http://localhost:4732/stage-preview.html
// Not referenced by the production build (package.json's dev/build scripts
// use the default vite.config.ts).
import { fileURLToPath } from "node:url";
import { defineConfig } from "vite";
import { svelte } from "@sveltejs/vite-plugin-svelte";

export default defineConfig({
  plugins: [svelte()],
  clearScreen: false,
  server: { port: 4732, strictPort: true },
  resolve: {
    alias: [
      {
        find: "@tauri-apps/api/core",
        replacement: fileURLToPath(new URL("./dev-preview/tauri-core-shim.ts", import.meta.url)),
      },
      {
        find: "@tauri-apps/api/event",
        replacement: fileURLToPath(new URL("./dev-preview/tauri-event-shim.ts", import.meta.url)),
      },
    ],
  },
});
