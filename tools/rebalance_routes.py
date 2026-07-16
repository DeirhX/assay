#!/usr/bin/env python3
"""Direction-aware stock/option routes for one simulated rebalance trade."""
from __future__ import annotations

import datetime as dt
import hashlib
import json
from typing import Any

import execution_quotes
import kid_block
import ibkr_trade
import option_market
import options_math
import options_overlay
import portfolio
from value_coercion import coerce_optional_limit_price

OPTION_MULTIPLIER = ibkr_trade.OPTION_MULTIPLIER
ROUND_UP_MAX_DEVIATION_PCT = options_math.DEFAULT_ROUND_UP_MAX_DEVIATION_PCT
QUOTE_MAX_AGE_SECONDS = ibkr_trade.OPTION_QUOTE_MAX_AGE_SECONDS
_UNSET = object()


def contracts_for_shares(planned_shares: Any, *, capacity: int | None = None) -> int:
    """Whole contracts near the intended shares, with Exit's bounded round-up."""
    return options_math.whole_contracts_for_shares(
        planned_shares,
        multiplier=OPTION_MULTIPLIER,
        round_up_max_deviation_pct=ROUND_UP_MAX_DEVIATION_PCT,
        capacity_contracts=capacity,
    )


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
    bid = nested.get("bid") if isinstance(nested, dict) else chain.get("underlying_bid")
    ask = nested.get("ask") if isinstance(nested, dict) else chain.get("underlying_ask")
    if (
        isinstance(bid, (int, float)) and bid > 0
        and isinstance(ask, (int, float)) and ask > 0
    ):
        return (float(bid) + float(ask)) / 2.0
    return 0.0


def _chain_currency(chain: dict[str, Any] | None, position: dict[str, Any] | None) -> str:
    """Instrument currency for FX sizing.

    IBKR secdef chains often omit currency; US equity option sources are USD.
    """
    if position:
        from_position = str(position.get("currency") or "").strip().upper()
        if from_position:
            return from_position
    if isinstance(chain, dict):
        from_chain = str(chain.get("currency") or "").strip().upper()
        if from_chain:
            return from_chain
        source = str(chain.get("source") or "").strip().lower()
        if source in {"ibkr", "yahoo", "alpaca"}:
            return "USD"
    return ""


def _fx_for_currency(
    holdings: dict[str, Any], currency: str | None, position: dict[str, Any] | None,
) -> float:
    if position:
        return portfolio.position_fx_to_base(position)
    wanted = str(currency or "").upper()
    if wanted:
        for row in holdings.get("positions") or []:
            if str(row.get("currency") or "").upper() == wanted:
                fx = portfolio.position_fx_to_base(row)
                if fx > 0:
                    return fx
    # Unheld names with a blank currency still need a book FX; prefer USD.
    preferred: list[float] = []
    for row in holdings.get("positions") or []:
        ccy = str(row.get("currency") or "").upper()
        fx = portfolio.position_fx_to_base(row)
        if not ccy or fx <= 0:
            continue
        if ccy == "USD" or ccy == wanted:
            return fx
        preferred.append(fx)
    if preferred:
        return preferred[0]
    return 1.0


def _decorate_execution(rungs: list[dict[str, Any]], *, now: dt.datetime) -> None:
    execution_quotes.decorate_ladder_rungs(
        rungs,
        now=now,
        max_age_seconds=QUOTE_MAX_AGE_SECONDS,
        staging_warning=execution_quotes.rebalance_staging_warning,
    )


