"""gh#181 part 2 — mirror SI's taxes child table populated from EE's
per-item tax breakdown.

Locks:
  - GST Settings lookup returns Output row for the company
  - IGST-only (inter-state) → one row with rate derived from EE amounts
  - CGST+SGST (intra-state) → two rows, one per bucket
  - Zero-tax → no rows appended
  - Missing GST accounts (config drift) → logged, no rows appended
  - Multi-item → aggregated sums per bucket
"""
from __future__ import annotations

import unittest
from types import SimpleNamespace
from unittest.mock import MagicMock, patch


def _fake_gst_settings(company, accounts=None):
    """Build a MagicMock GST Settings doc with one Output row."""
    doc = MagicMock()
    if accounts is None:
        accounts = {
            "company": company,
            "account_type": "Output",
            "igst_account": "Output Tax IGST - MMPL",
            "cgst_account": "Output Tax CGST - MMPL",
            "sgst_account": "Output Tax SGST - MMPL",
            "utgst_account": None,
        }
    row = SimpleNamespace(**accounts)
    doc.get.return_value = [row]
    return doc


class TestGh181Part2AppendTaxesFromEeRow(unittest.TestCase):
    def test_igst_only_appends_single_row(self):
        """Inter-state (Delhi buyer, Rajasthan seller) → one IGST row."""
        from ecommerce_super.easyecom.flows.b2b_sales.invoice_mirror import (
            _append_taxes_from_ee_row,
        )
        si = MagicMock()
        si.get.return_value = None
        si.taxes = []
        ee_row = {"order_items": [{
            "sku": "FG06476-CHOUHAN", "item_quantity": 1,
            "taxable_value": 300, "igst": 15, "cgst": 0, "sgst": 0, "utgst": 0,
        }]}

        with patch(
            "ecommerce_super.easyecom.flows.b2b_sales.invoice_mirror.frappe.get_cached_doc",
            return_value=_fake_gst_settings("Modern Marwar Private Limited"),
        ):
            _append_taxes_from_ee_row(
                si, ee_row=ee_row, company="Modern Marwar Private Limited"
            )

        si.append.assert_called_once()
        args, _ = si.append.call_args
        self.assertEqual(args[0], "taxes")
        row = args[1]
        self.assertEqual(row["charge_type"], "On Net Total")
        self.assertEqual(row["account_head"], "Output Tax IGST - MMPL")
        self.assertEqual(row["rate"], 5.0)  # 15/300*100
        self.assertIn("IGST", row["description"])

    def test_cgst_sgst_appends_two_rows(self):
        """Intra-state (Rajasthan buyer, Rajasthan seller) → CGST + SGST."""
        from ecommerce_super.easyecom.flows.b2b_sales.invoice_mirror import (
            _append_taxes_from_ee_row,
        )
        si = MagicMock()
        si.get.return_value = None
        ee_row = {"order_items": [{
            "sku": "FG06476-CHOUHAN", "item_quantity": 1,
            "taxable_value": 300, "cgst": 7.5, "sgst": 7.5, "igst": 0, "utgst": 0,
        }]}

        with patch(
            "ecommerce_super.easyecom.flows.b2b_sales.invoice_mirror.frappe.get_cached_doc",
            return_value=_fake_gst_settings("Modern Marwar Private Limited"),
        ):
            _append_taxes_from_ee_row(
                si, ee_row=ee_row, company="Modern Marwar Private Limited"
            )

        self.assertEqual(si.append.call_count, 2)
        buckets_seen = [
            call_args[0][1]["account_head"]
            for call_args in si.append.call_args_list
        ]
        self.assertIn("Output Tax CGST - MMPL", buckets_seen)
        self.assertIn("Output Tax SGST - MMPL", buckets_seen)

    def test_zero_tax_appends_no_rows(self):
        """Zero-value / zero-tax order → no taxes rows."""
        from ecommerce_super.easyecom.flows.b2b_sales.invoice_mirror import (
            _append_taxes_from_ee_row,
        )
        si = MagicMock()
        ee_row = {"order_items": [{
            "sku": "FG", "item_quantity": 1,
            "taxable_value": 0, "igst": 0, "cgst": 0, "sgst": 0, "utgst": 0,
        }]}

        # Should not even hit GST Settings — early-return.
        with patch(
            "ecommerce_super.easyecom.flows.b2b_sales.invoice_mirror.frappe.get_cached_doc"
        ) as get_cached:
            _append_taxes_from_ee_row(si, ee_row=ee_row, company="X")
            get_cached.assert_not_called()
        si.append.assert_not_called()

    def test_missing_order_items_no_op(self):
        """Empty ee_row → no crash, no rows."""
        from ecommerce_super.easyecom.flows.b2b_sales.invoice_mirror import (
            _append_taxes_from_ee_row,
        )
        si = MagicMock()
        _append_taxes_from_ee_row(si, ee_row={}, company="X")
        si.append.assert_not_called()

    def test_multi_item_aggregates_by_bucket(self):
        """Two items with IGST → aggregated to one IGST row."""
        from ecommerce_super.easyecom.flows.b2b_sales.invoice_mirror import (
            _append_taxes_from_ee_row,
        )
        si = MagicMock()
        si.get.return_value = None
        ee_row = {"order_items": [
            {"sku": "A", "item_quantity": 1, "taxable_value": 200, "igst": 10, "cgst": 0, "sgst": 0, "utgst": 0},
            {"sku": "B", "item_quantity": 1, "taxable_value": 100, "igst": 5,  "cgst": 0, "sgst": 0, "utgst": 0},
        ]}

        with patch(
            "ecommerce_super.easyecom.flows.b2b_sales.invoice_mirror.frappe.get_cached_doc",
            return_value=_fake_gst_settings("Modern Marwar Private Limited"),
        ):
            _append_taxes_from_ee_row(si, ee_row=ee_row, company="Modern Marwar Private Limited")

        self.assertEqual(si.append.call_count, 1)
        args, _ = si.append.call_args
        row = args[1]
        # Sum igst = 15, sum taxable = 300 → rate = 5%
        self.assertEqual(row["rate"], 5.0)
        self.assertEqual(row["account_head"], "Output Tax IGST - MMPL")


