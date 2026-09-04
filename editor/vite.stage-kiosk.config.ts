// Build config for the Stage kiosk bundle served by bosun-hub on the Pi
// (tools/rpi-hub). It is StageView plus a thin shell (src/kiosk/), with
// the Tauri API swapped for a WebSocket-to-the-hub transport.
//
//   npm run build:stage      -> editor/dist-stage/  (index.html + assets)
//   npm run dev:stage        -> vite dev server on :4733, add ?ws=ws://<hub>:8081
//                               to point at a running hub
import { existsSync, renameSync } from "node:fs";
import { fileURLToPath } from "node:url";
import { defineConfig } from "vite";
import { svelte } from "@sveltejs/vite-plugin-svelte";

const kioskCore = fileURLToPath(new URL("./src/kiosk/tauri-core.ts", import.meta.url));
const kioskEvent = fileURLToPath(new URL("./src/kiosk/tauri-event.ts", import.meta.url));
const outDir = fileURLToPath(new URL("./dist-stage/", import.meta.url));

export default defineConfig({
  plugins: [
    svelte(),
    {
      // The hub's static server serves `index.html` for `/`; vite names
      // the output after the input HTML, so rename it after the build.
      name: "stage-kiosk-index",
      closeBundle() {
        if (existsSync(outDir + "stage-kiosk.html")) {
          renameSync(outDir + "stage-kiosk.html", outDir + "index.html");
        }
      },
    },
  ],
  clearScreen: false,
  server: { port: 4733, strictPort: true },
  resolve: {
    alias: [
      { find: "@tauri-apps/api/core", replacement: kioskCore },
      { find: "@tauri-apps/api/event", replacement: kioskEvent },
    ],
  },
  build: {
    outDir: "dist-stage",
    emptyOutDir: true,
    rollupOptions: {
      input: fileURLToPath(new URL("./stage-kiosk.html", import.meta.url)),
    },
  },
});
