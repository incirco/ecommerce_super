"""gh#5 — Location Master shape validation for EE Company ID, GSTIN, Pincode.

The Location form was accepting clearly-malformed values for three FDE-set
master fields. These are sanity-checks on field SHAPE, layered alongside
the existing §8.4.1 state/Company invariants.

Rules:
  - ee_company_id: digits only (free-text in EE payloads, but the field is
    a numeric internal id — alpha input is data corruption).
  - gstin: India Compliance's canonical validator (15 chars + check digit).
  - pincode: exactly 6 digits.

All rules fire only when the field is set; blank stays allowed (Location
records that haven't yet been mapped to a Frappe Company often have no
GSTIN/pincode of their own — that's a valid mid-mapping state).
"""

from __future__ import annotations

import frappe
from frappe.tests.utils import FrappeTestCase

from ecommerce_super.tests.factories import cleanup_easyecom_state, make_location


class TestLocationMasterFieldValidation(FrappeTestCase):
    PREFIX = "mfv-"

    def setUp(self) -> None:
        self._wipe()

    def tearDown(self) -> None:
        self._wipe()
        cleanup_easyecom_state()

    def _wipe(self) -> None:
        for n in frappe.db.get_all(
            "EasyEcom Location",
            filters={"location_key": ("like", f"{self.PREFIX}%")},
            pluck="name",
        ):
            try:
                frappe.delete_doc(
                    "EasyEcom Location", n, force=True, ignore_permissions=True
                )
            except Exception:
                pass
        frappe.db.commit()

    # ----- ee_company_id -----

    def test_ee_company_id_rejects_alpha(self) -> None:
        name = make_location(f"{self.PREFIX}eecid-alpha")
        doc = frappe.get_doc("EasyEcom Location", name)
        doc.ee_company_id = "ABC123"
        with self.assertRaisesRegex(frappe.ValidationError, "EE Company ID"):
            doc.save(ignore_permissions=True)

    def test_ee_company_id_rejects_special_chars(self) -> None:
        name = make_location(f"{self.PREFIX}eecid-special")
        doc = frappe.get_doc("EasyEcom Location", name)
        doc.ee_company_id = "12-34"
        with self.assertRaisesRegex(frappe.ValidationError, "EE Company ID"):
            doc.save(ignore_permissions=True)

    def test_ee_company_id_accepts_numeric(self) -> None:
        name = make_location(f"{self.PREFIX}eecid-ok")
        doc = frappe.get_doc("EasyEcom Location", name)
        doc.ee_company_id = "9859099849"
        doc.save(ignore_permissions=True)  # no raise

    def test_ee_company_id_blank_allowed(self) -> None:
        name = make_location(f"{self.PREFIX}eecid-blank")
        doc = frappe.get_doc("EasyEcom Location", name)
        doc.ee_company_id = None
        doc.save(ignore_permissions=True)  # no raise

    # ----- gstin -----

    def test_gstin_rejects_wrong_length(self) -> None:
        name = make_location(f"{self.PREFIX}gstin-short")
        doc = frappe.get_doc("EasyEcom Location", name)
        doc.gstin = "29ABCDE1234F1Z"  # 14 chars
        with self.assertRaisesRegex(frappe.ValidationError, "GSTIN"):
            doc.save(ignore_permissions=True)

    def test_gstin_rejects_too_long(self) -> None:
        name = make_location(f"{self.PREFIX}gstin-long")
        doc = frappe.get_doc("EasyEcom Location", name)
        doc.gstin = "29ABCDE1234F1Z51"  # 16 chars
        with self.assertRaisesRegex(frappe.ValidationError, "GSTIN"):
            doc.save(ignore_permissions=True)

    def test_gstin_rejects_bad_format(self) -> None:
        name = make_location(f"{self.PREFIX}gstin-fmt")
        doc = frappe.get_doc("EasyEcom Location", name)
        doc.gstin = "INVALIDGSTIN123"  # 15 chars but wrong shape
        with self.assertRaisesRegex(frappe.ValidationError, "GSTIN"):
            doc.save(ignore_permissions=True)

    def test_gstin_blank_allowed(self) -> None:
        name = make_location(f"{self.PREFIX}gstin-blank")
        doc = frappe.get_doc("EasyEcom Location", name)
        doc.gstin = None
        doc.save(ignore_permissions=True)  # no raise

    # ----- pincode -----

    def test_pincode_rejects_alpha(self) -> None:
        name = make_location(f"{self.PREFIX}pin-alpha")
        doc = frappe.get_doc("EasyEcom Location", name)
        doc.pincode = "ABC123"
        with self.assertRaisesRegex(frappe.ValidationError, "Pincode"):
            doc.save(ignore_permissions=True)

    def test_pincode_rejects_wrong_length(self) -> None:
        name = make_location(f"{self.PREFIX}pin-short")
        doc = frappe.get_doc("EasyEcom Location", name)
        doc.pincode = "12345"  # 5 digits
        with self.assertRaisesRegex(frappe.ValidationError, "Pincode"):
            doc.save(ignore_permissions=True)

    def test_pincode_rejects_too_long(self) -> None:
        name = make_location(f"{self.PREFIX}pin-long")
        doc = frappe.get_doc("EasyEcom Location", name)
        doc.pincode = "1234567"  # 7 digits
        with self.assertRaisesRegex(frappe.ValidationError, "Pincode"):
            doc.save(ignore_permissions=True)

    def test_pincode_accepts_six_digits(self) -> None:
        name = make_location(f"{self.PREFIX}pin-ok")
        doc = frappe.get_doc("EasyEcom Location", name)
        doc.pincode = "560001"
        doc.save(ignore_permissions=True)  # no raise

    def test_pincode_blank_allowed(self) -> None:
        name = make_location(f"{self.PREFIX}pin-blank")
        doc = frappe.get_doc("EasyEcom Location", name)
        doc.pincode = None
        doc.save(ignore_permissions=True)  # no raise
