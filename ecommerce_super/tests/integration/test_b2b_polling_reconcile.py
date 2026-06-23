"""§11 Stage 3 — Polling reconciliation integration tests.

End-to-end state-propagation tests for reconcile_one_map. Each test
mocks the EE getOrderDetails response (no real HTTP) but asserts the
LOCAL state propagates correctly:
  - Map row transitions
  - Discrepancies created (with kind matching the FDE Worklist filters)
  - last_polled_at stamped (success AND failure paths)

§10 SI back-link lesson: assert state-as-persisted-to-DB, not just
that the code path was reached.
"""

from __future__ import annotations

import unittest
from unittest.mock import MagicMock, patch

import frappe

from ecommerce_super.easyecom.flows.b2b_sales.polling import (
    reconcile_one_map,
)


def _make_map_doc(
    name="ECS-B2B-SAL-ORD-001",
    sales_order="SAL-ORD-001",
    status="Queued",
    easyecom_account="Harmony",
):
    m = MagicMock()
    m.name = name
    m.sales_order = sales_order
    m.status = status
    m.easyecom_account = easyecom_account
    return m


def _ee_response(rows):
    """getOrderDetails response shape — `data` is a list."""
    return {
        "code": 200,
        "message": "Successful",
        "data": rows,
    }


def _b2b_row(
    order_status_id=2,
    invoice_number=None,
    invoice_id=12345,
    last_update_date="2026-06-08 18:00:00",
    order_items=None,
):
    return {
        "order_type_key": "businessorder",
        "order_status_id": order_status_id,
        "invoice_number": invoice_number,
        "invoice_id": invoice_id,
        "last_update_date": last_update_date,
        "order_items": order_items
        or [{"item_quantity": 5, "cancelled_quantity": 0}],
    }


class TestReconcileOnePersistsCancelledTransition(unittest.TestCase):
    def test_cancelled_transitions_map_and_raises_discrepancy(self) -> None:
        map_doc = _make_map_doc(status="Pushed")
        rows = [
            _b2b_row(
                order_status_id=9,
                order_items=[
                    {"item_quantity": 5, "cancelled_quantity": 5}
                ],
            )
        ]

        captured_set_values: list[tuple] = []
        captured_discrepancies: list[dict] = []

        def _set_value(dt, name, f, v=None, **kw):
            captured_set_values.append(
                (
                    dt,
                    name,
                    dict(f) if isinstance(f, dict) else f,
                    v,
                )
            )

        def _raise_disc(**kwargs):
            captured_discrepancies.append(kwargs)
            return "ECS-DISC-T1"

        client_mock = MagicMock()
        client_mock.get = MagicMock(return_value=_ee_response(rows))

        with (
            patch.object(
                frappe, "get_doc", return_value=map_doc
            ),
            patch.object(
                frappe.db,
                "get_value",
                side_effect=lambda dt, name, field, **kw: (
                    "EE-WH-001"
                    if dt == "Sales Order" and field == "set_warehouse"
                    else (
                        "_Test Company"
                        if dt == "Sales Order" and field == "company"
                        else None
                    )
                ),
            ),
            patch.object(
                frappe.db, "set_value", side_effect=_set_value
            ),
            patch.object(frappe.db, "commit"),
            patch(
                "ecommerce_super.easyecom.flows.b2b_sales.polling.EasyEcomClient",
                return_value=client_mock,
            ),
            patch(
                "ecommerce_super.easyecom.flows.b2b_sales.polling.get_ee_location_for_warehouse",
                return_value=MagicMock(location_key="ee-loc-001"),
            ),
            patch(
                "ecommerce_super.easyecom.flows.grn_pull._raise_discrepancy",
                side_effect=_raise_disc,
            ),
            patch(
                "ecommerce_super.easyecom.flows.b2b_sales.polling.now_datetime",
                return_value="2026-06-14 10:00:00",
            ),
        ):
            outcome = reconcile_one_map(map_doc.name)

        # Outcome reports the transition.
        self.assertTrue(outcome["transitioned"])
        self.assertEqual(outcome["decision"], "cancelled")

        # Map.status transitioned to Cancelled via set_value (dict form).
        cancel_writes = [
            row[2] for row in captured_set_values
            if row[0] == "EasyEcom B2B Order Map"
            and isinstance(row[2], dict)
            and row[2].get("status") == "Cancelled"
        ]
        self.assertEqual(len(cancel_writes), 1)
        self.assertIn("cancelled_at", cancel_writes[0])

        # last_polled_at also stamped (single-field form).
        polled_writes = [
            row for row in captured_set_values
            if row[0] == "EasyEcom B2B Order Map"
            and row[2] == "last_polled_at"
        ]
        self.assertEqual(len(polled_writes), 1)

        # Discrepancy raised with the correct kind string.
        self.assertEqual(len(captured_discrepancies), 1)
        d = captured_discrepancies[0]
        self.assertEqual(
            d["kind"], "B2B order cancelled by EE — polling-detected"
        )
        self.assertEqual(
            d["reference_doctype"], "EasyEcom B2B Order Map"
        )
        self.assertEqual(d["reference_name"], "ECS-B2B-SAL-ORD-001")


