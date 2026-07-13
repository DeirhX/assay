import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

const { replaceViewState, navFromUrl } = vi.hoisted(() => ({
  replaceViewState: vi.fn(),
  navFromUrl: vi.fn(() => ({
    view: "risk",
    range: "",
    benchmark: "",
    soon: "",
  })),
}));

vi.mock("../src/shell", () => ({
  navFromUrl,
  replaceViewState,
}));

import { initUrlBoundSelect } from "../src/url-select";

function selectWithOptions(values: string[]): HTMLSelectElement {
  const sel = document.createElement("select");
  values.forEach((v) => {
    const opt = document.createElement("option");
    opt.value = v;
    opt.textContent = v;
    sel.appendChild(opt);
  });
  document.body.appendChild(sel);
  return sel;
}

describe("initUrlBoundSelect", () => {
  beforeEach(() => {
    document.body.innerHTML = "";
    replaceViewState.mockReset();
    navFromUrl.mockReset();
    navFromUrl.mockReturnValue({
      view: "risk",
      range: "",
      benchmark: "",
      soon: "",
    });
  });

  afterEach(() => {
    document.body.innerHTML = "";
  });

  it("applies the default when the URL param is missing", () => {
    const sel = selectWithOptions(["3m", "1y", "3y"]);
    const onValue = vi.fn();
    const reload = vi.fn();
    initUrlBoundSelect({
      select: sel,
      param: "range",
      defaultValue: "1y",
      onValue,
      reload,
    });
    expect(sel.value).toBe("1y");
    expect(onValue).toHaveBeenCalledWith("1y");
    expect(reload).not.toHaveBeenCalled();
  });

  it("honours a valid URL param on init", () => {
    navFromUrl.mockReturnValue({
      view: "risk",
      range: "3y",
      benchmark: "",
      soon: "",
    });
    const sel = selectWithOptions(["3m", "1y", "3y"]);
    const onValue = vi.fn();
    initUrlBoundSelect({
      select: sel,
      param: "range",
      defaultValue: "1y",
      onValue,
      reload: vi.fn(),
    });
    expect(sel.value).toBe("3y");
    expect(onValue).toHaveBeenCalledWith("3y");
  });

  it("elides the default from the URL and reloads on change", () => {
    const sel = selectWithOptions(["60", "90", "120"]);
    const onValue = vi.fn();
    const reload = vi.fn();
    initUrlBoundSelect({
      select: sel,
      param: "soon",
      defaultValue: "60",
      onValue,
      reload,
    });
    sel.value = "90";
    sel.dispatchEvent(new Event("change"));
    expect(onValue).toHaveBeenLastCalledWith("90");
    expect(replaceViewState).toHaveBeenCalledWith({ soon: "90" });
    expect(reload).toHaveBeenCalledTimes(1);

    sel.value = "60";
    sel.dispatchEvent(new Event("change"));
    expect(replaceViewState).toHaveBeenLastCalledWith({ soon: "" });
  });

  it("wires the change listener only once", () => {
    const sel = selectWithOptions(["1y", "3y"]);
    const reload = vi.fn();
    const opts = {
      select: sel,
      param: "range" as const,
      defaultValue: "1y",
      onValue: vi.fn(),
      reload,
    };
    initUrlBoundSelect(opts);
    initUrlBoundSelect(opts);
    sel.value = "3y";
    sel.dispatchEvent(new Event("change"));
    expect(reload).toHaveBeenCalledTimes(1);
  });
});
