---
name: run-web
description: Start the rebalancing research web app locally — the Python API server (tools/serve.py on :6060) and the Vite frontend (:5173). Use when asked to run, launch, start, serve, or restart the web app, console, frontend, or dev server.
---

# Run the Web App

Launches both halves of the app together: the stdlib Python API
(`tools/serve.py`, port 6060, localhost-only) and the Vite dev server
(port 5173, HMR, proxying `/api` to the backend).

## Run it

```powershell
pwsh .cursor/skills/run-web/scripts/run-web.ps1
```

Then open http://localhost:5173. Edits under `web/src/` hot-reload.

The script kills any stale `serve.py` / `vite` first (avoids the recurring
port-6060 zombie), installs npm deps if `node_modules` is missing, then runs
the backend and frontend together. `Ctrl+C` stops both.

## Options

- `-Mode prod` — build the SPA and serve everything from :6060 via `serve.py`
  (no Vite, no HMR). Open http://127.0.0.1:6060.
- `-NoInstall` — skip the `npm install` check.
- `-ApiPort <n>` — backend port (default 6060).

## Gotchas

- **Python edits do not hot-reload.** After changing `tools/*.py`, re-run the
  script (or restart `serve.py`); its auto-reload does not rebind the port.
- The backend binds `127.0.0.1` only — never expose it.
- `SEC_USER_AGENT` defaults to a local-dev string if unset; override it with
  your own contact info for polite SEC EDGAR requests.

## Manual equivalent

```powershell
py -3 tools/serve.py        # backend :6060
npm run dev                 # frontend :5173 (separate terminal)
```
