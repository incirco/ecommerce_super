"""gh#14 follow-up — `company_scope_doc` must enforce User Permission
on the per-document read/write path, not just the list view.

PR #21 fixed `permission_query_conditions` (list filter). Garv999's
mmpl16 retest confirmed the per-doc path was still wide open: an
EasyEcom FDE with User Permission for Co A could open AND edit
`/app/easyecom-company-settings/Co B`. This test freezes the per-doc
contract.
"""

from __future__ import annotations

import unittest
from unittest.mock import MagicMock, patch

import frappe

from ecommerce_super.easyecom.permissions import company_scope_doc


class TestCompanyScopeDocPermission(unittest.TestCase):
    def test_administrator_passes_through(self) -> None:
        doc = MagicMock(company="Co B")
        self.assertTrue(
            company_scope_doc(doc, "write", user="Administrator")
        )

    def test_system_manager_passes_through(self) -> None:
        doc = MagicMock(company="Co B")
        with patch("frappe.get_roles", return_value=["System Manager"]):
            self.assertTrue(company_scope_doc(doc, "write", user="sm@x.com"))

    def test_create_ptype_is_allowed_for_any_user(self) -> None:
        """The New button must work — `company` isn't set yet at create
        time; validate-side check catches mismatches at save."""
        doc = MagicMock(company=None)
        with (
            patch("frappe.get_roles", return_value=["EasyEcom FDE"]),
            patch(
                "ecommerce_super.easyecom.permissions._user_company_filter",
                return_value=["Co A"],
            ),
        ):
            self.assertTrue(
                company_scope_doc(doc, "create", user="fde@x.com")
            )

    def test_doc_without_company_attribute_is_allowed(self) -> None:
        """Belt-and-braces: a doc-shape that doesn't carry `company` at
        all (unusual but defensive) defers to validate-time logic."""
        doc = MagicMock(company=None)
        with patch("frappe.get_roles", return_value=["EasyEcom FDE"]):
            self.assertTrue(company_scope_doc(doc, "read", user="fde@x.com"))

    def test_no_user_permission_restrictions_passes_through(self) -> None:
        """User has the role but no Company User Permissions configured
        → sees all (matches list filter's `allowed is None` branch)."""
        doc = MagicMock(company="Co B")
        with (
            patch("frappe.get_roles", return_value=["EasyEcom FDE"]),
            patch(
                "ecommerce_super.easyecom.permissions._user_company_filter",
                return_value=None,
            ),
        ):
            self.assertTrue(company_scope_doc(doc, "read", user="fde@x.com"))

    def test_fde_with_co_a_perm_can_read_co_a_doc(self) -> None:
        doc = MagicMock(company="Co A")
        with (
            patch("frappe.get_roles", return_value=["EasyEcom FDE"]),
            patch(
                "ecommerce_super.easyecom.permissions._user_company_filter",
                return_value=["Co A"],
            ),
        ):
            self.assertTrue(company_scope_doc(doc, "read", user="fde@x.com"))

    def test_fde_with_co_a_perm_cannot_read_co_b_doc(self) -> None:
        """gh#14 headline — the failure garv999 reproduced on mmpl16."""
        doc = MagicMock(company="Co B")
        with (
            patch("frappe.get_roles", return_value=["EasyEcom FDE"]),
            patch(
                "ecommerce_super.easyecom.permissions._user_company_filter",
                return_value=["Co A"],
            ),
        ):
            self.assertFalse(company_scope_doc(doc, "read", user="fde@x.com"))

    def test_fde_with_co_a_perm_cannot_write_co_b_doc(self) -> None:
        doc = MagicMock(company="Co B")
        with (
            patch("frappe.get_roles", return_value=["EasyEcom FDE"]),
            patch(
                "ecommerce_super.easyecom.permissions._user_company_filter",
                return_value=["Co A"],
            ),
        ):
            self.assertFalse(company_scope_doc(doc, "write", user="fde@x.com"))

    def test_fde_with_co_a_perm_cannot_delete_co_b_doc(self) -> None:
        doc = MagicMock(company="Co B")
        with (
            patch("frappe.get_roles", return_value=["EasyEcom FDE"]),
            patch(
                "ecommerce_super.easyecom.permissions._user_company_filter",
                return_value=["Co A"],
            ),
        ):
            self.assertFalse(
                company_scope_doc(doc, "delete", user="fde@x.com")
            )

    def test_fde_with_empty_company_perms_sees_nothing(self) -> None:
        """User has the role but EMPTY allowed list → see nothing.
        Mirrors company_scope's `'1=0'` branch."""
        doc = MagicMock(company="Co A")
        with (
            patch("frappe.get_roles", return_value=["EasyEcom FDE"]),
            patch(
                "ecommerce_super.easyecom.permissions._user_company_filter",
                return_value=[],
            ),
        ):
            self.assertFalse(company_scope_doc(doc, "read", user="fde@x.com"))


class TestHasPermissionRegistration(unittest.TestCase):
    """The hook must be wired in `hooks.py` for every Company-scoped
    DocType that doesn't already have a specialized hook."""

    def test_easyecom_company_settings_uses_company_scope_doc(self) -> None:
        from ecommerce_super import hooks

        self.assertEqual(
            hooks.has_permission["EasyEcom Company Settings"],
            "ecommerce_super.easyecom.permissions.company_scope_doc",
        )

    def test_easyecom_sync_record_uses_company_scope_doc(self) -> None:
        from ecommerce_super import hooks

        self.assertEqual(
            hooks.has_permission["EasyEcom Sync Record"],
            "ecommerce_super.easyecom.permissions.company_scope_doc",
        )

    def test_specialized_hooks_remain_in_place(self) -> None:
        """API Call / Webhook Event / Configuration Audit have their
        own special-case hooks — make sure they weren't clobbered by
        the spread-merge."""
        from ecommerce_super import hooks

        self.assertEqual(
            hooks.has_permission["EasyEcom API Call"],
            "ecommerce_super.easyecom.permissions.append_only",
        )
        self.assertEqual(
            hooks.has_permission["EasyEcom Webhook Event"],
            "ecommerce_super.easyecom.permissions.append_only",
        )
        self.assertEqual(
            hooks.has_permission["EasyEcom Configuration Audit"],
            "ecommerce_super.easyecom.permissions.audit_no_modify",
        )


if __name__ == "__main__":
    unittest.main()
