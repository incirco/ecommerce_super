"""Integration tests for Source-of-Truth Map DocType (§8.4.2 / §31.2.23).

Covers:
  - Schema present with all mapping and authority fields.
  - (company, warehouse) UNIQUE composite index enforced.
  - is_linked derived from ee_location_key presence.
  - Warehouse-in-Company validation.
  - Linked-location Company match validation.
"""

from __future__ import annotations

import frappe
from frappe.tests.utils import FrappeTestCase

from ecommerce_super.tests.factories import cleanup_easyecom_state, make_location
from ecommerce_super.tests.integration.test_location_validation import (
    _ensure_test_company,
)


def _wipe_sot() -> None:
    for n in frappe.db.get_all("Source-of-Truth Map", pluck="name"):
        try:
            frappe.delete_doc(
                "Source-of-Truth Map", n, force=True, ignore_permissions=True
            )
        except Exception:
            pass
    frappe.db.commit()


def _ensure_warehouse(name: str, company: str) -> str:
    if frappe.db.exists("Warehouse", name):
        return name
    doc = frappe.new_doc("Warehouse")
    doc.update(
        {
            "warehouse_name": name.split(" - ")[0] if " - " in name else name,
            "company": company,
            "is_group": 0,
            "warehouse_type": "Stores",
        }
    )
    doc.insert(ignore_permissions=True)
    return doc.name


class TestSourceOfTruthMapSchema(FrappeTestCase):
    """The DocType is named 'Source-of-Truth Map' (no EasyEcom prefix
    — matches the foundation's hooks.py forward-declaration) and has
    all the §31.2.23 fields."""

    def test_doctype_name_is_dashed(self) -> None:
        self.assertTrue(frappe.db.exists("DocType", "Source-of-Truth Map"))

    def test_authority_fields_present(self) -> None:
        meta = frappe.get_meta("Source-of-Truth Map")
        for field in (
            "company",
            "warehouse",
            "ee_location_key",
            "is_linked",
            "enabled",
            "inventory_master",
            "pr_origination",
            "adjustment_origination",
            "mirror_stock_reservations",
            "allow_negative_stock",
            "notes",
        ):
            self.assertIsNotNone(meta.get_field(field), f"Field {field!r} missing")

    def test_authority_select_options(self) -> None:
        meta = frappe.get_meta("Source-of-Truth Map")
        # inventory_master
        self.assertEqual(
            set(meta.get_field("inventory_master").options.split("\n")),
            {"ERPNext", "EasyEcom"},
        )
        # pr_origination
        self.assertEqual(
            set(meta.get_field("pr_origination").options.split("\n")),
            {"ERPNext direct", "EasyEcom GRN flow"},
        )
        # adjustment_origination
        self.assertEqual(
            set(meta.get_field("adjustment_origination").options.split("\n")),
            {"ERPNext", "EasyEcom"},
        )


class TestSourceOfTruthMapBehaviour(FrappeTestCase):
    """is_linked computed; validation rules; UNIQUE composite enforced."""

    @classmethod
    def setUpClass(cls) -> None:
        super().setUpClass()
        cls.company = _ensure_test_company()
        cls.wh1 = _ensure_warehouse(f"SoT WH 1 - TC", cls.company)
        cls.wh2 = _ensure_warehouse(f"SoT WH 2 - TC", cls.company)

    def setUp(self) -> None:
        _wipe_sot()
        cleanup_easyecom_state()

    def tearDown(self) -> None:
        _wipe_sot()
        cleanup_easyecom_state()

    def _new_row(self, **fields) -> "frappe.model.document.Document":
        defaults = {
            "company": self.company,
            "warehouse": self.wh1,
            "enabled": 1,
            "inventory_master": "ERPNext",
            "pr_origination": "ERPNext direct",
            "adjustment_origination": "ERPNext",
        }
        defaults.update(fields)
        doc = frappe.new_doc("Source-of-Truth Map")
        doc.update(defaults)
        return doc

    def test_unlinked_row_has_is_linked_zero(self) -> None:
        doc = self._new_row()
        doc.insert(ignore_permissions=True)
        self.assertEqual(doc.is_linked, 0)

    def test_linked_row_has_is_linked_one(self) -> None:
        loc = make_location(location_key="SOT-LINKED", frappe_company=self.company)
        doc = self._new_row(ee_location_key=loc)
        doc.insert(ignore_permissions=True)
        self.assertEqual(doc.is_linked, 1)

    def test_unique_company_warehouse_pair(self) -> None:
        """Composite UNIQUE (company, warehouse) — second row with same
        pair must fail."""
        a = self._new_row()
        a.insert(ignore_permissions=True)
        b = self._new_row()  # same company + warehouse
        with self.assertRaises(
            (frappe.DuplicateEntryError, frappe.exceptions.UniqueValidationError)
        ):
            b.insert(ignore_permissions=True)

    def test_two_rows_same_company_different_warehouse_ok(self) -> None:
        a = self._new_row(warehouse=self.wh1)
        a.insert(ignore_permissions=True)
        b = self._new_row(warehouse=self.wh2)
        b.insert(ignore_permissions=True)

    def test_warehouse_must_belong_to_company(self) -> None:
        """A warehouse from Company A cannot be mapped under Company B."""
        # Build a second Company + warehouse.
        if not frappe.db.exists("Company", "_Other Test Co"):
            other = frappe.new_doc("Company")
            other.update(
                {
                    "company_name": "_Other Test Co",
                    "abbr": "OTC",
                    "default_currency": "INR",
                    "country": "India",
                }
            )
            other.insert(ignore_permissions=True)
        other_wh = _ensure_warehouse("Other WH - OTC", "_Other Test Co")
        doc = self._new_row(company=self.company, warehouse=other_wh)
        with self.assertRaises(frappe.ValidationError):
            doc.insert(ignore_permissions=True)

    def test_linked_location_company_must_match(self) -> None:
        """If the linked EasyEcom Location resolves to a different Company,
        reject the SoT row."""
        if not frappe.db.exists("Company", "_Other Test Co"):
            other = frappe.new_doc("Company")
            other.update(
                {
                    "company_name": "_Other Test Co",
                    "abbr": "OTC",
                    "default_currency": "INR",
                    "country": "India",
                }
            )
            other.insert(ignore_permissions=True)
        loc = make_location(
            location_key="SOT-CO-MISMATCH", frappe_company="_Other Test Co"
        )
        doc = self._new_row(ee_location_key=loc, company=self.company)
        with self.assertRaises(frappe.ValidationError):
            doc.insert(ignore_permissions=True)

    def test_linked_unmapped_location_allowed(self) -> None:
        """A linked location with frappe_company blank (To Map state) is
        allowed — the SoT row may pre-stage the warehouse side."""
        loc = make_location(location_key="SOT-UNMAPPED")  # no frappe_company
        doc = self._new_row(ee_location_key=loc)
        # Must not raise.
        doc.insert(ignore_permissions=True)
        self.assertEqual(doc.is_linked, 1)
