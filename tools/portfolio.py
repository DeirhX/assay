"""Shared portfolio and target-model helpers.

The web server, research puller, and generated pages should not each invent
their own answer to "what is this position's weight?" or "what does this target
rule imply?" That is how portfolios become haunted by tiny inconsistent lies.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

REPO_ROOT = Path(__file__).resolve().parent.parent
DATA_DIR = REPO_ROOT / "data"
HOLDINGS_JSON = DATA_DIR / "current-holdings.json"
TARGET_MODEL_JSON = DATA_DIR / "target-model.json"
SYMBOL_ALIASES_JSON = DATA_DIR / "symbol-aliases.json"


def load_json(path: Path) -> dict[str, Any] | None:
    if not path.exists():
        return None
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return None


DATA_MISSING_HINT = (
    "Portfolio data not found. `data/` is a private git submodule and looks "
    "uninitialized.\n"
    "Initialize it (requires access to the private data repo) with:\n"
    "    git submodule update --init\n"
)


def data_initialized() -> bool:
    """True if the private `data/` submodule appears populated."""
    return HOLDINGS_JSON.exists() or TARGET_MODEL_JSON.exists()


def require_data() -> None:
    """Abort a CLI tool with a clear message when `data/` is not initialized."""
    if not data_initialized():
        raise SystemExit(DATA_MISSING_HINT)


def clean_symbol(symbol: str | None) -> str:
    return str(symbol or "").upper().strip()


def symbol_aliases() -> dict[str, str]:
    """Broker/display symbol -> provider/research symbol.

    IBKR and Yahoo do not share one canonical namespace. Keep the broker symbol
    for portfolio identity, but resolve to the provider symbol for research
    pulls. This deliberately lives in the shared portfolio adapter so every
    consumer gets the same mapping instead of re-learning exchange suffix trivia.
    """
    raw = load_json(SYMBOL_ALIASES_JSON)
    if not isinstance(raw, dict):
        return {}
    aliases: dict[str, str] = {}
    for key, value in raw.items():
        src = clean_symbol(str(key))
        dst = clean_symbol(str(value))
        if src and dst and src != dst:
            aliases[src] = dst
    return aliases


def provider_symbol_for(symbol: str | None, aliases: dict[str, str] | None = None) -> str:
    sym = clean_symbol(symbol)
    aliases = aliases if aliases is not None else symbol_aliases()
    return aliases.get(sym, sym)


def is_researchable_position(position: dict[str, Any]) -> bool:
    sym = clean_symbol(position.get("symbol"))
    if not sym or position.get("asset_class") == "OPT":
        return False
    # IBKR dividend-right rows are bookkeeping artifacts, not standalone
    # researchable securities.
    if ".DRRT" in sym:
        return False
    return True


def invested_value(positions: list[dict[str, Any]]) -> float:
    return sum(
        p["base_market_value"]
        for p in positions
        if isinstance(p.get("base_market_value"), (int, float))
    )


def position_weight_pct(position: dict[str, Any], invested: float) -> float | None:
    """Weight by actual market value over invested value.

    The broker's ``percent_of_nav`` field is usable for normal stocks but is
    wildly misleading for options. Recomputing from ``base_market_value`` keeps
    derivatives from poisoning concentration and target-band decisions.
    """
    bmv = position.get("base_market_value")
    if not isinstance(bmv, (int, float)) or not invested:
        return None
    return bmv / invested * 100.0


def parse_occ_symbol(symbol: str | None) -> tuple[str, float] | None:
    """OCC option symbol -> (right, strike), e.g. 'SPY   260618P00655000'.

    Layout: a 6-char space-padded root, then YYMMDD, then C/P, then an 8-digit
    strike in thousandths. Strip the padding and read the fixed 15-char tail.
    """
    compact = (symbol or "").replace(" ", "")
    if len(compact) < 15:
        return None
    core = compact[-15:]
    right = core[6]
    if right not in ("C", "P"):
        return None
    try:
        return right, int(core[7:]) / 1000.0
    except ValueError:
        return None


def option_exposure(position: dict[str, Any], invested: float) -> dict[str, Any] | None:
    """Notional exposure as a signed % of invested if the option were exercised.

    A long put is short the underlying on exercise (negative); a long call is long
    (positive); shorts flip the sign. The premium value of an option says nothing
    about what it does to the book -- a 2-lot SPY put worth ~870 CZK still hedges
    ~9% of NAV. Multiplier and FX are inferred from the position's own numbers
    rather than hardcoding 100 or a CZK/USD rate, so this survives index options
    and currency changes.
    """
    parsed = parse_occ_symbol(position.get("symbol"))
    qty = position.get("quantity")
    mv = position.get("market_value")
    mp = position.get("mark_price")
    bmv = position.get("base_market_value")
    if not parsed or not qty or not invested:
        return None
    right, strike = parsed

    multiplier = 100.0
    if mv and mp and qty:
        inferred = abs(mv) / (abs(mp) * abs(qty))
        if inferred > 1:
            multiplier = round(inferred)

    fx = (bmv / mv) if (bmv and mv) else None  # base currency per trading currency
    if fx is None:
        return None
    notional_base = abs(qty) * multiplier * strike * fx
    pct = notional_base / invested * 100.0
    direction = (1 if right == "C" else -1) * (1 if qty > 0 else -1)
    return {
        "right": right,
        "strike": strike,
        "contracts": qty,
        "multiplier": multiplier,
        "notional_base": notional_base,
        "exercise_pct": direction * pct,
    }


def holdings_weights(data: dict[str, Any] | None = None, *, include_aliases: bool = False) -> dict[str, float]:
    data = data if data is not None else load_json(HOLDINGS_JSON)
    if not data:
        return {}
    positions = data.get("positions", [])
    invested = invested_value(positions)
    if not invested:
        return {}
    aliases = symbol_aliases()
    weights: dict[str, float] = {}
    for p in positions:
        if not isinstance(p.get("symbol"), str):
            continue
        weight = position_weight_pct(p, invested)
        if weight is None:
            continue
        sym = clean_symbol(p["symbol"])
        weights[sym] = weight
        if include_aliases:
            provider_sym = provider_symbol_for(sym, aliases)
            if provider_sym != sym:
                weights[provider_sym] = weight
    return weights


def holdings_payload(data: dict[str, Any] | None = None) -> dict[str, Any]:
    data = data if data is not None else load_json(HOLDINGS_JSON) or {}
    positions = data.get("positions", [])
    invested = invested_value(positions)
    aliases = symbol_aliases()
    return {
        "net_asset_value": data.get("net_asset_value"),
        "invested_value": invested,
        "generated_at": data.get("generated_at"),
        "sizing_legend": data.get("sizing_legend", {}),
        "positions": [
            {
                "symbol": p["symbol"],
                "provider_symbol": provider_symbol_for(p.get("symbol"), aliases),
                "researchable": is_researchable_position(p),
                "description": p.get("description"),
                "asset_class": p.get("asset_class"),
                "percent_of_nav": position_weight_pct(p, invested),
                "broker_percent_of_nav": p.get("percent_of_nav"),
                "base_market_value": p.get("base_market_value"),
                "currency": p.get("currency"),
                "unrealized_pnl": p.get("unrealized_pnl"),
                "issuer_country_code": p.get("issuer_country_code"),
                "option": (
                    option_exposure(p, invested)
                    if p.get("asset_class") == "OPT"
                    else None
                ),
            }
            for p in positions
            if isinstance(p.get("symbol"), str)
        ],
    }


def target_context(model: dict[str, Any], symbol: str) -> dict[str, Any]:
    symbol = symbol.upper().strip()
    targets = model.get("targets", {})
    if symbol in targets:
        node = dict(targets[symbol])
        node["kind"] = "target"
        return node
    for sleeve_name, sleeve in model.get("sleeves", {}).items():
        if symbol in sleeve.get("members", []):
            node = dict(sleeve)
            node["kind"] = "sleeve"
            node["sleeve"] = sleeve_name
            node.pop("members", None)
            return node
    return {"kind": "none"}


def portfolio_context(symbol: str, *, holdings: dict[str, Any] | None = None, model: dict[str, Any] | None = None) -> dict[str, Any]:
    symbol = clean_symbol(symbol)
    weights = holdings_weights(holdings, include_aliases=True)
    model = model if model is not None else load_json(TARGET_MODEL_JSON) or {}
    current = weights.get(symbol)
    target = target_context(model, symbol)
    ctx: dict[str, Any] = {
        "current_weight_pct": current,
        "target": target,
    }
    low = target.get("low")
    high = target.get("high")
    if isinstance(current, (int, float)) and isinstance(low, (int, float)) and isinstance(high, (int, float)):
        if current < low:
            ctx["status"] = "below_band"
            ctx["gap_to_band_pct"] = round(low - current, 4)
        elif current > high:
            ctx["status"] = "above_band"
            ctx["gap_to_band_pct"] = round(high - current, 4)
        else:
            ctx["status"] = "in_band"
            ctx["gap_to_band_pct"] = 0.0
    elif current:
        ctx["status"] = "held_no_target"
    else:
        ctx["status"] = "not_held"
    return ctx


def decision_label(portfolio: dict[str, Any]) -> str:
    target = portfolio.get("target", {})
    rule = target.get("rule")
    status = portfolio.get("status")
    if rule == "avoid":
        return "avoid"
    if rule == "reduce" or (rule in {"trim_only", "do_not_add"} and status == "above_band"):
        return "trim"
    if rule == "accumulate" and status == "below_band":
        return "add_candidate"
    if rule == "wait":
        return "watch"
    if rule in {"hold", "trim_only", "do_not_add"}:
        return "hold"
    if rule == "accumulate":
        return "accumulate"
    return "research"
