#!/usr/bin/env python3
"""Direction-aware stock/option routes for one simulated rebalance trade."""
from __future__ import annotations

import datetime as dt
import hashlib
import json
import math
from typing import Any

import kid_block
import ibkr_trade
import option_market
import options_overlay
import portfolio
import timeutil

OPTION_MULTIPLIER = 100
ROUND_UP_MAX_DEVIATION_PCT = 0.15
QUOTE_MAX_AGE_SECONDS = 120.0
_UNSET = object()


def contracts_for_shares(planned_shares: Any, *, capacity: int | None = None) -> int:
    """Whole contracts near the intended shares, with Exit's bounded round-up."""
    try:
        planned = max(0, int(float(planned_shares or 0)))
    except (TypeError, ValueError):
        return 0
    contracts = planned // OPTION_MULTIPLIER
    rounded = contracts + 1
    assigned = rounded * OPTION_MULTIPLIER
    if (
        planned > 0
        and (assigned - planned) / planned <= ROUND_UP_MAX_DEVIATION_PCT + 1e-9
    ):
        contracts = rounded
    if capacity is not None:
        contracts = min(contracts, max(0, int(capacity)))
    return max(0, contracts)


def _position(holdings: dict[str, Any], symbol: str) -> dict[str, Any] | None:
    sym = portfolio.clean_symbol(symbol)
    for row in holdings.get("positions") or []:
        asset = str(row.get("asset_class") or row.get("assetClass") or "").upper()
        raw_symbol = row.get("symbol") or row.get("ticker")
        if asset != "OPT" and portfolio.clean_symbol(raw_symbol) == sym:
            return row
    return None


def _chain_spot(chain: dict[str, Any] | None) -> float:
    if not isinstance(chain, dict):
        return 0.0
    nested = chain.get("underlying_quote")
    candidates = [
        nested.get("last") if isinstance(nested, dict) else None,
        chain.get("underlying_last"),
        chain.get("underlying_price"),
    ]
    for raw in candidates:
        if isinstance(raw, (int, float)) and raw > 0:
            return float(raw)
    return 0.0


def _fx_for_currency(
    holdings: dict[str, Any], currency: str | None, position: dict[str, Any] | None,
) -> float:
    if position:
        return portfolio.position_fx_to_base(position)
    wanted = str(currency or "").upper()
    for row in holdings.get("positions") or []:
        if str(row.get("currency") or "").upper() == wanted:
            fx = portfolio.position_fx_to_base(row)
            if fx > 0:
                return fx
    return 1.0


def _decorate_execution(rungs: list[dict[str, Any]], *, now: dt.datetime) -> None:
    for rung in rungs:
        age = timeutil.age_seconds(rung.get("quote_timestamp"), now=now)
        rung["quote_age_seconds"] = round(age, 1) if age is not None else None
        rung["quote_fresh"] = age is not None and age <= QUOTE_MAX_AGE_SECONDS
        rung["limit_price"] = None
        if (
            rung.get("executable")
            and rung["quote_fresh"]
            and isinstance(rung.get("bid"), (int, float))
            and isinstance(rung.get("ask"), (int, float))
        ):
            rung["limit_price"] = math.floor(
                ((float(rung["bid"]) + float(rung["ask"])) / 2.0 + 1e-9) * 100,
            ) / 100
        elif rung.get("stageable"):
            rung["staging_warning"] = (
                "The exact IBKR contract can be staged, but preview requires a "
                "fresh two-sided quote."
            )


