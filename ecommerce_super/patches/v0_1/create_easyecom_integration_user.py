"""gh#166 hardening — create the dedicated 'EasyEcom Integration' user
and role that `_elevated_session` elevates into instead of Administrator.

Least-privilege scope: read/write/create/submit on Sales Invoice,
Delivery Note, Payment Entry; read on Sales Order / Customer / Item;
read/write/create on the four EasyEcom log DocTypes and the two Map
DocTypes the inbound handler touches. No permissions on User, Role,
System Settings, Server Script — the wide-blast-radius doctypes an
attacker with a leaked Bearer token would want to reach.

Idempotent: safe to re-run.
"""
from __future__ import annotations

import frappe


ROLE_NAME = "EasyEcom Integration"
USER_EMAIL = "easyecom-integration@internal.local"
USER_FULL_NAME = "EasyEcom Integration"


# DocTypes the inbound handler needs, keyed by permission flags.
# Order matters: apps that ship these DocTypes must be installed BEFORE
# the perm rows get inserted. Guarded per-doctype via frappe.db.exists.
_PERMISSIONS: dict[str, dict[str, int]] = {
    # SI: full write, submit, print — the core surface.
    "Sales Invoice": {
        "read": 1, "write": 1, "create": 1, "submit": 1,
        "print": 1, "email": 1, "report": 1,
    },
    # DN: mirror may create/link in follow-up work.
    "Delivery Note": {
        "read": 1, "write": 1, "create": 1, "submit": 1,
    },
    # Payment: for adjustments during dispatch/return follow-ups.
    "Payment Entry": {
        "read": 1, "write": 1, "create": 1, "submit": 1,
    },
    # Read-only on upstream masters — never modified by inbound flow.
    "Sales Order": {"read": 1},
    "Customer": {"read": 1},
    "Item": {"read": 1},
    "Address": {"read": 1, "write": 1, "create": 1},
    "Contact": {"read": 1, "write": 1, "create": 1},
    # Company + GST fields — read only.
    "Company": {"read": 1},
    # gh#166 followup (2026-07-14): SI insert / submit / mint chain
    # comprehensive audit. Reads for lookup, writes/creates only where
    # the flow actually persists (SI itself + ledger entries from
    # update_stock=1 + IC e-invoice fields).
    #
    # Tax-side lookups (SI validates account_head against Account).
    "Account": {"read": 1},
    "Cost Center": {"read": 1},
    "Warehouse": {"read": 1},
    "Item Tax Template": {"read": 1},
    "Item Tax Template Detail": {"read": 1},
    "Tax Category": {"read": 1},
    "Sales Taxes and Charges Template": {"read": 1},
    "Sales Taxes and Charges": {"read": 1},
    # GST-specific — GST Settings + GST Account + HSN codes.
    "GST Settings": {"read": 1},
    "GST Account": {"read": 1},
    "GST HSN Code": {"read": 1},
    # Item ancillaries — set_missing_values + IC hooks touch these.
    "Item Price": {"read": 1},
    "Item Group": {"read": 1},
    "Item Barcode": {"read": 1},
    "Item Default": {"read": 1},
    "Item Reorder": {"read": 1},
    "Item Tax": {"read": 1},
    "Item Website Specification": {"read": 1},
    # Customer master lookups.
    "Customer Group": {"read": 1},
    "Territory": {"read": 1},
    "Sales Partner": {"read": 1},
    "Sales Person": {"read": 1},
    # Currency / Fiscal / Country — SI set_missing_values.
    "Currency": {"read": 1},
    "Currency Exchange": {"read": 1},
    "Fiscal Year": {"read": 1},
    "Country": {"read": 1},
    # Naming — SI naming series.
    "Series": {"read": 1},
    # Stock — update_stock=1 creates SLEs. Serial and Batch Bundle
    # is v16's replacement for the old Batch link on stock rows.
    "Batch": {"read": 1, "write": 1, "create": 1},
    "Stock Ledger Entry": {"read": 1, "write": 1, "create": 1},
    "Serial and Batch Bundle": {"read": 1, "write": 1, "create": 1},
    "Serial No": {"read": 1, "write": 1, "create": 1},
    # GL / Payment Ledger — SI submit creates rows here.
    "GL Entry": {"read": 1, "create": 1},
    "Payment Ledger Entry": {"read": 1, "create": 1},
    # Post-submit reposts.
    "Repost Item Valuation": {"read": 1, "create": 1},
    "Repost Payment Ledger": {"read": 1, "create": 1},
    "Repost Payment Ledger Items": {"read": 1, "create": 1},
    # UOM — Item unit lookups.
    "UOM": {"read": 1},
    "UOM Conversion Detail": {"read": 1},
    # Payment terms — SI payment_schedule computation.
    "Payment Term": {"read": 1},
    "Payment Terms Template": {"read": 1},
    "Payment Terms Template Detail": {"read": 1},
    "Payment Schedule": {"read": 1, "write": 1, "create": 1},
    # PDF rendering (_render_si_pdf_base64).
    "Print Format": {"read": 1},
    "Letter Head": {"read": 1},
    # Address ancillaries.
    "Address Template": {"read": 1},
    # Child tables on Sales Invoice.
    "Sales Invoice Item": {"read": 1, "write": 1, "create": 1},
    "Sales Invoice Payment": {"read": 1, "write": 1, "create": 1},
    "Sales Invoice Advance": {"read": 1, "write": 1, "create": 1},
    "Sales Invoice Timesheet": {"read": 1, "write": 1, "create": 1},
    "Sales Team": {"read": 1, "write": 1, "create": 1},
    # EE own DocTypes — full CRUD on operational rows, read on config.
    "EasyEcom B2B Order Map": {
        "read": 1, "write": 1, "create": 1,
    },
    "EasyEcom Customer Map": {
        "read": 1, "write": 1, "create": 1,
    },
    "EasyEcom Item Map": {"read": 1},
    "EasyEcom Location": {"read": 1},
    "EasyEcom Account": {"read": 1},
    "EasyEcom API Call": {
        "read": 1, "write": 1, "create": 1,
    },
    "EasyEcom Sync Record": {
        "read": 1, "write": 1, "create": 1,
    },
    "EasyEcom Queue Job": {
        "read": 1, "write": 1, "create": 1,
    },
    "EasyEcom GSP Token": {
        "read": 1, "write": 1, "create": 1,
    },
    # Error surfacing.
    "Error Log": {"read": 1, "write": 1, "create": 1},
    "Comment": {"read": 1, "write": 1, "create": 1},
    "Version": {"read": 1, "create": 1},
    # India Compliance side — needed for e-invoice + e-way generation.
    "e-Invoice Log": {"read": 1, "write": 1, "create": 1},
    "e-Waybill Log": {"read": 1, "write": 1, "create": 1},
}


