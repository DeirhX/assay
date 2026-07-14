// Pure data shaping for the portfolio-history view: collapse trades by day,
// fold activity rows by name and by sector, paginate, and pretty-print option
// contract labels. No DOM here so it can be unit-tested directly
// (tests/history.test.ts) and mirrors the shapes the backend hands over.
// Extracted from history.ts.

export interface Trade {
  date?: string;
  source?: "flex" | "live" | string;
  provisional?: boolean;
  [k: string]: any;
}

export interface ActivityRow {
  symbol?: string;
  underlying?: string;
  is_option?: boolean;
  n?: number;
  buys?: number;
  sells?: number;
  bought_base?: number;
  sold_base?: number;
  net_base_cash_flow?: number;
  base_realized_pnl?: number;
  currency?: string;
  sector?: string;
  [k: string]: any;
}

export interface ActivityGroup {
  key: string;
  label: string;
  is_option: boolean;
  opt_count: number;
  n: number;
  buys: number;
  sells: number;
  bought_base: number;
  sold_base: number;
  net_base_cash_flow: number;
  base_realized_pnl: number;
  currencies: Set<string>;
  currency?: string;
  members: ActivityRow[];
}

export interface SectorGroup {
  sector: string;
  n: number;
  buys: number;
  sells: number;
  bought_base: number;
  sold_base: number;
  net_base_cash_flow: number;
  base_realized_pnl: number;
  rows: ActivityRow[];
  groups?: ActivityGroup[];
  names?: number;
}

export interface DayGroup {
  date: string;
  trades: Trade[];
}

// Collapse a trade list to one entry per execution day, ascending by date, so
// the chart can mark days (not individual fills) and a hover can list them.
export function dayGroups(trades: Trade[] | null | undefined): DayGroup[] {
  const m = new Map<string, Trade[]>();
  for (const t of trades || []) {
    if (!t || !t.date) continue;
    if (!m.has(t.date)) m.set(t.date, []);
    m.get(t.date)!.push(t);
  }
  return [...m.entries()]
    .map(([date, ts]) => ({ date, trades: ts }))
    .sort((a, b) => (a.date < b.date ? -1 : a.date > b.date ? 1 : 0));
}

// Group "activity by name" rows under a single underlying, so a name's shares
// AND its option contracts collapse into one row (e.g. SOFI stock + SOFI opts).
// Unexpanded shows the combined aggregate; expanded shows each leg separately.
// A name with a single leg (a lone stock, or one contract) stays a singleton.
export function groupActivity(rows: ActivityRow[] | null | undefined): ActivityGroup[] {
  const groups = new Map<string, ActivityGroup>();
  for (const r of rows || []) {
    const isOpt = !!r.is_option;
    // The underlying is the grouping key for both shares and options.
    const name = (isOpt ? r.underlying : r.symbol) || r.symbol || "?";
    let g = groups.get(name);
    if (!g) {
      g = { key: name, label: name, is_option: false, opt_count: 0, n: 0, buys: 0, sells: 0,
        bought_base: 0, sold_base: 0,
        net_base_cash_flow: 0, base_realized_pnl: 0, currencies: new Set<string>(), members: [] };
      groups.set(name, g);
    }
    g.n += Number(r.n) || 0;
    g.buys += Number(r.buys) || 0;
    g.sells += Number(r.sells) || 0;
    g.bought_base += Number(r.bought_base) || 0;
    g.sold_base += Number(r.sold_base) || 0;
    g.net_base_cash_flow += Number(r.net_base_cash_flow) || 0;
    // P&L is summed in BASE currency: a group can't be summed in native units
    // if its members trade in different ones (and the grand total never could).
    g.base_realized_pnl += Number(r.base_realized_pnl) || 0;
    if (r.currency) g.currencies.add(r.currency);
    if (isOpt) g.opt_count += 1;
    g.members.push(r);
  }
  const out = [...groups.values()];
  out.forEach((g) => {
    g.bought_base = Math.round(g.bought_base * 100) / 100;
    g.sold_base = Math.round(g.sold_base * 100) / 100;
    g.net_base_cash_flow = Math.round(g.net_base_cash_flow * 100) / 100;
    g.base_realized_pnl = Math.round(g.base_realized_pnl * 100) / 100;
    // is_option here means "this name has option legs" (drives the badge/hint).
    g.is_option = g.opt_count > 0;
    // The native trading currency of the name; "mixed" only if it ever varies.
    g.currency = g.currencies.size === 1 ? [...g.currencies][0] : g.currencies.size ? "mixed" : "";
    // Shares first, then contracts by trade count, so the equity leg leads.
    g.members.sort((a, b) =>
      (a.is_option ? 1 : 0) - (b.is_option ? 1 : 0) || (Number(b.n) || 0) - (Number(a.n) || 0));
  });
  return out.sort((a, b) => b.n - a.n);
}

