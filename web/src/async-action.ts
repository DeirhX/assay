// Conservative async button feedback: disable, pending label, restore on error.
// Callers with success-specific button text, delayed error recovery, or rich
// status choreography keep their own wiring (preview/place, exit staging, etc.).

export interface AsyncButtonOpts {
  btn: HTMLButtonElement;
  pendingLabel: string;
  run: () => Promise<void>;
  /** When true, the button stays disabled after a successful run. */
  keepBusyOnSuccess?: boolean;
  /** Optional label after success when keepBusyOnSuccess is set. */
  successLabel?: string;
}

/** Returns false when the button was already disabled or the run threw. */
export async function runAsyncButton(opts: AsyncButtonOpts): Promise<boolean> {
  const { btn, pendingLabel, run, keepBusyOnSuccess, successLabel } = opts;
  if (btn.disabled) return false;
  const prev = btn.textContent;
  btn.disabled = true;
  btn.textContent = pendingLabel;
  try {
    await run();
    if (keepBusyOnSuccess) {
      if (successLabel) btn.textContent = successLabel;
      return true;
    }
  } catch {
    btn.disabled = false;
    btn.textContent = prev;
    return false;
  }
  btn.disabled = false;
  btn.textContent = prev;
  return true;
}
