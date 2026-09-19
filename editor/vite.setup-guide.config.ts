import { defineConfig } from "vite";
import { svelte } from "@sveltejs/vite-plugin-svelte";
import { fileURLToPath } from "node:url";
// A single shareable HTML file. The same component powers the Desktop wizard.
export default defineConfig({
  plugins: [svelte(), {
    name: "standalone-guide",
    enforce: "post",
    generateBundle(_, bundle) {
      const html = bundle["setup-guide.html"];
      if (!html || html.type !== "asset") throw new Error("Missing guide HTML");
      let source = String(html.source);
      for (const [name, asset] of Object.entries(bundle)) {
        if (asset.type === "chunk") {
          if (asset.imports.length || asset.dynamicImports.length) throw new Error("Guide must have no external JS chunks");
          source = source.replace(/<script[^>]*src="[^"]+"[^>]*><\/script>/, () => `<script type="module">${asset.code.replace(/<\/script/gi,"<\\/script")}</script>`);
          delete bundle[name];
        } else if (name.endsWith(".css")) {
          source = source.replace(/<link[^>]*href="[^"]+\.css"[^>]*>/, () => `<style>${asset.source}</style>`);
          delete bundle[name];
        }
      }
      html.source = source;
    },
  }],
  build: { outDir: "../dist/setup-guide", emptyOutDir: true, modulePreload: false, rollupOptions: { input: fileURLToPath(new URL("./setup-guide.html",import.meta.url)) } },
});
