"""gh#14 — `company_scope` permission_query_conditions hook.

EasyEcom FDE users could view/edit Company Settings records for
Companies they had no User Permission for. Root cause: the hook
returned `` `tab{doctype}`.company in (...) `` with a literal
`{doctype}` placeholder that was never substituted — broken SQL
silently swallowed by Frappe, falling through to no-filter.

Fix: accept `doctype` as a kwarg (Frappe v15+ passes it via
frappe.call from DatabaseQuery.get_permission_query_conditions) and
embed the actual doctype name in the returned fragment.
"""

from __future__ import annotations

import unittest
from unittest.mock import patch

import frappe

from ecommerce_super.easyecom.permissions import company_scope


class TestCompanyScope(unittest.TestCase):
    def _patch_perms(self, *, roles=None, company_perms=None):
        return (
            patch("frappe.get_roles", return_value=list(roles or [])),
            patch(
                "ecommerce_super.easyecom.permissions._user_company_filter",
                return_value=company_perms,
            ),
        )

    def test_administrator_sees_all_no_filter(self) -> None:
        result = company_scope(user="Administrator", doctype="EasyEcom Company Settings")
        self.assertEqual(result, "")

    def test_system_manager_sees_all_no_filter(self) -> None:
        get_roles, perm_filter = self._patch_perms(
            roles=["System Manager"], company_perms=["Co A"]
        )
        with get_roles, perm_filter:
            result = company_scope(user="sm@x.com", doctype="EasyEcom Company Settings")
        self.assertEqual(result, "")

    def test_fde_with_no_user_permissions_sees_all(self) -> None:
        """Spec: no Company restrictions → see everything. Distinguishes
        'no permissions configured' from 'role assigned but no Companies
        allowed' (next test)."""
        get_roles, perm_filter = self._patch_perms(
            roles=["EasyEcom FDE"], company_perms=None
        )
        with get_roles, perm_filter:
            result = company_scope(user="fde@x.com", doctype="EasyEcom Company Settings")
        self.assertEqual(result, "")

    def test_fde_with_empty_company_perms_sees_nothing(self) -> None:
        get_roles, perm_filter = self._patch_perms(
            roles=["EasyEcom FDE"], company_perms=[]
        )
        with get_roles, perm_filter:
            result = company_scope(user="fde@x.com", doctype="EasyEcom Company Settings")
        self.assertEqual(result, "1=0")

    def test_fde_with_one_company_filters_to_that_company(self) -> None:
        get_roles, perm_filter = self._patch_perms(
            roles=["EasyEcom FDE"], company_perms=["Co A"]
        )
        with get_roles, perm_filter:
            result = company_scope(user="fde@x.com", doctype="EasyEcom Company Settings")
        self.assertEqual(
            result, "`tabEasyEcom Company Settings`.company in ('Co A')"
        )

    def test_fde_with_multi_company_lists_all_allowed(self) -> None:
        get_roles, perm_filter = self._patch_perms(
            roles=["EasyEcom FDE"], company_perms=["Co A", "Co B", "Co C"]
        )
        with get_roles, perm_filter:
            result = company_scope(user="fde@x.com", doctype="EasyEcom Company Settings")
        self.assertEqual(
            result,
            "`tabEasyEcom Company Settings`.company in ('Co A', 'Co B', 'Co C')",
        )

    def test_doctype_substitution_uses_actual_doctype_per_call(self) -> None:
        """Same user, different doctypes per call → each fragment uses its
        own tab<doctype> name. Frappe invokes the hook once per query."""
        get_roles, perm_filter = self._patch_perms(
            roles=["EasyEcom FDE"], company_perms=["Co A"]
        )
        with get_roles, perm_filter:
            sr = company_scope(user="fde@x.com", doctype="EasyEcom Sync Record")
            cs = company_scope(user="fde@x.com", doctype="EasyEcom Company Settings")
        self.assertIn("`tabEasyEcom Sync Record`.company", sr)
        self.assertIn("`tabEasyEcom Company Settings`.company", cs)

    def test_returned_sql_has_no_unsubstituted_placeholder(self) -> None:
        """Regression for the literal `{doctype}` bug — must never appear
        in the returned fragment."""
        get_roles, perm_filter = self._patch_perms(
            roles=["EasyEcom FDE"], company_perms=["Co A"]
        )
        with get_roles, perm_filter:
            result = company_scope(user="fde@x.com", doctype="EasyEcom API Call")
        self.assertNotIn("{doctype}", result)
        self.assertNotIn("tab{doctype}", result)

    def test_company_name_with_apostrophe_is_escaped(self) -> None:
        """Defense-in-depth: a Company name with a single quote must not
        break the SQL."""
        get_roles, perm_filter = self._patch_perms(
            roles=["EasyEcom FDE"], company_perms=["O'Reilly Inc"]
        )
        with get_roles, perm_filter:
            result = company_scope(user="fde@x.com", doctype="EasyEcom Sync Record")
        # Doubled single quote per SQL standard.
        self.assertIn("'O''Reilly Inc'", result)


if __name__ == "__main__":
    unittest.main()
