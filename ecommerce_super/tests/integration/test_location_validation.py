"""§3.11 acceptance bar 7-ish: Location validation rules.

- Exactly one Location per account has is_primary = 1.
- frappe_company is mandatory iff is_operational = 1, and must be empty
  when is_operational = 0.
- frappe_company is non-unique by design (many-to-one).
- A Location with neither flag is inert — created without error.
"""

from __future__ import annotations

import frappe
from frappe.tests.utils import FrappeTestCase

from ecommerce_super.tests.factories import cleanup_easyecom_state


def _ensure_test_company(name: str = "_Test Company") -> str:
    """Reuse an existing Company or create a minimal test one.

    ERPNext's Company.on_update auto-creates standard warehouses, which
    require certain Warehouse Type records to exist. On a fresh site
    those may not be present yet, so we pre-create them.
    """
    if frappe.db.exists("Company", name):
        return name
    existing = frappe.db.get_value("Company", filters={}, fieldname="name")
    if existing:
        return existing
    # Pre-create Warehouse Type records ERPNext auto-creation expects.
    for wt in ("Transit", "Stores", "Work In Progress", "Finished Goods"):
        if not frappe.db.exists("Warehouse Type", wt):
            try:
                wt_doc = frappe.new_doc("Warehouse Type")
                wt_doc.name = wt
                wt_doc.insert(ignore_permissions=True, ignore_if_duplicate=True)
            except Exception:
                pass
    doc = frappe.new_doc("Company")
    doc.update(
        {
            "company_name": name,
            "abbr": "TC",
            "default_currency": "INR",
            "country": "India",
        }
    )
    doc.insert(ignore_permissions=True)
    return doc.name


class TestLocationValidation(FrappeTestCase):
    @classmethod
    def setUpClass(cls) -> None:
        super().setUpClass()
        cls.company = _ensure_test_company()

    def setUp(self) -> None:
        cleanup_easyecom_state()

    def tearDown(self) -> None:
        cleanup_easyecom_state()

    def _new_location(self, **fields) -> "frappe.model.document.Document":
        defaults = {
            "location_key": "L1",
            "location_name": "Test Location",
            "enabled": 1,
            "is_primary": 0,
            "is_operational": 0,
            "is_wms_location": 0,
            "serialization_enabled": 0,
        }
        defaults.update(fields)
        doc = frappe.new_doc("EasyEcom Location")
        doc.update(defaults)
        return doc

    def test_exactly_one_primary_enforced(self) -> None:
        a = self._new_location(location_key="L-PRIMARY-A", is_primary=1)
        a.insert(ignore_permissions=True)
        # A second primary must fail.
        b = self._new_location(location_key="L-PRIMARY-B", is_primary=1)
        with self.assertRaises(frappe.ValidationError):
            b.insert(ignore_permissions=True)

    def test_frappe_company_required_iff_operational(self) -> None:
        # Operational without company → reject.
        op_no_co = self._new_location(
            location_key="L-OP-NOCO", is_operational=1, frappe_company=None
        )
        with self.assertRaises(frappe.ValidationError):
            op_no_co.insert(ignore_permissions=True)

        # Non-operational with company → reject.
        nonop_with_co = self._new_location(
            location_key="L-NONOP-CO",
            is_operational=0,
            frappe_company=self.company,
        )
        with self.assertRaises(frappe.ValidationError):
            nonop_with_co.insert(ignore_permissions=True)

        # Operational with company → OK.
        ok = self._new_location(
            location_key="L-OP-OK", is_operational=1, frappe_company=self.company
        )
        ok.insert(ignore_permissions=True)

    def test_frappe_company_is_non_unique(self) -> None:
        """Many-to-one resolution: two Locations may resolve to the same Company."""
        a = self._new_location(
            location_key="L-CO-1",
            is_operational=1,
            frappe_company=self.company,
        )
        a.insert(ignore_permissions=True)
        b = self._new_location(
            location_key="L-CO-2",
            is_operational=1,
            frappe_company=self.company,
        )
        # Must not raise — two locations sharing a Company is the design.
        b.insert(ignore_permissions=True)

    def test_inert_location_is_valid(self) -> None:
        """Neither primary nor operational → recorded but not synced (§3.1.3)."""
        inert = self._new_location(
            location_key="L-INERT",
            is_primary=0,
            is_operational=0,
        )
        inert.insert(ignore_permissions=True)
        self.assertFalse(inert.is_primary)
        self.assertFalse(inert.is_operational)
        self.assertIsNone(inert.frappe_company)

    def test_resolve_company_returns_none_for_inert(self) -> None:
        from ecommerce_super.easyecom.doctype.easyecom_location.easyecom_location import (
            resolve_company,
        )

        inert = self._new_location(
            location_key="L-INERT-2", is_primary=0, is_operational=0
        )
        inert.insert(ignore_permissions=True)
        self.assertIsNone(resolve_company("L-INERT-2"))
        self.assertIsNone(resolve_company("L-DOES-NOT-EXIST"))

    def test_jwt_encrypt_set_and_get(self) -> None:
        """set_jwt encrypts; get_jwt_plaintext returns the original."""
        loc = self._new_location(location_key="L-JWT", is_primary=1)
        loc.insert(ignore_permissions=True)
        # Reload to get a fresh handle (set_jwt uses db_set, which bypasses
        # the in-memory doc).
        loc = frappe.get_doc("EasyEcom Location", loc.name)
        original = "eyJraWQiOiJ0ZXN0IiwiYWxnIjoiUlMyNTYifQ.payload.signature"
        loc.set_jwt(original, validity_days=90)
        # Reload to read back from DB.
        loc = frappe.get_doc("EasyEcom Location", loc.name)
        # Cached value is ciphertext, not plaintext.
        self.assertNotEqual(loc.jwt_token, original)
        # But get_jwt_plaintext decrypts cleanly.
        self.assertEqual(loc.get_jwt_plaintext(), original)
        self.assertIsNotNone(loc.jwt_acquired_at)
        self.assertIsNotNone(loc.jwt_expires_at)
