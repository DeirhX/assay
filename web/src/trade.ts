import { $, api, el, esc, fmtCZK, isStaleToken, nextToken, sensitive, state } from "./core";
import { pollDeepJob } from "./jobs";
import { openJournalWith } from "./journal";
import { hydrateSparks, sparkPlaceholder } from "./spark";
import {
  basketMoneyFacts, gatewayOrigin, placeResultHtml, riskPanelHtml, sideTag,
  weightBandCaption, weightBandTrackHtml, weightScaleMax,
} from "./trade-model";
import type { OrderBand, PlaceResult, RiskDelta } from "./trade-model";

// ---- trade desk -----------------------------------------------------------
// The ONLY surface in Assay that can place real orders. It reuses the basket
// staged in the Rebalance planner (state.stagedBasket), previews it through
// IBKR's Client Portal Web API for margin/commission, and places it only after
// per-order human confirmation. Everything is gated server-side too; this UI
// just refuses early and explains why.

// /api/trade/status: gateway connection + account posture.
interface TradeAccount {
  id: string;
  kind?: string;
}

interface TradeStatus {
  trading_enabled?: boolean;
  authenticated?: boolean;
  gateway_base?: string | null;
  default_account?: string | null;
  accounts?: TradeAccount[];
  live_allowed?: boolean;
  competing?: boolean;
  // Present only on a /api/trade/reconnect response: the reason a reconnect
  // attempt failed (e.g. the saved SSO login expired), else null.
  reconnect_error?: string | null;
}

// /api/trade/tickle: the lightweight session-only shape used by the keepalive.
interface TradeTickle {
  trading_enabled?: boolean;
  authenticated?: boolean;
  connected?: boolean;
  competing?: boolean;
}

// One sized order inside a /api/trade/preview response.
interface TradeOrder {
  symbol?: string;
  conid?: string | number;
  side?: string;
  quantity?: number | string;
  orderType?: string;
  price?: number | null;
  tif?: string;
}

interface TradePreview {
  is_paper?: boolean;
  live_allowed?: boolean;
  account?: string;
  kind?: string;
  warnings?: string[];
  // Basket names the account can't buy directly (US-domiciled / no PRIIPs KID):
  // their buy orders were dropped server-side; reachable only via options.
  options_only?: string[];
  preview_ttl_s?: number;
  orders?: TradeOrder[];
  // The raw IBKR margin/commission blob; shape varies per account/order type.
  ibkr_preview?: any;
  // The normalized basket the token binds to: [{symbol, delta_czk}]. Echoed to
  // /api/trade/place and used here for the last-mile money facts on the modal.
  trades?: Array<{ symbol: string; delta_czk: number }>;
  token?: string;
  // Structured snapshot staleness (mirrors the prose warning) so the UI can
  // turn it into a soft gate instead of parsing the warnings[] strings.
  snapshot_age_days?: number | null;
  snapshot_stale?: boolean;
  stale_after_days?: number;
  // Per-target-symbol band context (before/after weight vs band) from the local
  // what-if, so each order can show its effect on its band at confirm time.
  order_bands?: Record<string, OrderBand>;
  // The local what-if recompute; carries the pre-trade risk delta.
  local_whatif?: { risk?: RiskDelta } | null;
}

// One live working order from /api/trade/orders. Field names vary by IBKR
// endpoint version, so every reader-facing alias is optional.
interface LiveOrder {
  orderId?: string | number;
  order_id?: string | number;
  ticker?: string;
  symbol?: string;
  conid?: string | number;
  side?: string;
  totalSize?: number | string;
  quantity?: number | string;
  remainingQuantity?: number | string;
  filledQuantity?: number | string;
  status?: string;
  order_status?: string;
  orderType?: string;
  order_type?: string;
  price?: number | string | null;
  tif?: string;
  timeInForce?: string;
  // IBKR's human-readable one-liner, e.g. "Sell 100 AAPL Limit 150.00 GTC".
  orderDesc?: string;
  // Epoch ms of the order's last update (placement/modify) — our age proxy.
  lastExecutionTime_r?: number;
  // Live market snapshot, hydrated asynchronously after the list paints (the
  // snapshot round-trip is ~as slow as the orders fetch, so it's a second call).
  quote?: Quote;
  // Average purchase price (holdings cost basis / share), in the instrument's
  // currency. Present when the order's symbol is held; drives a SELL's gain/loss.
  avg_cost?: number;
}

type Quote = { last?: number | null; bid?: number | null; ask?: number | null };

// One active peg from /api/trade/orders (folded in alongside `orders`). The
// server keeps these in memory; a restart clears them and the order simply
// rests at its last price.
interface PegState {
  order_id: string;
  state?: string;
  reprices?: number;
  price?: number | null;
  message?: string;
  side?: string;
  symbol?: string;
  bound?: number;
  tick?: number;
}

let _status: TradeStatus | null = null;   // last /api/trade/status
let _preview: TradePreview | null = null;  // last /api/trade/preview (carries the place token)
// The basket as it was placed — snapshotted before the staged store is cleared,
// so the "Log to journal" next step can still describe the executed trades.
let _placedBasket: Array<{ symbol: string; delta_czk: number }> = [];
// Mirrors the server's preview TTL: a 1s interval both counts the remaining time
// down on the Place button and locks it when the window lapses (the server
// enforces the same window on its side). Interval, not timeout, so the button
// can show "1:42 left" instead of silently flipping to expired.
let _previewTimer: ReturnType<typeof setInterval> | null = null;
// Symbols of the working orders resting at IBKR, upper-cased, cached from the
// last renderLiveOrders. The preview cross-checks the staged basket against
// these so a new SELL NVDA on top of a live GTC trim ladder is flagged before
// it double-fills — the preview path never fetches orders itself.
let _workingSymbols = new Set<string>();
// While any order is being pegged, poll the working-orders card so reprices
// show without the user hitting Refresh. Cleared when no peg is active.
let _pegPollTimer: ReturnType<typeof setInterval> | null = null;
const PEG_POLL_MS = 5000;
// While the Trade view is open, tickle the gateway so the brokerage session
// (which idles out after a few minutes) stays warm, and so a silent drop shows
// up without a manual refresh. Scoped to the view: the tick self-cancels once
// #view-trade is no longer active.
let _tickleTimer: ReturnType<typeof setInterval> | null = null;
const TICKLE_MS = 60000;

async function loadTrade() {
  const token = nextToken("trade");
  stopPreviewCountdown();  // a re-entry drops any previous preview's countdown
  const wrap = $("#trade-result");
  if (wrap) wrap.innerHTML = "";
  const refresh = $("#trade-refresh");
  // "Refresh connection" actively re-establishes the brokerage session (not just
  // a status re-read) so a session that idled out can recover without a browser
  // login, as long as the gateway still holds the SSO cookie.
  if (refresh) refresh.onclick = () => void reconnect();
  // /api/trade/status hits the gateway and can take several seconds; show a
  // skeleton so the page isn't blank behind it. The basket is decoupled from it.
  renderConnectionSkeleton();
  // Rehydrate the basket the planner persisted server-side so it survives a
  // reload or navigating away and back — it used to live only in browser memory.
  // Its own fetch is fast (~40ms), so we render it NOW rather than gating it
  // behind the slow status call, which is what made the basket appear late.
  try {
    const res = await api<{ trades?: Array<{ symbol: string; delta_czk: number }> }>("/api/trade/basket");
    if (isStaleToken("trade", token)) return;
    if (Array.isArray(res.trades)) state.stagedBasket = res.trades;
  } catch (_e) {
    if (isStaleToken("trade", token)) return;  // else: keep whatever's in memory
  }
  renderBasket();
  await renderConnection(token);
}

// Placeholder banner while the (slow) gateway status call is in flight.
function renderConnectionSkeleton(): void {
  const banner = $("#trade-banner");
  if (banner) {
    banner.innerHTML = `<div class="trade-bnr"><span class="spinner"></span> checking gateway connection\u2026</div>`;
  }
}

