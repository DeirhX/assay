import datetime as dt
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import options_overlay as ov  # noqa: E402

AS_OF = dt.datetime(2026, 7, 1, tzinfo=dt.timezone.utc)


def _pos(qty=1000.0, price=100.0, currency="USD", fx=23.0):
    return {"qty": qty, "price_local": price, "price_base": price * fx, "currency": currency, "fx": fx}


def _no_defer():
    return {"defer_lots": [], "tax_saved_by_waiting": 0.0}


def _defer(exempt_on="2026-10-01", saved=5000.0):
    return {"defer_lots": [{"bucket": "taxable_gain", "exempt_on": exempt_on, "gain": 40000}],
            "tax_saved_by_waiting": saved}


def _chain():
    # Two expiries around the covered-call window and well past exemption.
    return {
        "source": "yahoo",
        "symbol": "TEST",
        "underlying_price": 100.0,
        "expiries": [
            {"expiry": "2026-08-07", "expiry_epoch": 0,
             "calls": [{"strike": 105.0, "bid": 2.0, "ask": 2.4, "last": 2.2, "implied_vol": 0.3},
                       {"strike": 115.0, "bid": 0.8, "ask": 1.0, "last": 0.9, "implied_vol": 0.32}],
             "puts": [{"strike": 93.0, "bid": 1.8, "ask": 2.2, "last": 2.0, "implied_vol": 0.31}]},
            {"expiry": "2026-11-15", "expiry_epoch": 0,
             "calls": [{"strike": 115.0, "bid": 3.0, "ask": 3.4, "last": 3.2, "implied_vol": 0.33}],
             "puts": [{"strike": 93.0, "bid": 5.0, "ask": 5.6, "last": 5.3, "implied_vol": 0.34}]},
        ],
    }


# --------------------------------------------------------------------------- #
# Covered call
# --------------------------------------------------------------------------- #
def test_covered_call_from_live_chain():
    out = ov.suggest_for_position("TEST", _pos(), _no_defer(), as_of=AS_OF, chain=_chain(), rate=0.04)
    cc = out["covered_call"]
    assert cc is not None
    assert cc["source"] == "yahoo"
    assert cc["contracts"] == 10                 # 1000 shares / 100
    assert cc["strike"] == 105.0                 # first strike >= 5% OTM
    assert cc["premium"] == 2.2                  # mid of 2.0/2.4
    assert cc["effective_exit"] == 107.2
    assert cc["premium_yield_annual_pct"] and cc["premium_yield_annual_pct"] > 0
    assert cc["estimate"] is False


def test_covered_call_black_scholes_fallback():
    # No chain -> BS estimate, flagged as such.
    closes = [100.0 * (1.01 if i % 2 else 0.99) for i in range(60)]
    series = [{"date": f"d{i}", "close": c} for i, c in enumerate(closes)]
    out = ov.suggest_for_position("TEST", _pos(), _no_defer(), series=series,
                                  as_of=AS_OF, chain=None, rate=0.04)
    cc = out["covered_call"]
    assert cc is not None
    assert cc["source"] == "black_scholes"
    assert cc["estimate"] is True
    assert cc["premium"] > 0
    assert cc["strike"] >= 100.0


def test_no_contract_below_100_shares():
    out = ov.suggest_for_position("TEST", _pos(qty=50.0), _no_defer(), as_of=AS_OF, chain=_chain(), rate=0.04)
    assert out is None or out["covered_call"] is None


# --------------------------------------------------------------------------- #
# Assignment guard around a deferred near-exempt lot
# --------------------------------------------------------------------------- #
def test_assignment_guard_pushes_expiry_past_exemption():
    out = ov.suggest_for_position("TEST", _pos(), _defer("2026-10-01"), as_of=AS_OF, chain=_chain(), rate=0.04)
    cc = out["covered_call"]
    assert cc is not None
    assert cc.get("assignment_guard") is True
    # Expiry must be strictly after the exemption date.
    assert dt.date.fromisoformat(cc["expiry"]) > dt.date(2026, 10, 1)
    assert cc["strike"] == 115.0                 # far-OTM guard strike
    assert any("assignment" in n.lower() for n in out["notes"])


