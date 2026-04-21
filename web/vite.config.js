import { defineConfig } from "vite";
import react from "@vitejs/plugin-react";
import path from "node:path";

export default defineConfig(() => ({
  plugins: [react()],
  server: { port: 5173, host: true },
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