async function renderConnection(token?: number) {
  const banner = $("#trade-banner");
  const status = $("#trade-status");
  if (status) status.textContent = "";
  try {
    _status = await api<TradeStatus>("/api/trade/status", "GET", null, { timeoutMs: 15_000 });
  } catch (e) {
    if (token != null && isStaleToken("trade", token)) return;
    if (banner) banner.innerHTML = `<div class="trade-bnr bad">Could not read trade status: ${esc((e as Error).message)}</div>`;
    return;
  }
  if (token != null && isStaleToken("trade", token)) return;
  paintConnection(token);
}

// Attempt to re-establish the brokerage session, then repaint. Falls back to a
// plain status read when reconnect is refused (e.g. trading disabled -> 403).
async function reconnect(): Promise<void> {
  const token = nextToken("trade");
  const banner = $("#trade-banner");
  if (banner) banner.innerHTML = `<div class="trade-bnr warn"><span class="spinner"></span> reconnecting to the IBKR gateway\u2026</div>`;
  try {
    _status = await api<TradeStatus>("/api/trade/reconnect", "POST", null, { timeoutMs: 30_000 });
  } catch (_e) {
    if (isStaleToken("trade", token)) return;
    return void renderConnection(token);
  }
  if (isStaleToken("trade", token)) return;
  paintConnection(token);
}

// Render the banner + basket + working orders from the current _status, and
// (re)start the keepalive. Pure UI; callers own fetching _status.
function paintConnection(token?: number) {
  const banner = $("#trade-banner");
  const s = _status;
  if (!s) return;
  const bits = [];

  if (!s.trading_enabled) {
    bits.push(`<div class="trade-bnr bad"><strong>Trading is disabled.</strong> Set <code>IBKR_TRADING_ENABLED=1</code> in <code>tools/secrets.env</code> (and start the Client Portal Gateway), then refresh. Nothing here can place an order until you do.</div>`);
  }
  if (!s.authenticated) {
    const origin = gatewayOrigin(s.gateway_base);
    bits.push(`<div class="trade-bnr warn"><strong>Gateway not connected.</strong> Start the IBKR Client Portal Gateway, log in (with 2FA) at <a href="${esc(origin)}" target="_blank" rel="noopener">${esc(origin)}</a>, then press <em>Refresh connection</em>.</div>`);
    if (s.reconnect_error) {
      bits.push(`<div class="trade-bnr bad">Reconnect failed: ${esc(s.reconnect_error)}. The saved login has likely expired \u2014 log in at the gateway page above.</div>`);
    }
  } else {
    const accounts = s.accounts || [];
    const acct = s.default_account || (accounts[0] && accounts[0].id) || "?";
    const kind = accounts.find((a) => a.id === acct)?.kind || "?";
    const cls = kind === "live" ? "live" : "paper";
    bits.push(`<div class="trade-bnr ${cls}"><strong>${kind === "live" ? "LIVE" : "Paper"} account ${sensitive(esc(acct), "account id")}</strong>` +
      (kind === "live"
        ? (s.live_allowed ? " — live orders are unlocked. Real money." : " — live placement is <strong>locked</strong>. Validate on paper, then set <code>IBKR_ALLOW_LIVE=1</code>.")
        : " — safe simulated account.") +
      (s.competing ? " <em>(another session is competing for this login)</em>" : "") +
      `</div>`);
  }
  if (banner) banner.innerHTML = bits.join("");

  renderBasket();
  void renderLiveOrders(token);
  startKeepalive();
}

function startKeepalive() {
  stopKeepalive();
  if (!(_status && _status.trading_enabled)) return;  // nothing to keep warm
  _tickleTimer = setInterval(() => void keepaliveTick(), TICKLE_MS);
}

function stopKeepalive() {
  if (_tickleTimer) { clearInterval(_tickleTimer); _tickleTimer = null; }
}

async function keepaliveTick() {
  // Self-terminate once the user has navigated off the Trade view (no teardown
  // hook exists; the view just loses its .active class).
  const view = document.getElementById("view-trade");
  if (!view || !view.classList.contains("active")) return stopKeepalive();
  let t: TradeTickle;
  try {
    t = await api<TradeTickle>("/api/trade/tickle", "GET", null, { timeoutMs: 10_000 });
  } catch (_e) {
    return;  // transient; the next tick tries again
  }
  // If the session state flipped since the last paint (a silent drop, or a
  // recovery), re-read + repaint so the banner reflects reality without a click.
  if (_status && (t.authenticated !== _status.authenticated || t.competing !== _status.competing)) {
    void renderConnection();
  }
}

// A center-origin magnitude bar for a basket row: buys grow right (green),
// sells grow left (red), each scaled to the basket's largest trade. Turns the
// column of numbers into a shape you can scan for "what's the big move here".
function basketBar(delta: number, maxAbs: number): string {
  const frac = maxAbs > 0 ? Math.min(1, Math.abs(delta) / maxAbs) : 0;
  const w = (frac * 50).toFixed(1);   // half the track at most (center origin)
  const buy = delta >= 0;
  const style = buy ? `left:50%;width:${w}%` : `right:50%;width:${w}%`;
  return `<span class="basket-bar" aria-hidden="true">` +
    `<span class="basket-bar-fill ${buy ? "buy" : "sell"}" style="${style}"></span></span>`;
}

function renderBasket() {
  const wrap = $("#trade-result");
  if (!wrap) return;
  wrap.innerHTML = "";
  const basket = state.stagedBasket || [];

  const card = el("div", "trade-card");
  const head = el("div", "trade-card-head");
  head.innerHTML = `<span class="trade-card-title">Staged basket</span>` +
    `<span class="muted">${basket.length} trade${basket.length === 1 ? "" : "s"} from the Rebalance planner</span>`;
  card.appendChild(head);

  if (!basket.length) {
    card.appendChild(el("div", "hint",
      "No basket staged. Go to the Rebalance tab, edit the planned amounts, press " +
      "\u201cSimulate basket\u201d, then come back here to preview and place it."));
    wrap.appendChild(card);
    return;
  }

  const facts = basketMoneyFacts(basket);
  const maxAbs = facts.largest ? Math.abs(facts.largest.czk) : 0;
  const buys = basket.filter((t) => t.delta_czk >= 0).length;
  const sells = basket.length - buys;
  const net = facts.buy - facts.sell;      // >0 net cash out (buying), <0 net in
  const gross = facts.buy + facts.sell;

  const table = el("table", "trade-basket-table");
  table.innerHTML =
    `<thead><tr>` +
      `<th>Symbol</th>` +
      `<th class="tb-trend">3M trend</th>` +
      `<th>Side</th>` +
      `<th class="num">Planned (CZK)</th>` +
      `<th class="tb-weight">Relative size</th>` +
    `</tr></thead>` +
    `<tbody>${basket.map((t) => {
      const buy = t.delta_czk >= 0;
      const amt = `${buy ? "+" : "\u2212"}${fmtCZK(Math.abs(t.delta_czk))}`;
      return `<tr>` +
        `<td>${tickerLink(t.symbol)}</td>` +
        `<td class="tb-trend">${sparkPlaceholder(t.symbol)}</td>` +
        `<td>${sideTag(buy ? "BUY" : "SELL")}</td>` +
        `<td class="num ${buy ? "tb-buy" : "tb-sell"}">${sensitive(amt, "planned trade size")}</td>` +
        `<td class="tb-weight">${sensitive(basketBar(t.delta_czk, maxAbs), "relative trade size")}</td>` +
      `</tr>`;
    }).join("")}</tbody>` +
    `<tfoot><tr>` +
      `<td colspan="3" class="tb-foot-count">` +
        `<span class="trade-side buy">${buys} buy${buys === 1 ? "" : "s"}</span> · ` +
        `<span class="trade-side sell">${sells} sell${sells === 1 ? "" : "s"}</span>` +
      `</td>` +
      `<td class="num" title="net cash impact — buys minus sells">` +
        `<span class="muted">net</span> ${sensitive(`${net >= 0 ? "+" : "\u2212"}${fmtCZK(Math.abs(net))}`, "net basket cash")}` +
      `</td>` +
      `<td class="tb-weight muted" title="gross traded value — buys plus sells">gross ${sensitive(fmtCZK(gross), "gross basket value")}</td>` +
    `</tr></tfoot>`;
  card.appendChild(table);
  // Trend sparklines: cached-only batch fill after the table paints (degrades to
  // a blank cell for a name with no cached series).
  void hydrateSparks(table);

  const actions = el("div", "trade-actions");
  const previewBtn = el("button", "primary", "Preview through IBKR");
  previewBtn.type = "button";
  previewBtn.disabled = !(_status && _status.trading_enabled && _status.authenticated);
  if (previewBtn.disabled) previewBtn.title = "Enable trading and connect the gateway first";
  previewBtn.onclick = () => doPreview(previewBtn);
  actions.appendChild(previewBtn);
  card.appendChild(actions);
  card.appendChild(el("div", "status", "")).id = "trade-preview-status";

  wrap.appendChild(card);
}