class TestGh181Part2LookupGstAccounts(unittest.TestCase):
    def test_returns_matching_output_row(self):
        from ecommerce_super.easyecom.flows.b2b_sales.invoice_mirror import (
            _lookup_output_gst_accounts,
        )
        with patch(
            "ecommerce_super.easyecom.flows.b2b_sales.invoice_mirror.frappe.get_cached_doc",
            return_value=_fake_gst_settings("MMPL"),
        ):
            result = _lookup_output_gst_accounts("MMPL")
        self.assertEqual(result["igst_account"], "Output Tax IGST - MMPL")
        self.assertEqual(result["cgst_account"], "Output Tax CGST - MMPL")
        self.assertEqual(result["sgst_account"], "Output Tax SGST - MMPL")

    def test_returns_none_when_no_matching_company(self):
        """Different company on the Output row → None."""
        from ecommerce_super.easyecom.flows.b2b_sales.invoice_mirror import (
            _lookup_output_gst_accounts,
        )
        with patch(
            "ecommerce_super.easyecom.flows.b2b_sales.invoice_mirror.frappe.get_cached_doc",
            return_value=_fake_gst_settings("OTHER-CO"),
        ):
            result = _lookup_output_gst_accounts("MMPL")
        self.assertIsNone(result)

    def test_returns_none_on_get_doc_error(self):
        """GST Settings load fails → None + error logged."""
        from ecommerce_super.easyecom.flows.b2b_sales.invoice_mirror import (
            _lookup_output_gst_accounts,
        )
        with patch(
            "ecommerce_super.easyecom.flows.b2b_sales.invoice_mirror.frappe.get_cached_doc",
            side_effect=Exception("DB unavailable"),
        ), patch(
            "ecommerce_super.easyecom.flows.b2b_sales.invoice_mirror.frappe.log_error"
        ):
            result = _lookup_output_gst_accounts("MMPL")
        self.assertIsNone(result)
