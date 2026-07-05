import { $, api, el, esc, fmtCZK, isStaleToken, nextToken, sensitive, simpleTable, state } from "./core";
import { pollDeepJob } from "./errors";
import { openJournalWith } from "./journal";

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
  warnings?: string[];
  preview_ttl_s?: number;
  orders?: TradeOrder[];
  // The raw IBKR margin/commission blob; shape varies per account/order type.
  ibkr_preview?: any;
  trades?: unknown;
  token?: string;
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

let _status: TradeStatus | null = null;   // last /api/trade/status
let _preview: TradePreview | null = null;  // last /api/trade/preview (carries the place token)
// The basket as it was placed — snapshotted before the staged store is cleared,
// so the "Log to journal" next step can still describe the executed trades.
let _placedBasket: Array<{ symbol: string; delta_czk: number }> = [];
// Mirrors the server's preview TTL: when it lapses, the Place button locks and
// asks for a re-preview (the server enforces the same window on its side).
let _previewExpiry: ReturnType<typeof setTimeout> | null = null;

const sideTag = (side: string) =>
  `<span class="trade-side ${side === "BUY" ? "buy" : "sell"}">${esc(side)}</span>`;

function gatewayOrigin(base: string | null | undefined) {
  return String(base || "").replace(/\/v1\/api\/?$/, "") || "https://localhost:5000";
}

async function loadTrade() {
  const token = nextToken("trade");
  const wrap = $("#trade-result");
  if (wrap) wrap.innerHTML = "";
  const refresh = $("#trade-refresh");
  if (refresh) refresh.onclick = () => loadTrade();
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
    if (banner) banner.innerHTML = `<div class="trade-bnr bad">Could not read trade status: ${esc(e.message)}</div>`;
    return;
  }
  if (token != null && isStaleToken("trade", token)) return;
  const s = _status;
  const bits = [];

  if (!s.trading_enabled) {
    bits.push(`<div class="trade-bnr bad"><strong>Trading is disabled.</strong> Set <code>IBKR_TRADING_ENABLED=1</code> in <code>tools/secrets.env</code> (and start the Client Portal Gateway), then refresh. Nothing here can place an order until you do.</div>`);
  }
  if (!s.authenticated) {
    const origin = gatewayOrigin(s.gateway_base);
    bits.push(`<div class="trade-bnr warn"><strong>Gateway not connected.</strong> Start the IBKR Client Portal Gateway, log in (with 2FA) at <a href="${esc(origin)}" target="_blank" rel="noopener">${esc(origin)}</a>, then press <em>Refresh connection</em>.</div>`);
  } else {
    const acct = s.default_account || (s.accounts[0] && s.accounts[0].id) || "?";
    const kind = s.accounts.find((a) => a.id === acct)?.kind || "?";
    const cls = kind === "live" ? "live" : "paper";
    bits.push(`<div class="trade-bnr ${cls}"><strong>${kind === "live" ? "LIVE" : "Paper"} account ${esc(acct)}</strong>` +
      (kind === "live"
        ? (s.live_allowed ? " — live orders are unlocked. Real money." : " — live placement is <strong>locked</strong>. Validate on paper, then set <code>IBKR_ALLOW_LIVE=1</code>.")
        : " — safe simulated account.") +
      (s.competing ? " <em>(another session is competing for this login)</em>" : "") +
      `</div>`);
  }
  if (banner) banner.innerHTML = bits.join("");

  renderBasket();
  void renderLiveOrders(token);
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

