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

    def test_zero_qty_skipped(self):
        lines = _run_resolver([{
            "sku": "FG06476-CHOUHAN", "item_quantity": 0,
            "taxable_value": 0,
        }])
        self.assertEqual(lines, [])


class TestGh207FailLoudOnMissingTaxableValue(unittest.TestCase):
    """gh#207 — deleted two speculative fallback tiers (breakup_types
    sum, selling_price gross-to-net back-out) that were added in gh#181
    as defensive hedges. Every observed MMPL payload has included
    `taxable_value` on every line, so the fallbacks were dead code that
    silently reinvented tax arithmetic — a violation of the CLAUDE.md
    ERPNext-primitives-first rule.

    Post-#207 behavior: if EE ever sends a line without `taxable_value`,
    the mirror raises InvoiceMirrorError with the exact payload shape.
    MMPL ops sees the specific SO that broke and we add a locked-
    behavior test for the OBSERVED shape (not hypothetical ones).
    """

    def test_missing_taxable_value_raises_with_payload_shape(self):
        """The core contract: no taxable_value → clear throw naming
        sku, qty, and the actual keys we did receive."""
        from ecommerce_super.easyecom.flows.b2b_sales.invoice_mirror import (
            InvoiceMirrorError,
        )
        with self.assertRaises(InvoiceMirrorError) as ctx:
            _run_resolver([{
                "sku": "FG06476-CHOUHAN", "item_quantity": 2,
                "breakup_types": {
                    "Item Amount Excluding Tax": 200.0,
                    "Promotion Discount Excluding Tax": -50.0,
                },
                # No taxable_value.
            }])
        msg = str(ctx.exception)
        self.assertIn("FG06476-CHOUHAN", msg)
        self.assertIn("taxable_value", msg)
        self.assertIn("breakup_types", msg)  # actual payload keys listed
        self.assertIn("gh#207", msg)  # points to the audit that removed fallbacks

    def test_none_taxable_value_raises(self):
        """taxable_value=None (explicit null) is treated same as absent."""
        from ecommerce_super.easyecom.flows.b2b_sales.invoice_mirror import (
            InvoiceMirrorError,
        )
        with self.assertRaises(InvoiceMirrorError):
            _run_resolver([{
                "sku": "FG06476-CHOUHAN", "item_quantity": 1,
                "taxable_value": None,
            }])

    def test_unparseable_taxable_value_raises_with_specific_error(self):
        """Non-numeric taxable_value (e.g. EE bug returns a dict) →
        clear throw naming the offending value, not a bare TypeError."""
        from ecommerce_super.easyecom.flows.b2b_sales.invoice_mirror import (
            InvoiceMirrorError,
        )
        with self.assertRaises(InvoiceMirrorError) as ctx:
            _run_resolver([{
                "sku": "FG06476-CHOUHAN", "item_quantity": 1,
                "taxable_value": {"broken": "shape"},
            }])
        self.assertIn("taxable_value", str(ctx.exception))
        self.assertIn("FG06476-CHOUHAN", str(ctx.exception))

    def test_taxable_value_zero_is_valid_not_error(self):
        """SO-2610392 case: 100% promo yields taxable_value=0. That's a
        legitimate value, not a missing field — must NOT raise."""
        lines = _run_resolver([{
            "sku": "FG06476-CHOUHAN", "item_quantity": 1,
            "taxable_value": 0,
            "breakup_types": {
                "Item Amount Excluding Tax": 285.7143,
                "Promotion Discount Excluding Tax": -285.7143,
            },
        }])
        self.assertEqual(lines[0]["rate"], 0.0)

    def test_taxable_value_string_number_parsed(self):
        """EE sometimes returns numeric values as strings — the float()
        coercion in the mirror handles that. Regression guard."""
        lines = _run_resolver([{
            "sku": "FG06476-CHOUHAN", "item_quantity": 2,
            "taxable_value": "500.00",
        }])
        self.assertEqual(lines[0]["rate"], 250.0)
