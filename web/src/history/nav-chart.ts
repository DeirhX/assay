// The portfolio-history headline: NAV over time as an SVG "mountain" with every
// trading day marked (buy / sell / mixed) and a hover card digesting that day's
// fills. Pan by dragging, zoom toward the cursor with the wheel, double-click to
// reset. Extracted from history.ts; pure rendering over the NAV series + trade
// ledger the backend hands over -- no app state.
import { el, esc, sensitive } from "../core";
import { dayGroups, type DayGroup, type Trade } from "./data";
import { ccyTag, fmtMoney, fmtSigned } from "./format";

export interface NavPoint {
  date: string;
  nav: number | string;
}

const SVG_NS = "http://www.w3.org/2000/svg";
const W = 1000;
const H = 340;
const PAD = { l: 70, r: 16, t: 16, b: 30 };
const DAY_MS = 86400000;

const msOf = (d: string): number => new Date(String(d) + "T00:00:00Z").getTime();

function svg(tag: string, attrs: Record<string, string | number> = {}): SVGElement {
  const n = document.createElementNS(SVG_NS, tag);
  for (const [k, v] of Object.entries(attrs)) n.setAttribute(k, String(v));
  return n;
}

// Nearest NAV at-or-before a date, so a trade marker sits on the line.
function navAtOrBefore(series: NavPoint[], ms: number): NavPoint {
  let lo = 0, hi = series.length - 1, ans = series[0];
  while (lo <= hi) {
    const mid = (lo + hi) >> 1;
    if (msOf(series[mid].date) <= ms) { ans = series[mid]; lo = mid + 1; }
    else hi = mid - 1;
  }
  return ans;
}

