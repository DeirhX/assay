"use strict";

// Dev-only live reload. Polls the server's opaque asset/version token; when the
// server is started with --reload and the token changes, it reloads the page.
//
// The token changes on (a) any web/ or site.css edit and (b) an API restart
// (the boot token is regenerated each process start). A failed fetch means the
// server is mid-restart (os.execv), so we just back off and retry — the next
// successful poll returns a new token and we reload.
//
// When the server is NOT in --reload mode the first poll returns enabled:false
// and we stop, so this file is a harmless no-op in normal use.
(function () {
  let known = null;

  async function tick() {
    let info;
    try {
      const res = await fetch("/api/dev/livereload", { cache: "no-store" });
      info = await res.json();
    } catch (_err) {
      setTimeout(tick, 700); // server restarting or briefly unreachable
      return;
    }
    if (!info || !info.enabled) return; // not a dev server: stop polling
    if (known === null) {
      known = info.version;
    } else if (info.version !== known) {
      location.reload();
      return;
    }
    setTimeout(tick, 1000);
  }

  tick();
})();
