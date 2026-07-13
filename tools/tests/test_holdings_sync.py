import json
import sys
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import holdings_sync  # noqa: E402


def test_flex_as_of_uses_end_of_statement_day():
    stamp = holdings_sync._flex_as_of_stamp({"report_to_date": "20260710"})
    assert stamp == "2026-07-10T23:59:59+00:00"


def test_flex_as_of_falls_back_to_end_of_previous_utc_day():
    stamp = holdings_sync._flex_as_of_stamp(
        {},
        now=datetime(2026, 7, 13, 21, 0, tzinfo=timezone.utc),
    )
    assert stamp == "2026-07-12T23:59:59+00:00"


def test_authenticated_sync_prefers_live_portfolio_over_flex():
    with tempfile.TemporaryDirectory() as tmp:
        snapshot_path = Path(tmp) / "current-holdings.json"
        snapshot_path.write_text(json.dumps({
            "base_currency": "CZK",
            "net_asset_value": 1000.0,
            "positions": [{"symbol": "AMD", "asset_class": "STK"}],
        }), encoding="utf-8")
        live = {
            "account": {"currency": "CZK"},
            "positions": [{"ticker": "AMD", "position": 10}],
            "summary": {"netliquidation": {"amount": 2000, "currency": "CZK"}},
        }
        merged = {
            "base_currency": "CZK",
            "net_asset_value": 2000.0,
            "positions": [{"symbol": "AMD", "asset_class": "STK", "quantity": 10}],
        }
        messages = []
        with mock.patch.object(
            holdings_sync, "HOLDINGS_JSON", snapshot_path,
        ), mock.patch.object(
            holdings_sync.ibkr_trade, "auth_status",
            return_value={"authenticated": True},
        ), mock.patch.object(
            holdings_sync.holdings_live, "fetch_live_portfolio", return_value=live,
        ) as fetch_live, mock.patch.object(
            holdings_sync.holdings_live, "merge_live_snapshot", return_value=merged,
        ) as merge_live, mock.patch.object(
            holdings_sync, "holdings_payload",
            return_value={"generated_at": "now", "positions": []},
        ), mock.patch.object(
            holdings_sync, "regenerate_site", return_value={"ok": True},
        ):
            result = holdings_sync._sync_holdings(progress=messages.append)

        fetch_live.assert_called_once_with(assume_authenticated=True)
        merge_live.assert_called_once()
        assert result["sync_source"] == "live"
        assert json.loads(snapshot_path.read_text(encoding="utf-8")) == merged
        assert any("live positions" in message for message in messages)