// The bare preview round-trip + render, factored out so the stale-snapshot
// resync can re-issue it (fresh marks -> the staleness gate clears itself)
// without owning a Preview button.
async function requestPreview(): Promise<void> {
  // A wedged gateway can accept the socket yet never answer; without a bound this
  // spins forever with no feedback. Preview is read-only (whatif), so aborting it
  // is safe -- unlike Place, which we never time out client-side.
  _preview = await api<TradePreview>("/api/trade/preview", "POST", {
    trades: state.stagedBasket || [],
    account: _status && _status.default_account,
  }, { timeoutMs: 60_000 });
  renderPreview();
}

async function doPreview(btn: HTMLButtonElement) {
  const status = $("#trade-preview-status");
  if (status) { status.classList.remove("err"); status.innerHTML = `<span class="spinner"></span> previewing\u2026`; }
  if (btn) btn.disabled = true;
  try {
    await requestPreview();
    if (status) status.textContent = "";
  } catch (e) {
    if (status) { status.classList.add("err"); status.textContent = "preview failed: " + (e as Error).message; }
  } finally {
    if (btn) btn.disabled = false;
  }
}

function stopPreviewCountdown() {
  if (_previewTimer) { clearInterval(_previewTimer); _previewTimer = null; }
}

function renderPreview() {
  const wrap = $("#trade-result");
  if (!wrap || !_preview) return;
  const p = _preview;

  // Drop any earlier preview/place card, keep the basket card.
  wrap.querySelectorAll(".trade-preview-card").forEach((n) => n.remove());

  const card = el("div", "card trade-preview-card");
  const isLive = !p.is_paper;
  const liveBlocked = isLive && !p.live_allowed;

  card.appendChild(el("div", "trade-card-title",
    `Preview \u2014 ${isLive ? "LIVE" : "paper"} account ${sensitive(esc(p.account), "account id")}`));

  (p.warnings || []).forEach((w) =>
    card.appendChild(el("div", "trade-warn", esc(w))));

  const optionsOnly = p.options_only || [];
  if (optionsOnly.length) {
    const note = el("div", "trade-optonly");
    note.appendChild(el("div", "trade-optonly-hd",
      `Options-only \u2014 ${optionsOnly.map((s) => sensitive(esc(s), "ticker")).join(", ")}`));
    note.appendChild(el("div", "trade-optonly-body",
      "US-domiciled / no PRIIPs KID: EU retail can't buy the shares directly, so these buys were dropped from the order set. Get the exposure via options \u2014 sell a put or buy a call and take assignment / exercise into shares. (Selling or closing an existing position is unaffected.)"));
    card.appendChild(note);
  }

  if (!p.orders || !p.orders.length) {
    const msg = optionsOnly.length
      ? "Every buy in this basket is options-only (see above); nothing left to place directly."
      : "No orders could be sized from this basket. See the warnings above (no contract / no price / rounds to zero shares).";
    card.appendChild(el("div", "hint", msg));
    wrap.appendChild(card);
    return;
  }

  // IBKR margin/commission impact, when the gateway returned it.
  const impact = Array.isArray(p.ibkr_preview) ? p.ibkr_preview[0] : p.ibkr_preview;
  if (impact && (impact.amount || impact.initial || impact.maintenance || impact.commission)) {
    const grid = el("div", "trade-impact");
    const add = (label: string, val: any) => { if (val) grid.appendChild(el("div", "trade-impact-cell", `<span class="muted">${esc(label)}</span> ${esc(typeof val === "object" ? (val.amount || JSON.stringify(val)) : val)}`)); };
    add("Order value", impact.amount && (impact.amount.amount || impact.amount));
    add("Init margin", impact.initial && (impact.initial.after || impact.initial.amount));
    add("Maint margin", impact.maintenance && (impact.maintenance.after || impact.maintenance.amount));
    add("Est. commission", impact.commission || (impact.amount && impact.amount.commission));
    if (grid.childNodes.length) card.appendChild(grid);
  } else {
    card.appendChild(el("div", "hint", "IBKR did not return a margin/commission preview (some accounts or order types omit it). Confirm carefully."));
  }

  // Per-order confirmation. Place stays disabled until every box is ticked, the
  // stale-snapshot gate is cleared, and the preview hasn't timed out.
  const confirmState = p.orders.map(() => false);
  // A stale holdings snapshot arms a soft gate: the sizing math trusts its marks,
  // so Place is locked until the user resyncs or explicitly accepts stale marks.
  let staleAck = !p.snapshot_stale;
  let expired = false;
  // Seconds left on the preview window; null when there's nothing to count
  // (no TTL, or already expired). Drives the live countdown on the button.
  let secondsLeft: number | null = p.preview_ttl_s ? Math.round(p.preview_ttl_s) : null;

  const placeBtn = el("button", "danger", "");
  placeBtn.type = "button";
  const mmss = (s: number) => `${Math.floor(s / 60)}:${String(Math.max(0, s % 60)).padStart(2, "0")}`;

  const paintPlaceBtn = () => {
    if (liveBlocked) {
      placeBtn.disabled = true;
      placeBtn.textContent = "Live placement locked";
      return;
    }
    if (expired) {
      placeBtn.disabled = true;
      placeBtn.textContent = "Preview expired — re-preview";
      placeBtn.title = "Prices and sizes are stale; run Preview again to re-arm placement";
      return;
    }
    placeBtn.disabled = !(confirmState.every(Boolean) && staleAck);
    const n = (p.orders || []).length;
    const base = `Place ${n} order${n === 1 ? "" : "s"} on ${isLive ? "LIVE" : "paper"}`;
    placeBtn.textContent = secondsLeft != null ? `${base} — ${mmss(secondsLeft)} left` : base;
  };

  const table = el("table", "trade-orders-table");
  table.innerHTML = `<thead><tr><th>Confirm</th><th>Symbol</th><th>Side</th><th class="num">Qty</th><th>Type</th><th>conid</th></tr></thead>`;
  const tbody = el("tbody");
  // One shared axis for every band track, sized to the previewed names only.
  const bands = p.order_bands || {};
  const bandScale = weightScaleMax(
    p.orders.map((o) => bands[String(o.symbol || "").trim().toUpperCase()]).filter(Boolean) as OrderBand[]);
  p.orders.forEach((o, i) => {
    const tr = el("tr");
    const cb = el("input");
    cb.type = "checkbox";
    cb.addEventListener("change", () => { confirmState[i] = cb.checked; paintPlaceBtn(); });
    const td = el("td");
    td.appendChild(cb);
    tr.appendChild(td);
    const sym = String(o.symbol || "").trim().toUpperCase();
    const collides = !!sym && _workingSymbols.has(sym);
    if (collides) tr.classList.add("trade-collide-row");
    tr.insertAdjacentHTML("beforeend",
      `<td>${o.symbol ? tickerLink(o.symbol) : esc(String(o.conid ?? ""))}</td>` +
      `<td>${sideTag(o.side ?? "")}</td>` +
      `<td class="num">${esc(o.quantity)}</td>` +
      `<td>${o.orderType === "LMT" && o.price != null ? `<span class="trade-lmt">LMT @ ${esc(o.price)}</span>` : esc(o.orderType)} / ${esc(o.tif)}</td>` +
      `<td class="muted">${esc(o.conid)}</td>`);
    tbody.appendChild(tr);
    if (collides) {
      // A working order already rests on this symbol — placing another risks a
      // double fill. Loud, row-level, but not a hard block (the user may be
      // deliberately reconciling); they still tick the box to own the decision.
      const note = el("tr", "trade-collide-note");
      const cell = el("td");
      cell.colSpan = 6;
      cell.innerHTML = `\u26a0 <strong>${tickerLink(sym)}</strong> already has a working order at IBKR \u2014 ` +
        `placing this could double-trade. Cancel or reconcile the existing order (see Working orders below) before you tick this.`;
      note.appendChild(cell);
      tbody.appendChild(note);
    }
    // Effect-on-band track: ties this order back to its reason — where it moves
    // the name's weight relative to its target band. Out-of-band lands are a red
    // flag the plain qty/price row can't show.
    const band = bands[sym];
    if (band && (band.before_pct != null || band.after_pct != null)) {
      const brow = el("tr", "trade-band-row" +
        (String(band.status_after || "").toUpperCase() === "IN" ? "" : " out"));
      const cell = el("td");
      cell.colSpan = 6;
      cell.innerHTML = `<div class="trade-band-wrap">${weightBandTrackHtml(sym, band, bandScale)}` +
        `<span class="trade-band-cap">${weightBandCaption(band)}</span></div>`;
      brow.appendChild(cell);
      tbody.appendChild(brow);
    }
  });
  table.appendChild(tbody);
  card.appendChild(table);

  // Basket-level risk delta: concentration/diversification before -> after, with
  // threshold breaches surfaced as pre-flight warnings right where the decision is.
  const riskHtml = riskPanelHtml(p.local_whatif?.risk);
  if (riskHtml) card.insertAdjacentHTML("beforeend", riskHtml);

  if (liveBlocked) {
    card.appendChild(el("div", "trade-warn",
      "Live placement is locked. Set IBKR_ALLOW_LIVE=1 only after you have validated this flow on the paper account."));
  }

  // Stale-snapshot soft gate: a one-click resync (the job already exists) or an
  // explicit opt-out. Either arms Place; until then it stays locked.
  if (p.snapshot_stale) {
    const gate = el("div", "trade-stale-gate");
    gate.appendChild(el("div", "trade-stale-msg",
      `\u26a0 Holdings snapshot is <strong>${esc(p.snapshot_age_days)} day(s)</strong> old \u2014 ` +
      `order sizes come from its marks. Resync before placing real orders.`));
    const row = el("div", "trade-stale-actions");
    const resync = el("button", "ghost", "Resync from IBKR");
    resync.type = "button";
    resync.addEventListener("click", () => void resyncStaleSnapshot(resync));
    const ack = el("label", "trade-stale-ack");
    const ackBox = el("input") as HTMLInputElement;
    ackBox.type = "checkbox";
    ackBox.addEventListener("change", () => { staleAck = ackBox.checked; paintPlaceBtn(); });
    ack.appendChild(ackBox);
    ack.appendChild(document.createTextNode(" Size from stale marks anyway"));
    row.appendChild(resync);
    row.appendChild(ack);
    gate.appendChild(row);
    card.appendChild(gate);
  }

  const actions = el("div", "trade-actions");
  // "Confirm all" nukes the per-order attention ritual in one click — fine on
  // paper, exactly wrong on a live account, so it's omitted there.
  if (!isLive) {
    const allBtn = el("button", "ghost", "Confirm all");
    allBtn.type = "button";
    allBtn.onclick = () => {
      table.querySelectorAll<HTMLInputElement>('input[type="checkbox"]').forEach((cb, i) => { cb.checked = true; confirmState[i] = true; });
      paintPlaceBtn();
    };
    actions.appendChild(allBtn);
  }
  placeBtn.onclick = () => doPlace(placeBtn);
  actions.appendChild(placeBtn);
  card.appendChild(actions);
  card.appendChild(el("div", "status", "")).id = "trade-place-status";

  paintPlaceBtn();

  // Server-side the token expires after preview_ttl_s; mirror it here with a
  // visible countdown so the refusal is anticipated, not a surprise flip.
  stopPreviewCountdown();
  if (p.preview_ttl_s) {
    const deadline = Date.now() + p.preview_ttl_s * 1000;
    _previewTimer = setInterval(() => {
      const left = Math.round((deadline - Date.now()) / 1000);
      if (left <= 0) {
        expired = true;
        secondsLeft = null;
        stopPreviewCountdown();
      } else {
        secondsLeft = left;
      }
      paintPlaceBtn();
    }, 1000);
  }
  wrap.appendChild(card);
}