class TestReconcileOneInvoicePending(unittest.TestCase):
    def test_invoice_number_transitions_to_invoice_pending(self) -> None:
        map_doc = _make_map_doc(status="Pushed")
        rows = [_b2b_row(invoice_number="INV-2026-001")]

        captured_set_values: list[tuple] = []
        captured_discrepancies: list[dict] = []

        client_mock = MagicMock()
        client_mock.get = MagicMock(return_value=_ee_response(rows))

        with (
            patch.object(frappe, "get_doc", return_value=map_doc),
            patch.object(
                frappe.db,
                "get_value",
                side_effect=lambda dt, name, field, **kw: (
                    "EE-WH-001"
                    if dt == "Sales Order" and field == "set_warehouse"
                    else "_Test Company"
                ),
            ),
            patch.object(
                frappe.db,
                "set_value",
                side_effect=lambda dt, n, f, v=None, **kw: captured_set_values.append(
                    (dt, n, dict(f) if isinstance(f, dict) else f, v)
                ),
            ),
            patch.object(frappe.db, "commit"),
            patch(
                "ecommerce_super.easyecom.flows.b2b_sales.polling.EasyEcomClient",
                return_value=client_mock,
            ),
            patch(
                "ecommerce_super.easyecom.flows.b2b_sales.polling.get_ee_location_for_warehouse",
                return_value=MagicMock(location_key="ee-loc-001"),
            ),
            patch(
                "ecommerce_super.easyecom.flows.grn_pull._raise_discrepancy",
                side_effect=lambda **kw: captured_discrepancies.append(kw),
            ),
            patch(
                "ecommerce_super.easyecom.flows.b2b_sales.polling.now_datetime",
                return_value="2026-06-14 10:00:00",
            ),
        ):
            outcome = reconcile_one_map(map_doc.name)

        self.assertTrue(outcome["transitioned"])
        self.assertEqual(outcome["decision"], "invoice_pending")
        # No Discrepancy on this path — Phase 2 marker only.
        self.assertEqual(captured_discrepancies, [])
        invoice_writes = [
            row[2] for row in captured_set_values
            if row[0] == "EasyEcom B2B Order Map"
            and isinstance(row[2], dict)
            and row[2].get("status") == "Invoice Pending"
        ]
        self.assertEqual(len(invoice_writes), 1)


class TestReconcileOneOrphan(unittest.TestCase):
    def test_empty_response_raises_orphan_discrepancy(self) -> None:
        map_doc = _make_map_doc()
        captured_discrepancies: list[dict] = []

        client_mock = MagicMock()
        client_mock.get = MagicMock(return_value=_ee_response([]))

        with (
            patch.object(frappe, "get_doc", return_value=map_doc),
            patch.object(
                frappe.db,
                "get_value",
                side_effect=lambda dt, n, f, **kw: (
                    "EE-WH-001"
                    if f == "set_warehouse" else "_Test Company"
                ),
            ),
            patch.object(frappe.db, "set_value"),
            patch.object(frappe.db, "commit"),
            patch(
                "ecommerce_super.easyecom.flows.b2b_sales.polling.EasyEcomClient",
                return_value=client_mock,
            ),
            patch(
                "ecommerce_super.easyecom.flows.b2b_sales.polling.get_ee_location_for_warehouse",
                return_value=MagicMock(location_key="ee-loc-001"),
            ),
            patch(
                "ecommerce_super.easyecom.flows.grn_pull._raise_discrepancy",
                side_effect=lambda **kw: (
                    captured_discrepancies.append(kw) or "ECS-DISC-O"
                ),
            ),
            patch(
                "ecommerce_super.easyecom.flows.b2b_sales.polling.now_datetime",
                return_value="2026-06-14 10:00:00",
            ),
        ):
            outcome = reconcile_one_map(map_doc.name)

        self.assertFalse(outcome["transitioned"])
        self.assertEqual(outcome["decision"], "orphan")
        self.assertEqual(len(captured_discrepancies), 1)
        self.assertEqual(
            captured_discrepancies[0]["kind"], "B2B Map orphaned at EE"
        )