async function doPreview(btn: HTMLButtonElement) {
  const status = $("#trade-preview-status");
  if (status) { status.classList.remove("err"); status.innerHTML = `<span class="spinner"></span> previewing\u2026`; }
  if (btn) btn.disabled = true;
  try {
    _preview = await api<TradePreview>("/api/trade/preview", "POST", {
      trades: state.stagedBasket || [],
      account: _status && _status.default_account,
    });
    if (status) status.textContent = "";
    renderPreview();
  } catch (e) {
    if (status) { status.classList.add("err"); status.textContent = "preview failed: " + e.message; }
  } finally {
    if (btn) btn.disabled = false;
  }
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
    `Preview \u2014 ${isLive ? "LIVE" : "paper"} account ${esc(p.account)}`));

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

  // Per-order confirmation. Place stays disabled until every box is ticked.
  const confirmState = p.orders.map(() => false);
  const placeBtn = el("button", "danger", "");
  placeBtn.type = "button";

  const refreshPlaceBtn = () => {
    const all = confirmState.every(Boolean);
    placeBtn.disabled = !all || liveBlocked;
    placeBtn.textContent = liveBlocked
      ? "Live placement locked"
      : `Place ${p.orders.length} order${p.orders.length === 1 ? "" : "s"} on ${isLive ? "LIVE" : "paper"}`;
  };

  const table = el("table", "trade-orders-table");
  table.innerHTML = `<thead><tr><th>Confirm</th><th>Symbol</th><th>Side</th><th class="num">Qty</th><th>Type</th><th>conid</th></tr></thead>`;
  const tbody = el("tbody");
  p.orders.forEach((o, i) => {
    const tr = el("tr");
    const cb = el("input");
    cb.type = "checkbox";
    cb.addEventListener("change", () => { confirmState[i] = cb.checked; refreshPlaceBtn(); });
    const td = el("td");
    td.appendChild(cb);
    tr.appendChild(td);
    tr.insertAdjacentHTML("beforeend",
      `<td>${esc(o.symbol || o.conid)}</td>` +
      `<td>${sideTag(o.side)}</td>` +
      `<td class="num">${esc(o.quantity)}</td>` +
      `<td>${o.orderType === "LMT" && o.price != null ? `<span class="trade-lmt">LMT @ ${esc(o.price)}</span>` : esc(o.orderType)} / ${esc(o.tif)}</td>` +
      `<td class="muted">${esc(o.conid)}</td>`);
    tbody.appendChild(tr);
  });
  table.appendChild(tbody);
  card.appendChild(table);

  if (liveBlocked) {
    card.appendChild(el("div", "trade-warn",
      "Live placement is locked. Set IBKR_ALLOW_LIVE=1 only after you have validated this flow on the paper account."));
  }

  const actions = el("div", "trade-actions");
  const allBtn = el("button", "ghost", "Confirm all");
  allBtn.type = "button";
  allBtn.onclick = () => {
    table.querySelectorAll<HTMLInputElement>('input[type="checkbox"]').forEach((cb, i) => { cb.checked = true; confirmState[i] = true; });
    refreshPlaceBtn();
  };
  placeBtn.onclick = () => doPlace(placeBtn);
  actions.appendChild(allBtn);
  actions.appendChild(placeBtn);
  card.appendChild(actions);
  card.appendChild(el("div", "status", "")).id = "trade-place-status";

  refreshPlaceBtn();

  // Server-side the token expires after preview_ttl_s; mirror it here so the
  // button explains the refusal instead of surfacing a rejected place call.
  if (_previewExpiry) clearTimeout(_previewExpiry);
  if (p.preview_ttl_s) {
    _previewExpiry = setTimeout(() => {
      placeBtn.disabled = true;
      placeBtn.textContent = "Preview expired — re-preview";
      placeBtn.title = "Prices and sizes are stale; run Preview again to re-arm placement";
    }, p.preview_ttl_s * 1000);
  }
  wrap.appendChild(card);
}

async function doPlace(btn: HTMLButtonElement) {
  if (!_preview) return;
  const isLive = !_preview.is_paper;
  const msg = `Place ${_preview.orders.length} order(s) on the ${isLive ? "LIVE" : "paper"} account ${_preview.account}?` +
    (isLive ? "\n\nThis uses REAL money and cannot be undone here." : "");
  if (!window.confirm(msg)) return;

  const status = $("#trade-place-status");
  if (status) { status.classList.remove("err"); status.innerHTML = `<span class="spinner"></span> placing\u2026`; }
  if (btn) btn.disabled = true;
  try {
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
    if (status) { status.classList.add("err"); status.textContent = "placement failed: " + e.message; }
    if (btn) btn.disabled = false;
  }
}

interface PlaceResult {
  placed?: Array<Record<string, any>>;
  kind?: string;
  account?: string;
  staged_basket_cleared?: boolean;
}

