#!/usr/bin/env python3
"""Drive Perplexity in-app Deep Research via Python Playwright.

This is the *automation* counterpart to the ``perplexity-deep-research`` skill.
It puppets the **logged-in Perplexity web app** to spend the Pro subscription's
included Deep Research quota (~20/day) instead of the metered Sonar API.

Honest caveats (read before trusting this):
* Perplexity sits behind Cloudflare and gates Deep Research behind login. Pure
  headless is detectable and flaky, so the default window mode is **headed but
  off-screen** (``--window-position`` far off the desktop). It is invisible to
  you but presents as a normal browser. ``headless`` is opt-in for experiments.
* The browser profile cannot be opened twice. This worker uses its OWN profile
  dir (``PPLX_PROFILE_DIR`` env, default ``~/.cursor/pplx-automation-profile``)
  so it never fights the agent's ``user-playwright-pplx`` MCP browser. Log in
  once via :func:`ensure_login` (the website's "Set up login" button).
* Deep Research is narrative synthesis. Treat its numbers as *claims to verify*,
  not ground truth. The review gate does that downstream.
* This is browser automation of a web app -- gray area vs ToS, and the 20/day
  quota is shared with your manual usage. Don't burn it on smoke tests; use
  ``dry_run`` (selects the mode but does not submit) to validate plumbing.

It is intentionally NOT imported by ``serve.py`` at module load. ``serve.py``
imports it lazily inside a worker thread, so a missing Playwright install simply
disables the automated path and leaves the manual paste flow working.
"""

from __future__ import annotations

import os
import time
from pathlib import Path
from typing import Callable, Optional

PPLX_HOME = "https://www.perplexity.ai"

# A run is "going" while a Stop button exists; "done" when it's gone and the
# report body has rendered (large innerText, not just the echoed prompt).
_POLL_JS = """() => {
  const m = document.querySelector('main');
  const stop = !!document.querySelector('button[aria-label*=stop i]');
  return { running: stop, len: m ? m.innerText.length : 0, url: location.pathname };
}"""

# Click the exact "Deep research" menuitemradio once the Search-mode dropdown is
# open. Filtered to visible elements and exact text to dodge history items and
# the credit-billed Computer "Run deep research" starter card.
_DEEP_MENU_JS = """() => {
  const els = [...document.querySelectorAll('[role=menuitemradio],[role=menuitem]')]
    .filter(e => !!(e.offsetWidth || e.offsetHeight || e.getClientRects().length));
  const t = els.find(e => (e.innerText||e.textContent||'').trim() === 'Deep research');
  if (!t) return 'not found';
  t.click();
  return 'clicked';
}"""

# The composer mode pill reflects the current mode, so its accessible name is one
# of these. It opens a Radix menu (aria-haspopup=menu) -- which only opens on a
# REAL pointer click, never a synthetic element.click().
_MODE_PILL_NAMES = ("Search", "Deep research", "Research", "Auto", "Pro Search", "Labs", "Best")

_REPORT_JS = "() => (document.querySelector('main')?.innerText || '')"

# Extract unique source anchors from the Links tab (collapsed in the answer body).
_LINKS_JS = """() => {
  const main = document.querySelector('main') || document;
  const seen = new Set();
  return [...main.querySelectorAll('a[href]')]
    .map(a => ({
      label: (a.innerText || a.textContent || '').trim()
        || a.getAttribute('aria-label') || a.getAttribute('title') || 'source',
      href: a.href
    }))
    .filter(x => {
      if (!x.href || x.href.startsWith('javascript:')) return false;
      try { const u = new URL(x.href); if (u.hostname.includes('perplexity.ai')) return false; }
      catch (e) { return false; }
      if (seen.has(x.href)) return false;
      seen.add(x.href);
      return true;
    });
}"""

_OPEN_LINKS_JS = """() => {
  const t = [...document.querySelectorAll('[role=tab],button,a')]
    .find(e => (e.innerText||'').trim() === 'Links');
  if (!t) return false;
  t.click();
  return true;
}"""

_REAL_UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/148.0.0.0 Safari/537.36"
)


def default_profile_dir() -> Path:
    return Path(os.environ.get("PPLX_PROFILE_DIR") or (Path.home() / ".cursor" / "pplx-automation-profile"))


def _noop(_msg: str) -> None:
    pass


def _launch(pw, *, window_mode: str, profile_dir: Path):
    """Launch a persistent (logged-in) context.

    window_mode: "offscreen" (invisible, robust), "visible" (for login), or
    "headless" (truly windowless, most detectable).
    """
    profile_dir.mkdir(parents=True, exist_ok=True)
    headless = window_mode == "headless"
    args = ["--disable-blink-features=AutomationControlled"]
    if window_mode == "offscreen":
        args += ["--window-position=-2400,-2400", "--window-size=1340,1000"]
    elif window_mode == "visible":
        args += ["--window-position=60,60", "--window-size=1340,1000"]
    return pw.chromium.launch_persistent_context(
        str(profile_dir),
        headless=headless,
        args=args,
        ignore_default_args=["--enable-automation"],
        user_agent=_REAL_UA,
        viewport={"width": 1320, "height": 900},
    )