def test_protective_put_appears_only_when_deferring():
    no = ov.suggest_for_position("TEST", _pos(), _no_defer(), as_of=AS_OF, chain=_chain(), rate=0.04)
    assert no["protective_put"] is None
    yes = ov.suggest_for_position("TEST", _pos(), _defer("2026-10-01", saved=7500.0),
                                  as_of=AS_OF, chain=_chain(), rate=0.04)
    pp = yes["protective_put"]
    assert pp is not None
    assert dt.date.fromisoformat(pp["expiry"]) > dt.date(2026, 10, 1)
    assert pp["put_strike"] == 93.0
    assert pp["protected_floor"] == pp["put_strike"] - pp["put_premium"]
    assert pp["tax_saved_by_waiting_czk"] == 7500.0
    assert pp["net_collar_premium"] is not None  # collar financed by a call


# --------------------------------------------------------------------------- #
# CZK conversion
# --------------------------------------------------------------------------- #
def test_premium_converted_to_base_currency():
    out = ov.suggest_for_position("TEST", _pos(fx=23.0), _no_defer(), as_of=AS_OF, chain=_chain(), rate=0.04)
    cc = out["covered_call"]
    # 2.2 premium * 100 * 10 contracts * 23 fx = 50,600.
    assert cc["premium_czk"] == 50_600.0


# --------------------------------------------------------------------------- #
# Source labeling -- a from-chain premium inherits the chain's own source, so an
# IBKR chain is not mislabeled as "yahoo".
# --------------------------------------------------------------------------- #
def _ibkr_chain():
    c = _chain()
    c["source"] = "ibkr"
    return c


def test_covered_call_inherits_ibkr_source():
    out = ov.suggest_for_position("TEST", _pos(), _no_defer(), as_of=AS_OF, chain=_ibkr_chain(), rate=0.04)
    cc = out["covered_call"]
    assert cc["source"] == "ibkr"
    assert cc["estimate"] is False
    assert out["source"] == "ibkr"


def test_protective_put_inherits_ibkr_source():
    out = ov.suggest_for_position("TEST", _pos(), _defer("2026-10-01"), as_of=AS_OF, chain=_ibkr_chain(), rate=0.04)
    pp = out["protective_put"]
    assert pp["source"] == "ibkr"
    assert pp["estimate"] is False


def test_sourceless_chain_defaults_to_yahoo():
    c = _chain()
    c.pop("source", None)
    out = ov.suggest_for_position("TEST", _pos(), _no_defer(), as_of=AS_OF, chain=c, rate=0.04)
    assert out["covered_call"]["source"] == "yahoo"


# --------------------------------------------------------------------------- #
# Covered-call strike ladder (yield-ranked, liquidity-gated)
# --------------------------------------------------------------------------- #
def _ladder_chain(source="alpaca"):
    return {
        "source": source,
        "symbol": "TEST",
        "underlying_price": 100.0,
        "expiries": [
            {"expiry": "2026-08-07",
             "calls": [
                 # tight spread + healthy OI -> ok, richest yield
                 {"strike": 105.0, "bid": 3.0, "ask": 3.2, "last": 3.1,
                  "implied_vol": 0.30, "delta": 0.42, "open_interest": 500, "volume": 120},
                 {"strike": 110.0, "bid": 1.8, "ask": 2.0, "last": 1.9,
                  "implied_vol": 0.31, "delta": 0.30, "open_interest": 300, "volume": 80},
                 # tight spread but nobody home (OI/vol below floor) -> thin
                 {"strike": 115.0, "bid": 0.95, "ask": 1.05, "last": 1.0,
                  "implied_vol": 0.33, "delta": 0.18, "open_interest": 50, "volume": 5},
                 # wide spread -> thin regardless of OI
                 {"strike": 120.0, "bid": 0.10, "ask": 0.40, "last": 0.2,
                  "implied_vol": 0.36, "delta": 0.08, "open_interest": 1000, "volume": 200},
             ],
             "puts": []},
            {"expiry": "2026-11-15",
             "calls": [{"strike": 115.0, "bid": 3.0, "ask": 3.4, "last": 3.2, "implied_vol": 0.33}],
             "puts": []},
        ],
    }


