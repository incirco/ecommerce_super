"""Unit tests for the shared place-of-supply / taxType resolver.

Pure-function tests; no DB / Frappe. Pins the §9 / §11 / §12 contract:

  - Indian intra-state  → 2 (CGST + SGST)
  - Indian inter-state  → 1 (IGST)
  - Foreign supplier    → 3 (Custom)
  - Missing state(s)    → 1 (IGST, fail-safe — documented in docstring)
"""

from __future__ import annotations

import unittest

from ecommerce_super.easyecom.tax.place_of_supply import (
    TAX_TYPE_CGST_SGST,
    TAX_TYPE_CUSTOM,
    TAX_TYPE_IGST,
    compute_tax_type,
)


class TestComputeTaxType(unittest.TestCase):
    # ----- Indian intra-state (same state both sides) -----

    def test_intra_state_returns_cgst_sgst(self) -> None:
        self.assertEqual(
            compute_tax_type(
                supplier_state="Maharashtra",
                warehouse_state="Maharashtra",
                supplier_country="India",
            ),
            TAX_TYPE_CGST_SGST,
        )

    def test_intra_state_is_case_insensitive(self) -> None:
        """Address.gst_state is usually title-case but real data has
        casing drift. Normalisation handles it."""
        self.assertEqual(
            compute_tax_type(
                supplier_state="MAHARASHTRA",
                warehouse_state="maharashtra",
                supplier_country="India",
            ),
            TAX_TYPE_CGST_SGST,
        )

    def test_intra_state_tolerates_whitespace(self) -> None:
        self.assertEqual(
            compute_tax_type(
                supplier_state=" Karnataka ",
                warehouse_state="Karnataka",
                supplier_country="India",
            ),
            TAX_TYPE_CGST_SGST,
        )

    # ----- Indian inter-state (different states) -----

    def test_inter_state_returns_igst(self) -> None:
        self.assertEqual(
            compute_tax_type(
                supplier_state="Maharashtra",
                warehouse_state="Karnataka",
                supplier_country="India",
            ),
            TAX_TYPE_IGST,
        )

    def test_inter_state_with_none_country_defaults_to_indian(self) -> None:
        """Empty/None country is treated as Indian path (the conservative
        assumption — overseas is opt-in via explicit country)."""
        self.assertEqual(
            compute_tax_type(
                supplier_state="Maharashtra",
                warehouse_state="Karnataka",
                supplier_country=None,
            ),
            TAX_TYPE_IGST,
        )

    # ----- Foreign supplier → Custom -----

    def test_foreign_us_supplier_returns_custom(self) -> None:
        self.assertEqual(
            compute_tax_type(
                supplier_state=None,
                warehouse_state="Maharashtra",
                supplier_country="United States",
            ),
            TAX_TYPE_CUSTOM,
        )

    def test_foreign_with_state_still_returns_custom(self) -> None:
        """Foreign country wins over state comparison."""
        self.assertEqual(
            compute_tax_type(
                supplier_state="California",
                warehouse_state="Maharashtra",
                supplier_country="United States",
            ),
            TAX_TYPE_CUSTOM,
        )

    def test_foreign_country_case_insensitive(self) -> None:
        self.assertEqual(
            compute_tax_type(
                supplier_state=None,
                warehouse_state="Karnataka",
                supplier_country="united kingdom",
            ),
            TAX_TYPE_CUSTOM,
        )

    def test_india_lowercase_treated_as_indian(self) -> None:
        self.assertEqual(
            compute_tax_type(
                supplier_state="Maharashtra",
                warehouse_state="Maharashtra",
                supplier_country="india",
            ),
            TAX_TYPE_CGST_SGST,
        )

    # ----- Fail-safe: missing state(s) → IGST -----

    def test_missing_supplier_state_defaults_igst(self) -> None:
        self.assertEqual(
            compute_tax_type(
                supplier_state=None,
                warehouse_state="Maharashtra",
                supplier_country="India",
            ),
            TAX_TYPE_IGST,
        )

    def test_missing_warehouse_state_defaults_igst(self) -> None:
        self.assertEqual(
            compute_tax_type(
                supplier_state="Maharashtra",
                warehouse_state="",
                supplier_country="India",
            ),
            TAX_TYPE_IGST,
        )

    def test_both_states_missing_defaults_igst(self) -> None:
        self.assertEqual(
            compute_tax_type(
                supplier_state="",
                warehouse_state=None,
                supplier_country="India",
            ),
            TAX_TYPE_IGST,
        )


if __name__ == "__main__":
    unittest.main()
