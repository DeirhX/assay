import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

vi.mock("../src/core", async (importOriginal) => ({
  ...(await importOriginal<typeof import("../src/core")>()),
  api: vi.fn(),
}));

vi.mock("../src/jobs", () => ({
  pollDeepJob: vi.fn(),
}));

import { api } from "../src/core";
import { pollDeepJob } from "../src/jobs";
import {
  HOLDINGS_SYNC_JOB_LABEL,
  HOLDINGS_SYNC_PENDING_HTML,
  runHoldingsSync,
  siteMsg,
} from "../src/holdings-sync";

const apiMock = vi.mocked(api);
const pollMock = vi.mocked(pollDeepJob);

describe("siteMsg", () => {
  it("reports summary refresh outcomes", () => {
    expect(siteMsg(null)).toBe("Summary not refreshed.");
    expect(siteMsg({ ok: false, error: "disk full" })).toContain("disk full");
    expect(siteMsg({ ok: true, written: ["holdings.md"] })).toContain("refreshed");
    expect(siteMsg({ ok: true, written: [] })).toContain("already up to date");
  });
});

describe("runHoldingsSync", () => {
  beforeEach(() => {
    apiMock.mockReset();
    pollMock.mockReset();
    document.body.innerHTML = "";
  });

  afterEach(() => {
    document.body.innerHTML = "";
  });

  it("starts the sync job, polls, and restores the button", async () => {
    const btn = document.createElement("button");
    btn.textContent = "Resync";
    const status = document.createElement("div");
    document.body.append(btn, status);

    apiMock.mockResolvedValueOnce({ id: "job-1" });
    pollMock.mockImplementation(async (_id, _status, onDone) => {
      await onDone({ state: "done", result: {} } as any);
    });

    let done = false;
    await runHoldingsSync({
      btn,
      status,
      onDone: async () => { done = true; },
    });

    expect(apiMock).toHaveBeenCalledWith("/api/holdings/sync", "POST", {});
    expect(pollMock).toHaveBeenCalledWith("job-1", status, expect.any(Function), HOLDINGS_SYNC_JOB_LABEL);
    expect(status.innerHTML).toBe(HOLDINGS_SYNC_PENDING_HTML);
    expect(done).toBe(true);
    expect(btn.disabled).toBe(false);
    expect(btn.textContent).toBe("Resync");
  });

  it("surfaces sync failures on the status line", async () => {
    const btn = document.createElement("button");
    btn.textContent = "Resync";
    const status = document.createElement("div");
    document.body.append(btn, status);

    apiMock.mockRejectedValueOnce(new Error("gateway down"));

    await runHoldingsSync({ btn, status, onDone: async () => undefined });

    expect(status.classList.contains("err")).toBe(true);
    expect(status.textContent).toContain("gateway down");
    expect(btn.disabled).toBe(false);
    expect(btn.textContent).toBe("Resync");
  });

  it("can freeze the button after success", async () => {
    const btn = document.createElement("button");
    btn.textContent = "Resync holdings";
    document.body.append(btn);

    apiMock.mockResolvedValueOnce({ id: "job-2" });
    pollMock.mockImplementation(async (_id, _status, onDone) => {
      await onDone({ state: "done" } as any);
    });

    await runHoldingsSync({
      btn,
      freezeButtonOnSuccess: true,
      successButtonLabel: "Resynced ✓",
      onDone: async () => undefined,
    });

    expect(btn.disabled).toBe(true);
    expect(btn.textContent).toBe("Resynced ✓");
  });
});
