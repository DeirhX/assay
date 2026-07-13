import type { PlanRow as RebRow } from "./api-types";
import { POSITION_TRACK_SEL, positionTrackHtml } from "./band-viz";
import { el, esc, fmtCZK, sensitive } from "./core";
import {
  DELTA_EPS, inBandAfter, r1, rebDefaultDelta,
} from "./rebalance-model";

export interface PosRefs {
  track: HTMLElement;
  proj: HTMLElement;
  conn: HTMLElement;
  plannedReadout: HTMLElement;
  movementReadout: HTMLElement;
  landingReadout: HTMLElement;
  curP: number;
  curWeight: number;
}

export function projectedWeightFromPointer(
  clientX: number,
  trackLeft: number,
  trackWidth: number,
  scaleMax: number,
): number {
  if (!Number.isFinite(trackWidth) || trackWidth <= 0 || scaleMax <= 0) return 0;
  const ratio = Math.min(1, Math.max(0, (clientX - trackLeft) / trackWidth));
  return r1(ratio * scaleMax);
}

export function wireProjectedMarker(
  refs: PosRefs,
  scaleMax: number,
  applyProjected: (projected: number) => void,
  commit: () => void,
  ariaLabel = "Projected portfolio weight",
): void {
  const { proj, track } = refs;
  let dragging = false;
  track.classList.add("draggable");
  proj.classList.add("draggable");
  proj.setAttribute("role", "slider");
  proj.setAttribute("tabindex", "0");
  proj.setAttribute("aria-label", ariaLabel);
  proj.setAttribute("aria-valuemin", "0");
  proj.setAttribute("aria-valuemax", String(scaleMax));

  const setProjected = (projected: number) => {
    applyProjected(r1(Math.min(scaleMax, Math.max(0, projected))));
  };
  const setFromPointer = (event: PointerEvent) => {
    const rect = track.getBoundingClientRect();
    setProjected(projectedWeightFromPointer(event.clientX, rect.left, rect.width, scaleMax));
  };

  track.addEventListener("pointerdown", (event) => {
    if (event.button !== 0) return;
    dragging = true;
    proj.classList.add("dragging");
    track.setPointerCapture?.(event.pointerId);
    setFromPointer(event);
    event.preventDefault();
  });
  track.addEventListener("pointermove", (event) => {
    if (dragging) setFromPointer(event);
  });
  const stop = (event: PointerEvent) => {
    if (!dragging) return;
    dragging = false;
    proj.classList.remove("dragging");
    if (track.hasPointerCapture?.(event.pointerId)) {
      track.releasePointerCapture(event.pointerId);
    }
    commit();
  };
  track.addEventListener("pointerup", stop);
  track.addEventListener("pointercancel", stop);
  proj.addEventListener("keydown", (event) => {
    const current = Number(proj.getAttribute("aria-valuenow")) || 0;
    const step = event.shiftKey ? 0.5 : 0.1;
    let next: number | null = null;
    if (event.key === "ArrowLeft" || event.key === "ArrowDown") next = current - step;
    if (event.key === "ArrowRight" || event.key === "ArrowUp") next = current + step;
    if (event.key === "Home") next = 0;
    if (event.key === "End") next = scaleMax;
    if (next == null) return;
    event.preventDefault();
    setProjected(next);
    commit();
  });
}

export function distributeSleeveDelta(
  current: number[],
  defaults: number[],
  desiredTotal: number,
): number[] {
  if (!current.length) return [];
  const currentTotal = current.reduce((sum, value) => sum + value, 0);
  const defaultTotal = defaults.reduce((sum, value) => sum + value, 0);
  const seed = Math.abs(currentTotal) > DELTA_EPS
    ? current
    : Math.abs(defaultTotal) > DELTA_EPS
      ? defaults
      : current.map(() => 1);
  const seedTotal = seed.reduce((sum, value) => sum + value, 0);
  const raw = Math.abs(seedTotal) > DELTA_EPS
    ? seed.map((value) => value * desiredTotal / seedTotal)
    : seed.map(() => desiredTotal / seed.length);
  const rounded = raw.map((value) => r1(value));
  const remainder = r1(desiredTotal - rounded.reduce((sum, value) => sum + value, 0));
  rounded[rounded.length - 1] = r1(rounded[rounded.length - 1] + remainder);
  return rounded;
}

