"""gh#181 regression — mirror line-item rate must respect EE's
post-promotion `taxable_value` (or breakup_types with promo).
Live symptom: SO-2610392 → EE grand_total=₹0, SI=₹285.71.
"""
from __future__ import annotations

import unittest
from unittest.mock import patch


def _run_resolver(order_items, item_map_hit="FG06476-CHOUHAN"):
    from ecommerce_super.easyecom.flows.b2b_sales.invoice_mirror import (
        _resolve_line_items,
    )

    def _fake_get_value(*args, **kwargs):
        doctype = args[0]
        if doctype == "EasyEcom Item Map":
            return item_map_hit
        if doctype == "Item":
            return "12345678"  # HSN
        return None

    with patch(
        "ecommerce_super.easyecom.flows.b2b_sales.invoice_mirror.frappe.db.get_value",
        side_effect=_fake_get_value,
    ):
        return _resolve_line_items({"order_items": order_items})


class TestGh181TaxableValuePriority(unittest.TestCase):
    def test_uses_taxable_value_directly_when_present(self):
        """SO-2610392: 100% promotion → taxable_value=0. Rate=0."""
        lines = _run_resolver([{
            "sku": "FG06476-CHOUHAN", "item_quantity": 1,
            "taxable_value": 0, "tax_rate": 5,
            "breakup_types": {
                "Item Amount Excluding Tax": 285.7143,
                "Promotion Discount Excluding Tax": -285.7143,
                "Item Amount IGST": 14.2857,
                "Promotion Discount IGST": -14.2857,
            },
        }])
        self.assertEqual(len(lines), 1)
        self.assertEqual(lines[0]["rate"], 0.0)
        self.assertEqual(lines[0]["qty"], 1)

    def test_uses_taxable_value_for_normal_priced_line(self):
        lines = _run_resolver([{
            "sku": "FG06476-CHOUHAN", "item_quantity": 1,
            "taxable_value": 952.38, "tax_rate": 5,
        }])
        self.assertEqual(lines[0]["rate"], 952.38)

    def test_falls_back_to_breakup_sum_when_taxable_value_absent(self):
        """Legacy payload — sum *Excluding Tax entries in breakup."""
        lines = _run_resolver([{
            "sku": "FG06476-CHOUHAN", "item_quantity": 2,
            "breakup_types": {
                "Item Amount Excluding Tax": 200.0,
                "Promotion Discount Excluding Tax": -50.0,
                "Item Amount IGST": 10.0,
            },
        }])
        # Net = 200 - 50 = 150; per-unit = 75.
        self.assertEqual(lines[0]["rate"], 75.0)

    def test_final_fallback_selling_price_when_both_missing(self):
        """Neither taxable_value nor breakup_types → selling_price / (1 + rate/100)."""
        lines = _run_resolver([{
            "sku": "FG06476-CHOUHAN", "item_quantity": 1,
            "selling_price": 105.0, "tax_rate": 5,
        }])
        # Net = 105 / 1.05 = 100.
        self.assertEqual(lines[0]["rate"], 100.0)

    def test_zero_qty_skipped(self):
        lines = _run_resolver([{
            "sku": "FG06476-CHOUHAN", "item_quantity": 0,
            "taxable_value": 0,
        }])
        self.assertEqual(lines, [])
