import { defineConfig } from "vite";
import react from "@vitejs/plugin-react";

// Static, backend-less build of the UI over the frozen synthetic snapshot (see src/demo). One JS + one CSS file, inlined by scripts/inline-demo.mjs.
export default defineConfig({
  plugins: [react()],
  base: "./",
  define: { "import.meta.env.VITE_DEMO": JSON.stringify("1") },
  build: { outDir: "dist-demo", cssCodeSplit: false, assetsInlineLimit: 100000000, rollupOptions: { output: { inlineDynamicImports: true, entryFileNames: "demo.js", assetFileNames: "demo.[ext]" } } },
});
