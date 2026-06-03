---
name: perplexity-deep-research
description: Run Perplexity in-app Deep Research via a Playwright-controlled browser, using the Pro subscription's included 20/day quota instead of the pay-per-token API or the credit-billed Computer product. Use when the user wants automated, in-app Perplexity Deep Research (segment/ticker deep dives, multi-source narrative synthesis) driven from the agent, or mentions the playwright-pplx browser, perplexity.ai automation, or "free" Deep Research.
---

# Perplexity in-app Deep Research (browser automation)

Drives the **logged-in Perplexity web app** through a dedicated Playwright MCP server to run **Deep Research** queries. This path uses the **Pro subscription's included quota (~20 Deep Research/day)** — it does **not** spend API tokens and does **not** spend Computer credits.

## When to use which Perplexity path

| Path | Billing | Use for |
|------|---------|---------|
| **In-app Deep Research** (this skill) | Included in Pro (~20/day) | Narrative synthesis, segment/ticker deep dives, "GPT Deep Research"-style reports |
| `perplexity` MCP (Sonar API) | Pay per token + per search | Programmatic/numeric pulls, scripted verification, structured output |
| **Computer** (`/computer/tasks`) | Burns credits (none included on Pro) | Multi-step agentic tasks, dashboards, spreadsheets — **avoid for plain research** |

## ⚠️ The credit trap — read this first

There are **two** things on the Perplexity home screen with "deep research" in the name. They are NOT the same:

- ❌ **"Run deep research" starter card** (under "Put Computer to work") → routes to `/computer/tasks/<id>` → this is the **Computer** product and **spends credits**. Do **not** click it for ordinary research.
- ✅ **"Deep research"** option inside the **Search button's dropdown** in the composer → the included-quota search mode. After submitting, the URL becomes `/search/<uuid>`.

**Verification rule:** after submitting, the URL MUST be `/search/...`. If it is `/computer/...`, you triggered the paid path — abort (navigate away, do not answer its clarifying questions).

## One-time setup

### 1. Dedicated MCP server in `~/.cursor/mcp.json`

A separate server with its **own persistent profile** so the Perplexity login survives across runs and does not collide with other Playwright usage. Surfaces in tool calls as **`user-playwright-pplx`**.

```json
"playwright-pplx": {
  "command": "npx",
  "args": [
    "-y", "@playwright/mcp@latest",
    "--browser", "chromium",
    "--user-data-dir", "C:\\Users\\<you>\\.cursor\\pplx-chrome-profile"
  ]
}
```

Use bundled `chromium` (no admin rights). After editing `mcp.json`, reload MCP servers in Cursor settings (or restart Cursor).

### 2. Install the browser binary (once)

```powershell
npx -y @playwright/mcp@latest install-browser chrome-for-testing
```

### 3. Log in (once per profile)

Navigate to `https://www.perplexity.ai`. If it redirects to a Google/sign-in wall, **the user must complete login manually** in the launched window. The persistent profile then keeps the session. Confirm by re-navigating home and checking the snapshot shows the user avatar + "Perplexity Pro" badge.

## Tool quirks (this MCP server)

- `browser_click` / `browser_type` take **`target`** (the snapshot ref like `e185`, or a CSS selector) — **not** `element`+`ref`. `element` is only an optional human-readable description. Passing `ref` errors with `expected string, received undefined` on `target`.
- **Snapshots are huge** (full sidebar + history every time). Do NOT poll with `browser_snapshot`. Use `browser_evaluate` with a small JS probe instead — orders of magnitude cheaper.
- Composer textbox selector is stable: `#ask-input`.

## Run workflow

```
- [ ] 1. Navigate to https://www.perplexity.ai (confirm logged in)
- [ ] 2. Dismiss cookie dialog if present ("Only necessary")
- [ ] 3. Open the Search-mode dropdown (chevron on the "Search" pill)
- [ ] 4. Click the "Deep research" menuitemradio
- [ ] 5. Type the query into #ask-input and submit
- [ ] 6. Verify URL is /search/... (NOT /computer/...)
- [ ] 7. Poll cheaply until complete; scrape the report
```

**Step 3–4: select Deep research mode.** The dropdown options are `role=menuitemradio`. Robust click via evaluate (avoids ref churn):

```js
() => {
  const els = [...document.querySelectorAll('[role=menuitemradio],[role=menuitem],div,button')];
  const t = els.find(e => (e.innerText||'').trim() === 'Deep research' && e.offsetParent !== null);
  if (!t) return 'not found';
  t.click();
  return 'clicked';
}
```

If the dropdown isn't open yet, first `browser_click` the `Search` pill's chevron (target = the chevron ref from a one-off snapshot, or click the `Search` button).

**Step 5: submit the query.**

```
browser_type  target="#ask-input"  text="<the research prompt>"  submit=true
```

**Step 6: verify the free path.** Read the returned page URL. `/search/<uuid>` = correct (included quota). `/computer/...` = paid Computer — abort.

**Step 7: poll for completion** with `browser_evaluate` (NOT snapshots). Deep Research runs several minutes. A run is **still going** while a Stop button exists; it's **done** when the Stop button is gone and the report/citations have rendered:

```js
() => {
  const m = document.querySelector('main');
  const stop = !!document.querySelector('button[aria-label*=stop i]');
  return { url: location.pathname, running: stop, len: m ? m.innerText.length : 0 };
}
```

Poll every ~60–90s (sleep between checks). When `running` is false and `len` is large (full report, not just the echoed prompt), scrape the answer:

```js
() => (document.querySelector('main')?.innerText || '')
```

## Notes

- Keep prompts aligned with any prior API run so outputs are directly comparable.
- Deep Research output is **narrative synthesis** — treat its numbers as claims to verify against a numeric source (IBKR snapshot / Sonar API / `verify_claims.py`), not as ground truth.
- The 20/day quota is shared with manual in-app usage; don't burn it on smoke tests — validate plumbing with a single real query.
