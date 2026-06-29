"""§12 — EasyEcom Marketplace Account pseudo-customer bootstrap tests.

When an FDE creates a Marketplace Account row, the after_insert hook
auto-creates TWO per-account pool Customers — one in-state (CGST+SGST
via tax_category) and one out-of-state (IGST). Every B2C SI minted
via this Account picks one based on the shipping address state vs
the Company's state.

Mocks frappe DB primitives so tests run without a bench.
"""
from __future__ import annotations

import unittest
from unittest.mock import MagicMock, patch

from ecommerce_super.easyecom.doctype.easyecom_marketplace_account.easyecom_marketplace_account import (
    DEFAULT_TAX_CATEGORY_IN_STATE,
    DEFAULT_TAX_CATEGORY_OUT_OF_STATE,
    _bootstrap_one_customer,
    _bootstrap_pseudo_customers,
    _resolve_default_group,
    _resolve_default_territory,
    _resolve_tax_category,
)


# ============================================================
# _bootstrap_pseudo_customers — TWO customers per Account
# ============================================================


class TestBootstrapPseudoCustomers(unittest.TestCase):

    def test_creates_both_in_state_and_out_of_state(self):
        # get_doc returns a doc whose .name = the customer_name passed
        # in the dict — so the function returns the right strings.
        def make_doc(d):
            m = MagicMock()
            m.name = d["customer_name"]
            return m

        def exists_side(dt, name):
            return dt != "Customer"

        with (
            patch(
                "frappe.db.get_value",
                return_value={"display_name": "Amazon.in", "marketplace_name": "Amazon"},
            ),
            patch("frappe.db.exists", side_effect=exists_side),
            patch("frappe.get_doc", side_effect=make_doc),
        ):
            in_state, out_state = _bootstrap_pseudo_customers(
                marketplace="2", company="Acme Ltd",
            )

        self.assertEqual(in_state, "Amazon.in B2C In-State - Acme Ltd")
        self.assertEqual(out_state, "Amazon.in B2C Out-of-State - Acme Ltd")

    def test_idempotent_returns_existing_customer_names(self):
        """Re-bootstrap returns existing Customer names without
        creating duplicates."""
        with (
            patch(
                "frappe.db.get_value",
                return_value={"display_name": "Flipkart", "marketplace_name": "Flipkart"},
            ),
            patch("frappe.db.exists", return_value=True),  # both exist
            patch("frappe.get_doc") as mock_get_doc,
        ):
            in_state, out_state = _bootstrap_pseudo_customers(
                marketplace="7", company="Acme Ltd",
            )
        self.assertEqual(in_state, "Flipkart B2C In-State - Acme Ltd")
        self.assertEqual(out_state, "Flipkart B2C Out-of-State - Acme Ltd")
        mock_get_doc.assert_not_called()

    def test_returns_none_pair_when_marketplace_missing(self):
        result = _bootstrap_pseudo_customers(marketplace="", company="Acme Ltd")
        self.assertEqual(result, (None, None))

    def test_returns_none_pair_when_company_missing(self):
        result = _bootstrap_pseudo_customers(marketplace="2", company="")
        self.assertEqual(result, (None, None))

    def test_falls_back_to_marketplace_name_when_no_display_name(self):
        def make_doc(d):
            m = MagicMock()
            m.name = d["customer_name"]
            return m

        with (
            patch(
                "frappe.db.get_value",
                return_value={"display_name": None, "marketplace_name": "Myntra"},
            ),
            patch("frappe.db.exists", side_effect=lambda dt, name: dt != "Customer"),
            patch("frappe.get_doc", side_effect=make_doc),
        ):
            in_state, _ = _bootstrap_pseudo_customers(
                marketplace="9", company="Acme Ltd",
            )
        self.assertIn("Myntra", in_state)


# ============================================================
# _bootstrap_one_customer — single-Customer creation primitive
# ============================================================


class TestBootstrapOneCustomer(unittest.TestCase):

    def test_creates_with_provided_tax_category(self):
        new_doc = MagicMock()
        new_doc.name = "Some Pool Customer"
        captured: dict = {}

        def fake_get_doc(d):
            captured.update(d)
            return new_doc

        with (
            patch("frappe.db.exists", side_effect=lambda dt, name: dt != "Customer"),
            patch("frappe.get_doc", side_effect=fake_get_doc),
        ):
            result = _bootstrap_one_customer(
                customer_name="Some Pool Customer",
                tax_category="In-State",
            )
        self.assertEqual(result, "Some Pool Customer")
        self.assertEqual(captured["tax_category"], "In-State")
        self.assertEqual(captured["customer_type"], "Individual")

    def test_idempotent_returns_existing_name_without_insert(self):
        with (
            patch("frappe.db.exists", return_value=True),
            patch("frappe.get_doc") as mock_get_doc,
        ):
            result = _bootstrap_one_customer(
                customer_name="Existing", tax_category="In-State",
            )
        self.assertEqual(result, "Existing")
        mock_get_doc.assert_not_called()


