---
name: ship-pr
description: Open or update a pull request for recent changes, set it to squash auto-merge once CI passes, and sync local main after it lands. Use when the user says "PR merge", "ship it", "ship this", "open a PR and auto-merge", or "merge recent changes".
---

# Ship a PR

Codifies this repo's "PR merge" flow: branch → commit → push → PR → **squash
auto-merge after the pipeline finishes** → sync `main`. The agent handles
judgment (staging, commit message, PR body); `scripts/ship.ps1` handles the
deterministic push → PR → auto-merge → sync tail.

## Repo facts (don't relearn these)

- **Squash merges only.** Every merged PR is one squash commit (`… (#N)`).
- **Branch protection requires CI** to pass before a merge is allowed:
  *Python lint + unittest suite* and *Frontend typecheck + tests + build*.
- **Auto-merge may be DISABLED at the repo level** (`gh pr merge --auto` then
  fails with "Auto merge is not allowed for this repository"). If so, fall back
  to: wait for both checks to go green, then `gh pr merge <N> --squash
  --delete-branch` (a manual squash merge, which branch protection permits once
  CI is green). `scripts/ship.ps1` attempts `--auto` and you handle the
  fallback if it's refused.
- **Default shell is PowerShell 7, no heredoc.** Write commit/PR text to files
  (`COMMIT_MSG.txt`, `PR_BODY.txt`) and use `-F` / `--body-file`. Those names
  plus `.env*`, `secrets.*`, `*.token` are already gitignored.
- **`data/` is a PRIVATE submodule.** NEVER stage/bump its pointer in a public
  PR unless the user explicitly asks. It shows as modified — leave it unstaged.

## Workflow

```
- [ ] 1. Inspect: git status, git diff --stat, git log --oneline -10, branch
- [ ] 2. Branch: if on main, create feature/<slug>; else reuse current branch
- [ ] 3. Stage ONLY intended files (exclude data submodule + secrets); scan diff
- [ ] 4. Verify: run tests + build locally
- [ ] 5. Commit with a clear message (COMMIT_MSG.txt -> git commit -F)
- [ ] 6. Run scripts/ship.ps1 (push -> create/update PR -> auto-merge -> sync)
```

### 1. Inspect
Run `git status`, `git diff --stat`, `git log --oneline -10`, and
`git rev-parse --abbrev-ref HEAD` (batch them). Confirm what actually changed.

### 2. Branch
- On `main`/`master`: `git checkout -b feature/<short-kebab-slug>` describing the
  change. Call `SetActiveBranch` after creating it.
- Already on a feature branch: reuse it. Pushing updates the existing PR — no new
  PR is created.

### 3. Stage safely
Stage explicit paths, not `git add -A`. Typical: `git add tools web .cursor …`.
- **Exclude `data`** (private submodule). After staging, confirm `git status`
  shows `data` as unstaged (leading space-`M`), not staged (`M ` in column 1).
- Scan the staged diff for secrets (token/api_key/password/BEGIN PRIVATE KEY).
  `rg` may be absent in PowerShell — use `git diff --cached | Select-String`.
  If anything sensitive appears, STOP and ask.

### 4. Verify locally — run EVERYTHING CI runs, not just the build
CI has two required jobs and will block the PR if either fails. Reproduce both
locally first; `npm run build` alone is NOT enough (it skips lint/typecheck/tests
— exactly the checks that catch real breakage).

```powershell
py -3 -m pytest tools/tests -q          # Python lint + unittest job
npm install                              # ensure the toolchain matches package.json
npm run lint; npm run typecheck; npm run test; npm run build   # Frontend job, in CI's order
```

- **Run `npm install` first.** A stale `node_modules` (missing `eslint`/`vitest`
  after a dependency bump landed on `main`) makes the frontend checks silently
  un-runnable locally, so you push a lint/type error CI then rejects. If `lint`
  or `test` reports the binary "is not recognized", you skipped this.
- Any failure here = fix before pushing. A red CI run wastes a full cycle.

### 5. Commit
Write the message to `COMMIT_MSG.txt`, then `git commit -F COMMIT_MSG.txt`.
First line ≤ ~72 chars, imperative; body as grouped bullets explaining the *why*.

### 6. Ship
Write the PR body to `PR_BODY.txt` (Summary + Test plan), then:

```powershell
pwsh .cursor/skills/ship-pr/scripts/ship.ps1 -Title "<pr title>" -BodyFile PR_BODY.txt
```

The script: pushes `-u origin HEAD`; finds the branch's PR or creates one;
enables `--squash --auto --delete-branch`; then waits for CI and, once MERGED,
checks out `main` and `git pull --ff-only`. Finally delete the scratch files and
call `SetActiveBranch main`.

- `-NoWait` — set auto-merge and return immediately (don't poll for merge/sync).
- `-Base <branch>` — base branch (default `main`).

## Safety rules

- NEVER `git push --force` to `main`/`master`; never bypass hooks or branch
  protection with `--admin` unless the user explicitly asks.
- NEVER bump the `data` submodule pointer in a public PR unless asked.
- If CI fails, fix the cause and push a NEW commit — do not amend a pushed commit
  or force-merge.

## Updating an existing PR

If a PR already exists for the current branch, just commit + run the script (or
`git push`). The push updates the PR in place; re-running the script re-asserts
auto-merge. Don't open a duplicate.
