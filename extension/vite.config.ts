import { resolve } from "node:path";
import * as fs from "node:fs";
import { defineConfig } from "vite";
import react from "@vitejs/plugin-react";

// Multi-entry build that emits each MV3 piece (popup HTML, content script,
// service worker) as a separate bundle. We avoid @crxjs/vite-plugin on purpose
// to keep the toolchain small; in exchange we hand-maintain manifest.json and
// a post-build copy step.
export default defineConfig({
  plugins: [
    react(),
    {
      name: "copy-static-assets",
      apply: "build",
      closeBundle() {
        const root = process.cwd();
        const dist = resolve(root, "dist");
        if (!fs.existsSync(dist)) fs.mkdirSync(dist, { recursive: true });
        fs.copyFileSync(
          resolve(root, "manifest.json"),
          resolve(dist, "manifest.json"),
        );
        const publicDir = resolve(root, "public");
        if (fs.existsSync(publicDir)) {
          for (const f of fs.readdirSync(publicDir)) {
            const src = resolve(publicDir, f);
            const dst = resolve(dist, f);
            const stat = fs.statSync(src);
            if (stat.isFile()) fs.copyFileSync(src, dst);
          }
        }
      },
    },
  ],
  resolve: {
    alias: { "@": resolve(__dirname, "src") },
  },
  build: {
    outDir: "dist",
    emptyOutDir: true,
    sourcemap: true,
    rollupOptions: {
      input: {
        popup: resolve(__dirname, "src/popup/popup.html"),
        overlay: resolve(__dirname, "src/overlay/overlay.html"),
        "service-worker": resolve(__dirname, "src/background/service-worker.ts"),
        "content-script": resolve(__dirname, "src/content/content-script.ts"),
      },
      output: {
        // Predictable filenames so the manifest can reference them.
        entryFileNames: (chunk) => {
          if (chunk.name === "service-worker") return "service-worker.js";
          if (chunk.name === "content-script") return "content-script.js";
          return "assets/[name]-[hash].js";
        },
        chunkFileNames: "assets/[name]-[hash].js",
        assetFileNames: "assets/[name]-[hash][extname]",
      },
    },
  },
  test: {
    environment: "happy-dom",
    globals: true,
    include: ["tests/**/*.test.ts", "tests/**/*.test.tsx"],
  },
});
