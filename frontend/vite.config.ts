import { defineConfig } from "vite";
import react from "@vitejs/plugin-react";

export default defineConfig({
  plugins: [react()],
  server: {
    port: 5173,
    // The backend's CORS_ORIGINS already allows localhost:5173, so a proxy is
    // not strictly needed. It is here so the frontend can call "/api/..."
    // without hardcoding a host, which makes a later deployment one env var
    // rather than a code change.
    proxy: {
      "/api": { target: "http://localhost:8000", changeOrigin: true },
      "/health": { target: "http://localhost:8000", changeOrigin: true },
    },
  },
});
