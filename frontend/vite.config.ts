import { defineConfig } from "vite";
import react from "@vitejs/plugin-react";

/**
 * The API and the dashboard are the SAME ORIGIN now (Phase 12).
 *
 * The session is a cookie, and a cookie set by the API's host is not sent on
 * a request coming from a different site - that is what SameSite=Lax is for.
 * So in production the built bundle is served by the FastAPI app itself under
 * /dashboard, and in development the dev server proxies /api and the auth
 * routes through to it. Either way the browser only ever talks to one origin
 * and the cookie question never comes up.
 *
 * The proxy runs in the DEV SERVER, not in the browser, which is what makes
 * `http://app:8000` a usable target under docker compose. The old
 * VITE_API_BASE could never be a compose service name for exactly the
 * opposite reason: it was resolved by the browser, which has never heard of
 * a compose network.
 */

// Declared rather than pulled in with @types/node. This file is the only
// place in the project that touches process.env, and one line here is a
// cheaper dependency than the whole Node typings package.
declare const process: { env: Record<string, string | undefined> };

// Where the dev server forwards API calls. The host default is localhost;
// docker-compose overrides it with the `app` service name.
const target = process.env.VITE_PROXY_TARGET ?? "http://localhost:8000";

// Everything the dashboard needs that is served by FastAPI rather than Vite.
// /login and /auth/callback are here so the whole sign-in round trip can be
// done against the dev server without leaving :5173.
const proxied = ["/api", "/login", "/logout", "/auth", "/health"];

export default defineConfig({
  plugins: [react()],

  // The bundle is served from /dashboard/ in production, so asset URLs have
  // to be built with that prefix. Without it index.html asks for /assets/...
  // which resolves to the FastAPI app's root and returns a 404.
  base: "/dashboard/",

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
    proxy: Object.fromEntries(
      proxied.map((path) => [path, { target, changeOrigin: false }]),
    ),
    watch: {
      // File changes on a Windows bind mount do not raise the filesystem
      // events Linux containers watch for, so hot reload silently stops
      // working. Polling costs a little CPU and actually notices.
      usePolling: true,
      interval: 300,
    },
  },
});
