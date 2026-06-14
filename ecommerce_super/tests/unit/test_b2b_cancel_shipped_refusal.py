"""§11 Stage 3 — Cancellation shipped-state refusal tests.

Per EE FAQ #32 / #41: cancelOrder is refused after Shipped status.
Stage 3 detects this via defensive substring match on the EE error
message, raises a specific Discrepancy kind so the FDE Worklist
surfaces the RTO-flow handoff.
"""

from __future__ import annotations

import unittest
from unittest.mock import MagicMock, patch

import frappe

from ecommerce_super.easyecom.flows.b2b_sales.cancel import (
    _looks_like_shipped_state_refusal,
    cancel_b2b_order_from_erpnext,
)


class TestShippedStateHeuristic(unittest.TestCase):
    """Substring matcher for EE's various phrasings of shipped-state
    refusal. Defensive — captures the known variants."""

    def test_matches_already_shipped(self) -> None:
        self.assertTrue(
            _looks_like_shipped_state_refusal(
                "Cannot cancel — order already shipped"
            )
        )

    def test_matches_past_cancel_window(self) -> None:
        self.assertTrue(
            _looks_like_shipped_state_refusal(
                "Order is past the cancel window"
            )
        )

    def test_matches_in_transit(self) -> None:
        self.assertTrue(
            _looks_like_shipped_state_refusal(
                "Order is in transit; use RTO"
            )
        )

    def test_matches_lowercase(self) -> None:
        self.assertTrue(
            _looks_like_shipped_state_refusal("ALREADY SHIPPED")
        )

    def test_does_not_match_unrelated_error(self) -> None:
        self.assertFalse(
            _looks_like_shipped_state_refusal("reference_code not found")
        )
        self.assertFalse(_looks_like_shipped_state_refusal(""))
        self.assertFalse(_looks_like_shipped_state_refusal(None))


class TestCancelShippedStateRefusalRaisesDiscrepancy(unittest.TestCase):
    """When EE returns a non-200 with shipped-state error message,
    cancel.py raises a specific Discrepancy kind for the FDE Worklist
    to surface."""

    def test_shipped_refusal_raises_correct_discrepancy_kind(
        self,
    ) -> None:
        so = MagicMock()
        so.name = "SAL-ORD-SHIP-001"
        so.set_warehouse = "EE-WH-001"
        so.get = lambda k: (
            "ECS-B2B-SAL-ORD-SHIP-001"
            if k == "ecs_b2b_order_map" else None
        )
        map_doc = MagicMock()
        map_doc.name = "ECS-B2B-SAL-ORD-SHIP-001"
        map_doc.status = "Pushed"
        map_doc.easyecom_account = "Harmony"
        map_doc.sales_order = "SAL-ORD-SHIP-001"

        ee_account = MagicMock()
        ee_account.name = "Harmony"

        # EE returns HTTP 200 with body code=400 "already shipped".
        client_mock = MagicMock()
        client_mock.post = MagicMock(
            return_value={
                "code": 400,
                "message": "Cannot cancel — order already shipped",
                "data": [],
            }
        )

        captured_discrepancies: list[dict] = []

        def _get_doc(doctype, name=None):
            if doctype == "Sales Order":
                return so
            if doctype == "EasyEcom B2B Order Map":
                return map_doc
            if doctype == "EasyEcom Account":
                return ee_account
            return MagicMock()

        with (
            patch.object(frappe, "get_doc", side_effect=_get_doc),
            patch.object(
                frappe.db,
                "get_value",
                return_value="_Test Company",
            ),
            patch.object(frappe.db, "set_value"),
            patch.object(frappe.db, "commit"),
            patch.object(frappe, "as_json", side_effect=lambda x: str(x)),
            patch(
                "ecommerce_super.easyecom.flows.b2b_sales.cancel.EasyEcomClient",
                return_value=client_mock,
            ),
            patch(
                "ecommerce_super.easyecom.flows.b2b_sales.cancel.get_ee_location_for_warehouse",
                return_value=MagicMock(location_key="ee-loc-001"),
            ),
            patch(
                "ecommerce_super.easyecom.flows.grn_pull._raise_discrepancy",
                side_effect=lambda **kw: (
                    captured_discrepancies.append(kw) or "ECS-DISC-S"
                ),
            ),
            self.assertRaises(frappe.ValidationError) as exc_ctx,
        ):
            cancel_b2b_order_from_erpnext(so.name)

        # Throw message indicates shipped-state refusal.
        self.assertIn(
            "already shipped or past the cancel window",
            str(exc_ctx.exception),
        )
        # Discrepancy raised with the shipped-specific kind.
        self.assertEqual(len(captured_discrepancies), 1)
        self.assertEqual(
            captured_discrepancies[0]["kind"],
            "B2B cancellation refused by EE — order already shipped "
            "or past cancel window",
        )

    def test_generic_refusal_raises_generic_discrepancy_kind(
        self,
    ) -> None:
        """Non-shipped EE refusal → still raises a Discrepancy but
        with the generic kind."""
        so = MagicMock()
        so.name = "SAL-ORD-GEN-001"
        so.set_warehouse = "EE-WH-001"
        so.get = lambda k: (
            "ECS-B2B-SAL-ORD-GEN-001"
            if k == "ecs_b2b_order_map" else None
        )
        map_doc = MagicMock()
        map_doc.name = "ECS-B2B-SAL-ORD-GEN-001"
        map_doc.status = "Pushed"
        map_doc.easyecom_account = "Harmony"
        map_doc.sales_order = "SAL-ORD-GEN-001"
        ee_account = MagicMock()
        ee_account.name = "Harmony"

        client_mock = MagicMock()
        client_mock.post = MagicMock(
            return_value={
                "code": 400,
                "message": "reference_code not found in EE",
                "data": [],
            }
        )
        captured_discrepancies: list[dict] = []

        def _get_doc(doctype, name=None):
            if doctype == "Sales Order":
                return so
            if doctype == "EasyEcom B2B Order Map":
                return map_doc
            if doctype == "EasyEcom Account":
                return ee_account
            return MagicMock()

        with (
            patch.object(frappe, "get_doc", side_effect=_get_doc),
            patch.object(
                frappe.db, "get_value", return_value="_Test Company"
            ),
            patch.object(frappe.db, "set_value"),
            patch.object(frappe.db, "commit"),
            patch.object(frappe, "as_json", side_effect=lambda x: str(x)),
            patch(
                "ecommerce_super.easyecom.flows.b2b_sales.cancel.EasyEcomClient",
                return_value=client_mock,
            ),
            patch(
                "ecommerce_super.easyecom.flows.b2b_sales.cancel.get_ee_location_for_warehouse",
                return_value=MagicMock(location_key="ee-loc-001"),
            ),
            patch(
                "ecommerce_super.easyecom.flows.grn_pull._raise_discrepancy",
                side_effect=lambda **kw: (
                    captured_discrepancies.append(kw) or "ECS-DISC-G"
                ),
            ),
            self.assertRaises(frappe.ValidationError),
        ):
            cancel_b2b_order_from_erpnext(so.name)

        self.assertEqual(len(captured_discrepancies), 1)
        self.assertEqual(
            captured_discrepancies[0]["kind"],
            "B2B cancellation refused by EE",
        )


if __name__ == "__main__":
    unittest.main()