def _page(ctx):
    return ctx.pages[0] if ctx.pages else ctx.new_page()


def _dismiss_cookies(page) -> None:
    for label in ("Only necessary", "Accept all", "Accept"):
        try:
            btn = page.get_by_role("button", name=label)
            if btn.count() and btn.first.is_visible():
                btn.first.click(timeout=2000)
                return
        except Exception:
            pass


# The composer (#ask-input) is shown to anonymous users too -- it is NOT a
# logged-in signal. Treat the session as logged in only when the composer
# exists AND no Log in / Sign up CTA is visible.
_LOGIN_PROBE_JS = """() => {
  const vis = e => !!(e.offsetWidth || e.offsetHeight || e.getClientRects().length);
  const composer = !!document.querySelector('#ask-input');
  const ctas = [...document.querySelectorAll('button,a')].filter(vis)
    .map(e => (e.innerText||'').trim().toLowerCase());
  const signin = ctas.some(t => ['log in','sign in','sign up','login','log in or sign up'].includes(t));
  return { composer, signin };
}"""


def _select_deep_research(page) -> Optional[str]:
    """Switch the composer to Deep research. Returns None on success, else an
    error string. Uses real role clicks (Radix menus ignore synthetic clicks)."""
    opened = False
    for name in _MODE_PILL_NAMES:
        try:
            loc = page.get_by_role("button", name=name, exact=True)
            if loc.count():
                loc.first.click(timeout=4000)
                opened = True
                break
        except Exception:
            continue
    if not opened:
        return "search-mode pill not found"
    try:
        page.get_by_role("menuitemradio", name="Deep research", exact=True).click(timeout=4000)
    except Exception:
        if page.evaluate(_DEEP_MENU_JS) != "clicked":
            return "Deep research menu item not found"
    page.wait_for_timeout(500)
    try:
        if page.get_by_role("button", name="Deep research", exact=True).count():
            return None
    except Exception:
        pass
    return "mode did not switch to Deep research"


def _logged_in(page) -> bool:
    try:
        page.wait_for_selector("#ask-input", timeout=15000)
    except Exception:
        return False
    # The "Sign In" CTA renders in the top nav a beat after the composer, so
    # probe over a few seconds rather than once (avoids a false "logged in").
    deadline = time.time() + 6
    probe: dict = {}
    while time.time() < deadline:
        try:
            probe = page.evaluate(_LOGIN_PROBE_JS)
        except Exception:
            probe = {}
        if probe.get("signin"):
            return False
        time.sleep(1)
    return bool(probe.get("composer"))


def ensure_login(profile_dir: Optional[Path] = None, timeout_s: int = 240,
                 progress: Callable[[str], None] = _noop) -> dict:
    """Open a VISIBLE window and wait for the user to complete Perplexity login.

    Returns {"status": "logged_in"|"timeout"}. The session persists in the
    profile dir, so subsequent off-screen runs reuse it.
    """
    from playwright.sync_api import sync_playwright

    profile_dir = profile_dir or default_profile_dir()
    progress("Launching a visible browser for login...")
    with sync_playwright() as pw:
        ctx = _launch(pw, window_mode="visible", profile_dir=profile_dir)
        try:
            page = _page(ctx)
            page.goto(PPLX_HOME, wait_until="domcontentloaded", timeout=60000)
            _dismiss_cookies(page)
            if _logged_in(page):
                progress("Already logged in.")
                return {"status": "logged_in"}
            progress("Complete the Perplexity login in the opened window...")
            deadline = time.time() + timeout_s
            while time.time() < deadline:
                if _logged_in(page):
                    progress("Login detected.")
                    return {"status": "logged_in"}
                time.sleep(3)
            return {"status": "timeout"}
        finally:
            ctx.close()


def check_login(profile_dir: Optional[Path] = None,
                progress: Callable[[str], None] = _noop) -> dict:
    """Non-interactive login probe: open off-screen, report status, close.

    Returns {"status": "logged_in"|"needs_login"|"error", ...}. ~8s, spends no
    quota. Used by the website to decide whether to show the login button.
    """
    from playwright.sync_api import sync_playwright

    profile_dir = profile_dir or default_profile_dir()
    progress("Checking Perplexity login...")
    with sync_playwright() as pw:
        ctx = _launch(pw, window_mode="offscreen", profile_dir=profile_dir)
        try:
            page = _page(ctx)
            page.goto(PPLX_HOME, wait_until="domcontentloaded", timeout=60000)
            _dismiss_cookies(page)
            return {"status": "logged_in" if _logged_in(page) else "needs_login"}
        except Exception as exc:  # noqa: BLE001
            return {"status": "error", "detail": f"{type(exc).__name__}: {exc}"}
        finally:
            ctx.close()