// Resync the holdings snapshot from IBKR (read-only) so the stale-snapshot gate
// can clear itself: on completion we simply re-preview, and a fresh snapshot age
// drops the gate entirely.
async function resyncStaleSnapshot(btn: HTMLButtonElement) {
  const prev = btn.textContent;
  btn.disabled = true;
  btn.textContent = "Syncing…";
  const status = $("#trade-place-status");
  if (status) {
    status.classList.remove("err");
    status.innerHTML = `<span class="spinner"></span> Re-pulling portfolio from IBKR (read-only, can take a minute)…`;
  }
  try {
    const job = await api<{ id: string }>("/api/holdings/sync", "POST", {});
    await pollDeepJob(job.id, status, async () => {
      if (status) status.textContent = "Holdings resynced — re-previewing from fresh marks…";
      await requestPreview();  // fresh snapshot age -> the stale gate is gone
    }, "IBKR sync");
  } catch (e) {
    if (status) { status.classList.add("err"); status.textContent = "Sync failed: " + (e as Error).message; }
    btn.disabled = false;
    btn.textContent = prev;
  }
}

// The last gate before real orders. Replaces window.confirm() (reflex-clickable,
// browser-suppressible) with a modal that restates the facts a human should
// verify at the last moment. On a LIVE account the confirm button stays disabled
// until the account id (or the word PLACE) is typed — friction proportional to
// blast radius; paper keeps a single click.
function confirmPlaceModal(p: TradePreview): Promise<boolean> {
  return new Promise((resolve) => {
    const isLive = !p.is_paper;
    const n = (p.orders || []).length;
    const facts = basketMoneyFacts(p.trades);

    const overlay = el("div", "modal-overlay");
    const panel = el("div", "modal trade-confirm-modal");
    overlay.appendChild(panel);

    let done = false;
    const finish = (ok: boolean) => {
      if (done) return;
      done = true;
      document.removeEventListener("keydown", onKey);
      overlay.remove();
      resolve(ok);
    };
    const onKey = (e: KeyboardEvent) => { if (e.key === "Escape") finish(false); };
    document.addEventListener("keydown", onKey);
    overlay.addEventListener("click", (e) => { if (e.target === overlay) finish(false); });

    const largest = facts.largest
      ? `${esc(facts.largest.symbol)} ${facts.largest.czk >= 0 ? "+" : "\u2212"}${fmtCZK(Math.abs(facts.largest.czk))} CZK`
      : "\u2014";
    const ageLine = p.snapshot_age_days != null
      ? `<div class="trade-cf-row"><span>Snapshot age</span><span class="${p.snapshot_stale ? "bad" : ""}">` +
        `${esc(p.snapshot_age_days)} day(s)${p.snapshot_stale ? " \u2014 STALE" : ""}</span></div>`
      : "";
    panel.innerHTML =
      `<div class="modal-head"><h2 class="section">${isLive ? "Place LIVE orders \u2014 real money" : "Place paper orders"}</h2></div>` +
      `<div class="trade-cf-facts">` +
      `<div class="trade-cf-row"><span>Account</span><span class="${isLive ? "bad" : ""}">${isLive ? "LIVE" : "paper"} ${sensitive(esc(p.account), "account id")}</span></div>` +
      `<div class="trade-cf-row"><span>Orders</span><span>${n}</span></div>` +
      `<div class="trade-cf-row"><span>Gross buys</span><span>${sensitive(fmtCZK(facts.buy) + " CZK", "gross buys")}</span></div>` +
      `<div class="trade-cf-row"><span>Gross sells</span><span>${sensitive(fmtCZK(facts.sell) + " CZK", "gross sells")}</span></div>` +
      `<div class="trade-cf-row"><span>Largest single</span><span>${sensitive(largest, "largest order")}</span></div>` +
      ageLine +
      `</div>`;

    const confirm = el("button", "danger", isLive ? "Place LIVE orders" : "Place orders");
    confirm.type = "button";

    if (isLive) {
      const want = String(p.account || "").trim();
      const arm = el("div", "trade-cf-arm");
      arm.innerHTML = want
        ? `<label>Type <code>${esc(want)}</code> or <code>PLACE</code> to arm placement:</label>`
        : `<label>Type <code>PLACE</code> to arm placement:</label>`;
      const inp = el("input", "trade-cf-input") as HTMLInputElement;
      inp.type = "text";
      inp.autocomplete = "off";
      inp.spellcheck = false;
      confirm.disabled = true;
      inp.addEventListener("input", () => {
        const v = inp.value.trim();
        confirm.disabled = !((want && v === want) || v.toUpperCase() === "PLACE");
      });
      arm.appendChild(inp);
      panel.appendChild(arm);
      setTimeout(() => inp.focus(), 0);
    }

    const acts = el("div", "modal-actions");
    const cancel = el("button", "ghost", "Cancel");
    cancel.type = "button";
    cancel.addEventListener("click", () => finish(false));
    confirm.addEventListener("click", () => finish(true));
    acts.appendChild(cancel);
    acts.appendChild(confirm);
    panel.appendChild(acts);

    document.body.appendChild(overlay);
  });
}