// Group the same by_symbol rows by sector, folding names within each sector via
// groupActivity. Sums are base-currency (the only valid way across tickers).
// Names with no resolved sector collect into "Unknown", which always sorts last.
export function groupBySector(rows: ActivityRow[] | null | undefined): SectorGroup[] {
  const byS = new Map<string, SectorGroup>();
  for (const r of rows || []) {
    const sector = (r.sector || "").trim() || "Unknown";
    let g = byS.get(sector);
    if (!g) {
      g = { sector, n: 0, buys: 0, sells: 0, bought_base: 0, sold_base: 0, net_base_cash_flow: 0, base_realized_pnl: 0, rows: [] };
      byS.set(sector, g);
    }
    g.n += Number(r.n) || 0;
    g.buys += Number(r.buys) || 0;
    g.sells += Number(r.sells) || 0;
    g.bought_base += Number(r.bought_base) || 0;
    g.sold_base += Number(r.sold_base) || 0;
    g.net_base_cash_flow += Number(r.net_base_cash_flow) || 0;
    g.base_realized_pnl += Number(r.base_realized_pnl) || 0;
    g.rows.push(r);
  }
  const out = [...byS.values()];
  out.forEach((g) => {
    g.bought_base = Math.round(g.bought_base * 100) / 100;
    g.sold_base = Math.round(g.sold_base * 100) / 100;
    g.net_base_cash_flow = Math.round(g.net_base_cash_flow * 100) / 100;
    g.base_realized_pnl = Math.round(g.base_realized_pnl * 100) / 100;
    g.groups = groupActivity(g.rows); // folded names within the sector
    g.names = g.groups.length;
  });
  return out.sort((a, b) => {
    const au = a.sector === "Unknown", bu = b.sector === "Unknown";
    if (au !== bu) return au ? 1 : -1;
    return b.n - a.n;
  });
}

export interface Page<T> {
  page: number;
  pages: number;
  total: number;
  start: number;
  items: T[];
}

// 1-based pagination over an array; clamps the requested page into range.
export function paginate<T>(arr: T[], page: number, size: number): Page<T> {
  const total = arr.length;
  const pages = Math.max(1, Math.ceil(total / size));
  const p = Math.min(Math.max(1, page | 0 || 1), pages);
  const start = (p - 1) * size;
  return { page: p, pages, total, start, items: arr.slice(start, start + size) };
}

const _MONTHS = ["Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"];
const _MON_IDX: Record<string, number> = Object.fromEntries(_MONTHS.map((m, i) => [m.toUpperCase(), i]));
// Strike with trailing zeros trimmed: 7.50 -> "7.5", 310.0 -> "310".
const _strike = (n: number | string) => String(Number(n)).replace(/\.0+$/, "");
const _expiry = (day: number | string | null, monIdx: number | null, yy: number | string) =>
  monIdx == null || day == null ? "" : `${Number(day)} ${_MONTHS[monIdx]} '${yy}`;
const _right = (r: string) => (r === "P" ? "Put" : r === "C" ? "Call" : r);

// Nicely formatted option title with the ticker dropped (it's already the row's
// parent), e.g. "UAA 28JUN24 7 P" -> "7 Put · 28 Jun '24". Parses IBKR's
// readable label first, then the OCC symbol; falls back to the raw string.
export function contractLabel(m: { label?: string; description?: string; symbol?: string; underlying?: string }): string {
  const lab = m.label || m.description || "";
  // "TICKER 28JUN24 7 P"
  let mt = /^\S+\s+(\d{1,2})([A-Za-z]{3})(\d{2})\s+([\d.]+)\s+([CP])$/.exec(lab.trim());
  if (mt) {
    const [, d, mon, yy, strike, r] = mt;
    const idx = _MON_IDX[mon.toUpperCase()];
    if (idx != null) return `${_strike(strike)} ${_right(r)} · ${_expiry(d, idx, yy)}`;
  }
  // OCC symbol: "UAA   240628P00007000" (root, YYMMDD, right, strike*1000)
  mt = /^.+?\s+(\d{2})(\d{2})(\d{2})([CP])(\d{8})$/.exec((m.symbol || "").trim());
  if (mt) {
    const [, yy, mm, dd, r, strike] = mt;
    return `${_strike(Number(strike) / 1000)} ${_right(r)} · ${_expiry(dd, Number(mm) - 1, yy)}`;
  }
  // Last resort: at least strip a leading "TICKER " so we don't repeat it.
  const under = (m.underlying || "").trim();
  const raw = lab || m.symbol || "?";
  return under && raw.toUpperCase().startsWith(under.toUpperCase() + " ")
    ? raw.slice(under.length + 1).trim() : raw;
}
