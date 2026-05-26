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
    """Insert (or return name of) a test EasyEcom Account.

    Honours the §8.1 single-Account constraint (enforced by the
    Account controller's validate as of the audit follow-up): when
    creating an `enabled=True` account, first disable any other
    currently-enabled accounts. Tests that build accounts across
    several test classes therefore never trip the constraint —
    whichever test most-recently called make_account holds the
    "enabled" slot.

    Existing accounts already named the same are returned as-is
    (no constraint check needed since enabled state isn't being
    changed)."""
    if frappe.db.exists("EasyEcom Account", name):
        return name
    if enabled:
        # Disable any other currently-enabled account via db.set_value
        # (bypasses validate so we don't recurse into the constraint).
        for other in frappe.db.get_all(
            "EasyEcom Account", filters={"enabled": 1}, pluck="name"
        ):
            frappe.db.set_value(
                "EasyEcom Account", other, "enabled", 0, update_modified=False
            )
        frappe.db.commit()
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
    workflow_state: str | None = None,
) -> str:
    """Insert an EasyEcom Location. Returns its docname.

    `is_operational` is now workflow-derived (§8.4.1). The factory keeps
    its old kwarg signature for back-compat: passing `is_operational=True`
    auto-sets `workflow_state="Live"` so the derive resolves to 1; passing
    `is_operational=False` with `frappe_company` set lands "Mapped but not
    Live"; otherwise "To Map". Callers can override with the explicit
    `workflow_state` kwarg.
    """
    docname = f"ECS-LOC-{location_key}"
    if frappe.db.exists("EasyEcom Location", docname):
        return docname
    if workflow_state is None:
        if is_operational and frappe_company:
            workflow_state = "Live"
        elif frappe_company:
            workflow_state = "Mapped but not Live"
        else:
            workflow_state = "To Map"
    # Frappe's active Workflow auto-applies on insert and refuses
    # skip-transitions from the initial state. Always insert in "To Map"
    # (the workflow's initial state), then bump the row's workflow_state
    # via db.set_value if the caller asked for something else. For test
    # data this is fine — production paths use apply_workflow.
    doc = frappe.new_doc("EasyEcom Location")
    doc.update(
        {
            "location_key": location_key,
            "location_name": f"Test Location {location_key}",
            "is_primary": 1 if is_primary else 0,
            "workflow_state": "To Map",
            "frappe_company": None,  # set after if requested (avoids non-op + co rejection)
            "mapped_warehouse": None,
            "enabled": 1,
        }
    )
    doc.insert(ignore_permissions=True)
    # Now apply the requested workflow_state + mapping side without
    # re-running validate (which would re-derive is_operational and
    # might disagree with the caller's intent).
    updates: dict = {"workflow_state": workflow_state}
    if frappe_company:
        updates["frappe_company"] = frappe_company
    if mapped_warehouse:
        updates["mapped_warehouse"] = mapped_warehouse
    # is_operational is workflow-derived; only Live → 1.
    updates["is_operational"] = 1 if workflow_state == "Live" else 0
    frappe.db.set_value(
        "EasyEcom Location", doc.name, updates, update_modified=False
    )
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