def run_deep_research(prompt: str, *, window_mode: str = "offscreen",
                      profile_dir: Optional[Path] = None, timeout_s: int = 900,
                      poll_interval_s: int = 30, dry_run: bool = False,
                      progress: Callable[[str], None] = _noop) -> dict:
    """Run one Deep Research query end to end.

    Returns one of:
      {"status": "done", "source_url", "report", "citations"}
      {"status": "dry_run_ok", "mode_verified": True}
      {"status": "needs_login"}
      {"status": "computer_trap", "url"}   # paid path -- aborted
      {"status": "mode_failed", "detail"}
      {"status": "timeout"}
      {"status": "error", "detail"}
    """
    from playwright.sync_api import sync_playwright

    prompt = (prompt or "").strip()
    if not prompt:
        return {"status": "error", "detail": "empty prompt"}
    profile_dir = profile_dir or default_profile_dir()

    with sync_playwright() as pw:
        ctx = _launch(pw, window_mode=window_mode, profile_dir=profile_dir)
        try:
            page = _page(ctx)
            progress("Opening Perplexity...")
            page.goto(PPLX_HOME, wait_until="domcontentloaded", timeout=60000)
            _dismiss_cookies(page)
            if not _logged_in(page):
                return {"status": "needs_login"}

            progress("Selecting Deep research mode...")
            mode_err = _select_deep_research(page)
            if mode_err:
                return {"status": "mode_failed", "detail": mode_err}

            if dry_run:
                progress("Dry run: mode verified, not submitting.")
                return {"status": "dry_run_ok", "mode_verified": True}

            progress("Submitting query...")
            inp = page.locator("#ask-input")
            inp.click()
            try:
                inp.fill(prompt)
            except Exception:
                inp.type(prompt, delay=2)
            page.keyboard.press("Enter")

            # Verify the included-quota path: URL must become /search/, never /computer/.
            time.sleep(3)
            try:
                page.wait_for_url("**/search/**", timeout=20000)
            except Exception:
                if "/computer/" in page.url:
                    return {"status": "computer_trap", "url": page.url}
                # Some runs land on /search/ after a beat; fall through to polling.
            if "/computer/" in page.url:
                return {"status": "computer_trap", "url": page.url}

            progress("Researching (this takes minutes)...")
            time.sleep(8)  # let the Stop button appear before we trust "not running"
            deadline = time.time() + timeout_s
            stable = 0
            while time.time() < deadline:
                st = page.evaluate(_POLL_JS)
                progress(f"running={st['running']} chars={st['len']}")
                if not st["running"] and st["len"] > 800:
                    stable += 1
                    if stable >= 2:
                        break
                else:
                    stable = 0
                time.sleep(poll_interval_s)
            else:
                return {"status": "timeout"}

            progress("Scraping report and citations...")
            report = page.evaluate(_REPORT_JS)
            citations = []
            try:
                if page.evaluate(_OPEN_LINKS_JS):
                    time.sleep(2)
                citations = page.evaluate(_LINKS_JS)
            except Exception:
                pass
            return {
                "status": "done",
                "source_url": page.url,
                "report": report,
                "citations": citations,
            }
        except Exception as exc:  # noqa: BLE001
            return {"status": "error", "detail": f"{type(exc).__name__}: {exc}"}
        finally:
            ctx.close()


def _main(argv=None) -> int:
    import argparse
    import json

    ap = argparse.ArgumentParser(description="Perplexity in-app Deep Research worker")
    ap.add_argument("--login", action="store_true", help="open a visible window and wait for login")
    ap.add_argument("--dry-run", action="store_true", help="select Deep research mode but do not submit")
    ap.add_argument("--prompt", default="", help="the research prompt")
    ap.add_argument("--window-mode", default="offscreen", choices=["offscreen", "visible", "headless"])
    ap.add_argument("--timeout", type=int, default=900)
    args = ap.parse_args(argv)

    def log(msg):
        print(f"[pplx] {msg}", flush=True)

    if args.login:
        print(json.dumps(ensure_login(progress=log)))
        return 0
    if not args.prompt:
        ap.error("--prompt is required unless --login")
    res = run_deep_research(
        args.prompt, window_mode=args.window_mode, timeout_s=args.timeout,
        dry_run=args.dry_run, progress=log,
    )
    # Keep stdout parseable: drop the (potentially huge) report body from the echo.
    echo = dict(res)
    if "report" in echo:
        echo["report_chars"] = len(echo.pop("report"))
    print(json.dumps(echo))
    return 0


if __name__ == "__main__":
    raise SystemExit(_main())