export function navChart(series: NavPoint[], trades: Trade[], baseCcy = ""): HTMLElement {
  const x0 = msOf(series[0].date);
  const x1 = msOf(series[series.length - 1].date);
  const fullSpan = x1 - x0 || 1;
  const minSpan = Math.min(fullSpan, 5 * DAY_MS); // can't zoom tighter than ~5 days
  const navs = series.map((p) => Number(p.nav));
  let ymin = Math.min(...navs);
  let ymax = Math.max(...navs);
  const padY = (ymax - ymin) * 0.08 || Math.abs(ymax) * 0.08 || 1;
  ymin -= padY; ymax += padY;
  const yspan = ymax - ymin || 1;
  const plotW = W - PAD.l - PAD.r;

  // The currently visible time window, mapped across the full plot width. Zoom
  // narrows it (spreading points apart); pan slides it. y stays fixed so the
  // line doesn't rescale vertically as you scrub along time.
  const view = { a: x0, b: x1 };
  const xFor = (ms: number) => PAD.l + ((ms - view.a) / (view.b - view.a || 1)) * plotW;
  const yFor = (v: number) => H - PAD.b - ((v - ymin) / yspan) * (H - PAD.t - PAD.b);

  const root = svg("svg", {
    class: "hist-chart", viewBox: `0 0 ${W} ${H}`,
    preserveAspectRatio: "none", role: "img",
    "aria-label": "Portfolio value over time with trade markers",
  });

  // Clip dynamic content to the plot rect so panned line/dots don't spill over
  // the axes. Unique id in case more than one chart ever shares a page.
  const cid = "histclip-" + Math.random().toString(36).slice(2, 8);
  const defs = svg("defs");
  const clip = svg("clipPath", { id: cid });
  clip.appendChild(svg("rect", { x: PAD.l, y: PAD.t, width: plotW, height: H - PAD.t - PAD.b }));
  defs.appendChild(clip);
  root.appendChild(defs);

  // Static horizontal gridlines + y labels (y domain never changes on zoom).
  const ticks = 4;
  const axisG = svg("g", { "data-sensitive": "", class: "hist-axis" });
  for (let i = 0; i <= ticks; i++) {
    const v = ymin + (yspan * i) / ticks;
    const y = yFor(v);
    root.appendChild(svg("line", { class: "hist-grid", x1: PAD.l, x2: W - PAD.r, y1: y, y2: y }));
    const label = svg("text", { class: "hist-ylabel", x: PAD.l - 8, y: y + 4, "text-anchor": "end" });
    label.textContent = fmtMoney(v);
    axisG.appendChild(label);
  }

  const plotG = svg("g", { "clip-path": `url(#${cid})` }); // area + line + dots
  const xlabG = svg("g", { class: "hist-axis" });
  root.appendChild(plotG);
  root.appendChild(axisG);
  root.appendChild(xlabG);

  const wrap = el("div", "hist-chart-wrap");
  const tip = el("div", "hist-tip");
  tip.hidden = true;
  const place = (ev: MouseEvent) => {
    const r = wrap.getBoundingClientRect();
    const x = ev.clientX - r.left;
    const y = ev.clientY - r.top;
    const left = Math.max(6, Math.min(x + 14, wrap.clientWidth - tip.offsetWidth - 8));
    const top = Math.max(6, y - tip.offsetHeight - 12);
    tip.style.left = left + "px";
    tip.style.top = top + "px";
  };

  const allDays = dayGroups(trades);
  let dragging = false;

  // Rebuild everything that depends on the visible window.
  const render = () => {
    plotG.innerHTML = "";
    xlabG.innerHTML = "";

    const linePts = series.map((p) => `${xFor(msOf(p.date)).toFixed(1)},${yFor(Number(p.nav)).toFixed(1)}`);
    const areaD = `M ${linePts[0]} L ${linePts.join(" L ")} ` +
      `L ${xFor(x1).toFixed(1)},${(H - PAD.b).toFixed(1)} L ${xFor(x0).toFixed(1)},${(H - PAD.b).toFixed(1)} Z`;
    plotG.appendChild(svg("path", { class: "hist-area", d: areaD }));
    plotG.appendChild(svg("polyline", { class: "hist-line", points: linePts.join(" ") }));

    // x labels at the window's start / middle / end dates.
    const fmtMs = (ms: number) => new Date(ms).toISOString().slice(0, 10);
    ([[view.a, "start"], [(view.a + view.b) / 2, "middle"], [view.b, "end"]] as [number, string][]).forEach(([ms, anchor]) => {
      const tx = svg("text", { class: "hist-xlabel", x: xFor(ms), y: H - 10, "text-anchor": anchor });
      tx.textContent = fmtMs(ms);
      xlabG.appendChild(tx);
    });

    // Markers only for days inside the window (one per trading day).
    allDays.forEach((d) => {
      const ms = msOf(d.date);
      if (ms < view.a || ms > view.b) return;
      const ref = navAtOrBefore(series, ms);
      const cx = xFor(ms);
      const cy = yFor(Number(ref.nav));
      const sides = new Set(d.trades.map((t) => t.side));
      const cls = sides.size > 1 ? "mixed" : sides.has("BUY") ? "buy" : "sell";
      const rad = Math.min(6.5, 3 + Math.log2(d.trades.length + 1));
      plotG.appendChild(svg("circle", {
        class: "hist-dot " + cls, cx: cx.toFixed(1), cy: cy.toFixed(1), r: rad.toFixed(1),
      }));
      const hit = svg("circle", {
        class: "hist-hit", cx: cx.toFixed(1), cy: cy.toFixed(1), r: Math.max(9, rad + 5).toFixed(1),
      });
      hit.addEventListener("mouseenter", (ev) => {
        if (dragging) return;
        tip.innerHTML = dayTipHtml(d, baseCcy);
        tip.hidden = false;
        place(ev as MouseEvent);
      });
      hit.addEventListener("mousemove", (ev) => { if (!dragging) place(ev as MouseEvent); });
      hit.addEventListener("mouseleave", () => { tip.hidden = true; });
      plotG.appendChild(hit);
    });
  };

  // clientX -> SVG user-space x (viewBox is 0..W, stretched to the element).
  const userX = (clientX: number) => {
    const r = root.getBoundingClientRect();
    return r.width ? ((clientX - r.left) / r.width) * W : clientX;
  };
  const msForUserX = (ux: number) => view.a + ((ux - PAD.l) / plotW) * (view.b - view.a);
  const setWindow = (a: number, b: number) => {
    const span = Math.max(minSpan, Math.min(fullSpan, b - a));
    if (a < x0) a = x0;
    if (a + span > x1) a = x1 - span;
    view.a = Math.max(x0, a);
    view.b = Math.min(x1, view.a + span);
    render();
  };

  // Wheel = zoom toward the cursor; the date under the pointer stays put.
  root.addEventListener("wheel", (ev) => {
    ev.preventDefault();
    const anchor = msForUserX(userX((ev as WheelEvent).clientX));
    const span = view.b - view.a;
    const factor = (ev as WheelEvent).deltaY < 0 ? 0.82 : 1 / 0.82;
    const newSpan = Math.max(minSpan, Math.min(fullSpan, span * factor));
    const ratio = (anchor - view.a) / span; // keep anchor at same fractional x
    setWindow(anchor - ratio * newSpan, anchor - ratio * newSpan + newSpan);
  }, { passive: false });

  // Drag = pan. Track on the document so a fast drag doesn't escape the svg.
  let dragStartX = 0, dragA = 0, dragB = 0;
  const onMove = (ev: MouseEvent) => {
    const dms = (userX(dragStartX) - userX(ev.clientX)) / plotW * (dragB - dragA);
    setWindow(dragA + dms, dragB + dms);
  };
  const onUp = () => {
    dragging = false;
    root.classList.remove("dragging");
    document.removeEventListener("mousemove", onMove);
    document.removeEventListener("mouseup", onUp);
  };
  root.addEventListener("mousedown", (ev) => {
    if (view.b - view.a >= fullSpan) return; // nothing to pan when fully zoomed out
    dragging = true;
    tip.hidden = true;
    dragStartX = (ev as MouseEvent).clientX; dragA = view.a; dragB = view.b;
    root.classList.add("dragging");
    document.addEventListener("mousemove", onMove);
    document.addEventListener("mouseup", onUp);
    ev.preventDefault();
  });
  // Double-click anywhere resets to the full span.
  root.addEventListener("dblclick", () => setWindow(x0, x1));

  render();
  wrap.appendChild(root);
  wrap.appendChild(tip);
  return wrap;
}

