"""§10 Stage 3 — test-isolation regression guard.

Item #0 from the Stage 3 prompt: §10 Stage 2's suite-after-Stage-1
green-rate regressed because Customer / Customer Map / Supplier /
Supplier Map rows from sibling §10 tests survived into the next test
class's setUp. cleanup_easyecom_state() now extends to cover these.

This module asserts the invariant: after a clean factories cleanup
runs, NO `INTL-CUST-%` / `INTL-SUPP-%` rows (the §10 auto-creation
naming convention) remain. Tightens the guard so a future stage that
adds a new auto-created party type and forgets to wipe surfaces here
red, not as a downstream flake.
"""

from __future__ import annotations

import frappe
from frappe.tests.utils import FrappeTestCase

from ecommerce_super.tests.factories import (
    cleanup_easyecom_state,
    cleanup_internal_pair_fabric,
)


class TestInternalPairFabricIsolation(FrappeTestCase):
    """After cleanup, the §10 Internal pair fabric must be empty."""

    def test_cleanup_easyecom_state_wipes_internal_pairs(self) -> None:
        """cleanup_easyecom_state() — the canonical full-state wipe —
        must also clear the §10 Internal pair fabric rows."""
        # Seed an Internal Customer + Supplier matching the
        # naming convention. Frappe's permission-bypass path lets us
        # create these without going through ensure_internal_party_pairs
        # (no Companies setup needed).
        target_co = (
            frappe.db.get_value("Company", filters={}, fieldname="name")
            or "_Test Company"
        )
        cust_group = (
            frappe.db.get_value(
                "Customer Group", {"is_group": 0}, "name"
            )
            or "All Customer Groups"
        )
        sup_group = (
            frappe.db.get_value(
                "Supplier Group", {"is_group": 0}, "name"
            )
            or "All Supplier Groups"
        )
        cust = frappe.new_doc("Customer")
        cust.update(
            {
                "customer_name": "INTL-CUST-for-IsolationGuardTest",
                "customer_type": "Company",
                "customer_group": cust_group,
                "is_internal_customer": 1,
                "represents_company": target_co,
            }
        )
        cust.insert(ignore_permissions=True)
        sup = frappe.new_doc("Supplier")
        sup.update(
            {
                "supplier_name": "INTL-SUPP-from-IsolationGuardTest",
                "supplier_type": "Company",
                "supplier_group": sup_group,
                "is_internal_supplier": 1,
                "represents_company": target_co,
            }
        )
        sup.insert(ignore_permissions=True)
        # Sanity — they exist.
        self.assertTrue(
            frappe.db.exists(
                "Customer", {"customer_name": ("like", "INTL-CUST-%")}
            )
        )
        self.assertTrue(
            frappe.db.exists(
                "Supplier", {"supplier_name": ("like", "INTL-SUPP-%")}
            )
        )

        # Act — run the cleanup.
        cleanup_easyecom_state()

        # Assert — the Internal pair fabric is empty.
        cust_residue = frappe.db.get_all(
            "Customer",
            filters={"customer_name": ("like", "INTL-CUST-%")},
            pluck="name",
        )
        sup_residue = frappe.db.get_all(
            "Supplier",
            filters={"supplier_name": ("like", "INTL-SUPP-%")},
            pluck="name",
        )
        self.assertEqual(
            cust_residue,
            [],
            "INTL-CUST-% Customer rows leaked through "
            "cleanup_easyecom_state",
        )
        self.assertEqual(
            sup_residue,
            [],
            "INTL-SUPP-% Supplier rows leaked through "
            "cleanup_easyecom_state",
        )

    def test_cleanup_internal_pair_fabric_is_idempotent(self) -> None:
        """Calling the public wipe twice should be safe (no exception)
        and end-state identical."""
        cleanup_internal_pair_fabric()
        cleanup_internal_pair_fabric()
        # End-state: no Internal pair rows.
        self.assertEqual(
            frappe.db.get_all(
                "Customer",
                filters={"customer_name": ("like", "INTL-CUST-%")},
                pluck="name",
            ),
            [],
        )

    def test_cleanup_wipes_customer_map_link(self) -> None:
        """When an Internal Customer has an EasyEcom Customer Map row,
        the cleanup must wipe the Map row before the Customer (otherwise
        ERPNext refuses the Customer delete due to the linked Map)."""
        target_co = (
            frappe.db.get_value("Company", filters={}, fieldname="name")
            or "_Test Company"
        )
        cust_group = (
            frappe.db.get_value(
                "Customer Group", {"is_group": 0}, "name"
            )
            or "All Customer Groups"
        )
        cust = frappe.new_doc("Customer")
        cust.update(
            {
                "customer_name": "INTL-CUST-for-MapLinkTest",
                "customer_type": "Company",
                "customer_group": cust_group,
                "is_internal_customer": 1,
                "represents_company": target_co,
            }
        )
        cust.insert(ignore_permissions=True)
        cm = frappe.new_doc("EasyEcom Customer Map")
        cm.update(
            {
                "ee_c_id": "isolation-guard-c-id",
                "ee_customer_id": "isolation-guard-ee-id",
                "erpnext_doctype": "Customer",
                "erpnext_name": cust.name,
                "status": "Mapped",
            }
        )
        cm.insert(ignore_permissions=True)
        cleanup_internal_pair_fabric()
        self.assertFalse(
            frappe.db.exists(
                "EasyEcom Customer Map",
                {"erpnext_name": cust.name},
            ),
            "Customer Map row must be wiped before the Customer "
            "row (or the Customer delete would fail on link).",
        )
        self.assertFalse(frappe.db.exists("Customer", cust.name))
