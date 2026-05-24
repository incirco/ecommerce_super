"""Integration tests for §5.12 versioning + rollback + audit invariants."""

from __future__ import annotations

import frappe
from frappe.tests.utils import FrappeTestCase

from ecommerce_super.easyecom.doctype.easyecom_field_mapping.easyecom_field_mapping import (
    rollback_to_version,
)


def _cleanup(prefix: str) -> None:
    for n in frappe.get_all(
        "EasyEcom Field Mapping",
        filters={"mapping_name": ("like", f"{prefix}%")},
        pluck="name",
    ):
        for v in frappe.get_all(
            "EasyEcom Field Mapping Version",
            filters={"parent_mapping": n},
            pluck="name",
        ):
            frappe.delete_doc(
                "EasyEcom Field Mapping Version", v, force=True, ignore_permissions=True
            )
        frappe.delete_doc(
            "EasyEcom Field Mapping", n, force=True, ignore_permissions=True
        )
    frappe.db.commit()


def _make(name, change_reason="initial"):
    doc = frappe.new_doc("EasyEcom Field Mapping")
    doc.mapping_name = name
    doc.entity_type = "Item"
    doc.direction = "Push"
    doc.missing_field_policy = "Permissive"
    doc.change_reason = change_reason
    doc.append(
        "rules",
        {
            "erpnext_path": "item_code",
            "easyecom_path": "sku",
            "transform_push": "identity",
            "transform_pull": "identity",
        },
    )
    doc.insert(ignore_permissions=True)
    return doc


class TestVersionSnapshot(FrappeTestCase):
    PREFIX = "test-fm-ver-"

    def setUp(self) -> None:
        _cleanup(self.PREFIX)

    def tearDown(self) -> None:
        _cleanup(self.PREFIX)

    def test_insert_creates_v1_snapshot(self) -> None:
        doc = _make(f"{self.PREFIX}v1")
        self.assertEqual(doc.version, 1)
        snaps = frappe.get_all(
            "EasyEcom Field Mapping Version",
            filters={"parent_mapping": doc.name},
            fields=["version", "change_reason"],
            order_by="version asc",
        )
        self.assertEqual(len(snaps), 1)
        self.assertEqual(snaps[0]["version"], 1)
        self.assertEqual(snaps[0]["change_reason"], "initial")

    def test_save_bumps_version_and_creates_snapshot(self) -> None:
        doc = _make(f"{self.PREFIX}bump")
        doc.append(
            "rules",
            {
                "erpnext_path": "qty",
                "easyecom_path": "quantity",
                "transform_push": "int_to_str",
                "transform_pull": "str_to_int",
            },
        )
        doc.change_reason = "added qty"
        doc.save(ignore_permissions=True)
        self.assertEqual(doc.version, 2)
        snaps = frappe.get_all(
            "EasyEcom Field Mapping Version",
            filters={"parent_mapping": doc.name},
            pluck="version",
        )
        self.assertIn(2, snaps)


class TestRollback(FrappeTestCase):
    PREFIX = "test-fm-roll-"

    def setUp(self) -> None:
        _cleanup(self.PREFIX)

    def tearDown(self) -> None:
        _cleanup(self.PREFIX)

    def test_rollback_restores_prior_state_as_new_version(self) -> None:
        doc = _make(f"{self.PREFIX}back")  # v1: 1 rule
        # v2: add a rule
        doc.append(
            "rules",
            {
                "erpnext_path": "qty",
                "easyecom_path": "qty",
                "transform_push": "int_to_str",
                "transform_pull": "str_to_int",
            },
        )
        doc.change_reason = "added qty"
        doc.save(ignore_permissions=True)
        self.assertEqual(doc.version, 2)
        self.assertEqual(len(doc.rules), 2)

        # Rollback to v1 → produces v3 with the single-rule shape.
        new_ver = rollback_to_version(doc.name, 1)
        self.assertEqual(int(new_ver), 3)

        restored = frappe.get_doc("EasyEcom Field Mapping", doc.name)
        self.assertEqual(len(restored.rules), 1)
        self.assertEqual(restored.change_reason, "Rollback to v1")

    def test_rollback_to_missing_version_raises(self) -> None:
        doc = _make(f"{self.PREFIX}miss")
        with self.assertRaises(frappe.ValidationError):
            rollback_to_version(doc.name, 999)


class TestAuditInvariants(FrappeTestCase):
    PREFIX = "test-fm-audit-"

    def setUp(self) -> None:
        _cleanup(self.PREFIX)

    def tearDown(self) -> None:
        _cleanup(self.PREFIX)

    def test_save_without_change_reason_rejected(self) -> None:
        doc = frappe.new_doc("EasyEcom Field Mapping")
        doc.mapping_name = f"{self.PREFIX}no-reason"
        doc.entity_type = "Item"
        doc.direction = "Push"
        doc.missing_field_policy = "Permissive"
        doc.change_reason = ""  # empty
        doc.append(
            "rules",
            {
                "erpnext_path": "x",
                "easyecom_path": "x",
                "transform_push": "identity",
                "transform_pull": "identity",
            },
        )
        with self.assertRaises(frappe.ValidationError):
            doc.insert(ignore_permissions=True)

    def test_audit_fields_stamped(self) -> None:
        doc = _make(f"{self.PREFIX}stamp")
        self.assertIsNotNone(doc.last_modified_by)
        self.assertIsNotNone(doc.last_modified_at)
