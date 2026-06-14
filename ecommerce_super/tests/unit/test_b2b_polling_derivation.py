"""§11 Stage 3 — Locked status derivation function tests.

The derive_local_status_from_ee_rows function is the heart of §11
polling. Its rule table is locked by design-lead 2026-06-14 per EE's
documented order_status_id enum + FAQ #23 (per-suborder partial
cancellation).

These tests freeze the contract:
  ("orphan", None)                   — no businessorder rows
  ("transition_to", "Cancelled")     — all rows status=9 AND all qty cancelled
  ("partial_cancel", {...})          — some qty cancelled, not all
  ("transition_to", "Invoice Pending") — any row has invoice_number
  ("no_change", None)                — latest row in {1,2,3,4,5,6,7,30}
  ("unknown", {...})                 — status_id outside known set

Plus the two multi-row semantics:
  - State-change history: same order_id, multiple rows for state
    progression.
  - Shipment splits: same reference_code + order_id, separate
    invoice_ids per fulfillment chunk.
"""

from __future__ import annotations

import unittest
from unittest.mock import MagicMock

from ecommerce_super.easyecom.flows.b2b_sales.polling import (
    derive_local_status_from_ee_rows,
)


def _row(
    *,
    order_status_id: int | None = 2,
    invoice_number: str | None = None,
    invoice_id: int | None = None,
    last_update_date: str = "2026-06-08 18:00:00",
    order_type_key: str = "businessorder",
    suborders: list | None = None,
):
    """Make a single getOrderDetails row mock."""
    return {
        "order_status_id": order_status_id,
        "invoice_number": invoice_number,
        "invoice_id": invoice_id,
        "last_update_date": last_update_date,
        "order_type_key": order_type_key,
        "suborders": suborders or [],
    }


def _suborder(item_quantity: int, cancelled_quantity: int = 0):
    return {
        "item_quantity": item_quantity,
        "cancelled_quantity": cancelled_quantity,
    }


def _local_map(status: str = "Pushed"):
    m = MagicMock()
    m.status = status
    return m


class TestOrphanCase(unittest.TestCase):
    def test_empty_rows_is_orphan(self) -> None:
        decision, payload = derive_local_status_from_ee_rows(
            _local_map(), []
        )
        self.assertEqual(decision, "orphan")
        self.assertIsNone(payload)

    def test_only_non_b2b_rows_is_orphan(self) -> None:
        """B2C / retail rows filtered out → no businessorder rows →
        orphan."""
        rows = [_row(order_type_key="retailorder")]
        decision, _ = derive_local_status_from_ee_rows(_local_map(), rows)
        self.assertEqual(decision, "orphan")


class TestFullCancellation(unittest.TestCase):
    def test_single_row_all_status_9_all_qty_cancelled(self) -> None:
        rows = [
            _row(
                order_status_id=9,
                suborders=[_suborder(5, 5)],
            )
        ]
        decision, payload = derive_local_status_from_ee_rows(
            _local_map(), rows
        )
        self.assertEqual(decision, "transition_to")
        self.assertEqual(payload, "Cancelled")

    def test_multi_row_history_all_status_9(self) -> None:
        """State-change history: EE adds a row per state change. If
        ALL rows show status=9 + all qty cancelled, it's a full
        cancellation."""
        rows = [
            _row(
                order_status_id=9,
                last_update_date="2026-06-08 18:00:00",
                suborders=[_suborder(5, 5)],
            ),
            _row(
                order_status_id=9,
                last_update_date="2026-06-08 19:00:00",
                suborders=[_suborder(5, 5)],
            ),
        ]
        decision, payload = derive_local_status_from_ee_rows(
            _local_map(), rows
        )
        self.assertEqual(decision, "transition_to")
        self.assertEqual(payload, "Cancelled")

    def test_status_9_but_qty_not_all_cancelled_is_NOT_full_cancel(
        self,
    ) -> None:
        """Defensive: a status_id=9 row with un-cancelled qty is a
        half-cancelled shipment split — not full cancellation.
        Falls through to partial_cancel."""
        rows = [
            _row(
                order_status_id=9,
                suborders=[_suborder(5, 2)],
            )
        ]
        decision, _ = derive_local_status_from_ee_rows(_local_map(), rows)
        self.assertNotEqual(decision, "transition_to")
        self.assertEqual(decision, "partial_cancel")


class TestPartialCancellation(unittest.TestCase):
    def test_some_qty_cancelled_not_all(self) -> None:
        rows = [
            _row(
                order_status_id=2,
                suborders=[
                    _suborder(5, 2),
                    _suborder(3, 0),
                ],
            )
        ]
        decision, payload = derive_local_status_from_ee_rows(
            _local_map(), rows
        )
        self.assertEqual(decision, "partial_cancel")
        self.assertEqual(payload["total_item_qty"], 8)
        self.assertEqual(payload["cancelled_qty"], 2)
        self.assertEqual(payload["cancelled_pct"], 25.0)

    def test_partial_cancel_across_shipment_splits(self) -> None:
        """Shipment-split semantic: 6 invoice rows, partial cancel on
        one suborder somewhere — aggregate qty math identifies it."""
        rows = [
            _row(
                order_status_id=5,
                invoice_id=i,
                suborders=[_suborder(2, 0)],
            )
            for i in range(1, 7)
        ]
        # Now mark one suborder in the 4th row as cancelled.
        rows[3]["suborders"][0]["cancelled_quantity"] = 2
        decision, payload = derive_local_status_from_ee_rows(
            _local_map(), rows
        )
        self.assertEqual(decision, "partial_cancel")
        # 12 total, 2 cancelled across the 6 splits.
        self.assertEqual(payload["total_item_qty"], 12)
        self.assertEqual(payload["cancelled_qty"], 2)


