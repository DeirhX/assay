import { beforeEach, describe, expect, it, vi } from "vitest";

vi.mock("../src/core", async (importOriginal) => {
  const actual = await importOriginal<typeof import("../src/core")>();
  return { ...actual, api: vi.fn() };
});

import { api } from "../src/core";
import { pinBlock } from "../src/deepdive/pin";


const apiMock = vi.mocked(api);

describe("conviction exit intent", () => {
  beforeEach(() => {
    document.body.innerHTML = "";
    apiMock.mockReset();
  });

  it("offers a clear zero-target action and persists a hard avoid pin", async () => {
    apiMock.mockResolvedValue({
      pin: {
        source: "user-pin",
        stance: "avoid",
        floor_pct: 0,
        ceiling_pct: 0,
      },
    });
    const block = pinBlock({ symbol: "PYPL" }, null);
    document.body.appendChild(block);

    block.querySelector<HTMLButtonElement>(".pin-toggle")!.click();
    expect(block.textContent).toContain("Exit & keep out");
    expect(block.textContent).toContain("not broker orders");
    block.querySelector<HTMLButtonElement>(".pin-exit")!.click();

    await vi.waitFor(() => expect(apiMock).toHaveBeenCalledWith(
      "/api/staging/edit",
      "POST",
      {
        op: "pin",
        key: "PYPL",
        stance: "avoid",
        floor_pct: 0,
        ceiling_pct: 0,
        rationale: "Standing exit decision; do not re-add from research.",
      },
    ));
    expect(block.querySelector(".chip")!.textContent).toBe("exit · 0%");
  });
});
