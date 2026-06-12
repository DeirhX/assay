/// <reference types="vite/client" />
// Dev-only live reload for the serve.py-hosted build. Polls the server's opaque
// asset/version token; when the server is started with --reload and the token
// changes, it reloads the page.
//
// The token changes on (a) any web/ or site.css edit (including a fresh
// `npm run build` into web/dist) and (b) an API restart (the boot token is
// regenerated each process start). A failed fetch means the server is
// mid-restart, so we back off and retry -- the next successful poll returns a
// new token and we reload.
//
// When the server is NOT in --reload mode the first poll returns enabled:false
// and we stop, so this module is a harmless no-op in normal use. Under the Vite
// dev server HMR owns reloading, so we don't poll at all.

if (!import.meta.env.DEV) {
  let known: string | null = null;

  const tick = async (): Promise<void> => {
    let info: { enabled?: boolean; version?: string } | undefined;
    try {
      const res = await fetch("/api/dev/livereload", { cache: "no-store" });
      info = await res.json();
    } catch (_err) {
      setTimeout(tick, 700); // server restarting or briefly unreachable
      return;
    }
    if (!info || !info.enabled) return; // not a dev server: stop polling
    if (known === null) {
      known = info.version ?? "";
    } else if (info.version !== known) {
      location.reload();
      return;
    }
    setTimeout(tick, 1000);
  };

  tick();
}

export {};
