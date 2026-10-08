import { defineConfig } from "vite";
import react from "@vitejs/plugin-react";

export default defineConfig({
  plugins: [react()],
  // `npm run dev` proxies /api to the customer API named by VITE_DEV_API (default: a local API on :8080).
  server: { port: 5173, host: "127.0.0.1", proxy: { "/api": { target: process.env.VITE_DEV_API ?? "http://127.0.0.1:8080", changeOrigin: true, secure: false, rewrite: (p) => p.replace(/^\/api/, "") } } },
  build: { outDir: "dist", sourcemap: false },
});
