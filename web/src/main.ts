// @ts-nocheck
import { ensureTickerSet } from "./analyses";
import { $, api, applyPrivacyMode, state } from "./core";
import { clearErrors, recordError, refreshLoginStatus, renderErrorCenter, toggleErrorPanel } from "./errors";
import "./livereload";
import { initShell, navFromUrl, restoreNav } from "./shell";
import { startTaskCenter } from "./tasks";

// ---- boot -----------------------------------------------------------------
// Catch-all for failures nobody handled locally: uncaught promise rejections
// (e.g. a view loader with no try/catch) and runtime script errors. Anything
// api() already logged carries _recorded, so we don't double-count it.
window.addEventListener("unhandledrejection", (ev) => {
  const r = ev.reason;
  if (r && r._recorded) return;
  const msg = (r && r.message) || String(r) || "unhandled promise rejection";
  const stackLine = r && r.stack ? String(r.stack).split("\n")[1] : "";
  recordError("js", msg, { detail: stackLine ? stackLine.trim() : "" });
});
window.addEventListener("error", (ev) => {
  if (ev.error && ev.error._recorded) return;
  const msg = ev.message || (ev.error && ev.error.message) || "script error";
  const where = ev.filename ? `${String(ev.filename).split("/").pop()}:${ev.lineno || "?"}` : "";
  recordError("js", msg, { detail: where });
});

const _errBtn = $("#error-indicator");
if (_errBtn) _errBtn.addEventListener("click", () => toggleErrorPanel());
const _errClear = $("#error-clear");
if (_errClear) _errClear.addEventListener("click", () => clearErrors());
const _errClose = $("#error-close");
if (_errClose) _errClose.addEventListener("click", () => toggleErrorPanel(false));
renderErrorCenter();

initShell();
applyPrivacyMode(state.privacyMode);
const initialNav = navFromUrl();
boot();

async function boot() {
  const params = new URLSearchParams(window.location.search);
  let nav = initialNav;
  if (!params.has("view")) {
    try {
      const setup = await api("/api/setup/status");
      if (setup?.data?.empty) {
        nav = { ...nav, view: "setup", ticker: "", segment: "", run: "" };
      }
    } catch (_e) {
      // If setup status itself is unavailable, fall through to the normal route;
      // the error center already records API failures.
    }
  }
  window.history.replaceState(nav, "", nav.view === "setup" ? "?view=setup" : window.location.href);
  await restoreNav(nav);
  refreshLoginStatus();
  ensureTickerSet();
  // Start the central Task Center poller: repopulates in-progress tasks from the
  // server so they survive navigation and a page reload (within a server run).
  startTaskCenter();
}

export {
  _errBtn,
  _errClear,
  _errClose,
  initialNav,
  boot,
};