async function doPlace(btn: HTMLButtonElement) {
  if (!_preview) return;
  // The last-mile confirmation modal (typed arming on LIVE) replaces the native
  // confirm() dialog people click through on reflex.
  if (!(await confirmPlaceModal(_preview))) return;

  const status = $("#trade-place-status");
  if (status) { status.classList.remove("err"); status.innerHTML = `<span class="spinner"></span> placing\u2026`; }
  if (btn) btn.disabled = true;
  try {
    // The preview window no longer matters once we've committed to placing.
    stopPreviewCountdown();
    // Snapshot the basket for the journal next-step before anything clears it.
    _placedBasket = (state.stagedBasket || []).slice();
    const res = await api<PlaceResult>("/api/trade/place", "POST", {
      trades: _preview.trades,
      account: _preview.account,
      token: _preview.token,
      confirm: true,
    });
    if (status) status.textContent = "";
    // The server cleared the staged basket on success; mirror it and reset the
    // view so the desk stops offering the just-placed basket, then append the
    // outcome + loop-closing next steps.
    if (res.staged_basket_cleared) state.stagedBasket = [];
    renderBasket();
    renderPlaceResult(res);
    void renderLiveOrders();
  } catch (e) {
    if (status) { status.classList.add("err"); status.textContent = "placement failed: " + (e as Error).message; }
    if (btn) btn.disabled = false;
  }
}

async function resyncAfterPlace(btn: HTMLButtonElement, status: HTMLElement | null) {
  const prev = btn.textContent;
  btn.disabled = true;
  btn.textContent = "Syncing…";
  if (status) {
    status.classList.remove("err");
    status.innerHTML = `<span class="spinner"></span> Re-pulling portfolio from IBKR (read-only, can take a minute)…`;
  }
  try {
    const job = await api<{ id: string }>("/api/holdings/sync", "POST", {});
    await pollDeepJob(job.id, status, async () => {
      if (status) status.textContent = "Holdings resynced — the planner now sees the post-trade book.";
      btn.textContent = "Resynced ✓";
    }, "IBKR sync");
  } catch (e) {
    if (status) { status.classList.add("err"); status.textContent = "Sync failed: " + (e as Error).message; }
    btn.disabled = false;
    btn.textContent = prev;
  }
}

function logPlacedToJournal(res: PlaceResult) {
  const trades = _placedBasket;
  const first = trades[0] || ({} as { symbol?: string; delta_czk?: number });
  const summary = trades
    .map((t) => `${t.symbol} ${t.delta_czk >= 0 ? "+" : "−"}${fmtCZK(Math.abs(t.delta_czk))}`)
    .join(", ");
  openJournalWith({
    symbol: first.symbol || "",
    action: (first.delta_czk || 0) < 0 ? "trim" : "buy",
    size_czk: first.delta_czk != null ? Math.abs(first.delta_czk) : "",
    thesis: `Placed basket on ${res.kind} account ${res.account}: ${summary || "(see IBKR)"}.`,
  });
}

function renderPlaceResult(res: PlaceResult) {
  const wrap = $("#trade-result");
  if (!wrap) return;
  wrap.querySelectorAll(".trade-result-card").forEach((n) => n.remove());
  const card = el("div", "card trade-result-card");
  card.appendChild(el("div", "trade-card-title", "Placement result"));
  const body = el("div");
  body.innerHTML = placeResultHtml(res);
  const status = el("div", "status");
  body.querySelectorAll<HTMLButtonElement>("[data-trade-next]").forEach((b) => {
    b.addEventListener("click", () => {
      if (b.dataset.tradeNext === "resync") void resyncAfterPlace(b, status);
      else if (b.dataset.tradeNext === "journal") logPlacedToJournal(res);
    });
  });
  card.appendChild(body);
  card.appendChild(status);
  wrap.appendChild(card);
}

// Working orders live only in CPAPI (the Client Portal Gateway), never in the
// Flex snapshot that feeds Holdings. We surface them on every trade-desk load
// and offer a manual refresh, so a GTC ladder placed earlier (e.g. a graceful
// exit) is visible without having to place something new first. Rendered as its
// own card in #trade-result; degrades to a note when the gateway is offline.
async function renderLiveOrders(token?: number) {
  const wrap = $("#trade-result");
  if (!wrap) return;
  wrap.querySelectorAll(".trade-live-card").forEach((n) => n.remove());
  const s = _status;
  if (!s || !s.trading_enabled) return;  // the banner already explains why

  const card = el("div", "card trade-live-card");
  const head = el("div", "trade-card-head");
  const title = el("span", "trade-card-title", "Working orders");
  head.appendChild(title);
  const refreshBtn = el("button", "ghost", "Refresh");
  refreshBtn.type = "button";
  refreshBtn.onclick = () => renderLiveOrders();  // user-initiated: no stale token
  head.appendChild(refreshBtn);
  card.appendChild(head);
  const body = el("div", "trade-live-body");
  card.appendChild(body);
  wrap.appendChild(card);

  if (!s.authenticated) {
    body.appendChild(el("div", "hint",
      "Connect the IBKR Client Portal Gateway (see above) to see your working orders here."));
    return;
  }

  body.innerHTML = `<span class="spinner"></span> loading working orders\u2026`;
  let data: { orders?: LiveOrder[]; pegs?: PegState[] } | undefined;
  try {
    data = await api<{ orders?: LiveOrder[]; pegs?: PegState[] }>("/api/trade/orders", "GET", null, { timeoutMs: 20_000 });
  } catch (e) {
    if (token != null && isStaleToken("trade", token)) return;
    stopPegPoll();
    body.innerHTML = "";
    body.appendChild(el("div", "trade-bnr warn",
      `Could not read working orders: ${esc((e as Error).message)}`));
    return;
  }
  if (token != null && isStaleToken("trade", token)) return;

  const all = (data && data.orders) || [];
  const pegs = (data && data.pegs) || [];
  // Seed quotes from the last hydration so a re-fetch (peg poll / manual
  // refresh) doesn't blink every market cell back to a placeholder before the
  // fresh snapshot lands.
  for (const o of all) {
    const c = orderConid(o);
    if (c != null && _quoteCache.has(c)) o.quote = _quoteCache.get(c);
  }
  // Cache the payload so column-sort clicks can re-render locally without a
  // refetch (which would re-hit IBKR on every click).
  _ordersData = { orders: all, pegs };
  const working = all.filter((o) => !orderTerminal(o));
  // Refresh the cross-check set for the preview: only names with a live working
  // order count as a collision risk.
  _workingSymbols = new Set(
    working.map((o) => String(o.ticker || o.symbol || "").trim().toUpperCase()).filter(Boolean),
  );
  // The market snapshot is a separate ~2s call; paint the list now and stream
  // the quotes into the market cells once they arrive, rather than blocking the
  // whole list on them (which used to double this endpoint's latency).
  _quotesPending = working.some((o) => !o.quote);
  paintOrders(body, title);
  void hydrateQuotes(token, body, title);
  // Keep the card live while a peg is running so its reprice count updates.
  if (pegs.length) startPegPoll();
  else stopPegPoll();
}

