"""Unit tests for PR G' — IC-native tax template resolution.

Covers the two helpers that replace the legacy charge_type="Actual"
tax-injection pattern:

  _resolve_gst_sales_taxes_template  → header-level template
  _resolve_item_tax_template         → per-line template

Both return None gracefully when their target template is missing on
the site (rather than raising), so SI insertion doesn't get blocked
on a config gap — the variance check still fires as a fallback signal.
"""
from __future__ import annotations

import unittest
from unittest.mock import patch

from ecommerce_super.easyecom.flows.b2c_sales.invoice_builder import (
    _resolve_gst_sales_taxes_template,
    _resolve_item_tax_template,
)


# ============================================================
# _resolve_item_tax_template
# ============================================================


class TestResolveItemTaxTemplate(unittest.TestCase):

    @patch(
        "ecommerce_super.easyecom.flows.b2c_sales.invoice_builder.frappe.db.exists",
        return_value=True,
    )
    @patch(
        "ecommerce_super.easyecom.flows.b2c_sales.invoice_builder.frappe.get_cached_value",
        return_value="PLPL",
    )
    def test_returns_gst_18_pct_template(self, _gcv, _exists):
        self.assertEqual(
            _resolve_item_tax_template(
                company="Puresta Lifestyle Pvt. Ltd.", tax_rate=18
            ),
            "GST 18% - PLPL",
        )

    @patch(
        "ecommerce_super.easyecom.flows.b2c_sales.invoice_builder.frappe.db.exists",
        return_value=True,
    )
    @patch(
        "ecommerce_super.easyecom.flows.b2c_sales.invoice_builder.frappe.get_cached_value",
        return_value="HQL",
    )
    def test_returns_gst_5_pct_hql_template(self, _gcv, _exists):
        self.assertEqual(
            _resolve_item_tax_template(
                company="Health Q Lifesciences", tax_rate=5
            ),
            "GST 5% - HQL",
        )

    @patch(
        "ecommerce_super.easyecom.flows.b2c_sales.invoice_builder.frappe.db.exists",
        return_value=True,
    )
    @patch(
        "ecommerce_super.easyecom.flows.b2c_sales.invoice_builder.frappe.get_cached_value",
        return_value="PLPL",
    )
    def test_zero_rate_maps_to_nil_rated(self, _gcv, _exists):
        # Nil-rated goods, exempt items, and export invoices all send
        # rate=0. The naming convention shows Nil-Rated as the target.
        self.assertEqual(
            _resolve_item_tax_template(
                company="Puresta Lifestyle Pvt. Ltd.", tax_rate=0
            ),
            "Nil-Rated - PLPL",
        )

    @patch(
        "ecommerce_super.easyecom.flows.b2c_sales.invoice_builder.frappe.get_cached_value",
        return_value="PLPL",
    )
    def test_non_standard_rate_returns_none(self, _gcv):
        """3% is not a valid GST slab. Return None so IC falls back to
        the Item master's default tax template rather than us guessing."""
        self.assertIsNone(
            _resolve_item_tax_template(
                company="Puresta Lifestyle Pvt. Ltd.", tax_rate=3.0
            )
        )

    @patch(
        "ecommerce_super.easyecom.flows.b2c_sales.invoice_builder.frappe.get_cached_value",
        return_value=None,
    )
    def test_no_company_abbr_returns_none(self, _gcv):
        """Company without an abbr can't produce a template name."""
        self.assertIsNone(
            _resolve_item_tax_template(company="Ghost Company", tax_rate=18)
        )

    @patch(
        "ecommerce_super.easyecom.flows.b2c_sales.invoice_builder.frappe.db.exists",
        return_value=False,
    )
    @patch(
        "ecommerce_super.easyecom.flows.b2c_sales.invoice_builder.frappe.get_cached_value",
        return_value="PLPL",
    )
    def test_missing_template_returns_none(self, _gcv, _exists):
        """Site hasn't shipped 'GST 12% - PLPL' template — return None
        so SI still inserts; variance check will catch the gap."""
        self.assertIsNone(
            _resolve_item_tax_template(
                company="Puresta Lifestyle Pvt. Ltd.", tax_rate=12
            )
        )

    @patch(
        "ecommerce_super.easyecom.flows.b2c_sales.invoice_builder.frappe.db.exists",
        return_value=True,
    )
    @patch(
        "ecommerce_super.easyecom.flows.b2c_sales.invoice_builder.frappe.get_cached_value",
        return_value="PLPL",
    )
    def test_float_rate_rounded_to_int(self, _gcv, _exists):
        """EE payload sometimes sends 18.0 as a float. Should round to
        18 and pick 'GST 18% - PLPL', not fail on the type mismatch."""
        self.assertEqual(
            _resolve_item_tax_template(
                company="Puresta Lifestyle Pvt. Ltd.", tax_rate=18.0
            ),
            "GST 18% - PLPL",
        )


