"""gh#50 — pre-check substrate for §8e/§8f dup-name disambiguation.

Original implementation caught `frappe.DuplicateEntryError` reactively
to disambiguate same-named EE customers/suppliers with a `(c_id)`
suffix. That's dead code under current ERPNext: autoname appends
" - N" to the docname BEFORE the duplicate exception fires, so the
catch never triggers and the second customer/supplier ends up as
`"DupName - 1"` with no EE-side identifier visible.

Fix: pre-check via `frappe.db.exists` BEFORE the insert; apply the
suffix proactively when collision is detected. The reactive catch
remains as a belt-and-braces fallback for the theoretical
concurrent-pull race.

These tests freeze the pre-check behavior. The full integration
tests (test_customer_pull_stage3 / test_supplier_pull_stage3) still
exercise the end-to-end pull; this is a focused contract test.
"""

from __future__ import annotations

import unittest
from unittest.mock import MagicMock, patch

import frappe


class TestCustomerDupNamePrecheck(unittest.TestCase):
    """`_create_customer` must pre-check existing customer_name and
    apply the (c_id) suffix on collision."""

    def _run(self, *, ee_c_id: str, customer_name: str, existing: bool):
        """Drive _create_customer with mocked frappe internals.

        Returns the customer_name that landed on the insert payload,
        captured from `update()`.
        """
        from ecommerce_super.easyecom.flows import customer_pull

        captured: dict = {}
        fake_doc = MagicMock()
        fake_doc.update = lambda d: captured.update(d)
        fake_doc.insert = MagicMock()
        fake_doc.name = "CUST-FAKE-001"

        with (
            patch("frappe.new_doc", return_value=fake_doc),
            patch.object(frappe.db, "exists", return_value=existing),
            patch.object(
                customer_pull, "_default_customer_group", return_value="All Customer Groups"
            ),
            patch.object(
                customer_pull, "_default_territory", return_value="All Territories"
            ),
        ):
            customer_pull._create_customer(
                ee_c_id=ee_c_id,
                erpnext_fields={"customer_name": customer_name},
                gstin="",
                gst_category="Unregistered",
            )
        return captured.get("customer_name")

    def test_first_customer_keeps_canonical_name(self) -> None:
        """No existing Customer with this name → no suffix applied."""
        landed = self._run(
            ee_c_id="C-001",
            customer_name="ACME Corp",
            existing=False,
        )
        self.assertEqual(landed, "ACME Corp")

    def test_second_customer_gets_c_id_suffix(self) -> None:
        """gh#50 headline — collision triggers proactive suffix."""
        landed = self._run(
            ee_c_id="C-002",
            customer_name="ACME Corp",
            existing=True,
        )
        self.assertEqual(landed, "ACME Corp (C-002)")

    def test_empty_customer_name_falls_back_to_ee_id(self) -> None:
        """Empty EE companyname → substrate manufactures
        'EE Customer {c_id}'. That name is unique by definition (the
        c_id is in it), so no suffix needed."""
        from ecommerce_super.easyecom.flows import customer_pull

        captured: dict = {}
        fake_doc = MagicMock()
        fake_doc.update = lambda d: captured.update(d)
        fake_doc.insert = MagicMock()
        fake_doc.name = "CUST-FAKE-001"

        with (
            patch("frappe.new_doc", return_value=fake_doc),
            patch.object(frappe.db, "exists", return_value=False),
            patch.object(
                customer_pull, "_default_customer_group", return_value="All Customer Groups"
            ),
            patch.object(
                customer_pull, "_default_territory", return_value="All Territories"
            ),
        ):
            customer_pull._create_customer(
                ee_c_id="C-XYZ",
                erpnext_fields={},  # no customer_name
                gstin="",
                gst_category="Unregistered",
            )
        self.assertEqual(captured.get("customer_name"), "EE Customer C-XYZ")


class TestSupplierDupNamePrecheck(unittest.TestCase):
    """`_create_supplier` mirrors the customer pre-check on supplier_name."""

    def _run(
        self,
        *,
        ee_vendor_c_id: str,
        supplier_name: str,
        existing: bool,
    ):
        from ecommerce_super.easyecom.flows import supplier_pull

        captured: dict = {}
        fake_doc = MagicMock()
        fake_doc.update = lambda d: captured.update(d)
        fake_doc.insert = MagicMock()
        fake_doc.name = "SUP-FAKE-001"

        with (
            patch("frappe.new_doc", return_value=fake_doc),
            patch.object(frappe.db, "exists", return_value=existing),
            patch.object(
                supplier_pull, "_default_supplier_group", return_value="All Supplier Groups"
            ),
        ):
            supplier_pull._create_supplier(
                ee_vendor_c_id=ee_vendor_c_id,
                erpnext_fields={"supplier_name": supplier_name},
                gstin="",
                pan="",
                gst_category="Unregistered",
                country="India",
                is_active=True,
            )
        return captured.get("supplier_name")

    def test_first_supplier_keeps_canonical_name(self) -> None:
        landed = self._run(
            ee_vendor_c_id="V-001",
            supplier_name="ACME Wholesale",
            existing=False,
        )
        self.assertEqual(landed, "ACME Wholesale")

    def test_second_supplier_gets_vendor_c_id_suffix(self) -> None:
        landed = self._run(
            ee_vendor_c_id="V-002",
            supplier_name="ACME Wholesale",
            existing=True,
        )
        self.assertEqual(landed, "ACME Wholesale (V-002)")


if __name__ == "__main__":
    unittest.main()
