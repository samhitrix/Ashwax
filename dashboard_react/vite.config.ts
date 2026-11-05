import { defineConfig } from "vite";
import react from "@vitejs/plugin-react";
import tailwindcss from "@tailwindcss/vite";

export default defineConfig({
  plugins: [react(), tailwindcss()],
  server: {
    port: 5173,
    // Docker sets VITE_HOST=0.0.0.0 so the published port is reachable; the API
    // stays on localhost inside the container and is only reached via the proxy.
    host: process.env.VITE_HOST || "localhost",
    strictPort: true, // fail rather than silently drift to 5174 — launcher frees 5173 first
    proxy: {
      "/api": { target: "http://localhost:3000", changeOrigin: true },
      "/ws": { target: "ws://localhost:3000", ws: true },
    },
  },
});