// Pure HTML for the placement-outcome card: an acknowledgement banner, the
// loop-closing next steps (resync holdings, log the decision), and the raw
// IBKR response tucked into a collapsed drawer instead of a wall of JSON.
// Exported for tests.
export function placeResultHtml(res: PlaceResult): string {
  const placed = res.placed || [];
  const ok = placed.filter((o) => o && (o.order_id || o.orderId || o.order_status)).length;
  const banner = `<div class="trade-bnr ${ok ? "paper" : "warn"}">` +
    `${ok} order(s) acknowledged by IBKR on ${esc(res.kind)} account ${esc(res.account)}.</div>`;
  const cleared = res.staged_basket_cleared
    ? `<span class="muted">The staged basket was cleared so it can't be placed twice.</span>` : "";
  const next = `<div class="trade-next">
    <div class="subhead">Close the loop</div>
    <ol class="trade-next-list">
      <li><strong>Resync holdings</strong> so the planner works from your new positions, not the pre-trade snapshot.
        <button class="ghost" type="button" data-trade-next="resync">Resync from IBKR</button></li>
      <li><strong>Log the decision</strong> while the reasoning is fresh — outcomes get scored later.
        <button class="ghost" type="button" data-trade-next="journal">Log to journal</button></li>
    </ol>
    ${cleared}
  </div>`;
  const raw = `<details class="trade-raw-det"><summary>Raw IBKR response</summary>` +
    `<pre class="trade-raw">${esc(JSON.stringify(placed, null, 2))}</pre></details>`;
  return banner + next + raw;
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
  let data: { orders?: LiveOrder[] } | undefined;
  try {
    data = await api<{ orders?: LiveOrder[] }>("/api/trade/orders");
  } catch (e) {
    if (token != null && isStaleToken("trade", token)) return;
    body.innerHTML = "";
    body.appendChild(el("div", "trade-bnr warn",
      `Could not read working orders: ${esc((e as Error).message)}`));
    return;
  }
  if (token != null && isStaleToken("trade", token)) return;

  const orders = (data && data.orders) || [];
  body.innerHTML = "";
  title.textContent = `Working orders (${orders.length})`;
  if (!orders.length) {
    body.appendChild(el("div", "hint", "No working orders at IBKR right now."));
    return;
  }
  orders.forEach((o) => body.appendChild(liveOrderRow(o)));
}

function liveOrderRow(o: LiveOrder): HTMLElement {
  const row = el("div", "trade-live-row");
  const oid = o.orderId || o.order_id || "";
  const side = String(o.side || "").toUpperCase();
  const qty = o.remainingQuantity ?? o.totalSize ?? o.quantity ?? "";
  const type = o.orderType || o.order_type || "";
  const priceBit = o.price != null && o.price !== "" ? ` @ ${esc(o.price)}` : "";
  const tif = o.tif || o.timeInForce || "";
  // Prefer IBKR's own one-liner when present; it already reads well.
  const detail = o.orderDesc
    ? esc(o.orderDesc)
    : `${esc(type)}${priceBit}${tif ? ` / ${esc(tif)}` : ""}`;
  row.innerHTML =
    `<span class="trade-live-sym">${esc(o.ticker || o.symbol || o.conid || "?")}</span>` +
    `<span>${side ? sideTag(side) : ""}</span>` +
    `<span class="num">${esc(qty)}</span>` +
    `<span class="trade-live-detail">${detail}</span>` +
    `<span class="trade-live-status">${esc(o.status || o.order_status || "")}</span>`;
  const cancel = el("button", "ghost", "Cancel");
  cancel.type = "button";
  cancel.onclick = async () => {
    cancel.disabled = true;
    try {
      await api("/api/trade/cancel", "POST", {
        order_id: oid,
        account: (_status && _status.default_account) || (_preview && _preview.account),
      });
      renderLiveOrders();
    } catch (e) { cancel.disabled = false; cancel.title = (e as Error).message; }
  };
  if (oid) row.appendChild(cancel);
  else row.appendChild(el("span"));  // keep the grid columns aligned
  return row;
}

export { loadTrade };
