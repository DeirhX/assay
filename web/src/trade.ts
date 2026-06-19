import { $, api, el, esc, fmtCZK, sensitive, simpleTable, state } from "./core";

// ---- trade desk -----------------------------------------------------------
// The ONLY surface in Assay that can place real orders. It reuses the basket
// staged in the Rebalance planner (state.stagedBasket), previews it through
// IBKR's Client Portal Web API for margin/commission, and places it only after
// per-order human confirmation. Everything is gated server-side too; this UI
// just refuses early and explains why.

let _status = null;       // last /api/trade/status
let _preview = null;      // last /api/trade/preview (carries the place token)

const sideTag = (side) =>
  `<span class="trade-side ${side === "BUY" ? "buy" : "sell"}">${esc(side)}</span>`;

function gatewayOrigin(base) {
  return String(base || "").replace(/\/v1\/api\/?$/, "") || "https://localhost:5000";
}

async function loadTrade() {
  const wrap = $("#trade-result");
  if (wrap) wrap.innerHTML = "";
  const refresh = $("#trade-refresh");
  if (refresh) refresh.onclick = () => loadTrade();
  await renderConnection();
}

async function renderConnection() {
  const banner = $("#trade-banner");
  const status = $("#trade-status");
  if (status) status.textContent = "";
  try {
    _status = await api("/api/trade/status");
  } catch (e) {
    if (banner) banner.innerHTML = `<div class="trade-bnr bad">Could not read trade status: ${esc(e.message)}</div>`;
    return;
  }
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
    const kind = (s.accounts.find((a) => a.id === acct) || {}).kind || "?";
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

async function doPreview(btn) {
  const status = $("#trade-preview-status");
  if (status) { status.classList.remove("err"); status.innerHTML = `<span class="spinner"></span> previewing\u2026`; }
  if (btn) btn.disabled = true;
  try {
    _preview = await api("/api/trade/preview", "POST", {
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
    const add = (label, val) => { if (val) grid.appendChild(el("div", "trade-impact-cell", `<span class="muted">${esc(label)}</span> ${esc(typeof val === "object" ? (val.amount || JSON.stringify(val)) : val)}`)); };
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
  wrap.appendChild(card);
}

async function doPlace(btn) {
  if (!_preview) return;
  const isLive = !_preview.is_paper;
  const msg = `Place ${_preview.orders.length} order(s) on the ${isLive ? "LIVE" : "paper"} account ${_preview.account}?` +
    (isLive ? "\n\nThis uses REAL money and cannot be undone here." : "");
  if (!window.confirm(msg)) return;

  const status = $("#trade-place-status");
  if (status) { status.classList.remove("err"); status.innerHTML = `<span class="spinner"></span> placing\u2026`; }
  if (btn) btn.disabled = true;
  try {
    const res = await api("/api/trade/place", "POST", {
      trades: _preview.trades,
      account: _preview.account,
      token: _preview.token,
      confirm: true,
    });
    if (status) status.textContent = "";
    renderPlaceResult(res);
    loadLiveOrders();
  } catch (e) {
    if (status) { status.classList.add("err"); status.textContent = "placement failed: " + e.message; }
    if (btn) btn.disabled = false;
  }
}

function renderPlaceResult(res) {
  const wrap = $("#trade-result");
  if (!wrap) return;
  wrap.querySelectorAll(".trade-result-card").forEach((n) => n.remove());
  const card = el("div", "card trade-result-card");
  card.appendChild(el("div", "trade-card-title", "Placement result"));
  const placed = res.placed || [];
  const ok = placed.filter((o) => o && (o.order_id || o.orderId || o.order_status));
  card.appendChild(el("div", ok.length ? "trade-bnr paper" : "trade-bnr warn",
    `${ok.length} order(s) acknowledged by IBKR on ${esc(res.kind)} account ${esc(res.account)}.`));
  const pre = el("pre", "trade-raw");
  pre.textContent = JSON.stringify(placed, null, 2);
  card.appendChild(pre);
  wrap.appendChild(card);
}

async function loadLiveOrders() {
  const wrap = $("#trade-result");
  if (!wrap) return;
  wrap.querySelectorAll(".trade-live-card").forEach((n) => n.remove());
  let data;
  try { data = await api("/api/trade/orders"); }
  catch (_e) { return; }
  const orders = (data && data.orders) || [];
  if (!orders.length) return;
  const card = el("div", "card trade-live-card");
  card.appendChild(el("div", "trade-card-title", `Live orders (${orders.length})`));
  orders.forEach((o) => {
    const row = el("div", "trade-live-row");
    const oid = o.orderId || o.order_id || "";
    row.innerHTML = `<span>${esc(o.ticker || o.symbol || o.conid || "?")}</span>` +
      `<span>${esc(o.side || "")}</span>` +
      `<span>${esc(o.totalSize || o.quantity || "")}</span>` +
      `<span class="trade-live-status">${esc(o.status || o.order_status || "")}</span>`;
    const cancel = el("button", "ghost", "Cancel");
    cancel.type = "button";
    cancel.onclick = async () => {
      cancel.disabled = true;
      try {
        await api("/api/trade/cancel", "POST", { order_id: oid, account: _preview && _preview.account });
        loadLiveOrders();
      } catch (e) { cancel.disabled = false; cancel.title = e.message; }
    };
    if (oid) row.appendChild(cancel);
    card.appendChild(row);
  });
  wrap.appendChild(card);
}

export { loadTrade };