// Live quotes, cached by conid so a re-fetch keeps showing the last market while
// the fresh snapshot loads. Whether a hydration is currently in flight drives
// the "loading" vs "no quote" placeholder in the market cell.
const _quoteCache = new Map<string | number, Quote>();
let _quotesPending = false;

function orderConid(o: LiveOrder): number | null {
  const c = o.conid;
  if (c == null || c === "") return null;
  const n = typeof c === "number" ? c : Number(c);
  return Number.isFinite(n) ? n : null;
}

// Fetch {last,bid,ask} for the working orders' conids and fold them into the
// cached payload, then repaint the market cells. Best-effort: a failure just
// leaves the cells in their current (placeholder or stale) state.
async function hydrateQuotes(token: number | undefined, body: HTMLElement, title: HTMLElement): Promise<void> {
  if (!_ordersData) return;
  const conids = Array.from(new Set(
    _ordersData.orders.filter((o) => !orderTerminal(o))
      .map(orderConid).filter((c): c is number => c != null),
  ));
  if (!conids.length) {
    _quotesPending = false;              // nothing to fetch: resolve placeholders
    if (body.isConnected) paintOrders(body, title);
    return;
  }
  let map: Record<string, Quote> = {};
  try {
    const res = await api<{ quotes?: Record<string, Quote> }>(`/api/trade/quotes?conids=${conids.join(",")}`, "GET", null, { timeoutMs: 15_000 });
    map = (res && res.quotes) || {};
  } catch {
    // leave the placeholder; a later poll/refresh may succeed
  }
  _quotesPending = false;
  if (token != null && isStaleToken("trade", token)) return;
  if (!_ordersData || !body.isConnected) return;
  for (const o of _ordersData.orders) {
    const c = orderConid(o);
    const q = c != null ? map[String(c)] : undefined;
    if (q) { o.quote = q; _quoteCache.set(c as number, q); }
  }
  paintOrders(body, title);
}

// Last fetched orders payload, kept so a sort click can repaint from memory.
let _ordersData: { orders: LiveOrder[]; pegs: PegState[] } | null = null;

// Client-side sort over the working list. Two sortable columns, each 3-state
// (desc -> asc -> off), so the operator can foreground either the orders drifting
// furthest from the market or the ones that have rested longest.
type OrdersSortKey = "lastdist" | "age";
let _ordersSort: { key: OrdersSortKey; dir: "asc" | "desc" } | null = null;

function cycleOrdersSort(key: OrdersSortKey): void {
  if (!_ordersSort || _ordersSort.key !== key) { _ordersSort = { key, dir: "desc" }; return; }
  if (_ordersSort.dir === "desc") { _ordersSort.dir = "asc"; return; }
  _ordersSort = null;  // third click restores IBKR's own order
}

// |limit - last| / last: how far the resting price has drifted from the last
// trade, as a fraction. null when either price is missing.
function orderLastDist(o: LiveOrder): number | null {
  const last = o.quote && typeof o.quote.last === "number" ? o.quote.last : null;
  const limit = typeof o.price === "number" ? o.price
    : (o.price != null && o.price !== "" ? Number(o.price) : NaN);
  if (last == null || last === 0 || !Number.isFinite(limit)) return null;
  return Math.abs(limit - last) / last;
}

function sortWorking(rows: LiveOrder[]): LiveOrder[] {
  if (!_ordersSort) return rows;
  const { key, dir } = _ordersSort;
  const value = key === "age"
    ? (o: LiveOrder) => (typeof o.lastExecutionTime_r === "number" ? Date.now() - o.lastExecutionTime_r : null)
    : (o: LiveOrder) => orderLastDist(o);
  const sign = dir === "asc" ? 1 : -1;
  return [...rows].sort((a, b) => {
    const va = value(a), vb = value(b);
    if (va == null && vb == null) return 0;
    if (va == null) return 1;   // rows without the metric always sink
    if (vb == null) return -1;
    return (va - vb) * sign;
  });
}

// Render the working list into an existing card body from _ordersData. Called
// after a fetch and on every sort click (no refetch).
function paintOrders(body: HTMLElement, title: HTMLElement): void {
  if (!_ordersData) return;
  const { orders: all, pegs } = _ordersData;
  const working = all.filter((o) => !orderTerminal(o));
  const done = all.filter((o) => orderTerminal(o));
  const pegById = new Map(pegs.map((p) => [String(p.order_id), p]));
  body.innerHTML = "";
  title.textContent = `Working orders (${working.length})`;
  if (!working.length && !done.length) {
    body.appendChild(el("div", "hint", "No working orders at IBKR right now."));
    return;
  }
  if (working.length) {
    body.appendChild(ordersLegend());
    // A single grid container (rows use display:contents) so columns line up
    // across every row instead of each row sizing its own auto columns.
    const rows = el("div", "trade-live-rows");
    const header = el("div", "trade-live-headrow");
    header.innerHTML = ordersHeaderCells();
    header.querySelectorAll<HTMLButtonElement>("[data-osort]").forEach((b) => {
      b.onclick = () => { cycleOrdersSort(b.dataset.osort as OrdersSortKey); paintOrders(body, title); };
    });
    rows.appendChild(header);
    sortWorking(working).forEach((o) =>
      rows.appendChild(liveOrderRow(o, pegById.get(String(o.orderId || o.order_id || "")))));
    body.appendChild(rows);
  } else {
    body.appendChild(el("div", "hint", "No working orders at IBKR right now."));
  }
  if (done.length) body.appendChild(doneSummary(done));
}

// Header cells for the shared grid (display:contents). The Market and Age cells
// are sort buttons; the rest are plain labels.
function ordersHeaderCells(): string {
  const arrow = (k: OrdersSortKey) =>
    _ordersSort && _ordersSort.key === k ? (_ordersSort.dir === "asc" ? " \u25b2" : " \u25bc") : "";
  const active = (k: OrdersSortKey) => (_ordersSort && _ordersSort.key === k ? " active" : "");
  return `<span class="trade-live-h">Symbol</span>` +
    `<span class="trade-live-h"></span>` +
    `<span class="trade-live-h num">Qty</span>` +
    `<span class="trade-live-h">Order</span>` +
    `<span class="trade-live-h num">Last</span>` +
    `<span class="trade-live-h">Bid \u00d7 Ask</span>` +
    `<span class="trade-live-h num">Spread</span>` +
    `<button class="trade-live-h sortable${active("lastdist")}" type="button" data-osort="lastdist" ` +
      `title="sort by how far the limit sits from the best price on its side">Edge${arrow("lastdist")}</button>` +
    `<span class="trade-live-h">Cost</span>` +
    `<button class="trade-live-h sortable${active("age")}" type="button" data-osort="age" ` +
      `title="sort by how long the order has rested">Age${arrow("age")}</button>` +
    `<span class="trade-live-h"></span>`;
}

