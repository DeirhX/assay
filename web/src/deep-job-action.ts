// Generic deep-job button choreography: disable one or more buttons, show a
// pending status line, POST-start a job, pollDeepJob, then restore controls.
// Holdings sync keeps its own helper (freeze-on-success differs); pipeline and
// analysis cards keep their multi-phase loaders.
import type { Job } from "./api-types";
import { pollDeepJob } from "./jobs";

export interface RunDeepJobActionOpts {
  buttons: HTMLButtonElement[];
  status: HTMLElement | null;
  pendingStatusHtml?: string;
  /** When set with activeLabel, only this button's label changes while running. */
  activeButton?: HTMLButtonElement | null;
  activeLabel?: string;
  startJob: () => Promise<{ id: string }>;
  jobLabel: string;
  onDone: (job: Job) => void | Promise<void>;
  failPrefix: string;
}

export async function runDeepJobAction(opts: RunDeepJobActionOpts): Promise<void> {
  const {
    buttons, status, pendingStatusHtml, activeButton, activeLabel,
    startJob, jobLabel, onDone, failPrefix,
  } = opts;
  if (!buttons.length || buttons.some((b) => b.disabled)) return;

  const prev = buttons.map((b) => b.textContent);
  buttons.forEach((b) => { b.disabled = true; });
  if (activeButton && activeLabel) activeButton.textContent = activeLabel;
  if (status) {
    status.classList.remove("err");
    if (pendingStatusHtml) status.innerHTML = pendingStatusHtml;
  }

  try {
    const job = await startJob();
    await pollDeepJob(job.id, status, onDone, jobLabel);
  } catch (e) {
    if (status) {
      status.textContent = failPrefix + (e as Error).message;
      status.classList.add("err");
    }
  } finally {
    buttons.forEach((b, i) => {
      b.disabled = false;
      b.textContent = prev[i];
    });
  }
}
