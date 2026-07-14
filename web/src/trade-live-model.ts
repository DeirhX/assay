import { esc } from "./core";
import type { LiveOrder, PegState } from "./trade-types";

export function orderExecutionDistance(order: LiveOrder): number | null {
  const side = String(order.side || "").toUpperCase();
  const reference = side === "BUY" ? order.quote?.ask : order.quote?.bid;
  const limit = typeof order.price === "number"
    ? order.price
    : order.price != null && order.price !== "" ? Number(order.price) : NaN;
  if (typeof reference !== "number" || reference === 0 || !Number.isFinite(limit)) return null;
  return Math.abs(limit - reference) / reference;
}

function px(value: number): string {
  return value.toLocaleString("en-US", {
    minimumFractionDigits: 2,
    maximumFractionDigits: 4,
  });
}

function pxCost(value: number): string {
  return value.toLocaleString("en-US", {
    minimumFractionDigits: 2,
    maximumFractionDigits: 2,
  });
}

const EMPTY_CELL = `<span class="trade-live-dim">\u00b7</span>`;

function edgeMeter(gapPct: number, label: string): string {
  const magnitude = Math.abs(gapPct);
  const cap = 120;
  const fraction = Math.min(1, Math.log1p(magnitude) / Math.log1p(cap));
  const fill = Math.max(6, Math.round(fraction * 100));
  const hue = Math.round((1 - fraction) * 130);
  return `<span class="trade-live-edge" title="Distance from execution: limit ${esc(label)}">` +
    `<span class="edge-meter"><span class="edge-fill" ` +
    `style="width:${fill}%;background:hsl(${hue}, 68%, 52%)"></span></span>` +
    `<span class="edge-num">${magnitude.toFixed(1)}%</span></span>`;
}

function costCell(order: LiveOrder): string {
  if (String(order.side || "").toUpperCase() !== "SELL") return EMPTY_CELL;
  const cost = typeof order.avg_cost === "number" ? order.avg_cost : null;
  if (cost == null || cost <= 0) return EMPTY_CELL;
  const limit = typeof order.price === "number"
    ? order.price
    : order.price != null && order.price !== "" ? Number(order.price) : NaN;
  if (Number.isFinite(limit)) {
    const gain = ((limit - cost) / cost) * 100;
    const cls = gain >= 0 ? "gain" : "loss";
    const sign = gain >= 0 ? "+" : "\u2212";
    return `<span class="trade-live-cost" title="avg purchase price ${esc(pxCost(cost))}; ` +
      `the limit locks ${sign}${Math.abs(gain).toFixed(1)}%">` +
      `${pxCost(cost)} <span class="${cls}">${sign}${Math.abs(gain).toFixed(1)}%</span></span>`;
  }
  return `<span class="trade-live-cost" title="average purchase price">${pxCost(cost)}</span>`;
}

export function marketCells(order: LiveOrder, quotesPending: boolean): string {
  const quote = order.quote;
  const bid = quote && typeof quote.bid === "number" ? quote.bid : null;
  const ask = quote && typeof quote.ask === "number" ? quote.ask : null;
  const last = quote && typeof quote.last === "number" ? quote.last : null;
  const cold = bid == null && ask == null && last == null;

  let quoteCell: string;
  let quoteTitle = "";
  let quoteWide = false;
  if (cold) {
    quoteCell = quotesPending
      ? `<span class="trade-live-quoteload"><span class="spinner"></span> quote\u2026</span>`
      : `<span class="muted">no quote</span>`;
  } else if (bid != null && ask != null) {
    const spread = ask - bid;
    const mid = (ask + bid) / 2;
    const spreadPct = mid > 0 ? (spread / mid) * 100 : 0;
    quoteWide = spreadPct >= 1;
    quoteTitle = `Bid-ask spread: ${spreadPct.toFixed(2)}% (\u0394${px(spread)})` +
      (quoteWide ? ". Unusually wide spread; execution may be costly or slow." : "");
    quoteCell = `${px(bid)} <span class="muted">\u00d7</span> ${px(ask)}` +
      (quoteWide ? ` <span class="trade-live-spread-hint">wide</span>` : "");
  } else {
    quoteCell = EMPTY_CELL;
  }

  const lastCell = last != null ? px(last) : EMPTY_CELL;
  let edgeCell = EMPTY_CELL;
  const limit = typeof order.price === "number"
    ? order.price
    : order.price != null && order.price !== "" ? Number(order.price) : NaN;
  const side = String(order.side || "").toUpperCase();
  if (Number.isFinite(limit) && (bid != null || ask != null)) {
    const reference = side === "BUY" ? ask : bid;
    if (reference != null && reference > 0) {
      const gapPct = side === "BUY"
        ? ((reference - limit) / reference) * 100
        : ((limit - reference) / reference) * 100;
      const word = side === "BUY"
        ? gapPct >= 0 ? "below ask" : "above ask"
        : gapPct >= 0 ? "above bid" : "below bid";
      const label = Math.abs(gapPct) < 0.05
        ? `at the ${side === "BUY" ? "ask" : "bid"}`
        : `${Math.abs(gapPct).toFixed(1)}% ${word}`;
      edgeCell = edgeMeter(gapPct, label);
    }
  }

  return `<span class="trade-live-edge-c">${edgeCell}</span>` +
    `<span class="trade-live-last num">${lastCell}</span>` +
    `<span class="trade-live-quote${quoteWide ? " wide" : ""}"` +
      `${quoteTitle ? ` title="${esc(quoteTitle)}"` : ""}>${quoteCell}</span>` +
    `<span class="trade-live-cost-c">${costCell(order)}</span>`;
}

function agoShort(sinceMs: number): string {
  const seconds = Math.max(0, (Date.now() - sinceMs) / 1000);
  const minutes = seconds / 60;
  const hours = minutes / 60;
  const days = hours / 24;
  if (seconds < 90) return "just now";
  if (minutes < 90) return `${Math.round(minutes)}m`;
  if (hours < 36) return `${Math.round(hours)}h`;
  if (days < 14) return `${Math.round(days)}d`;
  if (days < 60) return `${Math.round(days / 7)}w`;
  return `${Math.round(days / 30)}mo`;
}

function statusDot(order: LiveOrder): string {
  const status = String(order.status || order.order_status || "").trim();
  const normalized = status.toLowerCase();
  let tone = "unknown";
  if (normalized === "submitted") tone = "live";
  else if (normalized === "presubmitted") tone = "held";
  else if (normalized.startsWith("pending")) tone = "pending";
  else if (normalized === "inactive") tone = "inactive";
  return `<span class="trade-live-dot tone-${tone}" title="${esc(status || "unknown status")}"></span>`;
}

export function statusCell(order: LiveOrder, peg?: PegState): string {
  const dot = statusDot(order);
  if (peg) {
    const message = esc(peg.message || peg.state || "");
    return `${dot}${message ? `<span class="trade-live-pegmsg">${message}</span>` : ""}`;
  }
  const stamp = typeof order.lastExecutionTime_r === "number"
    ? order.lastExecutionTime_r
    : null;
  if (!stamp) return dot;
  return `${dot}<span class="trade-live-age" title="last update ` +
    `${esc(new Date(stamp).toLocaleString())}">${esc(agoShort(stamp))}</span>`;
}
