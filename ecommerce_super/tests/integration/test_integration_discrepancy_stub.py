"""§23 STUB DocType tests — frozen-contract regression.

These tests pin the 7-field stub shape so a future §23 build cannot
silently drop or rename a field that §9 / §11 / §12 / §13 flows depend
on. The stub establishes minimal viable wiring; §23 EXTENDS it.

Mirrors the test pattern of test_section_9_substrate.py.
"""

from __future__ import annotations

import frappe
from frappe.tests.utils import FrappeTestCase

from ecommerce_super.easyecom.doctype.easyecom_integration_discrepancy.easyecom_integration_discrepancy import (
    VALID_STATUS_VALUES,
)


_PREFIX = "TEST-S23-STUB-"


def _wipe_test_discrepancies() -> None:
    for n in frappe.db.get_all(
        "EasyEcom Integration Discrepancy",
        filters={"kind": ("like", f"{_PREFIX}%")},
        pluck="name",
    ):
        try:
            frappe.delete_doc(
                "EasyEcom Integration Discrepancy",
                n,
                force=True,
                ignore_permissions=True,
            )
        except Exception:
            pass
    frappe.db.commit()


class TestIntegrationDiscrepancyStubSchema(FrappeTestCase):
    """Pin the 7-field stub shape. §23 may ADD; it must not REMOVE
    or RENAME these fields."""

    def test_doctype_exists(self) -> None:
        self.assertTrue(
            frappe.db.exists("DocType", "EasyEcom Integration Discrepancy")
        )

    def test_autoname_uses_date_series(self) -> None:
        meta = frappe.get_meta("EasyEcom Integration Discrepancy")
        self.assertEqual(
            meta.autoname,
            "format:ECS-DISC-{YYYY}-{MM}-{DD}-{######}",
        )

    def test_status_enum_frozen_contract(self) -> None:
        """The three states are the minimal viable set §9+ flows depend
        on. §23 may add intermediate states; it must keep these three."""
        meta = frappe.get_meta("EasyEcom Integration Discrepancy")
        opts = set((meta.get_field("status").options or "").split("\n"))
        self.assertEqual(VALID_STATUS_VALUES, {"Open", "Resolved", "Dismissed"})
        self.assertEqual(
            opts,
            {"Open", "Resolved", "Dismissed"},
            "FROZEN CONTRACT: status enum must include exactly these three",
        )

    def test_default_status_is_open(self) -> None:
        meta = frappe.get_meta("EasyEcom Integration Discrepancy")
        self.assertEqual(meta.get_field("status").default, "Open")

    def test_seven_field_contract_present(self) -> None:
        """The 7 fields (counting Dynamic Link as a pair) are pinned."""
        meta = frappe.get_meta("EasyEcom Integration Discrepancy")
        required_fields = {
            "kind": "Data",
            "status": "Select",
            "reference_doctype": "Link",
            "reference_name": "Dynamic Link",
            "company": "Link",
            "reason": "Long Text",
            "resolution_note": "Small Text",
        }
        for fname, ftype in required_fields.items():
            f = meta.get_field(fname)
            self.assertIsNotNone(
                f, f"FROZEN CONTRACT field {fname!r} missing"
            )
            self.assertEqual(
                f.fieldtype,
                ftype,
                f"FROZEN CONTRACT field {fname!r} must be {ftype}, "
                f"got {f.fieldtype}",
            )

    def test_reqd_fields_match_frozen_contract(self) -> None:
        """6 of 7 fields are reqd; only resolution_note is optional."""
        meta = frappe.get_meta("EasyEcom Integration Discrepancy")
        reqd = {"kind", "status", "reference_doctype", "reference_name",
                "company", "reason"}
        for fname in reqd:
            self.assertTrue(
                meta.get_field(fname).reqd,
                f"FROZEN CONTRACT: {fname!r} must be reqd",
            )
        self.assertFalse(meta.get_field("resolution_note").reqd)

    def test_dynamic_link_targets_reference_doctype(self) -> None:
        """The Dynamic Link pair is intact — reference_name's options
        is the reference_doctype field name (Frappe's Dynamic Link
        convention)."""
        meta = frappe.get_meta("EasyEcom Integration Discrepancy")
        self.assertEqual(
            meta.get_field("reference_name").options, "reference_doctype"
        )


