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

## Two ways to run

1. **Automated (website-driven)** — Pipeline tab → **Run Deep Research**. A local
   Playwright worker (`tools/pplx_deep_research.py`) drives a logged-in session in
   an **off-screen** browser and auto-saves artifacts. Preferred for hands-off
   runs. See "Automated worker" below.
2. **Agent-driven (this MCP)** — drive `user-playwright-pplx` yourself with the
   manual workflow below. Use for debugging selectors or when the worker breaks.

## Automated worker (`tools/pplx_deep_research.py`)

Hard-won facts baked into the worker (do not relearn these the hard way):

- **Headless is blocked.** Perplexity is behind Cloudflare; a headless browser
  gets a challenge page (`btns: ['Cloudflare','Privacy']`, no `#ask-input`). Run
  **headed but off-screen** (`--window-position=-2400,-2400`). `headless` mode
  exists only as an experiment toggle.
- **`#ask-input` is NOT a logged-in signal** — anonymous users get the composer
  too. Detect login by the **absence of a "Sign In" CTA**, and poll for ~6s
  because that CTA renders a beat *after* the composer (else you get a false
  "logged in").
- **The mode menu is Radix** — it only opens on a **real pointer click**
  (Playwright `get_by_role("button", name="Search", exact=True).click()`), never
  a synthetic `element.click()`. Then click the `menuitemradio` named exactly
  `Deep research`. Beware: `has-text("Search")` also matches "Run deep**research**"
  (the Computer credit trap) — use exact role names.