export function posCell(
  row: RebRow,
  scaleMax: number,
): { cell: HTMLElement; refs: PosRefs } {
  const cell = el("div", "reb-c reb-pos");
  const low = typeof row.low === "number" ? row.low : 0;
  const high = typeof row.high === "number" ? row.high : low;
  const defaultDelta = row.interactive ? rebDefaultDelta(row) : (row.suggest_delta_pct || 0);
  const draggable = row.interactive || row.kind === "sleeve";
  const projected = (row.current_pct || 0) + defaultDelta;
  const inBand = inBandAfter(projected, low, high);
  const moveClass = defaultDelta > DELTA_EPS
    ? "buy"
    : defaultDelta < -DELTA_EPS ? "sell" : "";
  const connectionTone = moveClass === "buy"
    ? "buy"
    : moveClass === "sell" ? "sell" : "none";
  const movementHtml = (delta: number) => delta > DELTA_EPS
    ? `<b>→</b> Increase <strong>${Math.abs(delta).toFixed(2)} pp</strong>`
    : delta < -DELTA_EPS
      ? `<b>←</b> Reduce <strong>${Math.abs(delta).toFixed(2)} pp</strong>`
      : `<b>·</b> No move needed`;
  const bandText = `${low.toFixed(1)}–${high.toFixed(1)}%`;
  const meta =
    `<span class="reb-pos-cur"><i>Current</i><b>${row.current_pct.toFixed(2)}%</b></span>` +
    `<small>${sensitive(`${fmtCZK(row.current_czk)} CZK`, "position value")}</small>` +
    `<span class="reb-band-cue">Target band <b>${bandText}</b></span>`;
  const aria = `${esc(row.name)}: current ${row.current_pct.toFixed(1)}%, target band ` +
    `${low.toFixed(1)} to ${high.toFixed(1)}%, ` +
    `${row.status === "BELOW" ? "move right by adding" : row.status === "ABOVE" ? "move left by reducing" : "currently in band"}`;
  const trackBuilt = positionTrackHtml({
    scaleMax,
    band: { low, high },
    current: row.current_pct,
    projected,
    ariaLabel: aria,
    opts: {
      role: draggable ? "group" : "img",
      connTone: connectionTone,
      showConn: true,
      inBand,
      showAxis: false,
      currentTitle: `current ${row.current_pct.toFixed(2)}%`,
      projectedTitle: `projected ${projected.toFixed(2)}%`,
    },
  });
  cell.innerHTML =
    `<div class="reb-pos-meta">${meta}</div>` +
    trackBuilt.html +
    `<div class="reb-track-readout">` +
      `<span class="reb-track-movement ${moveClass}">${movementHtml(defaultDelta)}</span>` +
      `<span class="reb-track-planned"><i></i>Planned <b>${projected.toFixed(2)}%</b></span>` +
      `<span class="reb-track-landing ${inBand ? "in" : "out"}">${inBand ? "Inside target" : "Outside target"}</span>` +
    `</div>`;

  const track = cell.querySelector(`.${POSITION_TRACK_SEL.track}`) as HTMLElement;
  const proj = cell.querySelector(`.${POSITION_TRACK_SEL.projMark}`) as HTMLElement;
  const conn = cell.querySelector(`.${POSITION_TRACK_SEL.conn}`) as HTMLElement;
  const plannedReadout = cell.querySelector(".reb-track-planned b") as HTMLElement;
  const movementReadout = cell.querySelector(".reb-track-movement") as HTMLElement;
  const landingReadout = cell.querySelector(".reb-track-landing") as HTMLElement;
  return {
    cell,
    refs: {
      track,
      proj,
      conn,
      plannedReadout,
      movementReadout,
      landingReadout,
      curP: trackBuilt.geom.curP ?? 0,
      curWeight: row.current_pct,
    },
  };
}
