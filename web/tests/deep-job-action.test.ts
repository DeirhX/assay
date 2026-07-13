import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

vi.mock("../src/jobs", () => ({
  pollDeepJob: vi.fn(),
}));

import { pollDeepJob } from "../src/jobs";
import { runDeepJobAction } from "../src/deep-job-action";

const pollMock = vi.mocked(pollDeepJob);

describe("runDeepJobAction", () => {
  beforeEach(() => {
    document.body.innerHTML = "";
    pollMock.mockReset();
  });

  afterEach(() => {
    document.body.innerHTML = "";
  });

  it("disables buttons, polls, runs onDone, then restores controls", async () => {
    const btnA = document.createElement("button");
    btnA.textContent = "Update";
    const btnB = document.createElement("button");
    btnB.textContent = "Rebuild";
    const status = document.createElement("div");
    document.body.append(btnA, btnB, status);

    pollMock.mockImplementation(async (_id, _status, onDone) => {
      await onDone({ state: "done", result: { ok: true } } as never);
    });

    let done = false;
    await runDeepJobAction({
      buttons: [btnA, btnB],
      status,
      pendingStatusHtml: `<span class="spinner"></span> working…`,
      activeButton: btnA,
      activeLabel: "Updating…",
      startJob: async () => ({ id: "job-42" }),
      jobLabel: "test job",
      failPrefix: "failed: ",
      onDone: async () => { done = true; status.textContent = "Done"; },
    });

    expect(pollMock).toHaveBeenCalledWith("job-42", status, expect.any(Function), "test job");
    expect(done).toBe(true);
    expect(btnA.disabled).toBe(false);
    expect(btnB.disabled).toBe(false);
    expect(btnA.textContent).toBe("Update");
    expect(btnB.textContent).toBe("Rebuild");
  });

  it("surfaces start failures and restores buttons", async () => {
    const btn = document.createElement("button");
    btn.textContent = "Fetch";
    const status = document.createElement("div");
    document.body.append(btn, status);

    await runDeepJobAction({
      buttons: [btn],
      status,
      startJob: async () => { throw new Error("offline"); },
      jobLabel: "sectors",
      failPrefix: "Sector lookup failed: ",
      onDone: async () => undefined,
    });

    expect(status.classList.contains("err")).toBe(true);
    expect(status.textContent).toContain("offline");
    expect(btn.disabled).toBe(false);
    expect(btn.textContent).toBe("Fetch");
  });

  it("no-ops when any button is already disabled", async () => {
    const btn = document.createElement("button");
    btn.disabled = true;
    const startJob = vi.fn();
    await runDeepJobAction({
      buttons: [btn],
      status: null,
      startJob,
      jobLabel: "x",
      failPrefix: "err: ",
      onDone: async () => undefined,
    });
    expect(startJob).not.toHaveBeenCalled();
  });
});
