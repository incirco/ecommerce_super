"""Whitelisted endpoint for the §8.1.1 Item Master Mode flip.

`onboarding` (default) is bidirectional — pull + push, supervised by
the FDE. `erpnext_mastered` is the steady state where ERPNext owns
the catalogue; the pull becomes drift-detection only (§8.1.8). The
flip is a one-way design decision: once the FDE marks onboarding
complete, EE-side changes show as Drift flags rather than auto-
overwriting ERPNext data.

Reverse flips (back to onboarding) deliberately require manual
intervention (set the field via Console / DB) — the design intent is
one flip per client lifecycle. Allowing self-service un-flips would
invite "I'll just flip back, run my fix, and flip forward again"
patterns that defeat the audit trail.

Permission: EasyEcom FDE / System Manager / EasyEcom System Manager
can trigger the flip. Operator cannot.
"""

from __future__ import annotations

from typing import Any

import frappe


@frappe.whitelist()
def flip_to_erpnext_mastered(account: str, confirm: str | bool = False) -> dict[str, Any]:
    """Flip item_master_mode from onboarding → erpnext_mastered.

    Args:
        account: the EasyEcom Account docname.
        confirm: must be truthy — the JS dialog passes the user's
            confirmation through. A whitelisted method should never
            mutate a long-lived config flag without an explicit
            confirmation, even if the caller's role is privileged.

    Returns:
        {ok: bool, message: str, ...} — JS-friendly response.
    """
    roles = set(frappe.get_roles(frappe.session.user))
    if not roles.intersection(
        {"System Manager", "EasyEcom System Manager", "EasyEcom FDE"}
    ):
        frappe.throw(
            frappe._(
                "Flipping Item Master Mode requires EasyEcom FDE or "
                "System Manager privilege."
            ),
            frappe.PermissionError,
        )

    if not frappe.has_permission("EasyEcom Account", "write", doc=account):
        frappe.throw(
            frappe._(
                "You don't have write permission on EasyEcom Account {0}."
            ).format(account),
            frappe.PermissionError,
        )

    if not confirm or str(confirm).lower() in ("false", "0", "no"):
        return {
            "ok": False,
            "message": frappe._(
                "Confirmation required — pass confirm=true to flip."
            ),
        }

    if not frappe.db.exists("EasyEcom Account", account):
        return {
            "ok": False,
            "message": frappe._("Account {0} not found.").format(account),
        }

    current_mode = frappe.db.get_value("EasyEcom Account", account, "item_master_mode")
    if current_mode == "erpnext_mastered":
        return {
            "ok": False,
            "message": frappe._(
                "Account {0} is already in erpnext_mastered mode."
            ).format(account),
        }

    now = frappe.utils.now_datetime()
    frappe.db.set_value(
        "EasyEcom Account",
        account,
        {
            "item_master_mode": "erpnext_mastered",
            "item_master_flipped_at": now,
        },
        update_modified=True,
    )
    frappe.db.commit()
    return {
        "ok": True,
        "account": account,
        "mode": "erpnext_mastered",
        "flipped_at": str(now),
        "message": frappe._(
            "Account {0} is now in erpnext_mastered mode. The §8d pull "
            "is now drift-detection only — EE-side new products and "
            "edits to mapped items will flag as Drift rather than "
            "create/overwrite."
        ).format(account),
    }
