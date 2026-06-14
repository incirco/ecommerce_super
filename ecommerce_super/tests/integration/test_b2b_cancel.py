"""§11 Stage 2 — ERPNext-initiated cancellation tests.

Four scenarios:
  1. Cancel from Pushed → Map transitions to Cancelled, cancelled_at
     stamped, EE response stored.
  2. Cancel from Queued → same.
  3. Cancel from Cancelled → refused with packet message.
  4. Cancel from Invoice Generated → refused (post-invoice cancel is
     EE's responsibility, Phase 2 work).
  5. SO with no Map → refused.
"""

from __future__ import annotations

import unittest
from unittest.mock import MagicMock, patch

import frappe

from ecommerce_super.easyecom.flows.b2b_sales.cancel import (
    cancel_b2b_order_from_erpnext,
)


_CANCEL_RESPONSE_OK = {
    "code": 200,
    "message": "Successfully Cancelled the Order with reference_code SAL-ORD-T1",
    "data": [],
}


def _make_so(name="SAL-ORD-T1", map_name="ECS-B2B-SAL-ORD-T1"):
    so = MagicMock()
    so.name = name
    so.set_warehouse = "EE-WH-001"
    so.get = lambda k: {"ecs_b2b_order_map": map_name}.get(k)
    return so


def _make_map(status="Pushed", name="ECS-B2B-SAL-ORD-T1"):
    m = MagicMock()
    m.name = name
    m.status = status
    m.easyecom_account = "Harmony"
    return m


def _make_account(name="Harmony"):
    a = MagicMock()
    a.name = name
    return a


class TestCancelFromPushedStatus(unittest.TestCase):
    def test_pushed_status_cancels_successfully(self) -> None:
        so = _make_so()
        map_doc = _make_map(status="Pushed")
        client_post = MagicMock(return_value=_CANCEL_RESPONSE_OK)
        client_mock = MagicMock()
        client_mock.post = client_post

        captured_set_values: list[tuple] = []

        def _get_doc(doctype, name=None):
            if doctype == "Sales Order":
                return so
            if doctype == "EasyEcom B2B Order Map":
                return map_doc
            if doctype == "EasyEcom Account":
                return _make_account()
            return MagicMock()

        with (
            patch.object(frappe, "get_doc", side_effect=_get_doc),
            patch.object(
                frappe.db, "set_value",
                side_effect=lambda dt, n, f, v=None, **kw: captured_set_values.append(
                    (dt, n, dict(f) if isinstance(f, dict) else f, v)
                ),
            ),
            patch.object(frappe.db, "commit"),
            patch.object(frappe.utils, "now", return_value="2026-06-14 12:00:00"),
            patch.object(frappe, "as_json", side_effect=lambda x: str(x)),
            patch(
                "ecommerce_super.easyecom.flows.b2b_sales.cancel.EasyEcomClient",
                return_value=client_mock,
            ),
            patch(
                "ecommerce_super.easyecom.flows.b2b_sales.cancel.get_ee_location_for_warehouse",
                return_value=MagicMock(location_key="ee-loc-001"),
            ),
        ):
            result = cancel_b2b_order_from_erpnext(so.name)

        self.assertTrue(result["ok"])
        self.assertIn("Successfully Cancelled", result["ee_message"])
        self.assertEqual(client_post.call_args.kwargs["payload"], {"reference_code": so.name})

        # Map transitioned to Cancelled + cancelled_at stamped.
        # cancel.py uses the dict form of set_value.
        map_writes = [
            row[2] for row in captured_set_values
            if row[0] == "EasyEcom B2B Order Map" and isinstance(row[2], dict)
        ]
        self.assertEqual(len(map_writes), 1)
        updates = map_writes[0]
        self.assertEqual(updates["status"], "Cancelled")
        self.assertEqual(updates["cancelled_at"], "2026-06-14 12:00:00")


class TestCancelFromQueuedStatus(unittest.TestCase):
    def test_queued_status_cancels_successfully(self) -> None:
        so = _make_so()
        map_doc = _make_map(status="Queued")
        client_mock = MagicMock()
        client_mock.post = MagicMock(return_value=_CANCEL_RESPONSE_OK)

        def _get_doc(doctype, name=None):
            if doctype == "Sales Order":
                return so
            if doctype == "EasyEcom B2B Order Map":
                return map_doc
            if doctype == "EasyEcom Account":
                return _make_account()
            return MagicMock()

        with (
            patch.object(frappe, "get_doc", side_effect=_get_doc),
            patch.object(frappe.db, "set_value"),
            patch.object(frappe.db, "commit"),
            patch.object(frappe.utils, "now", return_value="2026-06-14 12:00:00"),
            patch.object(frappe, "as_json", side_effect=lambda x: str(x)),
            patch(
                "ecommerce_super.easyecom.flows.b2b_sales.cancel.EasyEcomClient",
                return_value=client_mock,
            ),
            patch(
                "ecommerce_super.easyecom.flows.b2b_sales.cancel.get_ee_location_for_warehouse",
                return_value=MagicMock(location_key="ee-loc-001"),
            ),
        ):
            result = cancel_b2b_order_from_erpnext(so.name)
        self.assertTrue(result["ok"])


class TestCancelFromCancelledRefuses(unittest.TestCase):
    def test_cancelled_status_refuses_with_packet_message(self) -> None:
        so = _make_so()
        map_doc = _make_map(status="Cancelled")

        def _get_doc(doctype, name=None):
            if doctype == "Sales Order":
                return so
            if doctype == "EasyEcom B2B Order Map":
                return map_doc
            return MagicMock()

        with (
            patch.object(frappe, "get_doc", side_effect=_get_doc),
            self.assertRaises(frappe.ValidationError) as exc_ctx,
        ):
            cancel_b2b_order_from_erpnext(so.name)
        msg = str(exc_ctx.exception)
        self.assertIn("Cannot cancel", msg)
        self.assertIn("Cancelled", msg)


class TestCancelFromInvoiceGeneratedRefuses(unittest.TestCase):
    def test_invoice_generated_refuses(self) -> None:
        """Post-invoice-generation cancel is Phase 2 work — use EE's
        cancellation flow."""
        so = _make_so()
        map_doc = _make_map(status="Invoice Generated")

        def _get_doc(doctype, name=None):
            if doctype == "Sales Order":
                return so
            if doctype == "EasyEcom B2B Order Map":
                return map_doc
            return MagicMock()

        with (
            patch.object(frappe, "get_doc", side_effect=_get_doc),
            self.assertRaises(frappe.ValidationError) as exc_ctx,
        ):
            cancel_b2b_order_from_erpnext(so.name)
        self.assertIn("Cannot cancel", str(exc_ctx.exception))


class TestCancelSOWithNoMapRefuses(unittest.TestCase):
    def test_no_map_refuses_with_packet_message(self) -> None:
        so = MagicMock()
        so.name = "SAL-ORD-NO-MAP"
        so.get = lambda k: None  # ecs_b2b_order_map empty

        with (
            patch.object(frappe, "get_doc", return_value=so),
            self.assertRaises(frappe.ValidationError) as exc_ctx,
        ):
            cancel_b2b_order_from_erpnext(so.name)
        # Frappe's str(exception) carries the msg only, not the title.
        self.assertIn(
            "has no §11 push to cancel", str(exc_ctx.exception)
        )
        self.assertIn("SAL-ORD-NO-MAP", str(exc_ctx.exception))


if __name__ == "__main__":
    unittest.main()
