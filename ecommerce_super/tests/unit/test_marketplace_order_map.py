"""Unit tests for EasyEcom Marketplace Order Map.

Covers the controller's two responsibilities:
  1. Composite uniqueness enforcement on (marketplace, marketplace_order_id, company)
  2. Auto-recomputation of total_shipments from the child table

These are pure-Python tests — they exercise validate() logic without
booting Frappe. The full end-to-end test (insert → assert row exists →
add child → assert total_shipments updates) runs in the integration
tier against a live site.
"""
from __future__ import annotations

import unittest
from unittest.mock import MagicMock, patch

from ecommerce_super.easyecom.doctype.easyecom_marketplace_order_map import (
    easyecom_marketplace_order_map as mod,
)


def _mock_map(**overrides):
    m = MagicMock(spec=mod.EasyEcomMarketplaceOrderMap)
    m.name = None
    m.marketplace = "Shopify"
    m.marketplace_order_id = "SQ-440390821"
    m.company = "Puresta Lifestyle Private Limited"
    m.shipments = []
    m.total_shipments = 0
    # is_new returns True when name is unset (mirrors Frappe behaviour)
    m.is_new = MagicMock(return_value=True)
    m.get_doc_before_save = MagicMock(return_value=None)
    for k, v in overrides.items():
        setattr(m, k, v)
    return m


class TestRecomputeTotalShipments(unittest.TestCase):

    def test_zero_shipments_when_child_empty(self):
        doc = _mock_map(shipments=[])
        mod.EasyEcomMarketplaceOrderMap._recompute_total_shipments(doc)
        self.assertEqual(doc.total_shipments, 0)

    def test_matches_child_row_count(self):
        doc = _mock_map(shipments=[MagicMock(), MagicMock(), MagicMock()])
        mod.EasyEcomMarketplaceOrderMap._recompute_total_shipments(doc)
        self.assertEqual(doc.total_shipments, 3)

    def test_handles_none_shipments_gracefully(self):
        doc = _mock_map(shipments=None)
        mod.EasyEcomMarketplaceOrderMap._recompute_total_shipments(doc)
        self.assertEqual(doc.total_shipments, 0)


class TestCompositeUniqueness(unittest.TestCase):

    @patch.object(mod.frappe.db, "get_value")
    def test_raises_when_duplicate_exists(self, get_value):
        """Same (marketplace, marketplace_order_id, company) — must throw."""
        get_value.return_value = "MOM-2026-EXISTING"
        doc = _mock_map()
        # frappe.throw raises frappe.exceptions.ValidationError under the hood
        with self.assertRaises(Exception) as ctx:
            mod.EasyEcomMarketplaceOrderMap._enforce_composite_uniqueness(doc)
        # The error message must name the existing row so the caller can
        # navigate to it — this is what makes split-shipment upsert cheap.
        self.assertIn("MOM-2026-EXISTING", str(ctx.exception))

    @patch.object(mod.frappe.db, "get_value")
    def test_ok_when_no_duplicate(self, get_value):
        get_value.return_value = None
        doc = _mock_map()
        # Should not raise
        mod.EasyEcomMarketplaceOrderMap._enforce_composite_uniqueness(doc)

    @patch.object(mod.frappe.db, "get_value")
    def test_scoping_query_uses_all_three_key_parts(self, get_value):
        """The uniqueness query must include marketplace + marketplace_order_id
        + company AND exclude the current row (name != self.name). Anything
        looser (e.g. dropping company) would incorrectly reject legitimate
        multi-company reuse of the same marketplace_order_id."""
        get_value.return_value = None
        doc = _mock_map(name="MOM-2026-SELF")
        mod.EasyEcomMarketplaceOrderMap._enforce_composite_uniqueness(doc)
        get_value.assert_called_once()
        _args, kwargs = get_value.call_args
        # Positional: doctype, filters, fieldname
        call_args = get_value.call_args[0]
        filters = call_args[1]
        self.assertEqual(filters["marketplace"], "Shopify")
        self.assertEqual(filters["marketplace_order_id"], "SQ-440390821")
        self.assertEqual(filters["company"], "Puresta Lifestyle Private Limited")
        self.assertEqual(filters["name"], ("!=", "MOM-2026-SELF"))


if __name__ == "__main__":
    unittest.main()