// Terminal (done) statuses: IBKR keeps recently filled/cancelled orders in the
// orders feed, but they can't be pegged or cancelled.
const TERMINAL_STATUS = /^(filled|cancelled|canceled|expired|rejected|apicancelled)$/i;
function orderTerminal(o: LiveOrder): boolean {
  return TERMINAL_STATUS.test(String(o.status || o.order_status || "").trim());
}

// Inline icons (currentColor) so actions read as buttons, not a wall of text.
const ICON_PEG =
  `<svg viewBox="0 0 16 16" width="14" height="14" aria-hidden="true"><path d="M3 3h10" ` +
  `stroke="currentColor" stroke-width="1.6" stroke-linecap="round"/><path d="M8 13V6M5 9l3-3 3 3" ` +
  `fill="none" stroke="currentColor" stroke-width="1.6" stroke-linecap="round" stroke-linejoin="round"/></svg>`;
const ICON_STOP =
  `<svg viewBox="0 0 16 16" width="14" height="14" aria-hidden="true">` +
  `<rect x="4" y="4" width="8" height="8" rx="1.5" fill="currentColor"/></svg>`;
const ICON_CANCEL =
  `<svg viewBox="0 0 16 16" width="14" height="14" aria-hidden="true"><path d="M4 4l8 8M12 4l-8 8" ` +
  `stroke="currentColor" stroke-width="1.7" stroke-linecap="round"/></svg>`;

function iconBtn(svg: string, label: string, cls: string): HTMLButtonElement {
  const b = el("button", `trade-ico ${cls}`) as HTMLButtonElement;
  b.type = "button";
  b.innerHTML = svg;
  b.setAttribute("aria-label", label);
  b.title = label;
  return b;
}

// One legend for the whole list, so the row icons don't need repeated captions.
function ordersLegend(): HTMLElement {
  const lg = el("div", "trade-live-legend hint");
  lg.innerHTML =
    `<span><b>Edge</b> = how far your limit sits from the best price on its side ` +
      `(green = at the touch, red = far); <b>Cost</b> = a sell's gain vs its purchase price</span>` +
    `<span class="trade-live-keys">${ICON_PEG} keep at top &nbsp; ${ICON_CANCEL} cancel</span>`;
  return lg;
}

function doneSummary(done: LiveOrder[]): HTMLElement {
  const names = done.map((o) => {
    const s = String(o.ticker || o.symbol || "").trim();
    return s ? tickerLink(s) : esc(String(o.conid ?? "?"));
  });
  const wrap = el("div", "trade-live-done hint");
  const label = done.length === 1 ? "1 recently filled/cancelled" : `${done.length} recently filled/cancelled`;
  wrap.innerHTML = `${esc(label)}: ${names.join(", ")}`;
  return wrap;
}

function startPegPoll() {
  if (_pegPollTimer) return;
  _pegPollTimer = setInterval(() => void renderLiveOrders(), PEG_POLL_MS);
}

function stopPegPoll() {
  if (_pegPollTimer) { clearInterval(_pegPollTimer); _pegPollTimer = null; }
}

function pegAccount(): string | undefined {
  return (_status && _status.default_account) || (_preview && _preview.account) || undefined;
}

async function startPeg(order_id: string, btn: HTMLButtonElement) {
  btn.disabled = true;
  try {
    // No bound sent: the peg defaults to this order's own limit as the floor and
    // never crosses the spread, which is the safe default we expose in the UI.
    await api("/api/trade/peg", "POST", { order_id, account: pegAccount() });
    renderLiveOrders();  // reflect the new peg immediately; poll takes over after
  } catch (e) {
    btn.disabled = false;
    btn.title = (e as Error).message;
  }
}

async function stopPeg(order_id: string, btn: HTMLButtonElement) {
  btn.disabled = true;
  try {
    await api("/api/trade/peg/stop", "POST", { order_id });
    renderLiveOrders();
  } catch (e) {
    btn.disabled = false;
    btn.title = (e as Error).message;
  }
}

// Format a price in the instrument's own currency: 2 decimals, up to 4 for the
// sub-dollar / fractional-tick names (e.g. 8.400, 1.54).
function px(n: number): string {
  return n.toLocaleString("en-US", { minimumFractionDigits: 2, maximumFractionDigits: 4 });
}

// Cost basis needs less precision than a live quote (it's not tick-sensitive):
// always two decimals, regardless of the price magnitude.
function pxCost(n: number): string {
  return n.toLocaleString("en-US", { minimumFractionDigits: 2, maximumFractionDigits: 2 });
}

// A ticker rendered as a deep-dive link. The global `a.tlink` click handler in
// shell intercepts it and routes to the deep dive (live-pulling on a cache
// miss), so this only needs the class + data-ticker; the href keeps it a real,
// middle-clickable link. Empty for a blank symbol (e.g. a conid-only fallback).
function tickerLink(sym: unknown): string {
  const s = String(sym ?? "").trim();
  if (!s) return "";
  const e = esc(s);
  return `<a class="tlink" data-ticker="${e}" href="?view=deepdive&ticker=${encodeURIComponent(s)}" title="Open ${e} deep-dive">${e}</a>`;
}

// A dim placeholder for a market column with nothing to show, so empty cells
// read as intentionally blank rather than a broken layout.
const EMPTY_CELL = `<span class="trade-live-dim">\u00b7</span>`;

// The five market columns for one order — bid × ask, spread, last, edge, cost —
// emitted as SEPARATE grid cells (not one wrapping blob) so figures line up down
// each column and stay scannable across rows. This is what makes "keep at top" a
// judgement call rather than a blind toggle.
function marketCells(o: LiveOrder): string {
  const q = o.quote;
  const bid = q && typeof q.bid === "number" ? q.bid : null;
  const ask = q && typeof q.ask === "number" ? q.ask : null;
  const last = q && typeof q.last === "number" ? q.last : null;
  const cold = bid == null && ask == null && last == null;

  let quoteC: string;
  if (cold) {
    quoteC = _quotesPending
      ? `<span class="trade-live-quoteload"><span class="spinner"></span> quote\u2026</span>`
      : `<span class="muted">no quote</span>`;
  } else if (bid != null && ask != null) {
    quoteC = `${px(bid)} <span class="muted">\u00d7</span> ${px(ask)}`;
  } else {
    quoteC = EMPTY_CELL;
  }

  let spreadC = EMPTY_CELL;
  if (bid != null && ask != null) {
    const spread = ask - bid;
    const mid = (ask + bid) / 2;
    const spreadPct = mid > 0 ? (spread / mid) * 100 : 0;
    const wide = spreadPct >= 0.5;  // a spread the peg can meaningfully work inside
    spreadC = `<span class="trade-live-spread${wide ? " wide" : ""}" ` +
      `title="bid-ask spread \u0394${esc(px(spread))}">${spreadPct.toFixed(2)}%</span>`;
  }

  const lastC = last != null ? px(last) : EMPTY_CELL;

  // Where the resting limit sits relative to the price it must beat to fill,
  // drawn as a distance meter (short/green = near the touch, long/red = far).
  let edgeC = EMPTY_CELL;
  const limit = typeof o.price === "number" ? o.price : (o.price != null && o.price !== "" ? Number(o.price) : NaN);
  const side = String(o.side || "").toUpperCase();
  if (Number.isFinite(limit) && (bid != null || ask != null)) {
    const ref = side === "BUY" ? ask : bid;  // buyers chase the ask; sellers the bid
    if (ref != null && ref > 0) {
      const gapPct = side === "BUY" ? ((ref - limit) / ref) * 100 : ((limit - ref) / ref) * 100;
      const word = side === "BUY"
        ? (gapPct >= 0 ? "below ask" : "above ask")
        : (gapPct >= 0 ? "above bid" : "below bid");
      const label = Math.abs(gapPct) < 0.05
        ? `at the ${side === "BUY" ? "ask" : "bid"}`
        : `${Math.abs(gapPct).toFixed(1)}% ${word}`;
      edgeC = edgeMeter(gapPct, label);
    }
  }

  // Last leads (the price you judge your limit against, so it's the headline);
  // bid × ask + spread follow as supporting microstructure, then edge and cost.
  return `<span class="trade-live-last num">${lastC}</span>` +
    `<span class="trade-live-quote">${quoteC}</span>` +
    `<span class="num">${spreadC}</span>` +
    `<span class="trade-live-edge-c">${edgeC}</span>` +
    `<span class="trade-live-cost-c">${costCell(o)}</span>`;
}

