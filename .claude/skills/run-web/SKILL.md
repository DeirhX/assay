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
pwsh .claude/skills/run-web/scripts/run-web.ps1
```

Then open http://127.0.0.1:5173 (use the IPv4 literal, **not** `localhost` —
Vite and the backend are both pinned to IPv4 `127.0.0.1`; `localhost` can
resolve to IPv6 `::1` first on Windows and stall ~2s per request). Both halves
pick up changes automatically:

- **Frontend** (`web/src/`, CSS): Vite HMR, instant.
- **Backend** (`tools/*.py`): the script runs `serve.py --reload`, whose
  supervisor auto-restarts the API in place on every Python edit. It
  syntax-checks the file first (a broken edit keeps the last good version
  serving) and defers the restart while a Deep Research job is in flight.

The script kills any stale instances first — a prior run's **supervisor**
(`pwsh`/`powershell` still running this script and holding port 6060 via its
`npm run dev`), plus stray `serve.py` / `vite` procs and anything bound to
:6060/:5173 — then installs npm deps if `node_modules` is missing and runs the
backend and frontend together. So just re-run it to take over a previous
session; no manual `Stop-Process` needed. `Ctrl+C` stops both.

## Options

- `-Mode prod` — build the SPA and serve everything from :6060 via `serve.py`
  (no Vite, no HMR). Open http://127.0.0.1:6060.
- `-NoInstall` — skip the `npm install` check.
- `-ApiPort <n>` — backend port (default 6060).

## Gotchas

- Python auto-restart only happens in `dev` mode (it passes `--reload`). In
  `prod` mode you must re-run the script after `tools/*.py` edits.
- A reload restart drops in-flight non-deep-research requests; the browser just
  retries on the next poll. Deep Research jobs are protected (restart deferred).
- The backend binds `127.0.0.1` only — never expose it.
- `SEC_USER_AGENT` defaults to a local-dev string if unset; override it with
  your own contact info for polite SEC EDGAR requests.

## Manual equivalent

```powershell
py -3 tools/serve.py        # backend :6060
npm run dev                 # frontend :5173 (separate terminal)
```
