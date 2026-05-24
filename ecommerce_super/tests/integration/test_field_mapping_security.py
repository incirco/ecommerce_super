"""§5 SECURITY block — compile-time rejection of malicious expressions.

Per the build packet's mandatory test:
> Add a test that a malicious expression (e.g. trying __import__ or os
> access) is rejected at compile.

These tests prove the rejection happens at SAVE time (the controller's
validate() hook surfaces it as a frappe.ValidationError, blocking the
write transaction). That is the spec's contract: the bad expression
never lands in the DB, never gets a chance to execute against live data.
"""

from __future__ import annotations

import frappe
from frappe.tests.utils import FrappeTestCase


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


def _attempt_save(name: str, *, expression: str) -> None:
    doc = frappe.new_doc("EasyEcom Field Mapping")
    doc.mapping_name = name
    doc.entity_type = "Item"
    doc.direction = "Push"
    doc.missing_field_policy = "Permissive"
    doc.change_reason = "trying malicious expression"
    doc.append(
        "rules",
        {
            "erpnext_path": "x",
            "easyecom_path": "x",
            "transform_push": "custom_python",
            "transform_pull": "identity",
            "transform_args": {"expression": expression},
        },
    )
    doc.insert(ignore_permissions=True)


class TestMaliciousExpressionRejectedAtSave(FrappeTestCase):
    PREFIX = "test-fm-sec-"

    def setUp(self) -> None:
        _cleanup(self.PREFIX)

    def tearDown(self) -> None:
        _cleanup(self.PREFIX)

    def test_dunder_import_rejected(self) -> None:
        """The headline attack: __import__('os').system(...) must fail
        at save with a clear error (§5 SECURITY block)."""
        with self.assertRaises(frappe.ValidationError):
            _attempt_save(
                f"{self.PREFIX}dunder",
                expression="__import__('os').system('echo HACK')",
            )
        # The malicious ruleset must NOT exist in the DB after the failed save.
        self.assertFalse(
            frappe.db.exists("EasyEcom Field Mapping", f"{self.PREFIX}dunder")
        )

    def test_frappe_access_rejected(self) -> None:
        with self.assertRaises(frappe.ValidationError):
            _attempt_save(
                f"{self.PREFIX}frappe",
                expression="frappe.get_doc('User', 'Administrator')",
            )
        self.assertFalse(
            frappe.db.exists("EasyEcom Field Mapping", f"{self.PREFIX}frappe")
        )

    def test_os_module_rejected(self) -> None:
        with self.assertRaises(frappe.ValidationError):
            _attempt_save(f"{self.PREFIX}os", expression="os.listdir('/')")

    def test_subprocess_rejected(self) -> None:
        with self.assertRaises(frappe.ValidationError):
            _attempt_save(
                f"{self.PREFIX}sp",
                expression="subprocess.run(['echo', 'hi'])",
            )

    def test_eval_call_rejected(self) -> None:
        with self.assertRaises(frappe.ValidationError):
            _attempt_save(f"{self.PREFIX}eval", expression="eval('1+1')")

    def test_open_file_rejected(self) -> None:
        with self.assertRaises(frappe.ValidationError):
            _attempt_save(
                f"{self.PREFIX}open",
                expression="open('/etc/passwd').read()",
            )

    def test_dunder_class_traversal_rejected(self) -> None:
        """Classic Python sandbox escape: walk __class__ → __mro__ →
        __subclasses__ to get to dangerous objects. Must reject."""
        with self.assertRaises(frappe.ValidationError):
            _attempt_save(
                f"{self.PREFIX}mro",
                expression="value.__class__.__mro__[1].__subclasses__()",
            )


class TestMaliciousConditionRejectedAtSave(FrappeTestCase):
    PREFIX = "test-fm-sec-cond-"

    def setUp(self) -> None:
        _cleanup(self.PREFIX)

    def tearDown(self) -> None:
        _cleanup(self.PREFIX)

    def test_malicious_condition_rejected(self) -> None:
        """The same rejection applies to per-rule condition expressions
        (not just custom_python)."""
        doc = frappe.new_doc("EasyEcom Field Mapping")
        doc.mapping_name = f"{self.PREFIX}cond"
        doc.entity_type = "Item"
        doc.direction = "Push"
        doc.missing_field_policy = "Permissive"
        doc.change_reason = "test"
        doc.append(
            "rules",
            {
                "erpnext_path": "x",
                "easyecom_path": "x",
                "transform_push": "identity",
                "transform_pull": "identity",
                "condition": "__import__('os').system('echo HACK') or True",
            },
        )
        with self.assertRaises(frappe.ValidationError):
            doc.insert(ignore_permissions=True)


class TestMaliciousComputedRejectedAtSave(FrappeTestCase):
    PREFIX = "test-fm-sec-comp-"

    def setUp(self) -> None:
        _cleanup(self.PREFIX)

    def tearDown(self) -> None:
        _cleanup(self.PREFIX)

    def test_malicious_computed_expression_rejected(self) -> None:
        """A computed_field's expression must also pass sandbox validation."""
        doc = frappe.new_doc("EasyEcom Field Mapping")
        doc.mapping_name = f"{self.PREFIX}comp"
        doc.entity_type = "Item"
        doc.direction = "Push"
        doc.missing_field_policy = "Permissive"
        doc.change_reason = "test"
        doc.append(
            "computed_fields",
            {
                "field_name": "bad",
                "expression": "__import__('os').system('rm -rf /')",
                "output_type": "String",
                "cache_per_record": 1,
            },
        )
        with self.assertRaises(frappe.ValidationError):
            doc.insert(ignore_permissions=True)