def build_route(
    holdings: dict[str, Any],
    symbol: str,
    delta_czk: Any,
    *,
    chain: Any = _UNSET,
    now: dt.datetime | None = None,
    expiry_mode: str = "monthly",
) -> dict[str, Any]:
    """Build stock and conditional option alternatives for one exact trade."""
    sym = portfolio.clean_symbol(symbol)
    try:
        delta = float(delta_czk)
    except (TypeError, ValueError):
        raise ValueError("delta_czk must be a number") from None
    if not sym or abs(delta) < 1:
        raise ValueError("symbol and a non-zero delta_czk are required")
    mode = options_overlay.normalize_expiry_mode(expiry_mode)

    current = now or dt.datetime.now(dt.timezone.utc)
    position = _position(holdings, sym)
    position_spot = 0.0
    if position:
        try:
            position_spot = float(position.get("mark_price") or 0)
        except (TypeError, ValueError):
            position_spot = 0.0
    position_currency = str(position.get("currency") or "") if position else ""
    position_fx = _fx_for_currency(holdings, position_currency, position)
    preflight_shares = (
        int(round(abs(delta) / (position_spot * position_fx)))
        if position_spot > 0 and position_fx > 0 else 0
    )
    direction = "increase" if delta > 0 else "reduce"
    preflight_contracts = contracts_for_shares(preflight_shares)
    # A bounded option contract needs at least ~87 planned shares. When the
    # holdings mark proves the trade is smaller, no expiry or strike can change
    # that answer, so avoid the entire option-chain request.
    skip_chain = (
        chain is _UNSET
        and position_spot > 0
        and position_fx > 0
        and preflight_contracts < 1
    )
    option_right = "P" if direction == "increase" else "C"
    # Opening the option route table always attempts a live IBKR rebuild so
    # stale contract samples cannot masquerade as the current ladder.
    chain_data = (
        None
        if skip_chain
        else option_market.cached_option_chain(
            sym,
            right=option_right,
            force_refresh=True,
            expiry_mode=mode,
        ) if chain is _UNSET
        else chain
    )
    spot = _chain_spot(chain_data)
    if spot <= 0:
        spot = position_spot
    currency = _chain_currency(
        chain_data if isinstance(chain_data, dict) else None,
        position,
    )
    fx = _fx_for_currency(holdings, currency, position)
    planned_shares = int(round(abs(delta) / (spot * fx))) if spot > 0 and fx > 0 else 0
    theoretical_contracts = contracts_for_shares(planned_shares)

    capacity = 0
    capacity_notes: list[str] = []
    raw_working: list[dict[str, Any]] = []
    if theoretical_contracts > 0 and option_market.session_ready():
        try:
            raw_working = ibkr_trade.live_orders()
        except ibkr_trade.CPAPIError:
            capacity_notes.append(
                "Working IBKR option orders could not be read; final staging will recheck capacity."
            )
    import trade_service

    cash_capacity = trade_service.cash_secured_put_capacity(holdings)
    # Margin capability is an account property. Do not require a sized contract
    # first — a missing mark must not demote the route to cash-secured copy.
    margin_enabled = (
        direction == "increase"
        and trade_service.margin_account_enabled()
    )
    available_cash = float(cash_capacity["available_cash_czk"])
    if direction == "reduce" and theoretical_contracts > 0:
        covered = trade_service.covered_call_capacity(sym, raw_working)
        capacity = int(covered.get("capacity_contracts") or 0)
    elif theoretical_contracts > 0 and spot > 0 and fx > 0 and not margin_enabled:
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
    contracts = contracts_for_shares(
        planned_shares,
        capacity=None if margin_enabled else capacity,
    )

    as_of = current.date()
    if contracts < 1:
        ladder = []
        option_kind = "covered_call" if direction == "reduce" else "cash_secured_put"
        option_label = (
            "Sell covered call"
            if direction == "reduce"
            else "Sell put (margin)" if margin_enabled
            else "Sell cash-secured put"
        )
    elif direction == "reduce":
        rate = option_market.cached_risk_free_rate()
        use_rate = float(rate) if isinstance(rate, (int, float)) else 0.04
        ladder = options_overlay.covered_call_ladder(
            spot, options_overlay.DEFAULT_VOL, use_rate, as_of, chain_data,
            contracts=contracts, fx=fx, guard_after=None, allow_synthetic=False,
            expiry_mode=mode,
        )
        option_kind = "covered_call"
        option_label = "Sell covered call"
    else:
        rate = option_market.cached_risk_free_rate()
        use_rate = float(rate) if isinstance(rate, (int, float)) else 0.04
        ladder = options_overlay.cash_secured_put_ladder(
            spot, options_overlay.DEFAULT_VOL, use_rate, as_of, chain_data,
            contracts=contracts, fx=fx, allow_synthetic=False,
            expiry_mode=mode,
        )
        option_kind = "cash_secured_put"
        option_label = "Sell put (margin)" if margin_enabled else "Sell cash-secured put"
    _decorate_execution(ladder, now=current)

    exact = [rung for rung in ladder if rung.get("stageable") and rung.get("conid")]
    reasons: list[str] = []
    reasons.extend(capacity_notes)
    if planned_shares < 1:
        if spot <= 0:
            reasons.append(
                "No usable underlying quote or holdings mark is available to size this option."
            )
        else:
            reasons.append(
                "The planned amount cannot be converted to shares from the available mark."
            )
    elif contracts < 1:
        if theoretical_contracts < 1:
            reasons.append(
                f"The planned {planned_shares}-share trade is too far from one "
                f"{OPTION_MULTIPLIER}-share option contract."
            )
        elif direction == "increase" and not margin_enabled and available_cash <= 0:
            cash = float(cash_capacity.get("cash_czk") or 0)
            held = float(cash_capacity.get("held_short_put_collateral_czk") or 0)
            reasons.append(
                (
                    "No uncommitted snapshot cash remains after held short puts and "
                    "working or queued obligations "
                    f"({held:,.0f} CZK held-put collateral; {cash:,.0f} CZK snapshot cash)."
                )
                if held > 0
                else "No uncommitted snapshot cash is available to secure a put."
            )
        elif direction == "increase" and not margin_enabled and capacity < 1:
            required = spot * fx * OPTION_MULTIPLIER
            reasons.append(
                f"One cash-secured put needs about {required:,.0f} CZK; "
                f"{available_cash:,.0f} CZK remains after held, working, and queued obligations."
            )
        elif direction == "reduce" and capacity < 1:
            reasons.append(
                "No uncovered 100-share lot is available after held and working calls."
            )
        else:
            reasons.append("The option contract exceeds the available capacity.")
    if contracts > 0:
        if not ladder:
            source = str((chain_data or {}).get("source") or "").strip().lower()
            if source == "ibkr":
                reasons.append(
                    "IBKR listed no usable OTM option strikes near the mark for this window."
                )
            else:
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
        "expiry_mode": mode,
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
            "collateral_mode": (
                "margin" if direction == "increase" and margin_enabled
                else "cash" if direction == "increase"
                else None
            ),
            "available_cash_czk": (
                None if margin_enabled
                else round(available_cash, 2) if direction == "increase"
                else None
            ),
            "snapshot_cash_czk": (
                round(float(cash_capacity.get("cash_czk") or 0), 2)
                if direction == "increase" else None
            ),
            "held_short_put_collateral_czk": (
                round(float(cash_capacity.get("held_short_put_collateral_czk") or 0), 2)
                if direction == "increase" else None
            ),
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