# ============================================================
# _resolve_gst_sales_taxes_template
# ============================================================


class TestResolveGstSalesTaxesTemplate(unittest.TestCase):

    @patch(
        "ecommerce_super.easyecom.flows.b2c_sales.invoice_builder.frappe.db.exists",
        return_value=True,
    )
    @patch(
        "ecommerce_super.easyecom.flows.b2c_sales.invoice_builder.frappe.get_cached_value",
        return_value="PLPL",
    )
    def test_intra_state_haryana_to_haryana_returns_in_state(
        self, _gcv, _exists
    ):
        """Seller in Haryana + place_of_supply Haryana → intra-state →
        In-state template (CGST + SGST split)."""
        self.assertEqual(
            _resolve_gst_sales_taxes_template(
                company="Puresta Lifestyle Pvt. Ltd.",
                place_of_supply="06-Haryana",
                seller_state="Haryana",
            ),
            "Output GST In-state - PLPL",
        )

    @patch(
        "ecommerce_super.easyecom.flows.b2c_sales.invoice_builder.frappe.db.exists",
        return_value=True,
    )
    @patch(
        "ecommerce_super.easyecom.flows.b2c_sales.invoice_builder.frappe.get_cached_value",
        return_value="PLPL",
    )
    def test_inter_state_haryana_to_maharashtra_returns_out_state(
        self, _gcv, _exists
    ):
        """Seller in Haryana + place_of_supply Maharashtra → inter-state →
        Out-state template (IGST)."""
        self.assertEqual(
            _resolve_gst_sales_taxes_template(
                company="Puresta Lifestyle Pvt. Ltd.",
                place_of_supply="27-Maharashtra",
                seller_state="Haryana",
            ),
            "Output GST Out-state - PLPL",
        )

    @patch(
        "ecommerce_super.easyecom.flows.b2c_sales.invoice_builder.frappe.db.exists",
        return_value=True,
    )
    @patch(
        "ecommerce_super.easyecom.flows.b2c_sales.invoice_builder.frappe.get_cached_value",
        return_value="PLPL",
    )
    def test_case_insensitive_state_match(self, _gcv, _exists):
        """Seller 'HARYANA' vs place_of_supply '06-Haryana' should still
        resolve as intra-state — case shouldn't matter."""
        self.assertEqual(
            _resolve_gst_sales_taxes_template(
                company="Puresta Lifestyle Pvt. Ltd.",
                place_of_supply="06-Haryana",
                seller_state="HARYANA",
            ),
            "Output GST In-state - PLPL",
        )

    @patch(
        "ecommerce_super.easyecom.flows.b2c_sales.invoice_builder.frappe.db.exists",
        return_value=True,
    )
    @patch(
        "ecommerce_super.easyecom.flows.b2c_sales.invoice_builder.frappe.get_cached_value",
        return_value="PLPL",
    )
    def test_missing_place_of_supply_defaults_to_out_state(
        self, _gcv, _exists
    ):
        """No place_of_supply — can't confirm intra-state — safest default
        is Out-state (IGST). SI will still insert; India Compliance may
        raise on submit if place_of_supply is genuinely required."""
        self.assertEqual(
            _resolve_gst_sales_taxes_template(
                company="Puresta Lifestyle Pvt. Ltd.",
                place_of_supply=None,
                seller_state="Haryana",
            ),
            "Output GST Out-state - PLPL",
        )

    @patch(
        "ecommerce_super.easyecom.flows.b2c_sales.invoice_builder.frappe.db.exists",
        return_value=False,
    )
    @patch(
        "ecommerce_super.easyecom.flows.b2c_sales.invoice_builder.frappe.get_cached_value",
        return_value="PLPL",
    )
    def test_missing_template_returns_none_does_not_raise(
        self, _gcv, _exists
    ):
        """Template not shipped on the site — helper returns None so SI
        insertion isn't blocked. Variance check catches the tax gap."""
        self.assertIsNone(
            _resolve_gst_sales_taxes_template(
                company="Puresta Lifestyle Pvt. Ltd.",
                place_of_supply="06-Haryana",
                seller_state="Haryana",
            )
        )

    @patch(
        "ecommerce_super.easyecom.flows.b2c_sales.invoice_builder.frappe.get_cached_value",
        return_value=None,
    )
    def test_no_company_abbr_returns_none(self, _gcv):
        self.assertIsNone(
            _resolve_gst_sales_taxes_template(
                company="Ghost Company",
                place_of_supply="06-Haryana",
                seller_state="Haryana",
            )
        )


if __name__ == "__main__":
    unittest.main()
