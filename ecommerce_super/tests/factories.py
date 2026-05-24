"""Test factories — small helpers to build common test inputs.

Used by both unit and integration tests. Pure helper code; no global state.
"""

from __future__ import annotations

import frappe


def make_account(
    name: str = "test-account",
    tier: str = "Silver",
    enabled: bool = True,
) -> str:
    """Insert (or return name of) a test EasyEcom Account."""
    if frappe.db.exists("EasyEcom Account", name):
        return name
    doc = frappe.new_doc("EasyEcom Account")
    doc.update(
        {
            "account_name": name,
            "enabled": 1 if enabled else 0,
            "environment_badge": "Sandbox",
            "api_endpoint": "https://api.easyecom.io",
            "x_api_key": "test-api-key-xxxxxxx",
            "email": "test@example.com",
            "password": "test-password",
            "rate_limit_tier": tier,
            # Disable webhooks in factory so tests that don't care about
            # webhook auth don't need to set webhook_token. Tests that
            # exercise webhook receive set webhook_enabled=1 explicitly.
            "webhook_enabled": 0,
        }
    )
    doc.insert(ignore_permissions=True)
    return doc.name


def make_location(
    location_key: str = "TEST-LOC-001",
    *,
    is_primary: bool = False,
    is_operational: bool = False,
    frappe_company: str | None = None,
    mapped_warehouse: str | None = None,
) -> str:
    """Insert an EasyEcom Location. Returns its docname."""
    docname = f"ECS-LOC-{location_key}"
    if frappe.db.exists("EasyEcom Location", docname):
        return docname
    doc = frappe.new_doc("EasyEcom Location")
    doc.update(
        {
            "location_key": location_key,
            "location_name": f"Test Location {location_key}",
            "is_primary": 1 if is_primary else 0,
            "is_operational": 1 if is_operational else 0,
            "frappe_company": frappe_company,
            "mapped_warehouse": mapped_warehouse,
            "enabled": 1,
        }
    )
    doc.insert(ignore_permissions=True)
    return doc.name


def cleanup_easyecom_state() -> None:
    """Tear-down helper: delete EasyEcom DocTypes' rows in a safe order.

    Used in test setUp/tearDown to ensure tests don't pollute each other.
    Uses frappe.delete_doc with force=True so DocPerm-restricted DocTypes
    (API Call, Webhook Event) can be cleaned in tests.
    """
    for dt in (
        "EasyEcom API Call",
        "EasyEcom Webhook Event",
        "EasyEcom Queue Job",
        "EasyEcom Sync Record",
        "EasyEcom Sync Cursor",
        "EasyEcom Location",
        "EasyEcom Company Settings",
        "EasyEcom Account",
    ):
        for name in frappe.db.get_all(dt, pluck="name"):
            try:
                frappe.delete_doc(dt, name, force=True, ignore_permissions=True)
            except Exception:
                pass
    frappe.db.commit()
