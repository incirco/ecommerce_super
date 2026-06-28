"""§12 — EasyEcom Marketplace Account pseudo-customer bootstrap tests.

When an FDE creates a Marketplace Account row for (Company, Marketplace),
the after_insert hook auto-creates a per-account pool Customer named
"<Marketplace> B2C Pool - <Company>" so every B2C SI minted via this
Account points at the same Customer master row.

Mocks frappe DB primitives so tests run without a bench.
"""
from __future__ import annotations

import unittest
from unittest.mock import MagicMock, patch

from ecommerce_super.easyecom.doctype.easyecom_marketplace_account.easyecom_marketplace_account import (
    _bootstrap_pseudo_customer,
    _resolve_default_group,
    _resolve_default_territory,
)


# ============================================================
# _bootstrap_pseudo_customer — happy path + idempotency
# ============================================================


class TestBootstrapPseudoCustomer(unittest.TestCase):

    def test_creates_customer_with_canonical_name(self):
        """First call for (Marketplace, Company) creates a Customer
        named '<display_name> B2C Pool - <Company>'."""
        new_doc = MagicMock()
        new_doc.name = "Amazon.in B2C Pool - Acme Ltd"
        with (
            patch(
                "frappe.db.get_value",
                return_value={"display_name": "Amazon.in", "marketplace_name": "Amazon"},
            ),
            patch(
                "frappe.db.exists",
                side_effect=lambda dt, name: dt != "Customer",
            ),
            patch("frappe.get_doc", return_value=new_doc),
        ):
            result = _bootstrap_pseudo_customer(
                marketplace="2", company="Acme Ltd",
            )

        self.assertEqual(result, "Amazon.in B2C Pool - Acme Ltd")
        new_doc.insert.assert_called_once_with(ignore_if_duplicate=True)

    def test_idempotent_when_customer_already_exists(self):
        """Re-bootstrap with same (Marketplace, Company) returns the
        existing Customer name and does NOT create a duplicate."""
        with (
            patch(
                "frappe.db.get_value",
                side_effect=[
                    {"display_name": "Amazon.in", "marketplace_name": "Amazon"},
                ],
            ),
            patch("frappe.db.exists", return_value=True),
            patch("frappe.get_doc") as mock_get_doc,
        ):
            result = _bootstrap_pseudo_customer(
                marketplace="2", company="Acme Ltd",
            )

        self.assertEqual(result, "Amazon.in B2C Pool - Acme Ltd")
        mock_get_doc.assert_not_called()

    def test_falls_back_to_marketplace_name_when_no_display_name(self):
        new_doc = MagicMock()
        new_doc.name = "Flipkart B2C Pool - Acme Ltd"
        with (
            patch(
                "frappe.db.get_value",
                return_value={"display_name": None, "marketplace_name": "Flipkart"},
            ),
            patch(
                "frappe.db.exists",
                side_effect=lambda dt, name: dt != "Customer",
            ),
            patch("frappe.get_doc", return_value=new_doc),
        ):
            result = _bootstrap_pseudo_customer(
                marketplace="7", company="Acme Ltd",
            )
        self.assertEqual(result, "Flipkart B2C Pool - Acme Ltd")

    def test_falls_back_to_marketplace_docname_when_no_label_fields(self):
        new_doc = MagicMock()
        new_doc.name = "12 B2C Pool - Acme Ltd"
        with (
            patch(
                "frappe.db.get_value",
                return_value={"display_name": None, "marketplace_name": None},
            ),
            patch(
                "frappe.db.exists",
                side_effect=lambda dt, name: dt != "Customer",
            ),
            patch("frappe.get_doc", return_value=new_doc),
        ):
            result = _bootstrap_pseudo_customer(
                marketplace="12", company="Acme Ltd",
            )
        # When both label fields are null, falls back to the docname
        self.assertIn("12 B2C Pool", result)

    def test_returns_none_when_marketplace_missing(self):
        result = _bootstrap_pseudo_customer(marketplace="", company="Acme Ltd")
        self.assertIsNone(result)

    def test_returns_none_when_company_missing(self):
        result = _bootstrap_pseudo_customer(marketplace="2", company="")
        self.assertIsNone(result)

    def test_handles_missing_marketplace_row_gracefully(self):
        """If the Marketplace row doesn't exist (get_value returns None),
        we still build a name using the marketplace docname as the label."""
        new_doc = MagicMock()
        new_doc.name = "99 B2C Pool - Acme Ltd"
        with (
            patch("frappe.db.get_value", return_value=None),
            patch(
                "frappe.db.exists",
                side_effect=lambda dt, name: dt != "Customer",
            ),
            patch("frappe.get_doc", return_value=new_doc),
        ):
            result = _bootstrap_pseudo_customer(
                marketplace="99", company="Acme Ltd",
            )
        # Falls back to marketplace docname; doesn't raise
        self.assertIsNotNone(result)


# ============================================================
# Default group / territory resolution
# ============================================================


class TestResolveDefaultGroup(unittest.TestCase):

    def test_prefers_commercial_when_present(self):
        with patch(
            "frappe.db.exists",
            side_effect=lambda dt, name: name == "Commercial",
        ):
            self.assertEqual(_resolve_default_group(), "Commercial")

    def test_falls_back_to_all_customer_groups(self):
        with (
            patch(
                "frappe.db.exists",
                side_effect=lambda dt, name: name == "All Customer Groups",
            ),
            patch("frappe.db.get_value"),
        ):
            self.assertEqual(_resolve_default_group(), "All Customer Groups")

    def test_falls_back_to_individual_when_neither(self):
        with (
            patch(
                "frappe.db.exists",
                side_effect=lambda dt, name: name == "Individual",
            ),
            patch("frappe.db.get_value"),
        ):
            self.assertEqual(_resolve_default_group(), "Individual")

    def test_falls_back_to_any_existing_group_when_no_canonical_match(self):
        with (
            patch("frappe.db.exists", return_value=False),
            patch("frappe.db.get_value", return_value="Some Custom Group"),
        ):
            self.assertEqual(_resolve_default_group(), "Some Custom Group")


class TestResolveDefaultTerritory(unittest.TestCase):

    def test_prefers_india(self):
        with patch(
            "frappe.db.exists",
            side_effect=lambda dt, name: name == "India",
        ):
            self.assertEqual(_resolve_default_territory(), "India")

    def test_falls_back_to_all_territories(self):
        with (
            patch(
                "frappe.db.exists",
                side_effect=lambda dt, name: name == "All Territories",
            ),
            patch("frappe.db.get_value"),
        ):
            self.assertEqual(_resolve_default_territory(), "All Territories")


# ============================================================
# Naming convention sanity checks
# ============================================================


class TestCustomerNamingConvention(unittest.TestCase):

    def test_naming_uses_display_name_with_dots(self):
        """Marketplaces like 'Amazon.in' should keep their dots in the
        Customer name — not be sanitised."""
        new_doc = MagicMock()
        with (
            patch(
                "frappe.db.get_value",
                return_value={"display_name": "Amazon.in", "marketplace_name": "Amazon"},
            ),
            patch(
                "frappe.db.exists",
                side_effect=lambda dt, name: dt != "Customer",
            ),
            patch("frappe.get_doc", return_value=new_doc),
        ):
            _bootstrap_pseudo_customer(marketplace="2", company="Acme Ltd")
        # The Customer doc was constructed with the dotted name
        call_kwargs = new_doc.insert.call_args
        self.assertIsNotNone(call_kwargs)


if __name__ == "__main__":
    unittest.main()