class TestReconcileOnePartialCancel(unittest.TestCase):
    def test_partial_cancel_raises_discrepancy_no_transition(self) -> None:
        map_doc = _make_map_doc(status="Pushed")
        rows = [
            _b2b_row(
                order_status_id=2,
                order_items=[
                    {"item_quantity": 5, "cancelled_quantity": 2}
                ],
            )
        ]
        captured_set_values: list[tuple] = []
        captured_discrepancies: list[dict] = []

        client_mock = MagicMock()
        client_mock.get = MagicMock(return_value=_ee_response(rows))

        with (
            patch.object(frappe, "get_doc", return_value=map_doc),
            patch.object(
                frappe.db,
                "get_value",
                side_effect=lambda dt, n, f, **kw: (
                    "EE-WH-001"
                    if f == "set_warehouse" else "_Test Company"
                ),
            ),
            patch.object(
                frappe.db,
                "set_value",
                side_effect=lambda dt, n, f, v=None, **kw: captured_set_values.append(
                    (dt, n, dict(f) if isinstance(f, dict) else f, v)
                ),
            ),
            patch.object(frappe.db, "commit"),
            patch(
                "ecommerce_super.easyecom.flows.b2b_sales.polling.EasyEcomClient",
                return_value=client_mock,
            ),
            patch(
                "ecommerce_super.easyecom.flows.b2b_sales.polling.get_ee_location_for_warehouse",
                return_value=MagicMock(location_key="ee-loc-001"),
            ),
            patch(
                "ecommerce_super.easyecom.flows.grn_pull._raise_discrepancy",
                side_effect=lambda **kw: (
                    captured_discrepancies.append(kw) or "ECS-DISC-P"
                ),
            ),
            patch(
                "ecommerce_super.easyecom.flows.b2b_sales.polling.now_datetime",
                return_value="2026-06-14 10:00:00",
            ),
        ):
            outcome = reconcile_one_map(map_doc.name)

        self.assertFalse(outcome["transitioned"])
        self.assertEqual(outcome["decision"], "partial_cancel")
        # No status change on partial — Phase 2 territory.
        status_changes = [
            row[2] for row in captured_set_values
            if row[0] == "EasyEcom B2B Order Map"
            and isinstance(row[2], dict)
            and "status" in row[2]
        ]
        self.assertEqual(status_changes, [])
        # Discrepancy with the partial kind.
        self.assertEqual(len(captured_discrepancies), 1)
        self.assertEqual(
            captured_discrepancies[0]["kind"],
            "B2B order partial cancellation detected",
        )
        # Reason contains the qty math.
        reason = captured_discrepancies[0]["reason"]
        self.assertIn("total_item_qty=5", reason)
        self.assertIn("cancelled_qty=2", reason)


class TestReconcileOneUnknownStatus(unittest.TestCase):
    def test_unknown_status_pickles_payload_to_reason(self) -> None:
        map_doc = _make_map_doc()
        rows = [_b2b_row(order_status_id=999, invoice_id=42)]
        captured_discrepancies: list[dict] = []

        client_mock = MagicMock()
        client_mock.get = MagicMock(return_value=_ee_response(rows))

        with (
            patch.object(frappe, "get_doc", return_value=map_doc),
            patch.object(
                frappe.db,
                "get_value",
                side_effect=lambda dt, n, f, **kw: (
                    "EE-WH-001"
                    if f == "set_warehouse" else "_Test Company"
                ),
            ),
            patch.object(frappe.db, "set_value"),
            patch.object(frappe.db, "commit"),
            patch(
                "ecommerce_super.easyecom.flows.b2b_sales.polling.EasyEcomClient",
                return_value=client_mock,
            ),
            patch(
                "ecommerce_super.easyecom.flows.b2b_sales.polling.get_ee_location_for_warehouse",
                return_value=MagicMock(location_key="ee-loc-001"),
            ),
            patch(
                "ecommerce_super.easyecom.flows.grn_pull._raise_discrepancy",
                side_effect=lambda **kw: (
                    captured_discrepancies.append(kw) or "ECS-DISC-U"
                ),
            ),
            patch(
                "ecommerce_super.easyecom.flows.b2b_sales.polling.now_datetime",
                return_value="2026-06-14 10:00:00",
            ),
        ):
            reconcile_one_map(map_doc.name)

        self.assertEqual(len(captured_discrepancies), 1)
        d = captured_discrepancies[0]
        self.assertEqual(d["kind"], "B2B unknown order_status_id")
        self.assertIn("999", d["reason"])  # The unknown status_id
        self.assertIn("42", d["reason"])  # latest_row_invoice_id
        # Payload pickled to reason for forensic visibility.
        self.assertIn("order_type_key", d["reason"])


if __name__ == "__main__":
    unittest.main()
