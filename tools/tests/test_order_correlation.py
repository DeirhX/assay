import tempfile
from pathlib import Path

import _support  # noqa: F401
import order_correlation


def _basket():
    return [
        {
            "type": "stock",
            "leg_id": "stock:AMD",
            "symbol": "AMD",
            "provenance": [
                {"execution_item_id": "intent-1"},
                {"execution_item_id": "intent-2"},
            ],
        },
        {
            "type": "covered_call",
            "leg_id": "covered_call:NVDA:55",
            "symbol": "NVDA",
            "provenance": [{"execution_item_id": "intent-3"}],
        },
    ]


def _ack(order_id, *, symbol, coid, leg_id=None, status="Submitted"):
    sent = {
        "cOID": coid,
        "symbol": symbol,
        "side": "BUY",
        "quantity": 3,
    }
    if leg_id:
        sent["leg_id"] = leg_id
        sent["instrument_type"] = "covered_call"
    return {
        "order_id": order_id,
        "order_status": status,
        "assay_order": sent,
    }


def test_records_acknowledgements_by_enriched_sent_order_not_position():
    with tempfile.TemporaryDirectory() as tmp:
        path = Path(tmp) / "correlations.json"
        records = order_correlation.record_placements(
            "DU1",
            _basket(),
            [
                _ack(
                    "call-1",
                    symbol="NVDA",
                    coid="assay-call",
                    leg_id="covered_call:NVDA:55",
                ),
                _ack("stock-1", symbol="AMD", coid="assay-stock"),
            ],
            path=path,
            now="2026-07-14T00:00:00+00:00",
        )
        by_id = {record["broker_order_id"]: record for record in records}
        assert by_id["call-1"]["execution_item_ids"] == ["intent-3"]
        assert by_id["stock-1"]["execution_item_ids"] == ["intent-1", "intent-2"]
        assert by_id["stock-1"]["leg_id"] == "stock:AMD"


def test_sync_tracks_partial_then_terminal_and_reopens_only_failed_intent():
    with tempfile.TemporaryDirectory() as tmp:
        path = Path(tmp) / "correlations.json"
        order_correlation.record_placements(
            "DU1",
            _basket(),
            [
                _ack("stock-1", symbol="AMD", coid="assay-stock"),
                _ack(
                    "call-1",
                    symbol="NVDA",
                    coid="assay-call",
                    leg_id="covered_call:NVDA:55",
                ),
            ],
            path=path,
        )
        partial = order_correlation.sync_orders(
            [{
                "orderId": "stock-1", "status": "Submitted",
                "filledQuantity": 1, "totalSize": 3,
            }],
            path=path,
        )
        assert partial["summary"]["partial"] == 1
        assert partial["reopen_item_ids"] == []

        terminal = order_correlation.sync_orders(
            [
                {
                    "orderId": "stock-1", "status": "Cancelled",
                    "filledQuantity": 1, "totalSize": 3,
                },
                {
                    "orderId": "call-1", "status": "Filled",
                    "filledQuantity": 3, "totalSize": 3,
                },
            ],
            path=path,
        )
        assert terminal["summary"]["active"] == 0
        assert terminal["summary"]["recent_filled"] == 1
        assert terminal["summary"]["recent_failed"] == 1
        assert terminal["reopen_item_ids"] == ["intent-1", "intent-2"]


def test_duplicate_coid_updates_existing_record():
    with tempfile.TemporaryDirectory() as tmp:
        path = Path(tmp) / "correlations.json"
        first = _ack("stock-1", symbol="AMD", coid="assay-stock")
        order_correlation.record_placements("DU1", _basket(), [first], path=path)
        second = _ack("stock-1", symbol="AMD", coid="assay-stock", status="PreSubmitted")
        order_correlation.record_placements("DU1", _basket(), [second], path=path)
        assert len(order_correlation.load_state(path)["records"]) == 1