def build_route(
    holdings: dict[str, Any],
    symbol: str,
    delta_czk: Any,
    *,
    chain: Any = _UNSET,
    now: dt.datetime | None = None,
) -> dict[str, Any]:
    """Build stock and conditional option alternatives for one exact trade."""
    sym = portfolio.clean_symbol(symbol)
    try:
        delta = float(delta_czk)
    except (TypeError, ValueError):
        raise ValueError("delta_czk must be a number") from None
    if not sym or abs(delta) < 1:
        raise ValueError("symbol and a non-zero delta_czk are required")

    current = now or dt.datetime.now(dt.timezone.utc)
    chain_data = option_market.cached_option_chain(sym) if chain is _UNSET else chain
    position = _position(holdings, sym)
    spot = _chain_spot(chain_data)
    if spot <= 0 and position:
        try:
            spot = float(position.get("mark_price") or 0)
        except (TypeError, ValueError):
            spot = 0.0
    currency = (
        str((chain_data or {}).get("currency") or position.get("currency") or "")
        if position else str((chain_data or {}).get("currency") or "")
    )
    fx = _fx_for_currency(holdings, currency, position)
    planned_shares = int(round(abs(delta) / (spot * fx))) if spot > 0 and fx > 0 else 0
    direction = "increase" if delta > 0 else "reduce"

    capacity = 0
    capacity_notes: list[str] = []
    raw_working: list[dict[str, Any]] = []
    if option_market.session_ready():
        try:
            raw_working = ibkr_trade.live_orders()
        except ibkr_trade.CPAPIError:
            capacity_notes.append(
                "Working IBKR option orders could not be read; final staging will recheck capacity."
            )
    import trade_service

    available_cash = float(
        trade_service.cash_secured_put_capacity(holdings)["available_cash_czk"],
    )
    if direction == "reduce":
        covered = trade_service.covered_call_capacity(sym, raw_working)
        capacity = int(covered.get("capacity_contracts") or 0)
    elif spot > 0 and fx > 0:
        # A previous rebalance alternative will be replaced atomically. Unrelated
        # queue commitments and every resting short put remain real obligations.
        staged = [
            leg for leg in trade_service.load_basket()
            if not _is_rebalance_leg(leg)
        ]
        available_cash -= trade_service.working_short_put_collateral(raw_working)
        available_cash -= sum(
            max(0.0, float(leg.get("delta_czk") or 0))
            for leg in staged if leg.get("type") in {None, "stock"}
        )
        available_cash -= sum(
            float(leg.get("strike") or 0) * OPTION_MULTIPLIER
            * int(leg.get("contracts") or 0) * float(leg.get("fx_to_base") or 1)
            for leg in staged if leg.get("type") == "cash_secured_put"
        )
        available_cash = max(0.0, available_cash)
        capacity = int(available_cash // (spot * fx * OPTION_MULTIPLIER))
    contracts = contracts_for_shares(planned_shares, capacity=capacity)

    rate = option_market.cached_risk_free_rate()
    use_rate = float(rate) if isinstance(rate, (int, float)) else 0.04
    as_of = current.date()
    if direction == "reduce":
        ladder = options_overlay.covered_call_ladder(
            spot, options_overlay.DEFAULT_VOL, use_rate, as_of, chain_data,
            contracts=contracts, fx=fx, guard_after=None,
        )
        option_kind = "covered_call"
        option_label = "Sell covered call"
    else:
        ladder = options_overlay.cash_secured_put_ladder(
            spot, options_overlay.DEFAULT_VOL, use_rate, as_of, chain_data,
            contracts=contracts, fx=fx,
        )
        option_kind = "cash_secured_put"
        option_label = "Sell cash-secured put"
    _decorate_execution(ladder, now=current)

    exact = [rung for rung in ladder if rung.get("stageable") and rung.get("conid")]
    reasons: list[str] = []
    reasons.extend(capacity_notes)
    if planned_shares < 1:
        reasons.append("The planned amount cannot be converted to shares from the available mark.")
    elif contracts < 1:
        if direction == "increase" and available_cash <= 0:
            reasons.append("No uncommitted snapshot cash is available to secure a put.")
        else:
            reasons.append(
                f"The planned {planned_shares}-share trade is too far from one "
                f"{OPTION_MULTIPLIER}-share option contract or exceeds capacity."
            )
    if not ladder:
        reasons.append("No suitable option strike ladder is available.")
    elif not exact:
        source = str((chain_data or {}).get("source") or "modeled").replace("_", " ")
        reasons.append(
            f"Indicative {source} levels are available; staging needs an exact IBKR contract."
        )

    direct_kind = "buy_shares" if direction == "increase" else "sell_shares"
    direct_eligible = not (
        direction == "increase" and sym in kid_block.blocked_symbols()
    )
    option_eligible = contracts > 0 and bool(ladder)
    return {
        "symbol": sym,
        "delta_czk": round(delta, 2),
        "direction": direction,
        "planned_shares": planned_shares,
        "underlying": spot or None,
        "currency": currency or None,
        "fx_to_base": fx,
        "source": str((chain_data or {}).get("source") or "black_scholes"),
        "direct": {
            "kind": direct_kind,
            "label": "Buy shares" if direction == "increase" else "Sell shares",
            "eligible": direct_eligible,
            "reasons": (
                [] if direct_eligible
                else ["Direct purchase is blocked by PRIIPs/KID restrictions for this account."]
            ),
        },
        "option": {
            "kind": option_kind,
            "label": option_label,
            "eligible": option_eligible,
            "stageable": contracts > 0 and bool(exact),
            "reasons": reasons,
            "contracts": contracts,
            "assignment_shares": contracts * OPTION_MULTIPLIER,
            "share_deviation": contracts * OPTION_MULTIPLIER - planned_shares,
            "rounded_up": contracts * OPTION_MULTIPLIER > planned_shares,
            "available_cash_czk": round(available_cash, 2) if direction == "increase" else None,
        },
        "recommended": option_kind if not direct_eligible and option_eligible else direct_kind,
        "ladder": ladder,
    }


def _is_rebalance_leg(leg: dict[str, Any]) -> bool:
    return any(
        str(prov.get("source") or "") == "rebalance_routes"
        for prov in leg.get("provenance") or []
        if isinstance(prov, dict)
    )


def stage_routes(
    holdings: dict[str, Any],
    trades: Any,
    selections: Any,
    mode: str = "replace",
) -> dict[str, Any]:
    """Validate mixed routes, then append them or replace prior rebalance legs."""
    import trade_service

    mode = str(mode or "replace").strip().lower()
    if mode not in {"append", "replace"}:
        raise ValueError("mode must be 'append' or 'replace'")
    netted = portfolio.normalize_basket(trades)
    if not netted:
        raise ValueError("nothing to stage")
    if selections is None:
        selections = []
    if not isinstance(selections, list) or not all(isinstance(row, dict) for row in selections):
        raise ValueError("selections must be a list")
    selected = {
        portfolio.clean_symbol(row.get("symbol")): row
        for row in selections
        if portfolio.clean_symbol(row.get("symbol"))
    }
    fingerprint = hashlib.sha256(
        json.dumps(netted, sort_keys=True, separators=(",", ":")).encode("utf-8"),
    ).hexdigest()[:16]
    generated: list[dict[str, Any]] = []
    selected_routes: list[dict[str, Any]] = []

    for sym, delta in sorted(netted.items()):
        choice = selected.get(sym) or {}
        default_route = "buy_shares" if delta > 0 else "sell_shares"
        route_kind = str(choice.get("route") or default_route)
        option_kind = "cash_secured_put" if delta > 0 else "covered_call"
        if route_kind == default_route:
            generated.append({
                "type": "stock",
                "symbol": sym,
                "delta_czk": delta,
                "provenance": [{
                    "source": "rebalance_routes",
                    "route": default_route,
                    "plan_fingerprint": fingerprint,
                }],
            })
            selected_routes.append({"symbol": sym, "route": default_route})
            continue
        if route_kind != option_kind:
            raise ValueError(f"{sym}: route {route_kind!r} does not match the trade direction")

        route = build_route(holdings, sym, delta)
        option = route["option"]
        if not option.get("eligible"):
            raise ValueError(f"{sym}: " + "; ".join(option.get("reasons") or ["option route unavailable"]))
        try:
            conid_raw = choice.get("conid")
            strike_raw = choice.get("strike")
            contracts_raw = choice.get("contracts")
            if conid_raw is None or strike_raw is None or contracts_raw is None:
                raise TypeError
            conid = int(conid_raw)
            strike = float(strike_raw)
            contracts = int(contracts_raw)
        except (TypeError, ValueError):
            raise ValueError(f"{sym}: option route needs numeric conid, strike, and contracts") from None
        expiry = str(choice.get("expiry") or "")
        if contracts != int(option.get("contracts") or 0):
            raise ValueError(f"{sym}: contract count no longer matches the current rebalance route")
        rung = next((
            candidate for candidate in route.get("ladder") or []
            if int(candidate.get("conid") or 0) == conid
            and str(candidate.get("expiry") or "") == expiry
            and abs(float(candidate.get("strike") or 0) - strike) < 1e-6
        ), None)
        if not rung or not rung.get("stageable"):
            raise ValueError(f"{sym}: selected option is not an exact stageable IBKR contract")
        resolver = (
            ibkr_trade.resolve_executable_put
            if option_kind == "cash_secured_put"
            else ibkr_trade.resolve_executable_call
        )
        error_type = (
            ibkr_trade.ExecutablePutError
            if option_kind == "cash_secured_put"
            else ibkr_trade.ExecutableCallError
        )
        try:
            exact = resolver(
                sym, expiry, strike, expected_conid=conid,
                max_quote_age_seconds=QUOTE_MAX_AGE_SECONDS,
                allow_missing_quote=True,
            )
        except error_type as exc:
            raise ValueError(
                f"{sym}: exact option contract or quote could not be refreshed ({exc.reason})"
            ) from exc
        provenance = [{
            "source": "rebalance_routes",
            "route": option_kind,
            "plan_fingerprint": fingerprint,
            "intended_shares": route.get("planned_shares"),
            "intended_assigned_shares": contracts * OPTION_MULTIPLIER,
            "rung": {"conid": conid, "expiry": expiry, "strike": strike},
        }]
        leg = {
            "type": option_kind,
            "leg_id": f"{option_kind}:{sym}:{conid}",
            "symbol": sym,
            "route": option_kind,
            "conid": conid,
            "expiry": expiry,
            "strike": strike,
            "right": "P" if option_kind == "cash_secured_put" else "C",
            "contracts": contracts,
            "multiplier": OPTION_MULTIPLIER,
            "limit_price": exact.get("limit_price"),
            "quote_timestamp": exact.get("quote_timestamp"),
            "staging_warning": exact.get("staging_warning"),
            "currency": route.get("currency"),
            "fx_to_base": route.get("fx_to_base"),
            "provenance": provenance,
        }
        generated.append(leg)
        selected_routes.append({
            "symbol": sym, "route": option_kind,
            "assignment_shares": contracts * OPTION_MULTIPLIER,
        })

    current = trade_service.load_basket()
    existing = (
        current
        if mode == "append"
        else [leg for leg in current if not _is_rebalance_leg(leg)]
    )

    # Verify the complete resulting queue, including existing legs in append mode.
    all_legs = trade_service._normalize_basket(existing + generated)
    call_symbols = {
        str(leg.get("symbol") or "") for leg in all_legs
        if leg.get("type") == "covered_call"
    }
    raw_working: list[dict[str, Any]] = []
    if any(leg.get("type") in {"covered_call", "cash_secured_put"} for leg in all_legs):
        try:
            raw_working = ibkr_trade.live_orders()
        except ibkr_trade.CPAPIError as exc:
            raise ValueError("working option orders could not be verified") from exc
    if call_symbols:
        account_id = trade_service._resolve_trade_account(None)
        for sym in call_symbols:
            capacity = trade_service.covered_call_capacity(
                sym, raw_working, live_account_id=account_id,
            )
            calls = sum(
                int(leg.get("contracts") or 0)
                for leg in all_legs
                if leg.get("type") == "covered_call" and leg.get("symbol") == sym
            )
            stock_sells = sum(
                abs(float(leg.get("delta_czk") or 0))
                for leg in all_legs
                if leg.get("type") == "stock"
                and leg.get("symbol") == sym
                and float(leg.get("delta_czk") or 0) < 0
            )
            mark = float((_position(holdings, sym) or {}).get("mark_price") or 0)
            fx = _fx_for_currency(holdings, None, _position(holdings, sym))
            sell_shares = int(round(stock_sells / (mark * fx))) if mark > 0 and fx > 0 else 0
            available_shares = max(0, int(capacity.get("current_shares") or 0) - sell_shares)
            available_contracts = max(
                0,
                available_shares // OPTION_MULTIPLIER
                - int(capacity.get("held_short_calls") or 0)
                - int(capacity.get("working_short_calls") or 0),
            )
            if calls > available_contracts:
                raise ValueError(f"{sym}: selected calls and stock sales exceed live share coverage")

    # Aggregate cash-secured puts, working puts, and immediate stock buys.
    cash_capacity = trade_service.cash_secured_put_capacity()
    available_cash = float(cash_capacity["available_cash_czk"])
    stock_buys = sum(
        max(0.0, float(leg.get("delta_czk") or 0))
        for leg in all_legs if leg.get("type") == "stock"
    )
    staged_puts = sum(
        float(leg.get("strike") or 0) * OPTION_MULTIPLIER
        * int(leg.get("contracts") or 0) * float(leg.get("fx_to_base") or 1)
        for leg in all_legs if leg.get("type") == "cash_secured_put"
    )
    working_puts = trade_service.working_short_put_collateral(raw_working)
    required_cash = stock_buys + staged_puts + working_puts
    if required_cash > available_cash + 0.01:
        raise ValueError(
            f"stock buys and cash-secured puts need {required_cash:,.0f} CZK, "
            f"but only {available_cash:,.0f} CZK is available"
        )

    basket = trade_service.save_basket(all_legs)
    return {
        "staged": True,
        "mode": mode,
        "basket": basket,
        "routes": selected_routes,
        "required_cash_czk": round(required_cash, 2),
        "available_cash_czk": round(available_cash, 2),
    }
