"""§11 Phase 1 — master_resolution helpers (Item Map + Customer Map).

These helpers replace the §11 packet's reference to
`item.ecs_easyecom_product_sku_code` and
`customer.ecs_easyecom_customer_id` Custom Fields that don't exist
on this codebase. Instead the resolvers query the §8d / §8e Map
DocTypes, which are the actual source of truth for EE identifiers.

Test contract: mapped → returns the value; unmapped → returns None;
non-Mapped status (Pending / Flagged / Drift) → returns None.
"""

from __future__ import annotations

import unittest
from unittest.mock import MagicMock, patch

import frappe

from ecommerce_super.easyecom.helpers.master_resolution import (
    resolve_ee_customer_id,
    resolve_ee_sku,
)


class TestResolveEeSku(unittest.TestCase):
    def test_returns_ee_sku_when_item_map_status_is_mapped(self) -> None:
        with patch.object(
            frappe.db, "get_value", return_value="EE-SKU-123"
        ) as get_value:
            self.assertEqual(resolve_ee_sku("FB-WIDGET-001"), "EE-SKU-123")
            get_value.assert_called_once_with(
                "EasyEcom Item Map",
                {
                    "erpnext_doctype": "Item",
                    "erpnext_name": "FB-WIDGET-001",
                    "status": "Mapped",
                },
                "ee_sku",
            )

    def test_returns_none_when_no_item_map(self) -> None:
        with patch.object(frappe.db, "get_value", return_value=None):
            self.assertIsNone(resolve_ee_sku("UNMAPPED-ITEM"))

    def test_returns_none_when_status_not_mapped(self) -> None:
        """A Pending / Flagged / Drift Item Map shouldn't satisfy
        the resolver — the filter is status=Mapped specifically."""
        # The filter is enforced by the SQL — if no Mapped row exists,
        # get_value returns None.
        with patch.object(frappe.db, "get_value", return_value=None):
            self.assertIsNone(resolve_ee_sku("PENDING-MAP-ITEM"))

    def test_returns_none_when_item_code_empty(self) -> None:
        """Empty/None inputs short-circuit without a DB call —
        callers should be able to ask 'is this synced?' without
        pre-validating."""
        with patch.object(frappe.db, "get_value") as get_value:
            self.assertIsNone(resolve_ee_sku(""))
            self.assertIsNone(resolve_ee_sku(None))
            get_value.assert_not_called()


class TestResolveEeCustomerId(unittest.TestCase):
    def test_returns_ee_customer_id_when_customer_map_status_is_mapped(
        self,
    ) -> None:
        with patch.object(
            frappe.db, "get_value", return_value="EE-CUST-9001"
        ) as get_value:
            self.assertEqual(
                resolve_ee_customer_id("ACME Industries"), "EE-CUST-9001"
            )
            get_value.assert_called_once_with(
                "EasyEcom Customer Map",
                {
                    "erpnext_doctype": "Customer",
                    "erpnext_name": "ACME Industries",
                    "status": "Mapped",
                },
                "ee_customer_id",
            )

    def test_returns_none_when_no_customer_map(self) -> None:
        with patch.object(frappe.db, "get_value", return_value=None):
            self.assertIsNone(resolve_ee_customer_id("UNMAPPED-CUST"))

    def test_returns_none_when_customer_empty(self) -> None:
        with patch.object(frappe.db, "get_value") as get_value:
            self.assertIsNone(resolve_ee_customer_id(""))
            self.assertIsNone(resolve_ee_customer_id(None))
            get_value.assert_not_called()


if __name__ == "__main__":
    unittest.main()
