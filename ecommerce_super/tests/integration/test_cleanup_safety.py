"""Regression: cleanup_easyecom_state must not delete user data.

Historic incident: the prior implementation iterated every row of
every EasyEcom DocType and deleted+committed unconditionally. When
the test suite ran against a shared site that had real onboarded
state (an FDE adding sandbox credentials to a "Harmony" account in
parallel with development), cleanup wiped the production account,
all locations, all Company Settings, and all historical logs. The
user had to re-enter credentials and re-configure locations.

This regression test creates an account whose name does NOT match
the test prefix pattern, runs cleanup, and asserts the account is
still there. If anyone weakens the filter, this fails immediately."""
from __future__ import annotations

import frappe
from frappe.tests.utils import FrappeTestCase

from ecommerce_super.tests.factories import cleanup_easyecom_state


class TestCleanupRespectsUserData(FrappeTestCase):
    def test_non_test_account_survives_cleanup(self) -> None:
        # Pick a name that DOES NOT match "test-%" or "TEST-%" filters.
        # "Harmony-Like" mimics the real account name from the incident.
        sentinel = "Harmony-Like-Sentinel"
        if not frappe.db.exists("EasyEcom Account", sentinel):
            doc = frappe.new_doc("EasyEcom Account")
            doc.update({
                "account_name": sentinel,
                "enabled": 0,
                "environment_badge": "Sandbox",
                "api_endpoint": "https://api.easyecom.io",
                "x_api_key": "user-real-key-do-not-delete",
                "email": "real@example.com",
                "password": "real-password",
                "rate_limit_tier": "Silver",
                "webhook_enabled": 0,
            })
            doc.insert(ignore_permissions=True)
            frappe.db.commit()

        try:
            cleanup_easyecom_state()
            self.assertTrue(
                frappe.db.exists("EasyEcom Account", sentinel),
                f"cleanup_easyecom_state() deleted a non-test account "
                f"({sentinel!r}). The filter must only delete rows whose "
                "name matches 'test-%' or 'TEST-%' - never user data.",
            )
        finally:
            # Best-effort manual cleanup so this test doesn't leak the
            # sentinel itself. Use direct db delete to bypass any
            # safety checks the cleanup helper might add later.
            try:
                frappe.delete_doc(
                    "EasyEcom Account", sentinel,
                    force=True, ignore_permissions=True,
                )
                frappe.db.commit()
            except Exception:
                pass

    def test_non_test_location_survives_cleanup(self) -> None:
        # Real EE locations are auto-named "ECS-LOC-<location_key>"
        # where the key is the EE-side identifier (e.g. "ee9859099849").
        # Test locations have "TEST" or "MOCK" in the suffix.
        # A real-style name must survive.
        sentinel = "ECS-LOC-real99999999"
        if not frappe.db.exists("EasyEcom Location", sentinel):
            doc = frappe.new_doc("EasyEcom Location")
            doc.update({
                "location_key": "real99999999",
                "location_name": "Real User Warehouse",
                "ee_location_name": "Real User Warehouse",
                "workflow_state": "To Map",
            })
            doc.insert(ignore_permissions=True)
            frappe.db.commit()

        try:
            cleanup_easyecom_state()
            self.assertTrue(
                frappe.db.exists("EasyEcom Location", sentinel),
                f"cleanup_easyecom_state() deleted a non-test location "
                f"({sentinel!r}). The filter must only delete locations "
                "whose name matches 'ECS-LOC-TEST%' or 'ECS-LOC-MOCK%'.",
            )
        finally:
            try:
                frappe.delete_doc(
                    "EasyEcom Location", sentinel,
                    force=True, ignore_permissions=True,
                )
                frappe.db.commit()
            except Exception:
                pass