class TestInvoiceGeneration(unittest.TestCase):
    def test_single_row_with_invoice_number(self) -> None:
        rows = [
            _row(
                order_status_id=5,
                invoice_number="INV-2026-0001",
                suborders=[_suborder(5, 0)],
            )
        ]
        decision, payload = derive_local_status_from_ee_rows(
            _local_map(), rows
        )
        self.assertEqual(decision, "transition_to")
        self.assertEqual(payload, "Invoice Pending")

    def test_invoice_on_any_row_triggers(self) -> None:
        """Across shipment splits, an invoice_number on ANY row
        triggers Invoice Pending transition."""
        rows = [
            _row(invoice_number=None, suborders=[_suborder(2, 0)]),
            _row(invoice_number="INV-CHUNK-2", suborders=[_suborder(2, 0)]),
            _row(invoice_number=None, suborders=[_suborder(2, 0)]),
        ]
        decision, payload = derive_local_status_from_ee_rows(
            _local_map(), rows
        )
        self.assertEqual(decision, "transition_to")
        self.assertEqual(payload, "Invoice Pending")


class TestNoChange(unittest.TestCase):
    def test_active_status_no_invoice_no_cancel(self) -> None:
        """Plain active order, no signal to act on."""
        rows = [
            _row(
                order_status_id=2,
                suborders=[_suborder(5, 0)],
            )
        ]
        decision, payload = derive_local_status_from_ee_rows(
            _local_map(), rows
        )
        self.assertEqual(decision, "no_change")
        self.assertIsNone(payload)

    def test_each_known_active_status_id(self) -> None:
        """Every status_id in {1,2,3,4,5,6,7,30} is a no_change."""
        for status_id in (1, 2, 3, 4, 5, 6, 7, 30):
            with self.subTest(status_id=status_id):
                rows = [
                    _row(
                        order_status_id=status_id,
                        suborders=[_suborder(5, 0)],
                    )
                ]
                decision, _ = derive_local_status_from_ee_rows(
                    _local_map(), rows
                )
                self.assertEqual(decision, "no_change")

    def test_latest_row_decides_for_multi_row(self) -> None:
        """When multiple rows have different active statuses, the
        latest-by-last_update_date wins."""
        rows = [
            _row(
                order_status_id=2,
                last_update_date="2026-06-08 10:00:00",
                suborders=[_suborder(5, 0)],
            ),
            _row(
                order_status_id=6,
                last_update_date="2026-06-08 18:00:00",
                suborders=[_suborder(5, 0)],
            ),
        ]
        decision, _ = derive_local_status_from_ee_rows(_local_map(), rows)
        self.assertEqual(decision, "no_change")


class TestUnknownStatus(unittest.TestCase):
    def test_status_id_outside_enum_is_unknown(self) -> None:
        rows = [
            _row(
                order_status_id=999,
                invoice_id=12345,
                suborders=[_suborder(5, 0)],
            )
        ]
        decision, payload = derive_local_status_from_ee_rows(
            _local_map(), rows
        )
        self.assertEqual(decision, "unknown")
        self.assertEqual(payload["status_id"], 999)
        self.assertEqual(payload["latest_row_invoice_id"], 12345)

    def test_null_status_id_is_unknown(self) -> None:
        rows = [
            _row(
                order_status_id=None,
                suborders=[_suborder(5, 0)],
            )
        ]
        decision, payload = derive_local_status_from_ee_rows(
            _local_map(), rows
        )
        self.assertEqual(decision, "unknown")


class TestPrecedence(unittest.TestCase):
    """Decision precedence: orphan → cancelled → partial → invoice →
    no_change → unknown. Verify ordering when multiple signals exist."""

    def test_full_cancel_wins_over_invoice(self) -> None:
        """A row with status=9 + all qty cancelled + invoice_number
        (e.g., EE generated an invoice then cancelled). Cancellation
        should win — the local Map needs to go to Cancelled, NOT
        Invoice Pending."""
        rows = [
            _row(
                order_status_id=9,
                invoice_number="INV-CANCELLED-001",
                suborders=[_suborder(5, 5)],
            )
        ]
        decision, payload = derive_local_status_from_ee_rows(
            _local_map(), rows
        )
        self.assertEqual(decision, "transition_to")
        self.assertEqual(payload, "Cancelled")

    def test_partial_cancel_wins_over_invoice(self) -> None:
        """Partial cancel + invoice on another row → partial_cancel
        Discrepancy (Phase 2 territory; no local transition)."""
        rows = [
            _row(
                order_status_id=5,
                invoice_number="INV-PARTIAL",
                suborders=[_suborder(5, 2)],
            )
        ]
        decision, _ = derive_local_status_from_ee_rows(_local_map(), rows)
        self.assertEqual(decision, "partial_cancel")


if __name__ == "__main__":
    unittest.main()