def _rung(ladder, strike):
    return next(r for r in ladder if abs(r["strike"] - strike) < 1e-6)


def test_ladder_ranked_by_yield_and_otm_only():
    out = ov.suggest_for_position("TEST", _pos(), _no_defer(), as_of=AS_OF,
                                  chain=_ladder_chain(), rate=0.04)
    ladder = out["covered_call_ladder"]
    assert len(ladder) == 4                       # all four OTM strikes, none ITM
    assert all(r["strike"] >= 105.0 for r in ladder)
    yields = [r["premium_yield_annual_pct"] for r in ladder]
    assert yields == sorted(yields, reverse=True)  # richest yield first
    assert ladder[0]["strike"] == 105.0
    # The headline pick is flagged in the ladder.
    assert _rung(ladder, 105.0).get("recommended") is True


def test_ladder_liquidity_gating():
    ladder = ov.suggest_for_position("TEST", _pos(), _no_defer(), as_of=AS_OF,
                                     chain=_ladder_chain(), rate=0.04)["covered_call_ladder"]
    assert _rung(ladder, 105.0)["liquidity"] == "ok"
    assert _rung(ladder, 110.0)["liquidity"] == "ok"
    assert _rung(ladder, 115.0)["liquidity"] == "thin"   # thin OI + volume
    assert _rung(ladder, 120.0)["liquidity"] == "thin"   # wide spread


def test_ladder_uses_chain_delta_for_assignment():
    ladder = ov.suggest_for_position("TEST", _pos(), _no_defer(), as_of=AS_OF,
                                     chain=_ladder_chain(), rate=0.04)["covered_call_ladder"]
    # Chain delta 0.42 -> 42% assignment, not a BS recompute.
    assert _rung(ladder, 105.0)["assignment_prob_pct"] == 42.0
    assert _rung(ladder, 105.0)["source"] == "alpaca"
    assert _rung(ladder, 105.0)["estimate"] is False


def test_ladder_synthesized_without_chain():
    closes = [100.0 * (1.01 if i % 2 else 0.99) for i in range(60)]
    series = [{"date": f"d{i}", "close": c} for i, c in enumerate(closes)]
    out = ov.suggest_for_position("TEST", _pos(), _no_defer(), series=series,
                                  as_of=AS_OF, chain=None, rate=0.04)
    ladder = out["covered_call_ladder"]
    assert len(ladder) == ov.LADDER_SIZE          # modeled rungs off spot
    assert all(r["estimate"] is True for r in ladder)
    assert all(r["liquidity"] == "unknown" for r in ladder)
    assert all(r["source"] == "black_scholes" for r in ladder)


def test_ladder_empty_below_one_contract():
    out = ov.suggest_for_position("TEST", _pos(qty=50.0), _no_defer(), as_of=AS_OF,
                                  chain=_ladder_chain(), rate=0.04)
    assert out is None or out["covered_call_ladder"] == []


def test_ibkr_chain_without_quotes_estimates_premium():
    # An IBKR chain that resolved strikes/expiries but carries no quotes (no
    # options market-data subscription): the premium is modeled, so the
    # suggestion is labeled black_scholes even though the chain came from IBKR --
    # while the chain-level source still records where the chain came from.
    c = _ibkr_chain()
    for exp in c["expiries"]:
        for side in ("calls", "puts"):
            for k in exp[side]:
                k["bid"] = k["ask"] = k["last"] = None
    out = ov.suggest_for_position("TEST", _pos(), _no_defer(), as_of=AS_OF, chain=c, rate=0.04)
    cc = out["covered_call"]
    assert cc["source"] == "black_scholes"
    assert cc["estimate"] is True
    assert out["source"] == "ibkr"