// A SELL's average purchase price and the limit's gain/loss against it — the
// "am I selling at a profit?" read — for its own column. Dim for buys or a name
// with no cost basis. With a limit, shows the % the fill would lock in.
function costCell(o: LiveOrder): string {
  if (String(o.side || "").toUpperCase() !== "SELL") return EMPTY_CELL;
  const cost = typeof o.avg_cost === "number" ? o.avg_cost : null;
  if (cost == null || cost <= 0) return EMPTY_CELL;
  const limit = typeof o.price === "number" ? o.price : (o.price != null && o.price !== "" ? Number(o.price) : NaN);
  if (Number.isFinite(limit)) {
    const g = ((limit - cost) / cost) * 100;
    const cls = g >= 0 ? "gain" : "loss";
    const sign = g >= 0 ? "+" : "\u2212";
    return `<span class="trade-live-cost" title="avg purchase price ${esc(pxCost(cost))}; the limit locks ${sign}${Math.abs(g).toFixed(1)}%">` +
      `${pxCost(cost)} <span class="${cls}">${sign}${Math.abs(g).toFixed(1)}%</span></span>`;
  }
  return `<span class="trade-live-cost" title="average purchase price">${pxCost(cost)}</span>`;
}

// A bar for the limit-vs-touch gap. LOG-scaled (cap ~120%) rather than linear:
// resting targets span <1% to 100%+, so a linear 5% cap saturated almost every
// bar to full-width red. Log spreads that range out, and the fill's colour runs
// continuously green (at the touch) -> amber -> red (far) so distance reads at a
// glance. The exact figure sits beside it; direction is in the tooltip.
function edgeMeter(gapPct: number, label: string): string {
  const mag = Math.abs(gapPct);
  const CAP = 120;
  const frac = Math.min(1, Math.log1p(mag) / Math.log1p(CAP));
  const fill = Math.max(6, Math.round(frac * 100));   // floor so a ~0% gap still shows
  const hue = Math.round((1 - frac) * 130);           // 130=green (near) .. 0=red (far)
  return `<span class="trade-live-edge" title="limit ${esc(label)}">` +
    `<span class="edge-meter"><span class="edge-fill" ` +
      `style="width:${fill}%;background:hsl(${hue}, 68%, 52%)"></span></span>` +
    `<span class="edge-num">${mag.toFixed(1)}%</span></span>`;
}

// Compact "how long it's rested" from an epoch-ms stamp: 5m / 3h / 2d / 4w.
function agoShort(sinceMs: number): string {
  const secs = Math.max(0, (Date.now() - sinceMs) / 1000);
  const mins = secs / 60, hrs = mins / 60, days = hrs / 24;
  if (secs < 90) return "just now";
  if (mins < 90) return `${Math.round(mins)}m`;
  if (hrs < 36) return `${Math.round(hrs)}h`;
  if (days < 14) return `${Math.round(days)}d`;
  if (days < 60) return `${Math.round(days / 7)}w`;
  return `${Math.round(days / 30)}mo`;
}

// IBKR-style status-as-colour: a dot carries the state (exact text on hover), so
// the uninteresting "PreSubmitted" string doesn't need a whole column of prose.
// green = working at the exchange, blue = accepted/held (e.g. resting GTC),
// amber = pending, grey = inactive/unknown.
function statusDot(o: LiveOrder): string {
  const st = String(o.status || o.order_status || "").trim();
  const k = st.toLowerCase();
  let tone = "unknown";
  if (k === "submitted") tone = "live";
  else if (k === "presubmitted") tone = "held";
  else if (k.startsWith("pending")) tone = "pending";
  else if (k === "inactive") tone = "inactive";
  return `<span class="trade-live-dot tone-${tone}" title="${esc(st || "unknown status")}"></span>`;
}

// Status column: the colour dot plus the order's age; while pegging, the live
// reprice/resting message is more useful than the age, so show that instead.
function statusCell(o: LiveOrder, peg?: PegState): string {
  const dot = statusDot(o);
  if (peg) {
    const msg = esc(peg.message || peg.state || "");
    return `${dot}${msg ? `<span class="trade-live-pegmsg">${msg}</span>` : ""}`;
  }
  const ms = typeof o.lastExecutionTime_r === "number" ? o.lastExecutionTime_r : null;
  if (!ms) return dot;
  return `${dot}<span class="trade-live-age" title="last update ${esc(new Date(ms).toLocaleString())}">` +
    `${esc(agoShort(ms))}</span>`;
}

function liveOrderRow(o: LiveOrder, peg?: PegState): HTMLElement {
  const row = el("div", "trade-live-row");
  const oid = String(o.orderId || o.order_id || "");
  const side = String(o.side || "").toUpperCase();
  const qty = o.remainingQuantity ?? o.totalSize ?? o.quantity ?? "";
  const type = o.orderType || o.order_type || "";
  const priceStr = o.price != null && o.price !== "" ? esc(o.price) : "";
  const tif = o.tif || o.timeInForce || "";
  // Compact order line: price + tif. The "Limit" word is dropped (the column is
  // headed "Order" and these are limits); a non-limit type keeps its label. The
  // redundant "Sell 250 GSK" prose is gone — side/qty/symbol have their columns.
  const isLimitType = /lmt|limit/i.test(String(type));
  const typePrefix = isLimitType || !type ? "" : `${esc(type)} `;
  const detail = `${typePrefix}${priceStr}${tif ? ` <span class="muted">· ${esc(tif)}</span>` : ""}`;
  const pegBadge = peg
    ? `<span class="trade-peg-badge" title="${esc(peg.message || "")}">pegging${peg.reprices ? ` ·${peg.reprices}` : ""}</span>`
    : "";
  if (peg) row.classList.add("is-pegging");
  const symRaw = String(o.ticker || o.symbol || "").trim();
  const symCell = symRaw ? tickerLink(symRaw) : esc(String(o.conid ?? "?"));
  row.innerHTML =
    `<span class="trade-live-sym">${symCell}${pegBadge}</span>` +
    `<span>${side ? sideTag(side) : ""}</span>` +
    `<span class="num">${esc(qty)}</span>` +
    `<span class="trade-live-detail">${detail}</span>` +
    marketCells(o) +
    `<span class="trade-live-status">${statusCell(o, peg)}</span>`;

  const actions = el("span", "trade-live-actions");
  // Only a resting limit order can be pegged (needs a price to improve on).
  const isLimit = /lmt|limit/i.test(String(type)) && o.price != null && o.price !== "";
  if (oid && isLimit) {
    if (peg) {
      const stop = iconBtn(ICON_STOP, "Stop keeping at top", "stop");
      stop.onclick = () => void stopPeg(oid, stop);
      actions.appendChild(stop);
    } else {
      const keep = iconBtn(ICON_PEG, "Keep at top", "peg");
      keep.title = "Keep at top — hold this order one tick better than the best price on its side (never crosses the spread)";
      keep.onclick = () => void startPeg(oid, keep);
      actions.appendChild(keep);
    }
  }

  if (oid) {
    const cancel = iconBtn(ICON_CANCEL, "Cancel order", "cancel");
    cancel.onclick = async () => {
      cancel.disabled = true;
      try {
        await api("/api/trade/cancel", "POST", { order_id: oid, account: pegAccount() });
        renderLiveOrders();
      } catch (e) { cancel.disabled = false; cancel.title = (e as Error).message; }
    };
    actions.appendChild(cancel);
  }
  row.appendChild(actions);
  return row;
}

export { loadTrade };
