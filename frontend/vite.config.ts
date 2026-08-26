import { defineConfig } from "vite";
import react from "@vitejs/plugin-react";

export default defineConfig({
  plugins: [react()],
  server: {
    // Listen on every interface, not only the container's loopback.
    // Without this, Vite binds to 127.0.0.1 INSIDE the container, the
    // published port maps to nothing, and the browser gets a connection
    // reset while the container's own logs look perfectly healthy.
    host: true,
    port: 5173,
    // Fail loudly if 5173 is taken rather than quietly moving to 5174,
    // which would no longer match the port published in compose or the
    // origin allowed by CORS.
    strictPort: true,
    watch: {
      // File changes on a Windows bind mount do not raise the filesystem
      // events Linux containers watch for, so hot reload silently stops
      // working. Polling costs a little CPU and actually notices.
      usePolling: true,
      interval: 300,
    },
  },
});
