import { $, api, el, esc, fmtCZK, isStaleToken, nextToken, sensitive, simpleTable, state } from "./core";
import { pollDeepJob } from "./jobs";
import { openJournalWith } from "./journal";
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
}

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
  // Rehydrate the basket the planner persisted server-side so it survives a
  // reload or navigating away and back — it used to live only in browser memory.
  try {
    const res = await api<{ trades?: Array<{ symbol: string; delta_czk: number }> }>("/api/trade/basket");
    if (isStaleToken("trade", token)) return;
    if (Array.isArray(res.trades)) state.stagedBasket = res.trades;
  } catch (_e) {
    if (isStaleToken("trade", token)) return;  // else: keep whatever's in memory
  }
  await renderConnection(token);
}

async function renderConnection(token?: number) {
  const banner = $("#trade-banner");
  const status = $("#trade-status");
  if (status) status.textContent = "";
  try {
    _status = await api<TradeStatus>("/api/trade/status");
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
    _status = await api<TradeStatus>("/api/trade/reconnect", "POST");
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
    t = await api<TradeTickle>("/api/trade/tickle");
  } catch (_e) {
    return;  // transient; the next tick tries again
  }
  // If the session state flipped since the last paint (a silent drop, or a
  // recovery), re-read + repaint so the banner reflects reality without a click.
  if (_status && (t.authenticated !== _status.authenticated || t.competing !== _status.competing)) {
    void renderConnection();
  }
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

  card.appendChild(simpleTable({
    className: "trade-basket-table",
    head: `<tr><th>Symbol</th><th class="num">Planned (CZK)</th></tr>`,
    rows: basket,
    cells: (t) => `<td>${esc(t.symbol)}</td>` +
      `<td class="num">${sensitive(`${t.delta_czk >= 0 ? "+" : "\u2212"}${fmtCZK(Math.abs(t.delta_czk))}`, "planned trade size")}</td>`,
  }));

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
  _preview = await api<TradePreview>("/api/trade/preview", "POST", {
    trades: state.stagedBasket || [],
    account: _status && _status.default_account,
  });
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

  if (!p.orders || !p.orders.length) {
    card.appendChild(el("div", "hint", "No orders could be sized from this basket. See the warnings above (no contract / no price / rounds to zero shares)."));
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
      `<td>${esc(o.symbol || o.conid)}</td>` +
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
      cell.innerHTML = `\u26a0 <strong>${esc(sym)}</strong> already has a working order at IBKR \u2014 ` +
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
    data = await api<{ orders?: LiveOrder[]; pegs?: PegState[] }>("/api/trade/orders");
  } catch (e) {
    if (token != null && isStaleToken("trade", token)) return;
    stopPegPoll();
    body.innerHTML = "";
    body.appendChild(el("div", "trade-bnr warn",
      `Could not read working orders: ${esc((e as Error).message)}`));
    return;
  }
  if (token != null && isStaleToken("trade", token)) return;

  const orders = (data && data.orders) || [];
  const pegs = (data && data.pegs) || [];
  // Refresh the cross-check set for the preview: only names with a live working
  // order count as a collision risk.
  _workingSymbols = new Set(
    orders.map((o) => String(o.ticker || o.symbol || "").trim().toUpperCase()).filter(Boolean),
  );
  const pegById = new Map(pegs.map((p) => [String(p.order_id), p]));
  body.innerHTML = "";
  title.textContent = `Working orders (${orders.length})`;
  if (!orders.length) {
    body.appendChild(el("div", "hint", "No working orders at IBKR right now."));
    stopPegPoll();
    return;
  }
  orders.forEach((o) => body.appendChild(liveOrderRow(o, pegById.get(String(o.orderId || o.order_id || "")))));
  // Keep the card live while a peg is running so its reprice count updates.
  if (pegs.length) startPegPoll();
  else stopPegPoll();
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

async function startPeg(order_id: string, worstInput: HTMLInputElement, btn: HTMLButtonElement) {
  btn.disabled = true;
  const worst = worstInput.value.trim();
  try {
    await api("/api/trade/peg", "POST", {
      order_id,
      account: pegAccount(),
      ...(worst ? { worst_price: Number(worst) } : {}),
    });
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

function liveOrderRow(o: LiveOrder, peg?: PegState): HTMLElement {
  const row = el("div", "trade-live-row");
  const oid = String(o.orderId || o.order_id || "");
  const side = String(o.side || "").toUpperCase();
  const qty = o.remainingQuantity ?? o.totalSize ?? o.quantity ?? "";
  const type = o.orderType || o.order_type || "";
  const priceBit = o.price != null && o.price !== "" ? ` @ ${esc(o.price)}` : "";
  const tif = o.tif || o.timeInForce || "";
  // Prefer IBKR's own one-liner when present; it already reads well.
  const detail = o.orderDesc
    ? esc(o.orderDesc)
    : `${esc(type)}${priceBit}${tif ? ` / ${esc(tif)}` : ""}`;
  const pegBadge = peg
    ? `<span class="trade-peg-badge" title="${esc(peg.message || "")}">pegging${peg.reprices ? ` ·${peg.reprices}` : ""}</span>`
    : "";
  row.innerHTML =
    `<span class="trade-live-sym">${esc(o.ticker || o.symbol || o.conid || "?")}${pegBadge}</span>` +
    `<span>${side ? sideTag(side) : ""}</span>` +
    `<span class="num">${esc(qty)}</span>` +
    `<span class="trade-live-detail">${detail}</span>` +
    `<span class="trade-live-status">${esc(peg ? (peg.message || peg.state || "") : (o.status || o.order_status || ""))}</span>`;

  const actions = el("span", "trade-live-actions");
  // Only a resting limit order can be pegged (needs a price to improve on).
  const isLimit = /lmt|limit/i.test(String(type)) && o.price != null && o.price !== "";
  if (oid && isLimit) {
    if (peg) {
      const stop = el("button", "ghost", "Stop peg");
      stop.type = "button";
      stop.onclick = () => void stopPeg(oid, stop);
      actions.appendChild(stop);
    } else {
      const worst = el("input", "trade-peg-worst") as HTMLInputElement;
      worst.type = "number";
      worst.step = "any";
      worst.placeholder = side === "SELL" ? "min px" : "max px";
      worst.title = "Worst price the peg may move to (optional; defaults to this order's limit)";
      const keep = el("button", "ghost", "Keep at top");
      keep.type = "button";
      keep.title = "Keep this order one tick better than the best price on its side (never crosses the spread)";
      keep.onclick = () => void startPeg(oid, worst, keep);
      actions.appendChild(worst);
      actions.appendChild(keep);
    }
  }

  const cancel = el("button", "ghost", "Cancel");
  cancel.type = "button";
  cancel.onclick = async () => {
    cancel.disabled = true;
    try {
      await api("/api/trade/cancel", "POST", { order_id: oid, account: pegAccount() });
      renderLiveOrders();
    } catch (e) { cancel.disabled = false; cancel.title = (e as Error).message; }
  };
  if (oid) actions.appendChild(cancel);
  row.appendChild(actions);
  return row;
}

export { loadTrade };