def execute() -> None:
    _ensure_role()
    _ensure_role_permissions()
    _ensure_user()


def _ensure_role() -> None:
    if frappe.db.exists("Role", ROLE_NAME):
        return
    role = frappe.new_doc("Role")
    role.role_name = ROLE_NAME
    role.desk_access = 0
    role.two_factor_auth = 0
    role.disabled = 0
    role.flags.ignore_permissions = True
    role.insert()


def _ensure_role_permissions() -> None:
    for doctype, perms in _PERMISSIONS.items():
        if not frappe.db.exists("DocType", doctype):
            continue
        existing = frappe.db.get_value(
            "Custom DocPerm",
            {"parent": doctype, "role": ROLE_NAME, "permlevel": 0},
            "name",
        )
        if existing:
            # Update to align with the current allowlist — new fields
            # we grant later flow to existing perm rows.
            frappe.db.set_value(
                "Custom DocPerm", existing, perms, update_modified=True
            )
            continue
        row = frappe.new_doc("Custom DocPerm")
        row.parent = doctype
        row.parenttype = "DocType"
        row.parentfield = "permissions"
        row.role = ROLE_NAME
        row.permlevel = 0
        for k, v in perms.items():
            row.set(k, v)
        row.flags.ignore_permissions = True
        row.insert()


def _ensure_user() -> None:
    if frappe.db.exists("User", USER_EMAIL):
        # Ensure role membership on existing user (idempotent).
        u = frappe.get_doc("User", USER_EMAIL)
        if not any(r.role == ROLE_NAME for r in (u.roles or [])):
            u.append("roles", {"role": ROLE_NAME})
            u.flags.ignore_permissions = True
            u.save()
        # Ensure enabled + System User type.
        if u.enabled != 1 or u.user_type != "System User":
            u.enabled = 1
            u.user_type = "System User"
            u.flags.ignore_permissions = True
            u.save()
        return
    u = frappe.new_doc("User")
    u.email = USER_EMAIL
    u.first_name = USER_FULL_NAME
    u.enabled = 1
    u.user_type = "System User"
    u.send_welcome_email = 0
    u.append("roles", {"role": ROLE_NAME})
    u.flags.ignore_permissions = True
    u.insert()
