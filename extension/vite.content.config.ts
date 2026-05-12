// Separate build for the content script.
// Content scripts must be classic scripts (no top-level import/export), so we
// compile to IIFE format here which inlines all dependencies into one file.
// The main vite.config.ts handles popup/overlay/calibration as ES modules.
import { resolve } from "node:path";
import { defineConfig } from "vite";

export default defineConfig({
  resolve: {
    alias: { "@": resolve(__dirname, "src") },
  },
  build: {
    outDir: "dist",
    emptyOutDir: false, // never wipe the main build's output
    sourcemap: true,
    rollupOptions: {
      input: {
        "content-script": resolve(__dirname, "src/content/content-script.ts"),
      },
      output: {
        format: "iife",
        entryFileNames: "[name].js",
      },
    },
  },
});
