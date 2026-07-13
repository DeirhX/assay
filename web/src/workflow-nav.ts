// Execution-handoff navigation among the rebalance → target-state → trade loop
// (plus exit). Keeps pushNav + setActiveView together so scroll and labels stay
// consistent at each hop without shell importing the workflow views.

import { pushNav, setActiveView } from "./shell";

export type WorkflowView = "rebalance" | "exit" | "target-state" | "trade";

export interface GotoWorkflowOpts {
  scrollTop?: boolean;
}

export function gotoWorkflowView(view: WorkflowView, opts: GotoWorkflowOpts = {}): void {
  pushNav({ view });
  setActiveView(view);
  if (opts.scrollTop) window.scrollTo(0, 0);
}
