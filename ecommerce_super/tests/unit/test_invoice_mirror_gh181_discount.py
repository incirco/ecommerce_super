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

    # gh#207: mirror now instruments fallback tiers 2/3 via
    # frappe.log_error. Mock it so tests don't write real Error Log
    # rows. Individual tests that care can inspect via the return.
    with patch(
        "ecommerce_super.easyecom.flows.b2b_sales.invoice_mirror.frappe.db.get_value",
        side_effect=_fake_get_value,
    ), patch(
        "ecommerce_super.easyecom.flows.b2b_sales.invoice_mirror.frappe.log_error"
    ):
        return _resolve_line_items({"order_items": order_items})


def _run_resolver_capturing_logs(order_items, item_map_hit="FG06476-CHOUHAN"):
    """Same as _run_resolver but returns (lines, list_of_log_error_calls)
    so gh#207 instrumentation tests can inspect what was logged."""
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
    ), patch(
        "ecommerce_super.easyecom.flows.b2b_sales.invoice_mirror.frappe.log_error"
    ) as log_mock:
        lines = _resolve_line_items({"order_items": order_items})
        return lines, log_mock.call_args_list


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


class TestGh207FallbackInstrumentation(unittest.TestCase):
    """gh#207 — tier 2 (breakup_types sum) and tier 3 (selling_price)
    fallbacks are suspected dead code. As of 2026-07, every observed EE
    payload has included `taxable_value` on every line. Rather than
    delete without evidence (risk of a rare payload silently breaking),
    instrument both tiers with `frappe.log_error` so any live production
    fire lands in Error Log. Actions:

      - If a log fires in production over N days: add a locked-behavior
        test for the observed shape or investigate the payload variance.
      - If no logs fire: delete the tier as dead code.

    These tests lock the instrumentation itself so a future refactor
    doesn't silently regress back to invisible fallback.
    """

    def test_tier_1_normal_path_does_not_log(self):
        """Happy path: taxable_value present → no fallback → no log."""
        lines, log_calls = _run_resolver_capturing_logs([{
            "sku": "FG06476-CHOUHAN", "item_quantity": 1,
            "taxable_value": 100.0, "tax_rate": 5,
        }])
        self.assertEqual(lines[0]["rate"], 100.0)
        self.assertEqual(len(log_calls), 0,
            f"Tier 1 must not emit any Error Log entries — got: {log_calls}")

    def test_tier_2_breakup_fallback_logs_with_payload_shape(self):
        """When taxable_value is absent and tier 2 fires, an Error Log
        entry must be created naming the sku + payload keys."""
        lines, log_calls = _run_resolver_capturing_logs([{
            "sku": "FG06476-CHOUHAN", "item_quantity": 2,
            "breakup_types": {
                "Item Amount Excluding Tax": 200.0,
                "Promotion Discount Excluding Tax": -50.0,
            },
        }])
        self.assertEqual(lines[0]["rate"], 75.0)  # tier 2 math still works
        self.assertEqual(len(log_calls), 1,
            f"Tier 2 must emit exactly one Error Log entry, got {log_calls}")
        title = log_calls[0].kwargs.get("title", "")
        self.assertIn("gh#207", title)
        self.assertIn("tier 2", title)
        self.assertIn("FG06476-CHOUHAN", title)  # sku identified

    def test_tier_3_selling_price_fallback_logs_with_payload_shape(self):
        """When both taxable_value AND breakup_types-with-net are
        absent, tier 3 fires. Log entry must call out this as the
        coarsest tier (most suspect)."""
        lines, log_calls = _run_resolver_capturing_logs([{
            "sku": "FG06476-CHOUHAN", "item_quantity": 1,
            "selling_price": 105.0, "tax_rate": 5,
        }])
        self.assertEqual(lines[0]["rate"], 100.0)  # tier 3 math still works
        self.assertEqual(len(log_calls), 1,
            f"Tier 3 must emit exactly one Error Log entry, got {log_calls}")
        title = log_calls[0].kwargs.get("title", "")
        self.assertIn("gh#207", title)
        self.assertIn("tier 3", title)
        self.assertIn("FG06476-CHOUHAN", title)

    def test_taxable_value_with_zero_valid_does_not_log(self):
        """SO-2610392 case: 100% promo → taxable_value=0. That's still
        tier 1 (present, value 0), not a fallback. Must not log."""
        lines, log_calls = _run_resolver_capturing_logs([{
            "sku": "FG06476-CHOUHAN", "item_quantity": 1,
            "taxable_value": 0, "tax_rate": 5,
            "breakup_types": {
                "Item Amount Excluding Tax": 285.7143,
                "Promotion Discount Excluding Tax": -285.7143,
            },
        }])
        self.assertEqual(lines[0]["rate"], 0.0)
        self.assertEqual(len(log_calls), 0,
            "taxable_value=0 is a valid tier-1 result, not a fallback")

    def test_multiple_lines_only_falling_lines_log(self):
        """Multi-line SI where one line has taxable_value and another
        needs tier 2 — only the tier 2 line emits a log."""
        lines, log_calls = _run_resolver_capturing_logs([
            {  # normal
                "sku": "FG06476-CHOUHAN", "item_quantity": 1,
                "taxable_value": 100.0,
            },
            {  # tier 2 fallback
                "sku": "FG06476-CHOUHAN", "item_quantity": 2,
                "breakup_types": {"Item Amount Excluding Tax": 150.0},
            },
        ])
        self.assertEqual(len(lines), 2)
        self.assertEqual(len(log_calls), 1,
            f"Only the fallback line should log, got {log_calls}")
        self.assertIn("tier 2", log_calls[0].kwargs.get("title", ""))
