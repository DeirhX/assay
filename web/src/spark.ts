// One hand-rolled sparkline, same no-chart-library stance as band-viz. A view
// renders sparkPlaceholder() slots inline (instant table paint), then makes ONE
// /api/spark batch call via hydrateSparks() to fill them. The endpoint is
// cached-only, so a symbol with no series just leaves an empty slot -- the row
// degrades to no sparkline rather than blocking or erroring.
import { api, esc } from "./core";

export interface SparkSeries {
  points: number[];
  change?: number | null;
  currency?: string | null;
}

const BOX_W = 96;
const BOX_H = 24;
const PAD = 2;

// Pure: a fixed-box inline SVG polyline, no axes. Tone defaults to the sign of
// the first->last move; <title> carries the 3M change when provided.
export function sparkSvg(points: number[], opts: { tone?: string; change?: number | null } = {}): string {
  const pts = (points || []).filter((n) => typeof n === "number" && isFinite(n));
  if (pts.length < 2) return "";
  const min = Math.min(...pts);
  const max = Math.max(...pts);
  const span = max - min || 1;
  const stepX = (BOX_W - 2 * PAD) / (pts.length - 1);
  const coords = pts
    .map((v, i) => {
      const x = PAD + i * stepX;
      const y = PAD + (BOX_H - 2 * PAD) * (1 - (v - min) / span);
      return `${x.toFixed(1)},${y.toFixed(1)}`;
    })
    .join(" ");
  const tone = opts.tone || (pts[pts.length - 1] >= pts[0] ? "up" : "down");
  const title =
    opts.change != null
      ? `3M ${opts.change >= 0 ? "+" : "\u2212"}${Math.abs(opts.change * 100).toFixed(1)}%`
      : "";
  return (
    `<svg class="spark spark-${esc(tone)}" viewBox="0 0 ${BOX_W} ${BOX_H}" width="${BOX_W}" height="${BOX_H}" ` +
    `preserveAspectRatio="none" role="img" aria-label="${esc(title || "price trend")}">` +
    (title ? `<title>${esc(title)}</title>` : "") +
    `<polyline points="${coords}" fill="none" stroke="currentColor" stroke-width="1.5" ` +
    `stroke-linejoin="round" stroke-linecap="round"/></svg>`
  );
}

// The inline placeholder a view drops where a sparkline should appear.
export function sparkPlaceholder(symbol: string): string {
  const sym = (symbol || "").toUpperCase();
  if (!sym) return "";
  return `<span class="spark-slot" data-spark="${esc(sym)}"></span>`;
}

// Collect every [data-spark] slot under root, fetch all series in ONE call, and
// fill each. Never throws: a failed/empty fetch leaves slots blank.
export async function hydrateSparks(root: ParentNode = document): Promise<void> {
  const slots = [...root.querySelectorAll<HTMLElement>(".spark-slot[data-spark]")];
  if (!slots.length) return;
  const syms = [...new Set(slots.map((s) => (s.dataset.spark || "").toUpperCase()).filter(Boolean))];
  if (!syms.length) return;
  let series: Record<string, SparkSeries> = {};
  try {
    const r = await api<{ spark: Record<string, SparkSeries> }>(
      `/api/spark?symbols=${encodeURIComponent(syms.join(","))}`,
    );
    series = r.spark || {};
  } catch {
    return; // degrade silently to no sparklines
  }
  slots.forEach((slot) => {
    const s = series[(slot.dataset.spark || "").toUpperCase()];
    if (s && Array.isArray(s.points)) slot.innerHTML = sparkSvg(s.points, { change: s.change });
  });
}