class TestIntegrationDiscrepancyValidation(FrappeTestCase):
    def tearDown(self) -> None:
        _wipe_test_discrepancies()
        frappe.db.rollback()

    def _company(self) -> str:
        c = frappe.db.get_value("Company", filters={}, fieldname="name")
        return c or "Test Company"

    def test_can_insert_with_reference_to_existing_doc(self) -> None:
        """End-to-end stub: create a discrepancy referencing a real
        DocType row (the Company doc itself, just to have a real
        target). This is what §9 Stage 3 will do — minus the real
        reference DocType (GRN Map / PO Map)."""
        company = self._company()
        doc = frappe.new_doc("EasyEcom Integration Discrepancy")
        doc.update(
            {
                "kind": f"{_PREFIX}smoke",
                "status": "Open",
                "reference_doctype": "Company",
                "reference_name": company,
                "company": company,
                "reason": "Stub smoke test — verifies §23 stub accepts a "
                "well-formed discrepancy row.",
            }
        )
        doc.insert(ignore_permissions=True)
        self.assertTrue(doc.name.startswith("ECS-DISC-"))
        self.assertEqual(doc.status, "Open")

    def test_refuses_unknown_status(self) -> None:
        company = self._company()
        doc = frappe.new_doc("EasyEcom Integration Discrepancy")
        doc.update(
            {
                "kind": f"{_PREFIX}bad-status",
                "status": "Escalated",  # not in the frozen 3-set
                "reference_doctype": "Company",
                "reference_name": company,
                "company": company,
                "reason": "X",
            }
        )
        with self.assertRaises(frappe.ValidationError):
            doc.validate()

    def test_refuses_nonexistent_reference_target(self) -> None:
        """A discrepancy must point at a row that exists — typo'd
        references sitting open in the worklist help nobody."""
        company = self._company()
        doc = frappe.new_doc("EasyEcom Integration Discrepancy")
        doc.update(
            {
                "kind": f"{_PREFIX}ghost",
                "status": "Open",
                "reference_doctype": "EasyEcom GRN Map",
                "reference_name": "ECS-GRN-DOES-NOT-EXIST-9999",
                "company": company,
                "reason": "X",
            }
        )
        with self.assertRaises(frappe.ValidationError):
            doc.validate()


class TestSyncRecordLineLinkUnblocked(FrappeTestCase):
    """Verifies the original motivation: Sync Record Line's
    ecs_integration_discrepancy Link can now ACTUALLY be set to a
    real value at runtime (pre-stub it would fail Link validation)."""

    def tearDown(self) -> None:
        _wipe_test_discrepancies()
        frappe.db.rollback()

    def test_link_field_resolves_to_real_stub_row(self) -> None:
        company = frappe.db.get_value("Company", filters={}, fieldname="name")
        if not company:
            self.skipTest("no Company exists; skipping link-resolution probe")

        # Create a discrepancy row.
        disc = frappe.new_doc("EasyEcom Integration Discrepancy")
        disc.update(
            {
                "kind": f"{_PREFIX}link-probe",
                "status": "Open",
                "reference_doctype": "Company",
                "reference_name": company,
                "company": company,
                "reason": "Stub-link probe.",
            }
        )
        disc.insert(ignore_permissions=True)

        # Build a Sync Record carrying a line that references the
        # discrepancy. Pre-stub this would fail with LinkValidationError.
        sr = frappe.new_doc("EasyEcom Sync Record")
        sr.update(
            {
                "entity_type": "GRN",
                "entity_doctype": "Purchase Receipt",
                "entity_name": "TEST-S23-STUB-PR",
                "direction": "Pull",
                "status": "Failed",
                "outcome_reason": "Stub-link probe",
                "company": company,
                "correlation_id": f"{_PREFIX}corr-1",
                "idempotency_key": f"{_PREFIX}idem-1",
                "easyecom_account": frappe.db.get_value(
                    "EasyEcom Account", filters={}, fieldname="name"
                )
                or "test-account",
            }
        )
        sr.append(
            "lines",
            {
                "source_line_ref": "TEST-LINE-1",
                "target_field": "item_code",
                "line_status": "Discrepancy",
                "reason": "tolerance breach",
                "ecs_integration_discrepancy": disc.name,
            },
        )
        try:
            sr.insert(ignore_permissions=True, ignore_links=True)
            # The discrepancy link must round-trip cleanly.
            reloaded = frappe.get_doc("EasyEcom Sync Record", sr.name)
            self.assertEqual(
                reloaded.lines[0].ecs_integration_discrepancy, disc.name
            )
        finally:
            try:
                frappe.delete_doc(
                    "EasyEcom Sync Record",
                    sr.name,
                    force=True,
                    ignore_permissions=True,
                )
            except Exception:
                pass
            frappe.db.rollback()