- **Dedicated automation profile.** The worker uses `PPLX_PROFILE_DIR`
  (default `~/.cursor/pplx-automation-profile`), deliberately **separate** from
  the MCP `user-playwright-pplx` browser's `pplx-chrome-profile`. Chromium
  profiles are single-writer locked, so a shared profile would make the worker
  and the MCP browser fight over the lock, and their Chrome versions could skew.
  The automation profile needs its own one-time login (Setup tab → "Set up
  Perplexity login", or `--login`).
- One browser job at a time (profile lock + scarce quota). Validate plumbing with
  `--dry-run` (selects mode, never submits, spends no quota).

## Agent-driven run workflow

```
- [ ] 1. Open the local site (`py -3 tools\serve.py`, then http://127.0.0.1:6060)
- [ ] 2. In the Pipeline tab, choose/create the research segment and build the prompt
- [ ] 3. Navigate to https://www.perplexity.ai (confirm logged in)
- [ ] 4. Dismiss cookie dialog if present ("Only necessary")
- [ ] 5. Open the Search-mode dropdown by clicking the actual "Search" pill in the composer
- [ ] 6. Click the exact "Deep research" menuitemradio in that dropdown
- [ ] 6a. Verify the composer mode pill now says "Deep research" and is pressed
- [ ] 7. Type the query into #ask-input and submit
- [ ] 8. Verify URL is /search/... (NOT /computer/...)
- [ ] 9. Poll cheaply until complete; scrape the report text and citation URLs
- [ ] 10. Save report + Links-tab citations in the Pipeline tab, then run the review gate
```

**Step 3–4: select Deep research mode.** Do **not** rely on typing `/` as the primary path; it can expose a command menu and make it easy to click the wrong container or fail to persist the mode. Use the composer mode pill:

1. Take a focused snapshot of the composer.
2. Click the actual `Search` button/pill, not a sidebar history item and not the "Run deep research" Computer starter card.
3. In the resulting menu, click the exact `menuitemradio` named `Deep research`.
4. Verify the composer button changes from `Search` to `Deep research` and is pressed.

Reliable snapshot-driven version:

```text
browser_snapshot target=<composer container> depth=10
browser_click target=<Search pill ref> element="Search mode pill"
browser_snapshot depth=8
browser_click target=<Deep research menuitemradio ref> element="Deep research search mode menu item"
browser_snapshot target=<composer container> depth=10
```

Expected verified state in the composer snapshot:

```yaml
button "Deep research" [pressed]
```

Robust JS fallback for the menu click, after the real Search pill dropdown is open:

```js
() => {
  const els = [...document.querySelectorAll('[role=menuitemradio],[role=menuitem]')]
    .filter(e => !!(e.offsetWidth || e.offsetHeight || e.getClientRects().length));
  const t = els.find(e => (e.innerText||e.textContent||'').trim() === 'Deep research');
  if (!t) return 'not found';
  t.click();
  return 'clicked';
}
```

If the dropdown is not open yet, first click the `Search` pill ref from a one-off focused composer snapshot. Avoid broad DOM searches for `/deep research/i`: they can match prior history items or the Computer starter card instead of the menu.

**Step 5: submit the query.**

```
browser_type  target="#ask-input"  text="<the research prompt>"  submit=true
```

**Step 6: verify the free path.** Read the returned page URL. `/search/<uuid>` = correct (included quota). `/computer/...` = paid Computer — abort.

Also verify the page text indicates a Deep Research run, e.g. phrases like:

- `Starting a deep research review...`
- `Gathering ...`
- the composer/mode context still includes `Deep research`

If the output immediately looks like a normal search answer and does not show a deep-research workflow, treat it as a failed test. Do not save it as Deep Research output; return to the home composer and repeat mode selection from the real Search pill.

**Step 7: poll for completion** with `browser_evaluate` (NOT snapshots). Deep Research runs several minutes. Verified against 8 saved runs, the reliable **done** markers are **"Completed N steps"** and an **"N sources"** button (both 8/8); the prose footer is split between "Prepared **by**" (5/8) and "Prepared **with**" (3/8), so match `by|with` and treat it only as a third fallback. Do **NOT** gate on the Stop button disappearing — it lingers ~30-60s into finalization and is the main source of detection lag:

```js
() => {
  const m = document.querySelector('main');
  const body = document.body.innerText || '';
  const stop = !!document.querySelector('button[aria-label*=stop i]');
  const done = /\bcompleted \d+ steps?\b/i.test(body)
    || [...document.querySelectorAll('button')].some(b => /^\d+\s+sources?$/i.test((b.innerText||'').trim()))
    || /prepared (by|with) deep research/i.test(body);
  return { url: location.pathname, running: stop, done, len: m ? m.innerText.length : 0 };
}
```

Poll every ~10s. Accept completion as soon as `done` is true and `len` has stopped growing across two reads (the answer settled) — even if `running` is still true. Then scrape the answer text:

```js
() => (document.querySelector('main')?.innerText || '')
```

Then extract citation/source URLs from the `Links` tab, not only from the answer body. Do this automatically for every saved report; plain `innerText` collapses citations into labels such as `cnbc` or `+1`, and scanning the Answer tab can expose only a subset of sources.

Preferred citation extraction workflow:

1. Click the `Links` tab in the answer mode tabs.
2. Verify the URL usually changes to include `?sm=r`.
3. Extract unique anchors from `main`.
4. Save the full source list before returning to the Answer tab.

```js
() => {
  const main = document.querySelector('main') || document;
  const seen = new Set();
  return [...main.querySelectorAll('a[href]')]
    .map(a => ({
      label: (a.innerText || a.textContent || '').trim()
        || a.getAttribute('aria-label')
        || a.getAttribute('title')
        || 'source',
      href: a.href,
      aria: a.getAttribute('aria-label'),
      title: a.getAttribute('title')
    }))
    .filter(x => {
      if (!x.href || x.href.startsWith('javascript:') || seen.has(x.href)) return false;
      seen.add(x.href);
      return true;
    });
}
```

When saving markdown manually, include a citation section before the report body:

```markdown
## Extracted Citation Links

- `label`: https://example.com/source
```

Always save a sidecar JSON for downstream tooling. The website's Pipeline tab does
this via `data/research/deep/<segment>-<date>.sources.json`:

```json
{
  "source_url": "https://www.perplexity.ai/search/...",
  "mode": "perplexity_in_app_deep_research",
  "citations": [
    {"label": "cnbc", "href": "https://..."}
  ]
}
```

If only a few URLs are visible, you probably extracted from the Answer tab, not the Links tab. Click `Links` and retry. If the Links tab is unavailable or still exposes only a few URLs, save what is visible and note that additional collapsed `+N` labels were not exposed in the current DOM state.

## Saving and review gate

The normal storage path is website-managed, not hand-edited:

1. Paste the completed report into the Pipeline tab.
2. Paste the Links-tab citation JSON.
3. Save the artifact. This creates:
   - `data/research/deep/<segment>-<date>.md`
   - `data/research/deep/<segment>-<date>.sources.json`
4. Run the review gate from the Pipeline tab. This creates:
   - `data/research/deep/<segment>-<date>.review.md`
   - `data/research/deep/<segment>-<date>.target-proposal.json`

The review gate compares source quality, local deterministic ticker JSON, current
holdings, and `data/target-model.json`. Perplexity may propose thesis shifts; it
does not get to mutate allocation targets without explicit approval.

## Notes

- Keep prompts aligned with any prior API run so outputs are directly comparable.
- Deep Research output is **narrative synthesis** — treat its numbers as claims to verify against a numeric source (IBKR snapshot / Sonar API / `verify_claims.py`), not as ground truth.
- The 20/day quota is shared with manual in-app usage; don't burn it on smoke tests — validate plumbing with a single real query.
