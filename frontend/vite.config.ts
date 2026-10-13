import { defineConfig } from "vite";
import react from "@vitejs/plugin-react";

export default defineConfig({
  plugins: [react()],
  // Relative asset URLs, so the static build works from any subpath (GitHub Pages).
  base: "./",
  server: {
    port: 5173,
    strictPort: true,
    // The API serves committed packet files; no database is involved.
    proxy: { "/api": "http://127.0.0.1:8787" },
  },
  preview: {
    port: 5174,
    strictPort: true,
    proxy: { "/api": "http://127.0.0.1:8787" },
  },
  test: {
    environment: "jsdom",
    globals: true,
    exclude: ["node_modules/**"],
    coverage: { provider: "v8", include: ["src/**"], reporter: ["text"] },
  },
});
