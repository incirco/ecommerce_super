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
    """Tear-down helper - deletes ONLY test-pattern rows.

    Historic foot-gun: the previous implementation deleted every row
    of every EasyEcom DocType (`frappe.db.get_all(dt, pluck="name")`)
    and committed. When the test suite ran against a shared site
    that had real onboarded state, the cleanup nuked production
    accounts/locations/configuration. Rule going forward:

      * Tests may only delete rows the tests themselves created.
      * Identification is by name pattern (test-/TEST-/MOCK-) or by
        link to a test-pattern Account or _Test* Frappe Company.
      * Rows that look like user data are LEFT ALONE - persistent
        leftover test data across runs is a minor annoyance;
        deleting user data is a catastrophe.

    DocPerm-restricted DocTypes (API Call, Webhook Event) are still
    cleaned via force=True; the pattern filter keeps the blast
    radius bounded.
    """
    # 1. Identify test EasyEcom Accounts. factories.make_account
    #    default is "test-account"; tests may use "test-*", "TEST-*",
    #    or - in test_account_doctype.py and test_credentials_no_readback.py
    #    - "acc-*" (those tests create accounts via frappe.new_doc
    #    directly, not via the factory).
    test_accounts: list[str] = []
    for pattern in ("test-%", "TEST-%", "acc-%"):
        test_accounts.extend(
            frappe.db.get_all(
                "EasyEcom Account",
                filters=[["name", "like", pattern]],
                pluck="name",
            )
        )

    # 2. Identify test Companies (Frappe convention - "_Test*").
    test_companies = frappe.db.get_all(
        "Company",
        filters=[["name", "like", "\\_Test%"]],
        pluck="name",
    )

    # 3. Log rows: scope by account OR company link, depending on
    #    which link field each DocType carries.
    log_doctype_scope = (
        ("EasyEcom API Call", "easyecom_account", test_accounts),
        ("EasyEcom Sync Record", "company", test_companies),
        ("EasyEcom Queue Job", "company", test_companies),
        ("EasyEcom Sync Cursor", "company", test_companies),
        ("EasyEcom Webhook Event", "company", test_companies),
        ("EasyEcom Company Settings", "company", test_companies),
    )
    for dt, link_field, scope in log_doctype_scope:
        if not scope:
            continue
        for name in frappe.db.get_all(
            dt, filters=[[link_field, "in", scope]], pluck="name"
        ):
            try:
                frappe.delete_doc(
                    dt, name, force=True, ignore_permissions=True
                )
            except Exception:
                pass

    # 4. Test Locations - factories.make_location names them
    #    "ECS-LOC-{location_key}" where location_key defaults to
    #    "TEST-LOC-001" and mock tests pass "MOCK-LOC". Tests that
    #    create locations directly via frappe.new_doc use prefixes
    #    L-*, LOC-*, SOT-*. The location_discovery test fixture uses
    #    "ne2948810*" to mirror the real EE response shape it captured.
    #    Real EE locations the FDE imports are auto-named with the
    #    EE-side location id, which (per the real data observed in the
    #    Harmony sandbox) starts with a 2-letter prefix the user did
    #    NOT pick - so a generic "ECS-LOC-%" filter is unsafe and we
    #    enumerate the test prefixes explicitly.
    location_patterns = (
        "ECS-LOC-TEST%",
        "ECS-LOC-MOCK%",
        "ECS-LOC-L-%",
        "ECS-LOC-LOC-%",
        "ECS-LOC-SOT-%",
        "ECS-LOC-ne2948810%",  # location_discovery fixture
    )
    for pattern in location_patterns:
        for name in frappe.db.get_all(
            "EasyEcom Location",
            filters=[["name", "like", pattern]],
            pluck="name",
        ):
            try:
                frappe.delete_doc(
                    "EasyEcom Location",
                    name,
                    force=True,
                    ignore_permissions=True,
                )
            except Exception:
                pass

    # 5. Finally - the test Accounts themselves.
    for name in test_accounts:
        try:
            frappe.delete_doc(
                "EasyEcom Account",
                name,
                force=True,
                ignore_permissions=True,
            )
        except Exception:
            pass

    frappe.db.commit()
