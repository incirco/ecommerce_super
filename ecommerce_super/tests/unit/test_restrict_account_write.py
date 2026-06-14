"""gh#14 follow-up #2 — `restrict_account_write` must enforce
read-only Account access for everyone except System Manager and
EasyEcom System Manager.

Reporter (mmpl16, 2026-06-13): EasyEcom Account form rendered Actions
menu and Bulk Edit dialog for an FDE user, letting them proceed
toward modification. DocPerm denial fired only at save time. With
this hook, perm-check time refuses write/delete and the form layer
hides the edit affordances.
"""

from __future__ import annotations

import unittest
from unittest.mock import MagicMock, patch

import frappe

from ecommerce_super.easyecom.permissions import restrict_account_write


class TestRestrictAccountWrite(unittest.TestCase):
    def test_administrator_passes_through(self) -> None:
        for ptype in ("read", "write", "create", "delete"):
            with self.subTest(ptype=ptype):
                self.assertTrue(
                    restrict_account_write(
                        MagicMock(), ptype, user="Administrator"
                    )
                )

    def test_system_manager_can_write(self) -> None:
        with patch("frappe.get_roles", return_value=["System Manager"]):
            self.assertTrue(
                restrict_account_write(MagicMock(), "write", user="sm@x.com")
            )
            self.assertTrue(
                restrict_account_write(MagicMock(), "delete", user="sm@x.com")
            )

    def test_easyecom_system_manager_can_write(self) -> None:
        with patch(
            "frappe.get_roles", return_value=["EasyEcom System Manager"]
        ):
            self.assertTrue(
                restrict_account_write(
                    MagicMock(), "write", user="ee-sm@x.com"
                )
            )
            self.assertTrue(
                restrict_account_write(
                    MagicMock(), "create", user="ee-sm@x.com"
                )
            )

    def test_fde_can_read_but_not_write(self) -> None:
        """The mmpl16 headline scenario — FDE must NOT be able to
        write/delete/create Account."""
        with patch("frappe.get_roles", return_value=["EasyEcom FDE"]):
            self.assertTrue(
                restrict_account_write(
                    MagicMock(), "read", user="fde@x.com"
                )
            )
            self.assertTrue(
                restrict_account_write(
                    MagicMock(), "report", user="fde@x.com"
                )
            )
            self.assertTrue(
                restrict_account_write(
                    MagicMock(), "export", user="fde@x.com"
                )
            )
            self.assertFalse(
                restrict_account_write(
                    MagicMock(), "write", user="fde@x.com"
                )
            )
            self.assertFalse(
                restrict_account_write(
                    MagicMock(), "create", user="fde@x.com"
                )
            )
            self.assertFalse(
                restrict_account_write(
                    MagicMock(), "delete", user="fde@x.com"
                )
            )

    def test_operator_role_cannot_write(self) -> None:
        with patch("frappe.get_roles", return_value=["EasyEcom Operator"]):
            self.assertTrue(
                restrict_account_write(
                    MagicMock(), "read", user="op@x.com"
                )
            )
            self.assertFalse(
                restrict_account_write(
                    MagicMock(), "write", user="op@x.com"
                )
            )

    def test_no_role_user_cannot_write(self) -> None:
        """A user with NO roles can't get write access through this
        hook (DocPerm would also reject them but belt-and-braces)."""
        with patch("frappe.get_roles", return_value=[]):
            self.assertFalse(
                restrict_account_write(
                    MagicMock(), "write", user="nobody@x.com"
                )
            )


class TestHooksRegistration(unittest.TestCase):
    def test_easyecom_account_uses_restrict_account_write(self) -> None:
        from ecommerce_super import hooks

        self.assertEqual(
            hooks.has_permission["EasyEcom Account"],
            "ecommerce_super.easyecom.permissions.restrict_account_write",
        )

    def test_specialized_hooks_remain_in_place(self) -> None:
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

    def test_company_scoped_doctypes_still_use_company_scope_doc(self) -> None:
        """Sanity — adding Account to the overrides must not have
        clobbered the previous PR's wiring for Company Settings et al."""
        from ecommerce_super import hooks

        self.assertEqual(
            hooks.has_permission["EasyEcom Company Settings"],
            "ecommerce_super.easyecom.permissions.company_scope_doc",
        )
        self.assertEqual(
            hooks.has_permission["EasyEcom Sync Record"],
            "ecommerce_super.easyecom.permissions.company_scope_doc",
        )


if __name__ == "__main__":
    unittest.main()