def _call_coverage_violations(
    holdings: dict[str, Any],
    all_legs: list[dict[str, Any]],
    raw_working: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    """Use the durable queue validator with live IBKR coverage when staging."""
    import trade_service

    if not any(leg.get("type") == "covered_call" for leg in all_legs):
        return []
    account_id = trade_service._resolve_trade_account(None)
    return trade_service.basket_call_coverage_violations(
        all_legs,
        holdings,
        raw_working,
        live_account_id=account_id,
    )


def stage_routes(
    holdings: dict[str, Any],
    trades: Any,
    selections: Any,
    mode: str = "replace",
    source: str = "rebalance_routes",
) -> dict[str, Any]:
    """Validate mixed routes, then append them or replace prior rebalance legs."""
    import trade_service

    mode = str(mode or "replace").strip().lower()
    if mode not in {"append", "replace"}:
        raise ValueError("mode must be 'append' or 'replace'")
    source = str(source or "rebalance_routes")
    if source not in {"rebalance_routes", "ticker", "execution_plan"}:
        raise ValueError("invalid rebalance route source")
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
        execution_item_id = str(choice.get("execution_item_id") or "") or None
        execution_item_ids = [
            str(item_id) for item_id in choice.get("execution_item_ids") or []
            if str(item_id)
        ]
        if execution_item_id:
            execution_item_ids.append(execution_item_id)
        execution_item_ids = list(dict.fromkeys(execution_item_ids))
        provenance_ids: list[str | None] = list(execution_item_ids)
        if not provenance_ids:
            provenance_ids.append(None)
        limit_price = coerce_optional_limit_price(
            choice.get("limit_price"),
            numeric_error=f"{sym}: limit_price must be numeric",
            positive_error=f"{sym}: limit_price must be positive",
        )
        default_route = "buy_shares" if delta > 0 else "sell_shares"
        route_kind = str(choice.get("route") or default_route)
        option_kind = "cash_secured_put" if delta > 0 else "covered_call"
        if route_kind == default_route:
            generated.append({
                "type": "stock",
                "symbol": sym,
                "delta_czk": delta,
                "limit_price": limit_price,
                "provenance": [
                    {
                        "source": source,
                        "route": default_route,
                        "plan_fingerprint": fingerprint,
                        "execution_item_id": item_id,
                    }
                    for item_id in provenance_ids
                ],
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
        provenance = [
            {
                "source": source,
                "route": option_kind,
                "plan_fingerprint": fingerprint,
                "execution_item_id": item_id,
                "intended_shares": route.get("planned_shares"),
                "intended_assigned_shares": contracts * OPTION_MULTIPLIER,
                "rung": {"conid": conid, "expiry": expiry, "strike": strike},
            }
            for item_id in provenance_ids
        ]
        selected_limit = limit_price
        if selected_limit is not None:
            bid = float(exact.get("bid") or 0)
            if bid > 0 and selected_limit + 1e-9 < bid:
                raise ValueError(
                    f"{sym}: minimum option credit cannot be below the current {bid:g} bid"
                )
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
            "limit_price": selected_limit or exact.get("limit_price"),
            "quote_timestamp": exact.get("quote_timestamp"),
            "staging_warning": exact.get("staging_warning"),
            "currency": route.get("currency"),
            "fx_to_base": route.get("fx_to_base"),
            "collateral_mode": route.get("option", {}).get("collateral_mode"),
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
    raw_working: list[dict[str, Any]] = []
    if any(leg.get("type") in {"covered_call", "cash_secured_put"} for leg in all_legs):
        try:
            raw_working = ibkr_trade.live_orders()
        except ibkr_trade.CPAPIError as exc:
            raise ValueError("working option orders could not be verified") from exc
    coverage_violations = _call_coverage_violations(holdings, all_legs, raw_working)
    generated_symbols = {
        str(leg.get("symbol") or "") for leg in generated if leg.get("symbol")
    }
    introduced = [
        violation for violation in coverage_violations
        if violation["symbol"] in generated_symbols
    ]
    if introduced:
        symbols = ", ".join(violation["symbol"] for violation in introduced)
        raise ValueError(
            f"{symbols}: selected calls and stock sales exceed live share coverage"
        )

    # Aggregate cash-secured puts, working puts, and immediate stock buys.
    cash_capacity = trade_service.cash_secured_put_capacity(holdings)
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
    # A margin account is not cash-capped merely because this particular append
    # contains a stock buy or a covered call instead of a short put. IBKR owns
    # the real buying-power check at preview; this local snapshot guard is only
    # for cash accounts.
    margin_enabled = (
        required_cash > available_cash + 0.01
        and trade_service.margin_account_enabled()
    )
    if not margin_enabled and required_cash > available_cash + 0.01:
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
        "collateral_mode": "margin" if margin_enabled else "cash",
        "coverage_violations": coverage_violations,
    }
