"""§11 Phase 1 polling — ID backfill (grounded 2026-06-28).

The bug: New B2B push returns "Successfully Queued" with no IDs in
the response body. The Map row is created with empty
ee_order_id / ee_suborder_id / ee_invoice_id. Phase 1 polling
derivation focused on status transitions (Cancelled / Invoice
Pending / partial-cancel) and silently skipped ID backfill when no
status transition was needed — so the Map sat with null IDs until
status changed, which broke the FDE worklist surface
(`New B2B orders missing IDs (2h+)` would fire indefinitely on
healthy orders just waiting in their Pushed/Queued state).

Surfaced live during the Thuraya end-to-end smoke for
SAL-ORD-2026-00022 (2026-06-28). EE getOrderDetails returned the
real OrderID/SuborderID/InvoiceID but our polling derivation
returned "no_change" and left the Map row's IDs as null.

Fix: a new `_backfill_ee_ids_if_missing` runs before derivation —
inspects the EE response, writes missing OrderID/SuborderID/InvoiceID
back onto the Map row. Idempotent (only writes when local is null).
"""
from __future__ import annotations

import unittest
from unittest.mock import MagicMock, patch

from ecommerce_super.easyecom.flows.b2b_sales.polling import (
    _backfill_ee_ids_if_missing,
)


def _fake_map(*, name="ECS-B2B-FAKE", ee_order_id=None,
              ee_suborder_id=None, ee_invoice_id=None):
    m = MagicMock()
    m.name = name
    m.ee_order_id = ee_order_id
    m.ee_suborder_id = ee_suborder_id
    m.ee_invoice_id = ee_invoice_id
    return m


def _b2b_row(*, order_id=None, invoice_id=None, suborder_id=None,
             last_update="2026-06-28 14:00:00"):
    row = {
        "order_type_key": "businessorder",
        "last_update_date": last_update,
    }
    if order_id is not None:
        row["order_id"] = order_id
    if invoice_id is not None:
        row["invoice_id"] = invoice_id
    if suborder_id is not None:
        row["order_items"] = [{"suborder_id": suborder_id}]
    return row


class TestBackfillEeIds(unittest.TestCase):

    def test_backfills_all_three_ids_when_map_empty(self) -> None:
        """The headline scenario: New B2B Map sitting with null IDs
        after push; first polling tick fills them in."""
        map_doc = _fake_map()
        rows = [_b2b_row(order_id=561435048, invoice_id=657806781, suborder_id=864797685)]

        with patch("frappe.db.set_value") as set_value, patch("frappe.db.commit"):
            result = _backfill_ee_ids_if_missing(map_doc, rows)

        self.assertEqual(result, {
            "ee_order_id": "561435048",
            "ee_suborder_id": "864797685",
            "ee_invoice_id": "657806781",
        })
        set_value.assert_called_once()
        # In-memory map_doc updated so downstream derivation sees the IDs.
        self.assertEqual(map_doc.ee_order_id, "561435048")
        self.assertEqual(map_doc.ee_suborder_id, "864797685")
        self.assertEqual(map_doc.ee_invoice_id, "657806781")

    def test_returns_none_when_all_ids_already_set(self) -> None:
        """If Map already has IDs (Old B2B captured them at push time),
        the backfill is a no-op."""
        map_doc = _fake_map(
            ee_order_id="561378302",
            ee_suborder_id="864720907",
            ee_invoice_id="657745697",
        )
        rows = [_b2b_row(order_id=561378302, invoice_id=657745697, suborder_id=864720907)]

        with patch("frappe.db.set_value") as set_value:
            result = _backfill_ee_ids_if_missing(map_doc, rows)

        self.assertIsNone(result)
        set_value.assert_not_called()

    def test_partial_backfill_fills_only_missing_fields(self) -> None:
        """If some IDs are set and others null, fill only the nulls."""
        map_doc = _fake_map(ee_order_id="561435048")
        rows = [_b2b_row(order_id=561435048, invoice_id=657806781, suborder_id=864797685)]

        with patch("frappe.db.set_value") as set_value, patch("frappe.db.commit"):
            result = _backfill_ee_ids_if_missing(map_doc, rows)

        # Only the two that were null get filled.
        self.assertEqual(result, {
            "ee_suborder_id": "864797685",
            "ee_invoice_id": "657806781",
        })
        self.assertNotIn("ee_order_id", result)

    def test_no_b2b_rows_returns_none(self) -> None:
        """EE returned rows but none are businessorder — defensive
        path (e.g. stale data, wrong order_type_key)."""
        map_doc = _fake_map()
        rows = [{"order_type_key": "stocktransferorder", "order_id": 999}]

        with patch("frappe.db.set_value") as set_value:
            result = _backfill_ee_ids_if_missing(map_doc, rows)

        self.assertIsNone(result)
        set_value.assert_not_called()

    def test_picks_latest_row_for_multi_shipment_splits(self) -> None:
        """Multi-shipment scenarios produce multiple rows under the
        same reference_code. Backfill anchors on the latest by
        last_update_date — the most recent invoice."""
        map_doc = _fake_map()
        rows = [
            _b2b_row(order_id=100, invoice_id=200, suborder_id=300,
                     last_update="2026-06-28 10:00:00"),
            _b2b_row(order_id=100, invoice_id=999, suborder_id=888,
                     last_update="2026-06-28 14:00:00"),  # newer
            _b2b_row(order_id=100, invoice_id=500, suborder_id=600,
                     last_update="2026-06-28 12:00:00"),
        ]

        with patch("frappe.db.set_value"), patch("frappe.db.commit"):
            result = _backfill_ee_ids_if_missing(map_doc, rows)

        # All three IDs come from the latest row.
        self.assertEqual(result["ee_order_id"], "100")
        self.assertEqual(result["ee_invoice_id"], "999")
        self.assertEqual(result["ee_suborder_id"], "888")

    def test_empty_order_items_skips_suborder_id(self) -> None:
        """Defensive: row has order_id/invoice_id but no order_items
        array. Fill what's there, skip suborder."""
        map_doc = _fake_map()
        rows = [{
            "order_type_key": "businessorder",
            "order_id": 12345,
            "invoice_id": 67890,
            "last_update_date": "2026-06-28 14:00:00",
            "order_items": [],
        }]

        with patch("frappe.db.set_value"), patch("frappe.db.commit"):
            result = _backfill_ee_ids_if_missing(map_doc, rows)

        self.assertEqual(result, {
            "ee_order_id": "12345",
            "ee_invoice_id": "67890",
        })
        self.assertNotIn("ee_suborder_id", result)


if __name__ == "__main__":
    unittest.main()