# ============================================================
# _resolve_tax_category
# ============================================================


class TestResolveTaxCategory(unittest.TestCase):

    def test_returns_preferred_when_exists(self):
        with patch("frappe.db.exists", return_value=True):
            self.assertEqual(
                _resolve_tax_category(DEFAULT_TAX_CATEGORY_IN_STATE),
                DEFAULT_TAX_CATEGORY_IN_STATE,
            )

    def test_returns_none_when_missing(self):
        with patch("frappe.db.exists", return_value=False):
            self.assertIsNone(_resolve_tax_category("Nonexistent"))

    def test_returns_none_when_lookup_raises(self):
        with patch("frappe.db.exists", side_effect=RuntimeError("table not yet")):
            self.assertIsNone(_resolve_tax_category("In-State"))


# ============================================================
# Default group / territory resolution
# ============================================================


class TestResolveDefaultGroup(unittest.TestCase):
    """Resolver tries candidates in order; for each, checks exists +
    is_group=0 (live-bench fix 2026-06-29: 'Commercial' is a parent
    group on some seeded benches; only leaf groups are accepted)."""

    def test_prefers_individual_when_present_and_leaf(self):
        with (
            patch(
                "frappe.db.exists",
                side_effect=lambda dt, name: name == "Individual",
            ),
            patch("frappe.db.get_value", return_value=0),  # is_group = 0 (leaf)
        ):
            self.assertEqual(_resolve_default_group(), "Individual")

    def test_skips_commercial_when_it_is_a_parent_group(self):
        # Commercial exists but is_group=1 (parent) — should be skipped
        def is_group_lookup(dt, name, field=None):
            if name == "Commercial":
                return 1  # parent group, reject
            return 0
        with (
            patch(
                "frappe.db.exists",
                side_effect=lambda dt, name: name in ("Commercial", "All Customer Groups"),
            ),
            patch("frappe.db.get_value", side_effect=is_group_lookup),
        ):
            self.assertEqual(_resolve_default_group(), "All Customer Groups")

    def test_falls_back_to_any_leaf_group_when_no_canonical_match(self):
        # No canonical names exist; the generic leaf fallback fires
        # via frappe.db.get_value("Customer Group", {"is_group": 0}, "name")
        with (
            patch("frappe.db.exists", return_value=False),
            patch("frappe.db.get_value", return_value="Some Leaf Group"),
        ):
            self.assertEqual(_resolve_default_group(), "Some Leaf Group")


class TestResolveDefaultTerritory(unittest.TestCase):
    """Same leaf-only constraint as TestResolveDefaultGroup."""

    def test_prefers_india_when_leaf(self):
        with (
            patch(
                "frappe.db.exists",
                side_effect=lambda dt, name: name == "India",
            ),
            patch("frappe.db.get_value", return_value=0),  # leaf
        ):
            self.assertEqual(_resolve_default_territory(), "India")

    def test_skips_india_when_it_is_a_parent_territory(self):
        def is_group_lookup(dt, name, field=None):
            if name == "India":
                return 1  # parent territory
            return 0
        with (
            patch(
                "frappe.db.exists",
                side_effect=lambda dt, name: name in ("India", "All Territories"),
            ),
            patch("frappe.db.get_value", side_effect=is_group_lookup),
        ):
            self.assertEqual(_resolve_default_territory(), "All Territories")


# ============================================================
# Tax category constants
# ============================================================


class TestDefaultTaxCategories(unittest.TestCase):

    def test_default_in_state_constant(self):
        self.assertEqual(DEFAULT_TAX_CATEGORY_IN_STATE, "In-State")

    def test_default_out_of_state_constant_matches_india_compliance(self):
        # India Compliance ships "Out-State" (no "of"); default updated
        # to match. Older spec drafts said "Out-of-State" — kept as a
        # candidate in the resolver fallback list.
        self.assertEqual(DEFAULT_TAX_CATEGORY_OUT_OF_STATE, "Out-State")


if __name__ == "__main__":
    unittest.main()
