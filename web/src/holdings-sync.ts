// IBKR holdings resync: POST /api/holdings/sync + pollDeepJob + button/status
// choreography shared by Holdings, Today, Setup, and Trade stale/post-place gates.
// Leaf module (core + jobs only) so shell and trade never import each other.

import type { Job } from "./api-types";
import { api } from "./core";
import { pollDeepJob } from "./jobs";
import { runAsyncButton } from "./async-action";

export const HOLDINGS_SYNC_PENDING_HTML =
  `<span class="spinner"></span> Syncing portfolio from IBKR (live connection preferred)…`;

export const HOLDINGS_SYNC_JOB_LABEL = "IBKR sync";

interface SiteRegen {
  ok?: boolean;
  error?: string;
  written?: string[];
}

/** Human-readable tail for whether generate_site refreshed the holdings summary. */
export function siteMsg(site: SiteRegen | null | undefined): string {
  if (!site) return "Summary not refreshed.";
  if (site.ok === false) return "Summary not refreshed: " + (site.error || "unknown error");
  return (site.written || []).length ? "Holdings summary refreshed." : "Holdings summary already up to date.";
}

export interface HoldingsSyncOpts {
  btn: HTMLButtonElement;
  status?: HTMLElement | null;
  onDone: (job: Job) => void | Promise<void>;
  /** Keep the button disabled after success (trade stale gate / post-place resync). */
  freezeButtonOnSuccess?: boolean;
  /** Button label after success when freezeButtonOnSuccess is set. */
  successButtonLabel?: string;
}

export interface QuietHoldingsSyncOpts {
  status?: HTMLElement | null;
  onDone: (job: Job) => void | Promise<void>;
}

function setSyncPending(status: HTMLElement | null | undefined): void {
  if (!status) return;
  status.classList.remove("err");
  status.innerHTML = HOLDINGS_SYNC_PENDING_HTML;
}

function setSyncError(status: HTMLElement | null | undefined, err: unknown): void {
  if (!status) return;
  status.textContent = "Sync failed: " + (err as Error).message;
  status.classList.add("err");
}

let _activeSync: Promise<Job> | null = null;

function performHoldingsSync(status?: HTMLElement | null): Promise<Job> {
  if (_activeSync) return _activeSync;
  _activeSync = (async () => {
    const job = await api<{ id: string }>("/api/holdings/sync", "POST", {});
    let completed: Job | null = null;
    await pollDeepJob(job.id, status ?? null, (done) => {
      completed = done;
    }, HOLDINGS_SYNC_JOB_LABEL);
    if (!completed) throw new Error("IBKR sync ended without a result");
    return completed;
  })().finally(() => {
    _activeSync = null;
  });
  return _activeSync;
}

/** Join the same in-flight sync used by the button flow, without requiring UI chrome. */
export async function runQuietHoldingsSync(opts: QuietHoldingsSyncOpts): Promise<void> {
  const { status, onDone } = opts;
  setSyncPending(status);
  try {
    await onDone(await performHoldingsSync(status));
  } catch (e) {
    setSyncError(status, e);
    throw e;
  }
}

export async function runHoldingsSync(opts: HoldingsSyncOpts): Promise<void> {
  const { btn, status, onDone, freezeButtonOnSuccess, successButtonLabel } = opts;
  if (btn.disabled) return;
  setSyncPending(status);
  await runAsyncButton({
    btn,
    pendingLabel: "Syncing…",
    keepBusyOnSuccess: !!freezeButtonOnSuccess,
    successLabel: successButtonLabel,
    run: async () => {
      try {
        await onDone(await performHoldingsSync(status));
      } catch (e) {
        setSyncError(status, e);
        throw e;
      }
    },
  });
}
