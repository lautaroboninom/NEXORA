import { defineConfig } from "vite";
import react from "@vitejs/plugin-react";
import path from "node:path";

export default defineConfig(() => ({
  plugins: [react()],
  server: {
    port: 5173,
    host: true,
    allowedHosts: ["localhost", "127.0.0.1", "100.91.53.60", "nexora.tail7bb880.ts.net"],
    proxy: {
      "/api": {
        target: process.env.VITE_DEV_API_PROXY || "http://localhost:18100",
        changeOrigin: true,
      },
    },
  },
  resolve: {
    alias: {
      "@": path.resolve(__dirname, "./src"),
    },
  },
  build: {
    // Habilitar sourcemaps opcionalmente para debug de produccion
    sourcemap: process.env.VITE_SOURCEMAP === "1",
  },
}));