// Hover card for a single trading day: a per-name digest (options folded under
// their underlying) plus the day's net cash flow, capped so a 29-fill day stays
// readable.
function dayTipHtml(d: DayGroup, baseCcy = ""): string {
  const n = d.trades.length;
  const head =
    `<div class="hist-tip-date">${esc(d.date)} · ${n} trade${n > 1 ? "s" : ""}</div>`;
  const byName = new Map<string, Trade[]>();
  for (const t of d.trades) {
    const name = t.underlying || t.symbol || "?";
    if (!byName.has(name)) byName.set(name, []);
    byName.get(name)!.push(t);
  }
  const entries = [...byName.entries()];
  const shown = entries.slice(0, 8);
  const rows = shown.map(([name, ts]) => tradeLine(name, ts)).join("");
  const more = entries.length > shown.length
    ? `<div class="hist-tip-more">+${entries.length - shown.length} more name(s)</div>`
    : "";
  const net = d.trades.reduce((s, t) => s + (Number(t.base_cash_flow) || 0), 0);
  const netLine =
    `<div class="hist-tip-net">net cash ${sensitive(fmtSigned(net), "cash flow")}${ccyTag(baseCcy)}</div>`;
  return head + `<div class="hist-tip-rows">${rows}</div>` + more + netLine;
}

function tradeLine(name: string, ts: Trade[]): string {
  if (ts.length === 1 && !ts[0].is_option) {
    const t = ts[0];
    const side = t.side === "BUY" ? "buy" : "sell";
    return `<div class="hist-tip-row"><span class="hist-tip-side ${side}">${esc(t.side)}</span> ` +
      `${esc(Math.abs(Number(t.quantity)))} <strong>${esc(name)}</strong> @ ${esc(t.price)}${ccyTag(t.currency)}</div>`;
  }
  const buys = ts.filter((t) => t.side === "BUY").length;
  const sells = ts.length - buys;
  const isOpt = ts.some((t) => t.is_option);
  const what = isOpt ? `${ts.length} opt${ts.length > 1 ? "s" : ""}` : `${ts.length} trades`;
  return `<div class="hist-tip-row"><strong>${esc(name)}</strong> · ${what} ` +
    `<span class="muted">(${buys}B/${sells}S)</span></div>`;
}

export function legend(): HTMLElement {
  const l = el("div", "hist-legend");
  l.innerHTML =
    `<span><i class="hist-key line"></i> NAV</span>` +
    `<span><i class="hist-key buy"></i> Buy day</span>` +
    `<span><i class="hist-key sell"></i> Sell day</span>` +
    `<span><i class="hist-key mixed"></i> Buy + sell</span>`;
  return l;
}
