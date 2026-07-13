import { afterEach, describe, expect, it, vi } from "vitest";

vi.mock("../src/shell", () => ({
  pushNav: vi.fn(),
  setActiveView: vi.fn(),
}));

import { pushNav, setActiveView } from "../src/shell";
import { gotoWorkflowView } from "../src/workflow-nav";

const pushMock = vi.mocked(pushNav);
const activeMock = vi.mocked(setActiveView);

describe("gotoWorkflowView", () => {
  afterEach(() => {
    pushMock.mockClear();
    activeMock.mockClear();
    vi.restoreAllMocks();
  });

  it("pushes nav state and activates the view", () => {
    gotoWorkflowView("target-state");
    expect(pushMock).toHaveBeenCalledWith({ view: "target-state" });
    expect(activeMock).toHaveBeenCalledWith("target-state");
  });

  it("scrolls to top when requested", () => {
    const scroll = vi.spyOn(window, "scrollTo").mockImplementation(() => undefined);
    gotoWorkflowView("trade", { scrollTop: true });
    expect(scroll).toHaveBeenCalledWith(0, 0);
    scroll.mockRestore();
  });

  it("does not scroll by default", () => {
    const scroll = vi.spyOn(window, "scrollTo").mockImplementation(() => undefined);
    gotoWorkflowView("exit");
    expect(scroll).not.toHaveBeenCalled();
    scroll.mockRestore();
  });
});
