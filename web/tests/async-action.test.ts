import { afterEach, describe, expect, it } from "vitest";

import { runAsyncButton } from "../src/async-action";

function button(label = "Go"): HTMLButtonElement {
  const btn = document.createElement("button");
  btn.type = "button";
  btn.textContent = label;
  document.body.appendChild(btn);
  return btn;
}

describe("runAsyncButton", () => {
  afterEach(() => {
    document.body.innerHTML = "";
  });

  it("disables, shows pending label, then restores on success", async () => {
    const btn = button("Sync");
    let ran = false;
    const ok = await runAsyncButton({
      btn,
      pendingLabel: "Syncing…",
      run: async () => { ran = true; },
    });
    expect(ok).toBe(true);
    expect(ran).toBe(true);
    expect(btn.disabled).toBe(false);
    expect(btn.textContent).toBe("Sync");
  });

  it("restores on failure", async () => {
    const btn = button("Sync");
    const ok = await runAsyncButton({
      btn,
      pendingLabel: "Syncing…",
      run: async () => { throw new Error("nope"); },
    });
    expect(ok).toBe(false);
    expect(btn.disabled).toBe(false);
    expect(btn.textContent).toBe("Sync");
  });

  it("keeps the button busy on success when requested", async () => {
    const btn = button("Resync");
    const ok = await runAsyncButton({
      btn,
      pendingLabel: "Syncing…",
      keepBusyOnSuccess: true,
      successLabel: "Resynced ✓",
      run: async () => undefined,
    });
    expect(ok).toBe(true);
    expect(btn.disabled).toBe(true);
    expect(btn.textContent).toBe("Resynced ✓");
  });

  it("no-ops when the button is already disabled", async () => {
    const btn = button("Sync");
    btn.disabled = true;
    let ran = false;
    const ok = await runAsyncButton({
      btn,
      pendingLabel: "Syncing…",
      run: async () => { ran = true; },
    });
    expect(ok).toBe(false);
    expect(ran).toBe(false);
  });
});
