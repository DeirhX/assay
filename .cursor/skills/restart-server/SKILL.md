---
name: restart-server
description: Pull the latest origin/main into the current branch, then restart the local web app (serve.py on :6060 + Vite on :5173), force-killing every stale instance first. Use when asked to restart the server, pull main and restart, sync and relaunch, refresh main and bounce the dev servers, or "main-pull and run it".
---

# Pull main + restart the server

Two steps, in order: (1) merge the latest `origin/main` into the current branch,
then (2) restart the web app fresh, killing all other instances. Step 2 is a
plain re-run of `run-web`, which already force-kills stale supervisors, the API,
Vite, and any process bound to :6060/:5173.

## Workflow

```
- [ ] 1. Pull origin/main into the current branch (fetch → assess → merge/ff)
- [ ] 2. Verify the merged tree (repo tests/build)
- [ ] 3. Restart the server, killing all other instances (run-web.ps1)
- [ ] 4. Confirm both halves are up
```

## 1. Pull origin/main

Batch the assessment before touching anything:
```bash
git fetch origin
git rev-parse --abbrev-ref HEAD
git status --short
git rev-list --count HEAD..origin/main          # commits on main you lack
git log --oneline origin/main..HEAD              # your commits not on main
git diff --stat (git merge-base HEAD origin/main) origin/main   # what main changed
```

- If `HEAD..origin/main` is `0`, the branch already has main — say so, skip to step 3.
- If on `main`/`master`, this skill's merge step is moot — `git pull --ff-only`, then step 3.

**Clean tree** → merge directly (fast-forwards when you have no own commits):
```bash
git merge origin/main --no-edit
```

**Dirty tree** (uncommitted work you want to keep) → never merge over it. Stash
the tracked files (leave the private `data` submodule pointer alone), merge, pop:
```bash
git stash push -m "pre-main-pull" -- <changed files, excluding data>
git merge origin/main --no-edit
git stash pop
```
Resolve any pop/merge conflict **per file** with judgment (don't blanket
`-X ours/theirs`); confirm none remain: `git diff --name-only --diff-filter=U`
is empty. Sanity-check auto-merged files too (e.g. `git diff HEAD:<f> <f>`).

Only commit if the user asked, or a merge commit is required (a real merge, not a
fast-forward). Use `git commit --no-edit`.

## 2. Verify the merged tree

A merge can compile yet break behavior. Run the repo's real checks:
```powershell
npm run typecheck ; npm run test
py -3 -m pytest tools/tests -q
```

## 3. Restart the server (kills all other instances)

```powershell
pwsh .cursor/skills/run-web/scripts/run-web.ps1
```
Run it **in the background** (don't block the session on it). It first kills the
prior run's supervisor, stray `serve.py`/`vite`, and any owner of :6060/:5173,
then starts the API (`serve.py --reload`) and Vite. This is the whole "kill all
other instances + restart" — no manual `Stop-Process` needed.

## 4. Confirm both halves are up

Poll the run-web terminal output for readiness:
- Vite: `ready in …` and `Local: http://127.0.0.1:5173/`
- API: `GET /api/... 200` lines (200s, not `ECONNREFUSED`)

Report the URL as the IPv4 literal **http://127.0.0.1:5173** (not `localhost` —
Windows may resolve `::1` first and stall ~2s per request).

## Gotchas

- **Backend edits reload automatically** while running (`serve.py --reload`), so
  a restart is only needed to guarantee a clean state or after killing zombies —
  but that's exactly this skill's job, so just re-run the script.
- **`/api/trade/orders` 502** after restart means the IBKR Client Portal Gateway
  is offline, not a code fault; `/api/trade/status` 200 confirms the API is fine.
- **`main` checked out in another worktree**: only `origin/main` is needed here;
  a failure syncing local `main` is harmless.
- Default shell is PowerShell 7 — no heredoc. Prefer `--no-edit`; for a custom
  merge message write it to a scratch file, `git commit -F <file>`, then delete it.
