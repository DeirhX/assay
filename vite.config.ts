import { defineConfig } from "vite";

// The Python server (tools/serve.py) owns the API, jobs, auth, and static
// "Plan"/report pages. Vite only builds the SPA client.
//
//   dev:   `npm run dev`  -> Vite dev server on :5173 with HMR, proxying /api
//          (and the root *.html report pages) to the Python server on :6060.
//          Keep `py tools/serve.py` running alongside it for the API.
//   build: `npm run build` -> static bundle in web/dist, which serve.py serves
//          in prod (it prefers web/dist/ over the raw web/ source when present).
const API = "http://127.0.0.1:6060";

export default defineConfig({
  root: "web",
  publicDir: false,
  build: {
    outDir: "dist", // relative to root -> web/dist
    emptyOutDir: true,
    sourcemap: true,
  },
  server: {
    port: 5173,
    strictPort: false,
    proxy: {
      "/api": { target: API, changeOrigin: false },
      // Root static report pages (next-steps.html, losers.html, ...) are served
      // by Python; forward them so links work in the dev server too.
      "^/[a-z0-9-]+\\.html$": { target: API, changeOrigin: false },
      // ...and the sibling assets those report pages pull in. Kept explicit so
      // the SPA's own /style.css (served by Vite from web/) is NOT proxied.
      "^/(site\\.css|privacy\\.css|privacy\\.js)$": { target: API, changeOrigin: false },
    },
  },
});
